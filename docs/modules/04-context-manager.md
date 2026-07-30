# Context Manager 模块笔记

## 1. 模块定位

Context Manager 负责决定每次模型请求能够看到什么，并保持 OpenAI、Anthropic 工具消息结构正确。它把稳定规则、运行时背景、长期记忆和会话历史分层组织，让主要增长压力集中在 history，而不是让多个动态状态层同时破坏缓存。

核心职责：

- 将 Prefix 作为 system message。
- 将可用 Skill、Runtime context、Relevant memory 合并成一条背景 user message。
- 把 history summary 放在 recent history 前。
- 保持 assistant tool calls 和对应 tool results 为完整 group。
- 统计各 section 字符量和上下文 token 预算。
- 在 history 过大时压缩旧消息并保留最新原文。

## 2. 上下文分层

内部统计顺序是：

```text
prefix
available skills
runtime context
relevant memory
history summary
recent history
current request
```

真正发送模型时，`available skills`、`runtime context`、`relevant memory` 合并成同一个 user message：

```text
This message is runtime context, not a new user request. Use it as background.

Available skills:
...

Runtime context:
...

Relevant memory:
...
```

它们仍作为独立 section 统计字符数，但不会形成三个相邻的 user message。

## 3. Prefix

Prefix 是稳定的 agent 工作规则，在 Agent 启动时构建一次。它包括：

- 工具选择、停止条件和重复调用约束。
- 文件修改前调查、修改后检查 diff 的工作流。
- 行为验证和临时虚拟环境规则。
- commentary 中间进度规则。
- Todo、Skill 和 Web 工具使用规则。
- Relevant memory 的三类事实说明。
- 最终回答规则。

Prefix 不包含 Git 状态、当前时间、Todo 或已调用 Skill 等频繁变化的信息。稳定 Prefix 配合固定 tool schema，可以让服务端 prompt cache 覆盖更长的公共前缀。

## 4. Available Skills

Available skills 只提供名称和 description，帮助模型发现可用能力。完整 `SKILL.md` 不在这里重复发送；模型判断 Skill 与任务匹配后调用 `skill_load`，工具结果返回完整正文和绝对 root。

Description 单条最多 250 字符。完整 Skill 正文不截断，但 session 只保留最近三个不同 Skill，用于 compact 后恢复。

## 5. Runtime Context

Runtime context 提供当前请求可能需要的少量运行事实，例如：

- 当前日期。
- 时区。
- 项目对应的长期记忆目录。

它不承载 Todo、文件摘要、错误笔记或权限状态。权限和沙箱属于 runtime 控制信息，不需要模型理解。

## 6. Relevant Memory

Relevant memory 是针对最新用户请求召回的长期事实：

- `user_profile`
- `feedback_workflow`
- `project_context`

召回只在新用户请求开始时执行一次，同一轮内部工具循环复用结果。模型看到的是筛选后的事实，不是完整记忆文件。当前文件和工具结果与记忆冲突时，以当前观察为准。

## 7. History Summary

旧 history 经模型压缩后保存为六个固定字段：

```text
Working Directory
User Preferences And Constraints
Current State
Key Decisions
Changed Files
Validation And Issues
```

发送主模型和再次 compact 时统一包裹：

```text
This session is being continued from a previous conversation that ran out of context.
The summary below covers the earlier portion of the conversation.

Summary:
...
```

Compact 子请求只接收旧 summary、待压缩 history 和 compact request，不接收 recent history，也不提供工具。输出格式错误时最多重试三次；全部失败则恢复原 history 和 summary。

## 8. Recent History 与消息分组

Recent history 保存原始 user、assistant、tool 消息。工具交互按 group 处理：

```text
assistant(tool_calls A, B)
tool_result(A)
tool_result(B)
```

裁剪、去重和 compact 切分都以整个 group 为单位，不能留下孤立 tool result。Anthropic 要求同一轮多个 `tool_result` 紧跟对应 `tool_use`，因此这个边界属于协议正确性，而不只是上下文优化。

## 9. 观察结果清理

较旧的 `read_file`、`list_files`、`grep` 和 Web 工具结果可能非常大。History renderer 会：

- 对参数完全相同的旧只读调用去重。
- 只保留最近有限数量的完整观察结果。
- 将更早的大结果替换为 `Old tool result content cleared.`。
- 保持工具调用组完整。

因此 Prefix 要求大型调查及时在 commentary 中记录关键发现，避免有用结论只存在于可能被清理的原始结果里。

## 10. Todo 和 Skill 的状态恢复

Todo 和完整 Skill 正文不再每轮重复注入。

- `todo_write` 把完整计划写入 history，同时更新结构化 Todo 状态。
- `todo_list` 在模型需要时返回当前完整计划。
- `skill_load` 把完整 Skill 指令和 root 写入工具结果。

Compact 成功后，runtime 检查 retained recent history：

- 如果某个 Skill 的成功 `skill_load` 仍在 recent history，不重复恢复。
- 否则恢复最近调用的最多三个 Skill。
- 如果成功的 `todo_write` 或 `todo_list` 已经体现当前计划，不恢复 Todo。
- 否则在 recent history 前插入一次完整 Todo 状态。

恢复消息使用 `skill_context` 和 `todo_context` 标记。重复 compact 会先删除旧恢复消息，避免累积；这些内部消息也不会计入候选记忆用户轮次或长期记忆召回输入。

## 11. 预算管理

稳定层不做按比例裁剪，而是由各自边界控制规模：

- Prefix 在代码层保持稳定。
- Available skill 只放短 description。
- Relevant memory 最多召回 20 条。
- Tool result 单次最多 30000 字符。
- History 通过观察结果清理和 compact 控制增长。

Runtime 保存上一次模型返回的 token usage，并把后续工具结果做粗略 token 增量估计。当估算上下文达到模型上限的 90% 时，在下一次模型请求前触发 compact。

`/budget` 展示：

- 各 section 字符数和总字符数。
- Tool schema 数量和字符数。
- 最大上下文 token。
- Compact 阈值。
- 当前估算 token 和占比。

## 12. 设计取舍

移除结构化 Working Memory 的原因：

- Task summary 与当前 user message、history summary 重复。
- 文件前三行摘要通常只是 import 或文件头，语义价值有限。
- Process notes 与工具错误结果重复。
- Todo 和 Skill 每轮重复注入会不断截断 prompt cache。
- 文件摘要使用 SHA256，可能在组 prompt 时反复扫描文件。

新的边界是：

- 对话事实留在 history。
- 当前计划通过 Todo 工具管理。
- Skill 指令通过工具结果进入 history。
- 文件编辑安全由 session 中的轻量版本指纹保障。
- 跨会话事实由长期记忆召回。

这样上下文层更少，职责更明确，动态内容尽量追加在 history 尾部。

## 13. 面试复述

Codemate 的 Context Manager 将稳定 Prefix、运行背景、长期记忆和增长型 History 分开。可用 Skill、Runtime context 和 Relevant memory 合并为一条背景消息；工具交互按 group 保存，保证 OpenAI 和 Anthropic 格式都合法。

History 先清理旧的大型只读结果，接近 token 阈值时再用一次无工具模型请求生成六字段摘要，同时保留 recent history。Todo 和 Skill 平时只通过工具结果进入 history，compact 后仅在 recent history 缺少当前状态时恢复，从而兼顾任务连续性、缓存稳定性和 token 成本。
