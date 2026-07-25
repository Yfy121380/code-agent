# 持久化层与运行记录模块笔记

## 1. 模块定位

持久化层负责保存 Codemate 的会话状态、单次运行工件和运行过程 trace。它解决的是“agent 执行过程可恢复、可追踪、可复盘”的问题。

Coding agent 的问题经常不是单个函数报错，而是运行链路问题：上下文组装错了、模型返回格式没处理对、工具调用配对错了、审批结果没有按预期生效、session 恢复后状态不一致、MCP 工具没有加载成功等。如果只保存最终回答，很难定位这些问题。

因此 Codemate 把可恢复状态和审计记录分开：

- **session.json**：保存会话状态，用来恢复对话和工作现场。
- **runs/**：保存每次用户请求产生的运行工件，用来复盘一次 ask 的完整过程。
- **task_state.json**：保存一次运行当前进度和最终停止原因。
- **trace.jsonl**：保存一次运行中的事件时间线。

这个模块的核心思想是：agent 的中间过程本身就是重要数据。最终答案只能说明“结果是什么”，trace 和 task_state 才能说明“它为什么这样做”。

## 2. 存储目录结构

Codemate 当前采用项目隔离的用户级状态目录。项目内仍保留 `.codemate/settings.json` 和 `.codemate/skills/` 这类项目配置，但会话、运行记录和长期记忆放在用户级项目状态目录下。

整体结构是：

```text
<workspace>/
  .codemate/
    settings.json
    skills/

~/.codemate/
  settings.json
  skills/
  projects/
    <project-id>/
      sessions/
        <session-id>/
          session.json
          runs/
            <run-id>/
              task_state.json
              trace.jsonl
      memory/
        user_profile.md
        feedback_workflow.md
        project_context.md
        candidates/
        .dream_state.json
```

`project-id` 由 workspace 的真实绝对路径生成。比如：

```text
/home/xidiannss/Experiments/yfy/agents/code-agent
```

会变成：

```text
-home-xidiannss-Experiments-yfy-agents-code-agent
```

这样同一个用户机器上的不同项目不会互相混用 session 和 memory。

## 3. 为什么 session 和 runs 放在一起

早期如果所有 run 都放在全局 runs 目录下，排查时会很乱：你需要先找到 session，再去另一个目录里找 run，还要靠时间或 id 对应。

现在每个 session 目录下直接包含自己的 runs：

```text
sessions/
  20260722-153012-a8f31c/
    session.json
    runs/
      run_20260722-153015-xxxxxx/
      run_20260722-154020-yyyyyy/
```

这样结构更直观：

- 想恢复会话，看 `session.json`。
- 想复盘某轮用户请求，看这个 session 下的某个 run。
- 想知道一个会话发生了多少次交互，看 `runs/` 数量。

这个结构也方便以后做 UI：会话列表、会话详情、运行时间线都天然按层级组织。

## 4. Session 保存什么

Session 保存的是“恢复同一会话需要的状态”，不是单次运行日志。

核心字段包括：

```text
id
created_at
updated_at
title
title_slug
workspace_root
history
history_summary
memory
todos
active_skills
temporary_permissions
```

各字段职责：

- `id`：唯一会话 id，仍然使用时间加随机后缀，保证稳定且不冲突。
- `title`：人类可读标题，用于 banner 和 `/session` 列表。
- `title_slug`：标题归一化结果，用于按标题解析 session。
- `workspace_root`：记录会话所属 workspace。
- `history`：当前会话的消息历史，包括 user、assistant、tool。
- `history_summary`：history compact 后保存的旧历史摘要。
- `memory`：工作记忆状态，包括任务摘要、最近文件、文件摘要和过程笔记。
- `todos`：当前任务计划。
- `active_skills`：当前加载的 skill 内容。
- `temporary_permissions`：本会话临时允许的读写目录。

这里的关键点是：session 保存的是“继续工作所需的信息”。trace 里可以有大量细节，但恢复会话时不需要把 trace 全部读回来。

## 5. Session 字段补齐

恢复旧 session 时，Codemate 会补齐缺失字段。这样即使 session 是旧版本创建的，也能在当前 runtime 中继续使用。

会补齐的字段包括：

- `history`
- `history_summary`
- `title`
- `title_slug`
- `updated_at`
- `memory`
- `todos`
- `active_skills`
- `temporary_permissions`

这一步很重要。否则代码每次读 session 字段都要做大量兼容判断，主流程会变得混乱。

不过这个补齐只是结构兜底，不是旧业务逻辑兼容。已经废弃的旧字段不会继续参与运行。

## 6. Session 保存时机

Session 会在关键状态变化时保存：

- 新建 agent 后保存初始 session。
- 每次 `record()` 写入 history 后保存。
- todo 更新后保存。
- skill load / unload 后保存。
- 临时权限更新后保存。
- session rename 后保存。
- history compact 成功后保存。
- reset 后保存。

这样做的好处是：即使进程中途退出，session 也尽量停留在最近的可恢复状态。

## 7. Session List / Resume / Rename

Codemate 提供会话管理命令：

```text
/session
/session list
/session rename <title>
/session resume
codemate --resume
codemate --resume latest
codemate --resume <session-id-or-prefix-or-title>
```

`/session` 显示当前会话信息和 session 文件路径。

`/session list` 列出当前项目的所有普通用户 session，按最近更新时间倒序排列。

`/session rename` 修改当前 session 标题。标题只用于展示和选择，不影响 session id，也不改目录名。

`/session resume` 会弹出终端选择框，让用户用方向键选择要恢复的 session。

`codemate --resume` 在启动时进入选择模式；`--resume latest` 恢复最近 session；传入 id、id 前缀、标题或标题 slug 时，会尝试解析到唯一 session。

## 8. Session Title 设计

Session id 继续保持机器友好的唯一 id，不使用模型生成内容。模型只生成 title。

这样设计的原因是：

- id 必须稳定、唯一、适合做目录名。
- title 是给人看的，允许变化。
- title 生成失败不应该影响主任务。
- 用户可以随时 rename。

标题在第一轮 final answer 返回后生成。这样不会拖慢第一轮响应，也能结合用户请求和最终回答得到更准确的标题。

标题规则包括：

- 使用用户请求的语言。
- 优先概括用户实际任务，不照抄 assistant 的客套回复。
- 问候或闲聊返回 `临时对话` 或 `Casual Chat`。
- 中文标题最多 10 个汉字。
- 英文标题最多 6 个词。
- 移除 `<CPA_DONE>` 等异常标记。

Banner 中展示时会进一步裁剪，避免长标题撑破终端布局。

## 9. 子任务 Session

不是所有 session 都参与用户会话列表。

后台或子任务 session id 会带前缀，例如：

```text
dream-...
delegate-...
```

这些 session 会落盘，但不会出现在 `/session list` 和 `latest` 中。原因是它们不是用户主动对话，而是主 agent 派生出来的运行工件。

这样既能保留子 agent 的 trace 供排查，又不会污染用户选择会话时看到的列表。

Delegate 子 agent 会继承父 session 的 temporary permissions，保证已经审批过的外部读权限在调查任务中仍然生效。但它使用独立 history 和 run 目录，避免把子 agent 的大量搜索过程塞进父 agent history。

## 10. Temporary Permissions 持久化

临时权限指用户在审批时选择“本会话允许某个目录读/写”。它不会写入 settings.json，但会写入当前 session。

这样设计是因为：

- 它应该跨本 session 的多轮对话生效。
- 恢复 session 后权限行为应该一致。
- 它不应该变成项目或用户级长期配置。

存储结构大致是：

```json
{
  "temporary_permissions": {
    "permissions": {
      "read": {
        "allow": ["/abs/path"]
      },
      "write": {
        "allow": ["/abs/path"]
      }
    }
  }
}
```

每次临时权限更新后，会重新聚合权限规则，并保存 session。这样后续普通工具和 shell sandbox 都能使用同一份权限规则。

## 11. Run 是什么

一个 run 对应一次 `ask()`，也就是一次用户请求的完整执行过程。

Run 目录里目前保存：

```text
task_state.json
trace.jsonl
```

这两个文件职责不同：

- `task_state.json` 是当前状态快照，适合快速看这轮是否完成、停在哪里。
- `trace.jsonl` 是事件日志，适合按时间复盘完整过程。

这种分离很重要。状态快照会被反复覆盖更新，trace 则是追加写入的时间线。

## 12. Task State

Task state 描述一次 ask 的状态机。

核心字段包括：

```text
run_id
task_id
user_request
status
tool_steps
attempts
last_tool
stop_reason
final_answer
```

字段含义：

- `run_id`：本次运行目录 id。
- `task_id`：本次任务 id。
- `user_request`：用户原始请求。
- `status`：运行状态，可能是 `running`、`completed`、`stopped`、`failed`。
- `tool_steps`：真正进入执行阶段的工具调用次数。
- `attempts`：模型调用轮数，不等于工具调用次数。
- `last_tool`：最近执行的工具名。
- `stop_reason`：停止原因。
- `final_answer`：最终回答或停止说明。

常见 stop reason：

```text
final_answer_returned
step_limit_reached
retry_limit_reached
model_error
tool_timeout
approval_denied
delegate_failed
persistence_error
resume_load_error
```

主 agent 当前不设置普通步数上限，但 delegate 和 dream 这类受控子流程仍然会有上限。因此 task_state 仍然需要记录 step limit 和 retry limit。

## 13. Task State 写入策略

Task state 会在运行过程中不断更新：

- run 开始时写入。
- 每次模型调用前记录 attempt。
- 每次工具执行前记录 tool step 和 last_tool。
- 每次工具执行后写入最新状态。
- final answer 返回后写入 completed 状态。
- 达到停止条件或异常时写入 stopped/failed 状态。

写入使用原子写：先写临时文件，再 replace 到目标路径。这样即使进程中断，也不容易留下半截 JSON。

## 14. Trace 是什么

Trace 是一次 run 的事件时间线，保存为 JSONL。

JSONL 的好处是：

- 可以边运行边追加。
- 即使中途崩溃，前面已经写入的事件仍然可读。
- 每行都是独立 JSON，适合 grep、脚本解析和人工阅读。
- 不需要每次重写整个大文件。

Trace 主要用于回答这些问题：

- 这次请求什么时候开始？
- 组装出来的 prompt 元数据是什么？
- 模型返回的是 commentary、tool_calls 还是 final？
- 模型要求调用哪些工具？
- 工具参数、审批 gate、风险等级是什么？
- 工具结果和错误码是什么？
- history compact 是否触发？
- 长期记忆召回是否成功？
- 最后为什么结束？

## 15. Trace 事件

常见 trace event 包括：

```text
run_started
memory_retrieval_started
memory_retrieval_finished
memory_retrieval_failed
prompt_build
model_parsed
tool_executed
history_compact
history_compact_failed
skill_loaded
skill_unloaded
dream_scheduled
run_finished
```

不同事件保存的信息不同。

`run_started` 记录任务 id 和用户请求摘要。

`memory_retrieval_*` 记录长期记忆召回状态、召回数量、来源和耗时。

`prompt_build` 记录 prompt metadata、system 和一部分 messages。当前没有把所有 messages 完整写入 trace，是为了避免 trace 过大。

`model_parsed` 记录模型响应类型、工具调用数量、completion metadata 和耗时。

`tool_executed` 记录工具名、参数、结果摘要、耗时、token 估算、风险等级、只读标记、审批 gate、shell 分析、MCP 元信息、delegate 元信息等。

`history_compact` 和 `history_compact_failed` 记录 history 压缩结果。

`skill_loaded` 和 `skill_unloaded` 记录 skill 生命周期变化。

`dream_scheduled` 记录自动 dream 被调度的原因。

`run_finished` 记录最终状态、停止原因、最终回答和运行耗时。

## 16. 审批信息如何记录

审批没有单独一个固定 trace event。它的结果通过工具执行 metadata 进入 `tool_executed`。

例如工具 metadata 中会包含：

- `risk_level`
- `read_only`
- `approval_gate`
- `approval_reason`
- `approval_access`
- `approval_locations`
- `shell_kind`
- `shell_subjects`
- `shell_paths`
- `security_event_type`

如果用户拒绝审批，工具结果会变成 rejected，并记录 `tool_error_code = approval_denied`。

如果用户选择“本会话允许某目录”，这条临时权限会写入 session 的 `temporary_permissions`，而不是写入 trace 作为主要状态来源。Trace 用于复盘，session 用于恢复。

## 17. Trace 脱敏

Trace 可能保存工具参数、工具结果和 prompt metadata，因此必须做脱敏。

Codemate 会根据两类规则识别 secret：

- 用户通过 CLI 配置的 secret 环境变量名。
- 常见敏感环境变量命名模式，例如 token、key、secret 等。

写 trace 前会递归处理 payload：

- 如果字段名本身是敏感环境变量名，值替换为 `<redacted>`。
- 如果字符串内容包含当前环境中的 secret 值，也替换为 `<redacted>`。
- dict、list、tuple 会递归处理。

这样做不能保证所有可能的敏感内容都被发现，但能防止最常见的环境变量 secret 被原样写入 trace。

## 18. History 与 Trace 的区别

History 和 trace 都记录运行过程，但用途不同。

History 是模型上下文的一部分，会在后续请求中继续被模型看到。它保存的是对模型有意义的消息：

- user 消息。
- assistant commentary。
- assistant tool calls。
- tool results。
- assistant final。

Trace 是调试和复盘工件，不直接进入模型上下文。它保存的是工程上关心的执行细节：

- prompt metadata。
- model metadata。
- duration。
- risk level。
- approval gate。
- token usage。
- shell analysis。
- stop reason。

简单说：history 是给模型继续工作的，trace 是给开发者排查 agent 行为的。


## 19. 设计难点

### 难点一：恢复状态和复盘日志不能混在一起

如果把所有运行细节都塞进 session，恢复时会很重，而且容易把一次性调试信息带回模型上下文。现在 session 只保存恢复需要的状态，trace 保存复盘证据，职责更清晰。

### 难点二：运行中断时仍要留下可读信息

Agent 可能在模型请求、工具执行、审批、写文件或用户中断时停止。RunStore 使用 task_state 原子写和 trace 追加写，尽量保证即使异常退出，也能看到已经发生的事件。

### 难点三：会话标题不能影响会话身份

模型生成标题质量不稳定，可能太长、带标记或不准确。因此 session id 仍然是机器生成的稳定 id，title 只作为展示字段，可以自动生成，也可以用户手动 rename。

### 难点四：子 agent 工件不能污染用户会话列表

Delegate 和 dream 都会产生自己的 session 和 run。如果它们出现在 `/session list` 中，会让用户很难找到真正的会话。因此 session list 会过滤 `dream-` 和 `delegate-` 前缀，但这些目录仍然保留，方便排查子任务。

### 难点五：临时权限必须随 session 恢复

如果临时权限只保存在进程内，用户恢复 session 后同一个任务的权限行为会变化。现在临时权限写入 session，但不写 settings，既保证本会话连续性，也避免污染长期配置。

## 20. 面试复述版本

Codemate 的持久化层分成会话状态和运行记录两部分。会话状态保存在 `session.json`，用于恢复同一会话，里面包含 history、history summary、工作记忆、todo、active skills、临时权限和标题等信息。运行记录保存在 session 下的 runs 目录，每次用户请求对应一个 run，里面有 `task_state.json` 和 `trace.jsonl`。

`task_state.json` 是一次 ask 的状态快照，记录当前 status、模型调用次数、工具步数、最后工具、停止原因和最终回答。它会在运行过程中不断原子写入，方便中途观察或异常后恢复判断。`trace.jsonl` 是事件时间线，记录 run_started、prompt_build、model_parsed、tool_executed、history_compact、run_finished 等事件，用来复盘模型看到了什么、调用了什么工具、工具怎么返回、为什么结束。

会话管理上，session id 保持时间加随机后缀，稳定且唯一；session title 由首轮完成后模型生成，只用于展示和 `/session` 选择，不影响目录名。支持 `/session list`、`/session rename`、`/session resume` 和 `codemate --resume`。后台 dream 和 delegate 子任务也会落盘，但从普通 session 列表中过滤，避免污染用户会话选择。

整体设计的重点是职责分离：session 用于恢复，task_state 用于看当前运行状态，trace 用于复盘行为，memory 用于跨轮上下文，settings 用于长期配置。这样排查复杂 agent 行为时，可以从 session 找到 run，再沿 trace 逐步还原完整执行链路。
