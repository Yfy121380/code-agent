# 记忆系统模块笔记

CodeMate 提供两套互相隔离的长期记忆后端。新会话通过用户级或项目级
`settings.json` 选择；Session 创建后会固定保存所选后端，恢复会话不会因为
默认配置变化而切换实现。

```json
{
  "memory": {
    "backend": "legacy"
  }
}
```

- `legacy`：现有 Candidate 提取、Dream 整理和请求前相关记忆召回。
- `progressive`：用户级 Core Memory 与项目级 Ordinary Memory。
- `disabled`：不注入长期记忆、不注册记忆工具，也不启动后台维护。

两套后端不能在同一 Session 中同时运行。下文原有的三类 Markdown、Candidate
JSONL、Dream cursor 和召回流程均属于 Legacy 后端；切换后端不会删除或迁移它们。

## Progressive 后端

本节给出整体定位；完整的执行流程、评分、工具边界和故障处理见
[Progressive 长期记忆设计笔记](08-progressive-memory-design.md)。

Progressive 后端把记忆分为两层：

- Core Memory：用户级、跨项目，只接受当前用户请求明确表达的稳定身份事实、
  偏好、安全规则和隐私规则。
- Ordinary Memory：项目级，按稳定主题保存决策、约束、问题根因、解决方法、
  兼容要求和项目反馈。

```text
~/.codemate/memory/progressive/core.json

~/.codemate/projects/<project-id>/memory/progressive/
  INDEX.json
  ordinary/M001.md
  ordinary/M002.md
```

所有 Ordinary Memory 平等保存，不划分 Active 和 Archive。`INDEX.json` 是可重建
的轻量元数据索引，正文仍保存在独立 Markdown 文件中。

每轮用户请求开始时，Runtime 使用下面的可见性分数排序：

```text
score = (1 + ln(1 + access_count)) * 2 ^ (-inactive_days / 30)
inactive_days = now - max(last_accessed_at, updated_at)
```

主 Agent 默认只看到分数最高的 25 个 `ID + title`。分数只控制默认可见性，不代表
记忆是否有效，也不会删除低分记忆。默认索引没有相关主题时，可以通过分页
`memory_index` 搜索或浏览全部标题，再调用 `memory_read` 获取正文。只有主 Agent
和 Plan Mode 的正文读取会增加访问次数；标题展示、索引查询和 Consolidation 调查
不会增加热度。

Ordinary Memory 沿用完整 conversation 与稳定 checkpoint：未处理用户轮次达到
20，或未处理消息达到 50,000 字符时，在主任务完成后启动后台 Consolidation。
History Compact 前会同步执行一次。Consolidation 子 Agent 只能调用
`memory_index`、`memory_read`、`memory_create` 和 `memory_update`。它先检查默认
25 条，再搜索完整标题集合；只能更新本轮已经读取的记录，成功后才推进 checkpoint。

## 1. Legacy 后端定位

Legacy 记忆负责跨会话仍然有价值的用户事实、工作流反馈和项目事实。当前任务进度、Todo、文件内容和工具错误不属于长期记忆，它们分别由 history、Todo 状态、文件版本状态和工具结果负责。

Legacy 后端采用三阶段流水线：

```text
完整对话
  -> 候选记忆提取
  -> Dream 整理
  -> 新请求相关记忆召回
```

## 2. 三类正式记忆

正式记忆按职责分为：

- `user_profile`：用户稳定背景、目标、知识水平和沟通偏好。
- `feedback_workflow`：用户希望 agent 如何调查、修改、验证、汇报和使用工具。
- `project_context`：项目长期目标、架构决策、约束、目录布局和功能方向。

不保存当前 Todo、单次测试失败、刚读取的文件、临时调试结论或完整工具输出。这些内容生命周期短，长期化后容易过期并污染召回。

## 3. 存储位置

记忆绑定项目，保存在：

```text
~/.codemate/projects/<project-id>/memory/
  candidates/
    YYYY-MM-DD.jsonl
  user_profile.md
  feedback_workflow.md
  project_context.md
  dream_state.json
```

Agent 初始化时直接确保这些目录和文件存在，不再依赖短期记忆门面产生副作用。

## 4. 候选记忆提取

候选提取由 runtime 定期触发，而不是依靠主 agent 自觉写日志。满足任一条件即可：

- 新增五轮完整用户对话。
- 新增消息达到 50000 字符。
- History compact 前强制提取一次。

