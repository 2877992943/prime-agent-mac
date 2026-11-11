"""cua — Computer Use Agent skill (Prime Agent).

看屏 → 决策 → 鼠键注入的执行层。
- 视觉: 自配置的视觉模型 (sglang/OpenAI 兼容端点, 经 SSH 跳板), 每次只带最近 KEEP_IMAGES(默认2)张截图
- 图片永不进入会话上下文 (满足"历史图片只看2个")
- 观察与动作账本全是文本, 会话里不删减 (满足"历史文本不删减")
- 鼠键: Quartz CGEvent; 截图像素坐标自动换算为屏幕点坐标 (Retina/降采样)
- 步数预算: 代码强制; 超限后所有动作抛错, 逼模型总结收尾
"""

import asyncio
import base64
import json
import os
import subprocess
import time
from pathlib import Path

import Quartz

# ---------------------------------------------------------------- 配置
# 视觉模型的连接参数由使用者自己提供 (不内置任何主机/密钥)。
# 优先级: 环境变量 CUA_* > skill 根目录下的 cua.config.json > 报错要求配置。
def _load_cfg() -> dict:
    cfg_file = os.environ.get("CUA_CONFIG") or str(
        Path(__file__).resolve().parent.parent.parent / "cua.config.json")
    cfg = {}
    if Path(cfg_file).exists():
        cfg = json.loads(Path(cfg_file).read_text())
    def pick(env: str, key: str, default: str = "") -> str:
        return os.environ.get(env) or str(cfg.get(key) or default)
    missing = [k for k, v in {
        "CUA_SSH_REMOTE": pick("CUA_SSH_REMOTE", "ssh_remote"),
        "CUA_MODEL": pick("CUA_MODEL", "model"),
        "CUA_API_KEY": pick("CUA_API_KEY", "api_key"),
    }.items() if not v]
    if missing:
        raise RuntimeError(
            f"cua skill 未配置 {', '.join(missing)}: "
            f"请复制 config.example.json 为 skill 目录下的 cua.config.json 并填写, "
            "或设置对应 CUA_* 环境变量 (见 README)")
    return {
        "ssh_remote": pick("CUA_SSH_REMOTE", "ssh_remote"),
        "remote_port": pick("CUA_REMOTE_PORT", "remote_port", "5919"),
        "model": pick("CUA_MODEL", "model"),
        "api_key": pick("CUA_API_KEY", "api_key"),
        "ssh_timeout": int(pick("CUA_SSH_TIMEOUT", "ssh_timeout", "150")),
    }

_CFG = _load_cfg()
SSH_REMOTE = _CFG["ssh_remote"]   # 跳板机, 视觉服务在其 127.0.0.1 上
REMOTE_PORT = _CFG["remote_port"]
MODEL = _CFG["model"]
API_KEY = _CFG["api_key"]
SSH_TIMEOUT = _CFG["ssh_timeout"]

SHOT_DIR = Path.home() / "Desktop" / "tmp1"   # 截图+轨迹保存目录 (桌面/tmp1)
KEEP_IMAGES = 2        # 每次视觉调用携带的最近截图数
KEEP_FILES = 6         # 磁盘上最多保留的截图文件数
DEFAULT_BUDGET = 40    # 默认轮数上限
MAX_DIM = 1600         # 截图最长边(降采样, 控制 token 与传输)
JPEG_QUALITY = 60

DANGER_WORDS = ("删除", "清空", "格式化", "永久", "发送", "支付", "付款",
                "转账", "密码", "sudo", "rm -rf", "shutdown", "reboot")

KEYCODES = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "esc": 53, "escape": 53,
    "delete": 51, "backspace": 51, "forwarddelete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "left": 123, "right": 124, "down": 125,
    "up": 126, "f5": 96, "f6": 97, "f11": 103,
    # 字母 (US ANSI) — cmd+c/v/a/w 等快捷键依赖
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    # 数字与常用符号
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28,
    "9": 25, "0": 29, "-": 27, "=": 24, "[": 33, "]": 30, ";": 41, "'": 39,
    ",": 43, ".": 47, "/": 44, "\\": 42, "`": 50,
}
MODIFIERS = {"cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
             "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl,
             "alt": Quartz.kCGEventFlagMaskAlternate, "option": Quartz.kCGEventFlagMaskAlternate,
             "opt": Quartz.kCGEventFlagMaskAlternate, "shift": Quartz.kCGEventFlagMaskShift}

