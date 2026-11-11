# prime-agent-mac

[Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) 的 **Computer Use Agent skill**：截屏 → 视觉观察 → 鼠标键盘注入的 GUI 自动化执行层。看屏幕-决策-操作的循环带轮数预算、动作账本、循环检测与危险动作拦截。

- 视觉：自配置的 sglang / OpenAI 兼容多模态模型（经 SSH 跳板调用，每次只带最近 2 张截图，图片不进会话上下文）
- 鼠键：macOS Quartz CGEvent（截图像素坐标自动换算屏幕点坐标，支持 Retina）
- 预算：代码强制轮数上限，超限后所有动作抛错，逼模型总结收尾

## 前置要求

1. **macOS**（Quartz 注入依赖 pyobjc，仅 macOS）
2. 已安装 [prime-agent](https://github.com/PrimeIntellect-ai/prime-agent)（`curl -Ls https://pub-728493de92a943e2a9b2d17b4719f318.r2.dev/install.sh | sh`）
3. 能 SSH 免密登录到一台跑视觉模型的跳板机（模型服务监听该机 `127.0.0.1:<port>`，OpenAI `/v1/chat/completions` 协议）
4. 系统设置 → 隐私与安全性 → **辅助功能** 权限授予运行 prime-agent 的终端

## 安装

```sh
git clone https://github.com/2877992943/prime-agent-mac.git
cd prime-agent-mac
./install.sh
```

安装脚本做的事：

1. 把 `src/cua`、`SKILL.md`、`pyproject.toml` 拷入 `~/.prime/agent/skills/cua/`（依赖 `pyobjc-framework-Quartz` 由 prime-agent 的 kernel-venv 自动安装）
2. 若尚无 `~/.prime/agent/skills/cua/cua.config.json`，从 `config.example.json` 复制一份模板

## 配置（必填）

编辑 `~/.prime/agent/skills/cua/cua.config.json`：

```json
{
  "ssh_remote": "user@gateway.example.com",
  "remote_port": 5919,
  "model": "your-vision-model",
  "api_key": "your-api-key",
  "ssh_timeout": 150
}
```

| 字段 | 说明 |
|---|---|
| `ssh_remote` | 跳板机 SSH 目标（`user@host`，需已配好免密登录） |
| `remote_port` | 视觉服务在跳板机本机监听的端口（默认 5919） |
| `model` | 视觉模型 id（OpenAI 兼容协议） |
| `api_key` | 视觉服务的 API key |
| `ssh_timeout` | 单次视觉调用超时秒数（默认 150） |

也可以用环境变量覆盖：`CUA_SSH_REMOTE`、`CUA_REMOTE_PORT`、`CUA_MODEL`、`CUA_API_KEY`、`CUA_SSH_TIMEOUT`、`CUA_CONFIG`（指定配置文件路径）。环境变量优先于配置文件。

未配置时 skill 加载会直接报错并提示缺哪些项，不会带着空配置瞎跑。

## 使用

在 prime-agent 会话里（模块预载为 `cua`）：

```python
cua.probe()                                      # 自检: 显示器/截屏/权限, 不计轮数
cua.reset(budget=40, task="打开Dock里的浏览器")   # 每个任务开头
p = cua.screenshot()                              # 截主屏
obs = await cua.look("Dock在哪? 浏览器图标坐标?")  # 视觉观察
cua.click(990, 1400, what="Safari图标")           # 截图像素坐标
cua.paste("中文内容")                             # 中文输入走剪贴板
cua.key("return")                                # 按键/组合键
cua.status()                                      # 步数/账本
```

完整纪律（每动作后 `look(expect=...)` 验证、循环检测、危险动作 `arm_once`）见 `SKILL.md`。

## 给想接入自己视觉模型的人

skill 的视觉调用是在跳板机上执行 `curl http://127.0.0.1:<remote_port>/v1/chat/completions`（OpenAI 协议，带图走 base64），所以任何 OpenAI 兼容的多模态服务都可以接：改配置指向你的跳板机和端口即可，不需要改代码。