一次用户请求内的 user、assistant、tool 消息共享稳定 `conversation_id`。Checkpoint 保存最后处理的 conversation id，因此 history compact 改变消息数量后仍能定位增量范围。

提取子请求由：

```text
Candidate extraction system prompt
待提取的完整 conversations
Candidate extraction request
```

组成。模型只返回结构化 JSON，runtime 负责校验并追加 JSONL。格式错误最多重试三次，全部失败时不推进 checkpoint。

`skill_context` 和 `todo_context` 是 compact 恢复消息，不是真实用户轮次，也不会参与候选提取或字符计数。

## 5. 手动记忆

`/remember <text>` 会直接追加一条高置信度候选：

```json
{
  "type": "unspecified",
  "memory": "...",
  "evidence": "用户通过 /remember 要求记住这条信息。",
  "confidence": "high"
}
```

它仍然先进入 candidates，随后由 Dream 统一分类、去重和处理冲突。

## 6. Dream 整理

Dream 使用独立子 Agent 维护正式记忆，主要分三阶段：

1. 读取现有三类正式记忆。
2. 从未处理候选中筛选长期有效的信息。
3. 合并重复内容、更新过时事实、解决冲突并写回文件。

Dream 子 Agent 不继承主对话 history，只接收专用 system prompt 和候选批次。它只能使用记忆整理需要的文件、Todo 工具，不能继续主任务。

只有子 Agent 正常 final 结束后才推进 `dream_state` cursor。失败时保留原 cursor，避免候选被跳过。Dream 可以前台运行，也可以后台线程运行；文件锁防止多个整理任务同时修改记忆。

## 7. 相关记忆召回

每条新用户请求开始时执行一次召回。同一轮后续工具循环复用结果，避免每次模型请求都再调用召回模型。

召回输入由：

```text
Retrieval system prompt
最近十条普通消息
最新用户请求
Retrieval request
正式长期记忆
```

组成。工具结果最多保留 300 字符，`skill_context` 和 `todo_context` 不参与召回。

优化规则：

- 正式记忆为空时跳过召回。
- 总记忆条数较少时直接全部使用，不额外调用模型。
- 记忆较多时最多选择 20 条相关事实。
- 召回失败不阻断主任务，只降级为空结果。

## 8. Relevant Memory 如何进入上下文

召回结果按三类渲染在 `Relevant memory` 中，与 Available skills 和 Runtime context 合并成同一个背景 user message。

Relevant memory 是项目历史事实，不是命令，也不是当前文件真相。当前工具结果和代码内容与记忆冲突时，以当前观察为准。

## 9. 与 History Compact 的关系

Compact 前先提取候选，避免旧对话中的稳定信息只存在于即将压缩的原文中。Compact summary 负责当前任务连续性，长期记忆负责跨会话稳定事实，两者职责不同：

- Summary 可以保存当前修改状态、验证结果和下一步。
- Long-term memory 只保存未来会话仍有价值的信息。

Todo 和 Skill 的 compact 恢复也不属于长期记忆；它们是当前任务执行状态。

## 10. 设计难点

### 提取不能依赖主 Agent 自觉

主 Agent 在复杂任务中容易忘记主动记录，因此由 runtime 按对话轮数、字符数和 compact 时机自动触发。

### 临时信息容易污染长期记忆

候选阶段允许保留不确定信息，Dream 阶段再严格筛选长期价值，并统一去重和冲突消解。

### Checkpoint 不能依赖消息下标

Compact 会改写 history，消息数量不稳定。使用 conversation id 后，提取位置与 history 长度解耦。

### 召回不能拖慢每次工具循环

召回只在新用户请求开始时执行一次，小规模记忆直接使用；内部工具循环复用结果。

## 12. 面试复述

CodeMate 通过 Session 固定选择互相隔离的记忆后端。Legacy 使用候选提取、Dream 整理和请求时召回三阶段流水线；Progressive 使用跨项目 Core Memory 和按项目主题组织的 Ordinary Memory，常驻简短 Index，正文由 Agent 按需读取，并按完整对话增量更新。

两套后端都使用 conversation id 保存增量 checkpoint，并在 compact 前处理尚未提取的完整对话。Todo、Skill、文件版本和工具错误不进入长期记忆，从而避免职责混杂和过期信息污染。Legacy 小规模记忆直接使用、大规模记忆调用召回模型；Progressive 则按访问频率和时间衰减选择默认标题，并通过 `memory_index` 和 `memory_read` 渐进加载其他主题。
