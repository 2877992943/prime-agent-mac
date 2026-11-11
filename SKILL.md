---
name: cua
description: Computer Use Agent 执行层——截屏、视觉观察(自配置视觉模型, 最近2张图)、鼠标键盘注入(Quartz)。用于"看屏幕-决策-操作"的 GUI 自动化任务循环。带轮数预算、动作账本、循环检测与危险动作拦截。
---

# CUA (Computer Use Agent)

在 REPL 里直接调用（模块已预载为 `cua`）:

```python
cua.reset(budget=40, task="打开Dock里的浏览器")   # 每个任务开头
p = cua.screenshot()                              # 截主屏, 返回路径
obs = await cua.look("Dock在哪? 浏览器图标坐标?")  # 视觉观察, 返回JSON文本
cua.click(990, 1400, what="Safari图标")            # 截图像素坐标
cua.type_text("hello")                            # 键入(绕过输入法)
cua.key("return")                                 # 按键/组合键
cua.scroll("down", 3)
cua.status()                                      # 步数/账本
```

## 循环纪律

1. 每个动作后必须 `look()` 看新屏再决策——不盲连点。
2. **每个动作后必须验证**：`obs = await cua.look(expect="这个动作应产生的状态变化")`。
   - `expect_met: true` → 继续规划下一步；
   - `expect_met: false` → 错误原因已自动进入失败历史（后续 look 自动携带），
     你自己判断：换路径 / 换坐标重试 / 绕道 / 放弃并汇报——skill 不替你决定；
   - 唯一硬停是轮数预算（到顶所有动作锁死，只能总结汇报）。
   只观察不验证时才用不带 expect 的 `look()`。
3. `elements` 里的坐标是截图像素坐标，直接传给 `click(x, y)`，换算自动完成。
3.5 动作后**不需要**手动 `screenshot()`——`look()` 检测到"动作后没有新图"会自动补拍，验证永远是动作后的世界。
3.6 **中文输入用 `paste` 不用 `type_text`**：`paste("企业微信")`（写剪贴板+cmd+v，绕过输入法，粘贴后自动恢复原剪贴板；默认 restore=True）。`type_text` 仅对拉丁字符可靠（IME 激活时中文合成事件会被丢弃）。`key()` 支持全部字母/数字快捷键：`cmd+v / cmd+c / cmd+a / cmd+w / cmd+tab`。
4. 出现"疑似循环"警告 → 换策略（esc 回滚、滚动、换路径）或结束汇报。
5. 危险动作（删除/发送/支付类）会被拦截；只有用户明确确认后才能 `cua.arm_once()` 放行一次。
6. 轮数到上限后所有动作抛错——此时必须总结现状并结束回合，不许绕过。

## 上下文策略

- 图片**永不进入**会话上下文：视觉走带外通道（skill 持有截图滑动窗口，每次取最近 2 张发给视觉模型）。
- 观察文本与动作账本是唯一历史，会话里不删减。

## 自检

```python
cua.probe()   # 显示器信息/截屏能力/辅助功能权限, 不计轮数
```