# ---------------------------------------------------------------- 内部状态
_state = {
    "budget": DEFAULT_BUDGET,
    "steps": 0,
    "task": "",
    "armed": False,        # 危险动作一次性放行
    "images": [],          # 截图文件路径 (时间序)
    "ledger": [],          # 文本账本 (会话上下文里不删减的历史)
    "recent_acts": [],     # 循环检测用
    "last_action": "",     # 上一步动作描述 (验证提示词引用)
    "verify_fails": 0,     # 连续验证失败计数 (3 次锁动作)
    "failures": [],        # 失败历史 (注入后续视觉调用, 让 LLM 避免重蹈覆辙)
    "dirty": False,        # 动作后尚未截新图 (look 自动补拍的依据)
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _traj_init(task: str, file: str | None) -> None:
    ts = time.strftime("%Y%m%d_%H%M%S")
    _state["traj"] = {
        "task": task, "started": _now(), "budget": _state["budget"],
        "file": file or str(SHOT_DIR / f"trajectory_{ts}.json"),
        "rounds": [], "failures": [],
    }
    _traj_save()


def _traj_save() -> None:
    t = _state["traj"]
    t["failures"] = list(_state["failures"])
    t["steps"] = _state["steps"]
    try:
        Path(t["file"]).write_text(
            json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _traj_round(images: list, prompt: str, response: str, verdict) -> None:
    """登记一次 LLM 调用: 输入(截图路径+提示词) / 输出(原文+判定)。"""
    _state["traj"]["rounds"].append({
        "step": _state["steps"], "ts": _now(),
        "input": {"images": list(images), "prompt": prompt},
        "output": {"response": response, "expect_met": verdict},
        "action": None,
    })
    _traj_save()


def _traj_action(kind: str, args: dict, result=None, error=None, extra=None):
    """把动作挂到触发它的那次 LLM 决策上 (无决策则自成一条)。"""
    rounds = _state["traj"]["rounds"]
    entry = rounds[-1] if rounds and rounds[-1].get("action") is None else None
    if entry is None:
        entry = {"step": _state["steps"], "ts": _now(),
                 "input": {"images": list(_state["images"][-KEEP_IMAGES:])},
                 "output": None, "action": None}
        rounds.append(entry)
    entry["action"] = {"type": kind, **args, "result": result,
                       "error": error, **(extra or {})}
    if result == "ok" and kind in ("click", "type", "key", "scroll", "paste"):
        _state["dirty"] = True
    _traj_save()


def _log(line: str) -> None:
    _state["ledger"].append(f"[{len(_state['ledger'])+1:02d}] {line}")


def _consume(kind: str) -> None:
    # 唯一硬停是轮数预算; 验证失败不锁动作 — 失败进历史, 由模型决策换方式
    _state["steps"] += 1
    if _state["steps"] > _state["budget"]:
        raise RuntimeError(
            f"CUA 轮数上限已到达 (budget={_state['budget']})。禁止继续任何屏幕操作。"
            "请总结: 已完成什么、卡在哪、当前屏幕状态, 然后结束回合向用户汇报。")


def _danger_check(text: str, where: str) -> None:
    hit = next((w for w in DANGER_WORDS if w in (text or "").lower()), None)
    if hit and not _state["armed"]:
        raise PermissionError(
            f"危险动作被拦截({where} 命中 '{hit}')。请先向用户确认; 确认后调用 "
            "cua.arm_once() 放行一次, 再重试。")
    _state["armed"] = False


def _loop_check(kind: str, sig: str) -> str:
    sig = f"{kind}:{sig}"
    _state["recent_acts"].append(sig)
    del _state["recent_acts"][:-3]
    if len(_state["recent_acts"]) == 3 and len(set(_state["recent_acts"])) == 1:
        return "⚠️ 相同动作已连续 3 次, 疑似循环。请换策略或结束。"
    return ""


def _housekeep() -> None:
    while len(_state["images"]) > KEEP_FILES:
        old = _state["images"].pop(0)
        if old in _state["images"]:      # 路径仍被引用(中断/重拍产生的重复项) → 只弹出引用不删文件
            continue
        try:
            os.remove(old)
        except OSError:
            pass


# ---------------------------------------------------------------- 屏幕/坐标
def _display_points() -> tuple[float, float]:
    """主显示器 (宽, 高) 点尺寸。CUA 固定只工作在主屏。"""
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return float(b.size.width), float(b.size.height)


def _image_size(path: str) -> tuple[int, int]:
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                         capture_output=True, text=True).stdout
    w = h = 0
    for line in out.splitlines():
        if "pixelWidth" in line:
            w = int(line.split(":")[1])
        elif "pixelHeight" in line:
            h = int(line.split(":")[1])
    return w, h


def px_to_point(x: float, y: float) -> tuple[float, float]:
    """截图像素坐标(降采样后) → 主屏全局点坐标 (左上原点)。"""
    if not _state["images"]:
        raise RuntimeError("还没有截图; 先调用 cua.screenshot() 或 cua.look()。")
    pw, ph = _image_size(_state["images"][-1])
    dw, dh = _display_points()
    return x * dw / pw, y * dh / ph


# ---------------------------------------------------------------- 截屏
def _capture_to_file() -> str:
    """截主屏 → 降采样 JPEG → 入列。不含计步/轨迹(由调用方决定)。"""
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SHOT_DIR / "_raw.png"
    r = subprocess.run(["screencapture", "-x", "-D", "1", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"screencapture 失败: {r.stderr.strip() or r.returncode} "
                           "(检查 屏幕录制 权限)")
    n = len(_state["images"]) + 1
    out = SHOT_DIR / f"shot_{n:03d}.jpg"
    r2 = subprocess.run(["sips", "-s", "format", "jpeg",
                         "-s", "formatOptions", str(JPEG_QUALITY),
                         "-Z", str(MAX_DIM), str(tmp), "--out", str(out)],
                        capture_output=True, text=True)
    if r2.returncode != 0:
        raise RuntimeError(f"sips 转换失败: {r2.stderr.strip()}")
    tmp.unlink(missing_ok=True)
    _state["images"].append(str(out))
    _housekeep()
    _state["dirty"] = False
    return out


def screenshot() -> str:
    """截取主显示器, 降采样存 JPEG, 返回文件路径。计入轮数。"""
    _consume("screenshot")
    out = _capture_to_file()
    if _state.get("traj"):
        _traj_action("screenshot", {"path": str(out)}, result="ok")
    _log(f"screenshot → {out.name}")
    return str(out)


# ---------------------------------------------------------------- 视觉观察
_LOOK_PROMPT = """你是屏幕观察器。最多两张截图: 若有两张, 第一张是上一步操作前, 第二张是当前屏幕。
任务: {task}
指令: {instruction}

请只输出 JSON (不要 markdown 围栏):
{{
  "changed": "两图之间的可见变化; 若只有一张图则描述初始屏幕",
  "action_effect": "ok | no-change | unknown",
  "elements": [{{"name": "元素名", "x": 0, "y": 0, "note": "状态/外观"}}],
  "note": "给操作者的下一步建议(一句话)"
}}
elements 只列与任务相关的可交互元素; x,y 是第二张图上的中心像素坐标(原点左上)。
回复必须是一个 JSON 对象: 第一个字符是 {{ , 最后一个字符是 }} , 禁止任何解释、分析过程、前后缀文字。"""

_VERIFY_PROMPT = """你是屏幕验证器。两张截图: 第一张 = 动作执行前, 第二张 = 当前屏幕。
任务: {task}
上一步动作: {last_action}
预期产生的状态变化(expect): {expect}
补充指令: {instruction}

流程:
1. 严格对比两图, 判定 expect 描述的状态变化是否发生 → expect_met (true/false)
2. 若 expect_met=false: 思考错误原因 — 点偏/点错元素/元素不可用/被弹窗遮挡/动作无效/需要等待 —
   写入 error_cause, 并在 note 给出具体修正方案(含修正后的坐标)
3. 若 expect_met=true: elements 列出下一批与任务相关的可交互元素及坐标, note 给下一步建议

注意: 若只提供了一张截图, 则没有"动作前"参照, 直接基于当前画面与任务上下文判定 expect。
全程用中文, 尽量简短。回复必须是一个 JSON 对象: 第一个字符是 {{ , 最后一个字符是 }} ,
禁止任何解释、分析过程、前后缀文字; 字符串值内禁止使用未转义的半角双引号。形如:
{{
  "expect_met": true,
  "changed": "两图之间的可见差异",
  "error_cause": null,
  "elements": [{{"name": "元素名", "x": 0, "y": 0, "note": "状态"}}],
  "note": "一句话"
}}"""


async def _vision_call(content: list) -> str:
    payload = json.dumps({"model": MODEL,
                          "messages": [{"role": "user", "content": content}],
                          "max_tokens": 8192, "temperature": 0.2,
                          "chat_template_kwargs": {"enable_thinking": False}})
    remote = (f"curl -s -m 120 http://127.0.0.1:{REMOTE_PORT}/v1/chat/completions "
              f"-H 'Content-Type: application/json' "
              f"-H 'Authorization: Bearer {API_KEY}' -d @-")
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_REMOTE, remote,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload.encode()),
                                          timeout=SSH_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("视觉调用超时 (ssh+curl)")
    if proc.returncode != 0:
        raise RuntimeError(f"ssh/curl 失败: {err.decode()[:300]}")
    data = json.loads(out.decode())
    if "choices" not in data:
        raise RuntimeError(f"视觉服务异常响应: {str(data)[:300]}")
    msg = data["choices"][0]["message"]
    text = msg.get("content") or ""
    if not text.strip():
        raise RuntimeError(
            f"视觉模型返回空正文 (finish_reason={data['choices'][0].get('finish_reason')}, "
            f"reasoning_tokens={data.get('usage', {}).get('completion_tokens_details', {}).get('reasoning_tokens')}) — "
            "思考耗尽了输出预算或服务异常")
    return text


import re as _re

def _expect_verdict(text: str):
    """从模型返回里抓 expect_met 判定 (容忍损坏的 JSON/围栏/叙述)。"""
    m = _re.search(r'"expect_met"\s*:\s*(true|false)', text, _re.I)
    if not m:
        return None
    return m.group(1).lower() == "true"


def _record_failure(expect: str, raw: str) -> None:
    """验证失败 → 把 动作/期望/原因 写入失败历史 (最多留 4 条)。"""
    m = _re.search(r'"error_cause"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    cause = (m.group(1) if m else raw[:150]).strip()
    _state["failures"].append(
        f"[step{_state['steps']}] {_state['last_action'] or '(无动作)'} "
        f"expect={expect[:40]!r} → 原因: {cause[:180]}")
    del _state["failures"][:-4]


def _failures_block() -> str:
    """注入视觉提示词的失败历史段 (让 LLM 看到错误, 自行决策换路径)。"""
    if not _state["failures"]:
        return ""
    lines = "\n".join(f"  {i+1}. {f}" for i, f in enumerate(_state["failures"]))
    return ("\n近期失败记录 (这些做法已被证明无效, 决策时必须避开或换路径):\n"
            + lines + "\n")


async def look(instruction: str = "", expect: str = "") -> str:
    """看屏幕 + 可选验证。计入轮数。

    expect=""            → 纯观察: 返回变化判断 + 元素坐标 (原行为)。
    expect="预期状态"    → 验证上一步动作:
        正确 (expect_met=true)  → 清零失败计数, 返回下一步元素坐标, 继续规划;
        错误 (expect_met=false) → 返回 error_cause 错误原因分析 + 修正建议,
                                   连续 3 次失败后锁动作, 逼模型总结汇报。
    图片不进入会话上下文; 只有本返回值(文本)进入。
    """
    _consume("look")
    auto_shot = None
    _state["images"] = [p for p in _state["images"] if os.path.exists(p)]  # 清掉已被删除的引用
    if not _state["images"] or _state["dirty"]:
        auto_shot = str(_capture_to_file())   # 动作后没截过新图 → 自动补拍
    imgs = _state["images"][-KEEP_IMAGES:]
    content = []
    for p in imgs:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    if expect:
        prompt = _VERIFY_PROMPT.format(task=_state["task"] or "(未设置)",
                                       expect=expect,
                                       last_action=_state["last_action"] or "(无记录)",
                                       instruction=instruction or "(无)")
    else:
        prompt = _LOOK_PROMPT.format(task=_state["task"] or "(未设置)",
                                     instruction=instruction or "(无)")
    prompt = prompt + _failures_block()
    _state["last_prompt"] = prompt
    content.append({"type": "text", "text": prompt})
    try:
        raw = await _vision_call(content)
    except Exception as e:
        _traj_round(imgs, prompt, f"<LLM调用失败: {e}>", None)
        raise

    if expect:
        verdict = _expect_verdict(raw)
        if verdict is False:
            _record_failure(expect, raw)
            _state["verify_fails"] += 1
        elif verdict is True:
            _state["verify_fails"] = 0
        if verdict is not None:
            raw = raw + f"\n[验证状态: {'通过' if verdict else '未通过'} | 本轮连续失败 {_state['verify_fails']} 次]"

    _traj_round(imgs, prompt, raw, verdict if expect else None)
    if auto_shot:
        _state["traj"]["rounds"][-1]["input"]["auto_screenshot"] = auto_shot
        _traj_save()
    _log(f"look(expect={expect[:30]!r}, {instruction[:30]!r}) → {raw[:120]}...")
    return raw


# ---------------------------------------------------------------- 鼠键动作
def click(x: float, y: float, what: str = "") -> str:
    """在截图像素坐标 (x,y) 处左键单击。what=元素名(用于危险动作拦截与账本)。计入轮数。"""
    try:
        _consume("click")
        _danger_check(what, f"click({x:.0f},{y:.0f})")
        px, py = px_to_point(x, y)
        pt = Quartz.CGPointMake(px, py)
        for etype in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown,
                      Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(None, etype, pt, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.04)
        warn = _loop_check("click", f"{round(x/10)},{round(y/10)}")
        _state["last_action"] = f"click({x:.0f},{y:.0f}) {what}".strip()
        _traj_action("click", {"x": x, "y": y, "what": what},
                     result="ok", extra={"point": [round(px, 1), round(py, 1)]})
        _log(f"click({x:.0f},{y:.0f} 点坐标 {px:.0f},{py:.0f}) {what} -> ok")
        return f"clicked ({x:.0f},{y:.0f})px what={what!r}" + (f"\n{warn}" if warn else "")
    except Exception as e:
        _traj_action("click", {"x": x, "y": y, "what": what}, error=str(e))
        raise


def type_text(text: str) -> str:
    """键入文本 (Unicode, 绕过输入法)。计入轮数; 危险词拦截。"""
    try:
        _consume("type")
        _danger_check(text, "type")
        for chunk in (text[i:i+16] for i in range(0, len(text), 16)):
            down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            Quartz.CGEventKeyboardSetUnicodeString(down, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
            up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            Quartz.CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
            time.sleep(0.03)
        warn = _loop_check("type", text[:20])
        _state["last_action"] = f"type({text[:40]!r})"
        _traj_action("type", {"text": text}, result="ok")
        _log(f"type({text[:40]!r}) -> ok")
        return f"typed {text[:60]!r}" + (f"\n{warn}" if warn else "")
    except Exception as e:
        _traj_action("type", {"text": text}, error=str(e))
        raise


def _post_combo(combo: str) -> None:
    """底层组合键注入 (key/paste 共用)。"""
    parts = [p.strip().lower() for p in combo.split("+")]
    flags = 0
    for p in parts[:-1]:
        if p not in MODIFIERS:
            raise ValueError(f"未知修饰键: {p}")
        flags |= MODIFIERS[p]
    main = parts[-1]
    if main not in KEYCODES:
        raise ValueError(f"未知按键: {main} (可用: {sorted(KEYCODES)})")
    code = KEYCODES[main]
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.04)


def key(combo: str) -> str:
    """按键/组合键, 如 key('return')、key('cmd+v')、key('cmd+shift+3')。计入轮数。"""
    try:
        _consume("key")
        _post_combo(combo)
        warn = _loop_check("key", combo)
        _state["last_action"] = f"key({combo})"
        _traj_action("key", {"combo": combo}, result="ok")
        _log(f"key({combo}) -> ok")
        return f"key {combo} ok" + (f"\n{warn}" if warn else "")
    except Exception as e:
        _traj_action("key", {"combo": combo}, error=str(e))
        raise


def scroll(direction: str = "down", lines: int = 3) -> str:
    """滚轮。direction: up/down。计入轮数。"""
    try:
        _consume("scroll")
        delta = -abs(lines) if direction == "down" else abs(lines)
        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine,
                                                  1, delta)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        _state["last_action"] = f"scroll({direction},{lines})"
        _traj_action("scroll", {"direction": direction, "lines": lines}, result="ok")
        _log(f"scroll({direction},{lines}) -> ok")
        return f"scrolled {direction} {lines} lines"
    except Exception as e:
        _traj_action("scroll", {"direction": direction, "lines": lines}, error=str(e))
        raise


def _clipboard_get() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def _clipboard_set(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


def paste(text: str, restore: bool = True) -> str:
    """把 text 写入剪贴板并 cmd+v 粘贴到当前聚焦位置。

    绕过输入法 — 中文/IME 激活时的可靠输入通道 (type_text 对中文不可靠)。
    默认粘贴后恢复原剪贴板内容 (restore=False 保留新内容)。
    调用前先用 click 把焦点放到目标输入框。计入轮数; 危险词拦截。
    """
    try:
        _consume("paste")
        _danger_check(text, "paste")
        old = _clipboard_get()
        _clipboard_set(text)
        _post_combo("cmd+v")
        if restore:
            time.sleep(0.8)          # 等目标应用处理完粘贴
            _clipboard_set(old)
        warn = _loop_check("paste", text[:20])
        _state["last_action"] = f"paste({text[:40]!r})"
        _traj_action("paste", {"text": text}, result="ok")
        _log(f"paste({text[:40]!r}) -> ok")
        return f"pasted {text[:60]!r}" + (f"\n{warn}" if warn else "")
    except Exception as e:
        _traj_action("paste", {"text": text}, error=str(e))
        raise


# ---------------------------------------------------------------- 管理面
def reset(budget: int = DEFAULT_BUDGET, task: str = "", file: str | None = None) -> str:
    """开始新任务: 清零计数/账本(磁盘截图保留), 设定轮数预算与任务描述。
    file: 轨迹 JSON 的保存路径 (默认 /tmp/cua_shots/trajectory_<时间戳>.json)。"""
    _state["budget"] = budget
    _state["steps"] = 0
    _state["task"] = task
    _traj_init(task, file)
    _state["armed"] = False
    _state["recent_acts"] = []
    _state["last_action"] = ""
    _state["verify_fails"] = 0
    _state["failures"] = []
    _state["ledger"].append(f"── 新任务: {task!r} (budget={budget}) ──")
    return f"reset ok, budget={budget}, task={task!r}"


def arm_once() -> str:
    """用户确认后放行一次危险动作。"""
    _state["armed"] = True
    return "已放行下一次危险动作 (一次性)"


def trajectory() -> str:
    """本任务的动作+LLM输入输出轨迹 JSON 文件路径 (每步自动落盘)。"""
    return _state["traj"]["file"]


def failures() -> str:
    """当前任务的失败历史 (每次视觉决策已自动携带)。"""
    return "\n".join(_state["failures"]) or "(无失败记录)"


def status() -> str:
    """当前状态: 步数/预算/账本全文。"""
    return (f"steps={_state['steps']}/{_state['budget']} task={_state['task']!r}\n"
            + "\n".join(_state["ledger"]))


def probe() -> str:
    """自检: 显示器信息、截屏能力、事件注入权限。不计轮数。"""
    dw, dh = _display_points()
    info = [f"CUA 固定使用主屏: {dw:.0f}x{dh:.0f}pt (多屏时副屏不可见不可点)"]
    try:
        p = screenshot()
        _state["steps"] -= 1  # probe 不计入预算
        w, h = _image_size(p)
        info.append(f"截屏 OK: {w}x{h}px (scale={w/dw:.2f})")
    except Exception as e:
        return "截屏失败: " + str(e)
    ev0 = Quartz.CGEventCreate(None)
    loc = Quartz.CGEventGetLocation(ev0)
    pt = Quartz.CGPointMake(loc.x + 2, loc.y)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(
                           None, Quartz.kCGEventMouseMoved, pt, 0))
    time.sleep(0.15)
    loc2 = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    if abs(loc2.x - loc.x) >= 1:
        info.append(f"事件注入 OK (鼠标 {loc.x:.0f},{loc.y:.0f} → {loc2.x:.0f},{loc2.y:.0f})")
    else:
        info.append("事件注入失败: 鼠标未移动 → 需要在 系统设置→隐私与安全性→辅助功能 "
                    "里给宿主进程授权")
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(
                           None, Quartz.kCGEventMouseMoved, loc, 0))
    return "\n".join(info)
