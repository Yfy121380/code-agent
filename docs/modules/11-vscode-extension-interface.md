# VS Code 扩展通信接口文档

## 1. 文档目标

本文从接口和事件流角度说明 CodeMate VS Code 扩展。它关注的不是某个函数内部如何
渲染按钮，而是下面这些跨边界问题：

- Webview、Extension Host 和 Python Backend 各自负责什么。
- 三个运行环境通过什么通道交换数据。
- 每种消息由谁发送、由谁监听、包含哪些字段、触发什么行为。
- 普通请求、流式输出、工具审批、会话恢复和 Diff 等完整流程如何串联。
- 请求 ID、交互 ID、工具 ID 和变更集 ID 分别解决什么问题。

相关源码阅读顺序和文件职责参见
[10-vscode-extension.md](./10-vscode-extension.md)。

---

## 2. 三层架构与职责边界

```text
┌─────────────────────────────────────────────────────────────┐
│ Webview 浏览器环境                                          │
│ webview/chat.ts                                             │
│                                                             │
│ 表单、按钮、会话列表、Markdown、工具过程、审批和 Changes UI │
└──────────────────────────┬──────────────────────────────────┘
                           │ postMessage
                           │ 进程内结构化克隆消息
┌──────────────────────────▼──────────────────────────────────┐
│ VS Code Extension Host                                     │
│ chatViewProvider.ts / codemateProcess.ts                    │
│ changeDocumentProvider.ts                                   │
│                                                             │
│ 消息路由、VS Code API、Python 进程、Diagnostics、原生 Diff  │
└──────────────────────────┬──────────────────────────────────┘
                           │ stdin / stdout
                           │ JSON Lines
┌──────────────────────────▼──────────────────────────────────┐
│ Python Backend                                              │
│ codemate/bridge/server.py / protocol.py / ui.py             │
│                                                             │
│ Agent loop、模型、工具、权限、Session、Memory、持久化       │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 Webview

Webview 是隔离的浏览器页面，只持有展示状态。它可以使用 DOM API，但不能直接调用
`vscode` 模块、读取工作区文件或启动进程。

Webview 负责：

- 收集用户输入并发送声明式操作消息。
- 渲染后端状态、历史、流式文本、工具调用和最终回答。
- 收集审批、规划问题和计划审查的用户选择。
- 保存当前页面的临时状态，例如折叠状态、输入内容和待发送附件。

### 2.2 Extension Host

Extension Host 是受信任的 Node.js 环境，也是两个隔离环境之间的中介。

它负责：

- 启动和停止 `codemate-bridge` Python 子进程。
- 把 Webview 操作转换为后端 JSONL 请求。
- 把 Python JSONL 事件转换为 VS Code `EventEmitter` 事件并转发给 Webview。
- 读取选区、文件和 Diagnostics，打开文件、设置页和原生 Diff。
- 在数据进入 Webview 前隐藏快照绝对路径等宿主内部信息。

### 2.3 Python Backend

Python Backend 是权威业务状态所在位置。它负责 Agent 行为，而不是页面渲染。

它负责：

- 串行执行普通请求、重试请求和结构化命令。
- 调用 CodeMate Runtime，并通过 `JsonUI` 把 UI 回调转换为事件。
- 管理 Session、History、Transcript、Checkpoint 和 ChangeSet。
- 在审批或用户问题期间阻塞当前 Runtime，并等待匹配的交互回复。

### 2.4 状态归属

| 状态 | 权威来源 | 其他层的职责 |
|---|---|---|
| Session、History、Checkpoint | Python Backend | Host 转发，Webview 展示 |
| Provider、Model、Approval、Workflow Mode | Python Backend | Webview 根据最新 `state` 刷新控件 |
| 当前活动请求 ID | Extension Host | Python 使用对应 `request_id` 标记事件 |
| 审批等待队列 | Python `InteractionBroker` | Webview 只返回选择 |
| 页面、折叠、输入框状态 | Webview | 不作为 Runtime 权威状态 |
| VS Code Diagnostics | Extension Host | Webview 只渲染投影 |
| 完整 ChangeSet 快照路径 | Python / Extension Host | Webview 只能看到安全投影 |

---

## 3. 四条通信通道

### 3.1 Webview 到 Extension Host

Webview 获取一次受限 API：

```ts
const vscode = acquireVsCodeApi();
```

发送消息：

```ts
vscode.postMessage({ type: 'sendMessage', text: '检查项目' });
```

Extension Host 监听：

```ts
webview.webview.onDidReceiveMessage(async (message) => { ... });
```

这不是 HTTP，也不会直接调用 Python。`ChatViewProvider` 会校验 `message.type`，再决定
调用 VS Code API 还是 `CodeMateProcess`。

### 3.2 Extension Host 到 Webview

Extension Host 发送：

```ts
webview.postMessage({
  type: 'backendEvent',
  event: { type: 'commentary', text: '正在检查入口。' },
});
```

Webview 监听浏览器消息：

```ts
window.addEventListener('message', (event) => { ... });
```

外层 `type` 表示 Host 到 Webview 的消息类别；`backendEvent.event.type` 才是 Python
事件或 Host 生成的兼容事件类别。

### 3.3 Extension Host 到 Python

`CodeMateProcess` 将对象序列化成单行 JSON，并写入子进程 stdin：

```ts
child.stdin.write(`${JSON.stringify(message)}\n`);
```

例如：

```json
{"id":"req-1","type":"ask","text":"检查项目","attachments":[]}
```

Python 使用独立输入线程逐行读取。每一行必须是一个完整 JSON 对象，不能在 stdout
协议中夹杂普通日志。

### 3.4 Python 到 Extension Host

Python 使用 `JsonLineWriter.emit()` 把事件写到 stdout：

```json
{"type":"commentary","request_id":"req-1","text":"正在检查入口。"}
```

Extension Host 使用 `readline` 按行读取：

```text
child.stdout
  -> readline line
  -> JSON.parse
  -> isBridgeEvent
  -> eventEmitter.fire
  -> ChatViewProvider.onEvent
```

Python stderr 不属于协议，直接写入 VS Code 的 CodeMate Output Channel。

---

## 4. 公共标识符与公共结构

### 4.1 `request_id`

Extension Host 每次普通请求或命令开始前生成 UUID。入站 Python 消息中字段名是 `id`，
Python 发回事件时字段名是 `request_id`。

```text
Host -> Python: { id: "...", type: "ask" }
Python -> Host: { request_id: "...", type: "run_started" }
```

同一个请求产生的 commentary、流、工具和结束事件共享同一个 `request_id`。Host 同时只
允许一个活动请求，但审批回复和取消消息可以在该请求等待期间发送。

### 4.2 `interaction_id`

`InteractionBroker` 为一次阻塞交互生成：

```text
interaction_<12位十六进制>
```

它用于匹配：

```text
approval_request / user_input_request / plan_review_request
                          ↕
                 interaction_response
```

`interaction_id` 与 `request_id` 作用不同：前者定位某个等待用户的交互，后者定位整轮
请求。

### 4.3 `stream_id`

一次流式输出使用同一个 `stream_id` 关联：

```text
stream_start -> text_delta... -> stream_end
```

`phase` 决定文本进入 Process 还是最终回答区域，当前主要值为 `commentary` 和
`final_answer`。

### 4.4 `tool_id`

`JsonUI.tool_start()` 创建 `tool_id`，`tool_result()` 使用相同 ID 更新对应工具卡片：

```text
tool_start(tool_id=tool_xxx)
tool_result(tool_id=tool_xxx)
```

### 4.5 `conversation_id`

`conversation_id` 来自 Runtime 的对话记录，用于把用户请求、assistant 消息、工具消息
和 ChangeSet 归入同一轮可见对话。它主要服务于历史重建和 UI 挂载，不替代
`request_id`。

### 4.6 `change_set_id`

ChangeSet ID 标识一次 Agent run 产生的整组文件变化，用于：

- 打开某个文件的 before/after Diff。
- 对整组变化执行 Undo All 或 Redo All。
- 将 VS Code Diagnostics 挂到正确的 Changes 面板。

### 4.7 `BridgeState`

Python 在 `ready`、`run_started`、`run_finished`、`command_result` 等事件中返回状态快照：

```json
{
  "workspace_root": "/workspace/project",
  "provider": "openai",
  "model": "gpt-5.4",
  "available_providers": ["openai", "anthropic"],
  "available_models": ["gpt-5.4", "gpt-5.5"],
  "approval_policy": "ask",
  "workflow_mode": "agent",
  "session": {
    "id": "session-id",
    "title": "实现插件接口",
    "created_at": "...",
    "updated_at": "..."
  },
  "retry": {
    "available": true,
    "user_request": "上一条请求",
    "response_annotations": []
  },
  "change_sets": []
}
```

Webview 收到带 `state` 的事件时会先合并状态，再处理具体事件。

### 4.8 `DisplayHistoryItem`

用于恢复页面的历史投影包含：

```text
id, role, kind, content, created_at, conversation_id
name?, tool_calls?, tool_call_id?, metadata?, content_hash?, response_annotations?
```

它来自独立 `transcript.jsonl`，不等同于模型使用的可压缩 History。Bridge 会过滤内部
context，完整保留用户可见文本，并隐藏写入工具的大段修改参数。

---

## 5. Webview 到 Extension Host 消息

所有消息由 `chat.ts` 调用 `vscode.postMessage()` 发送，由
`ChatViewProvider.resolveWebviewView()` 中的 `onDidReceiveMessage()` 监听。

| `type` | 主要字段 | Host 行为 |
|---|---|---|
| `ready` | 无 | 按需启动 Backend；必要时重放最近 `ready` |
| `sendMessage` | `text`, `attachments`, `responseAnnotations` | 当前 Session 中发送普通请求或 Slash Command |
| `newTask` | `text` | 新建 Session 并执行第一条请求 |
| `retryRequest` | `text`, `attachments`, `responseAnnotations` | 恢复 checkpoint 后执行替换请求 |
| `listSessions` | 无 | 发送 `session_list` 命令 |
| `openSession` | `sessionId` | 发送 `session_resume` 命令 |
| `renameSession` | `sessionId`, `title` | 发送 `session_rename` 命令 |
| `setRuntimeSetting` | `setting`, `value` | 设置 provider/model/approval |
| `requestAttachment` | `kind` | 读取选区、文件或 Problems |
| `openDiff` | `changeSetId`, `path` | 调用原生 `vscode.diff` |
| `changeAction` | `action`, `changeSetId` | 执行整轮 undo/redo |
| `openLocation` | `path`, `line` | 打开文件并跳转到行 |
| `interactionResponse` | `interactionId`, `value` | 返回审批、问题或计划决策 |
| `cancel` | 无 | 取消当前活动请求 |
| `restart` | 无 | 重启 Python Backend |
| `openSettings` | 无 | 打开 CodeMate Settings |

### 5.1 请求消息

普通请求：

```json
{
  "type": "sendMessage",
  "text": "修复这个问题",
  "attachments": []
}
```

重试：

```json
{
  "type": "retryRequest",
  "text": "修改后的请求",
  "attachments": []
}
```

附件字段结构：

```json
{
  "id": "timestamp-selection",
  "kind": "selection",
  "label": "src/app.py:10-20",
  "path": "src/app.py",
  "start_line": 10,
  "end_line": 20,
  "content": "..."
}
```

允许的 `kind` 是 `selection`、`file`、`problems`。Webview 每次请求最多携带 8 个附件；
单个附件最多 30,000 字符，合计渲染后的 editor context 最多 60,000 字符。

### 5.2 最终回答批注

只有已经收到 `run_finished`、并绑定持久化消息 ID 的最终回答允许创建批注。Webview
从渲染后的 Markdown DOM 中取得纯文本选区，并按 `p`、`li`、`pre`、标题、表格单元格
等语义块收集附近上下文；不会把 HTML 发送给 Agent。

每条批注包含来源消息 ID、内容哈希、选中文本、附近纯文本和可选评论。选中文本最多
2,000 字符，附近上下文最多 3,000 字符，一次最多发送 10 条。正文可以为空，只要至少
存在一条批注；评论也可以为空。Backend 会使用 Transcript 校验来源确实是 final 且内容
未变化，再将真实选区和评论转换成模型可理解的自然语言请求。内部消息 ID 不进入模型。

已发送批注作为用户消息元数据写入 `transcript.jsonl`，因此恢复会话后仍显示为批注卡片；
checkpoint 同时保存批注，使“编辑并重试”能够恢复原请求与原批注。

### 5.3 设置消息

```json
{
  "type": "setRuntimeSetting",
  "setting": "approval",
  "value": "auto"
}
```

Host 只接受 `provider`、`model`、`approval`。`approval` 会转换为 Python 命令参数
`{ "mode": "auto" }`。

### 5.4 交互回复

审批回复：

```json
{
  "type": "interactionResponse",
  "interactionId": "interaction_xxx",
  "value": { "allowed": true }
}
```

问题回复：

```json
{
  "type": "interactionResponse",
  "interactionId": "interaction_xxx",
  "value": {
    "status": "answered",
    "answers": {
      "storage": { "type": "option", "value": "SQLite" }
    }
  }
}
```

计划回复的 `value` 之一：

```json
{"decision":"approved"}
{"decision":"revision_requested","feedback":"补充恢复逻辑"}
{"decision":"cancelled"}
```

---

## 6. Extension Host 到 Webview 消息

Webview 统一通过 `window.addEventListener('message', ...)` 监听。

| 外层 `type` | 字段 | 用途 |
|---|---|---|
| `backendEvent` | `event` | Python 事件或 Host 本地兼容事件 |
| `attachmentResult` | `attachment` | 返回读取成功的编辑器上下文 |
| `attachmentError` | `message` | 返回附件读取错误 |
| `attachmentCancelled` | 无 | 用户取消文件选择 |

### 6.1 `backendEvent` 信封

```json
{
  "type": "backendEvent",
  "event": {
    "type": "tool_start",
    "request_id": "req-1",
    "tool_id": "tool-1",
    "name": "read_file",
    "args": {"path":"src/app.py"}
  }
}
```

`ChatViewProvider` 在发送前先执行：

```text
rememberChangeSets(raw event)
  -> 保存包含快照路径的完整变更集
forWebview(raw event)
  -> 只保留 Webview 可展示字段
postMessage(visible event)
```

### 6.2 Host 本地事件

以下事件使用与 Python 事件相同的 `backendEvent` 信封，但由 Extension Host 产生：

| 事件 | 产生位置 | 含义 |
|---|---|---|
| `connecting` | `restart()` | Backend 正在重启 |
| `connection_error` | 子进程 `error` | 进程无法启动或连接 |
| `disconnected` | 子进程 `exit` | Backend 已退出，包含 `code/signal/expected` |
| `protocol_error` | JSON 解析/结构校验 | stdout 不是合法 Bridge 事件 |
| `ui_error` | Provider 消息处理异常 | VS Code 本地操作失败 |
| `changeDiagnostics` | Diagnostics 发布 | ChangeSet 文件对应的错误和警告 |
| `command_result` (`help`) | 本地 Slash Command | `/help` 不发送给 Python |

`editor_diagnostics_request` 虽然也从 Python stdout 发出，但它不是 Webview 展示事件。
`ChatViewProvider` 会在 Extension Host 内截获它，读取 VS Code Diagnostics 后直接发送
`interaction_response`，因此聊天页面不会看到这类内部请求。

---

## 7. Extension Host 到 Python 消息

### 7.1 普通队列消息

这些消息必须包含非空 `id`，由 Python 主线程串行执行。

| `type` | 字段 | 行为 |
|---|---|---|
| `ask` | `id`, `text`, `attachments`, `response_annotations` | 当前 Session 执行 `agent.ask()` |
| `new_ask` | `id`, `text`, `attachments` | 创建新 Session 后执行请求 |
| `retry` | `id`, `text`, `attachments?`, `response_annotations?` | 恢复 checkpoint 和 transcript 后重试 |
| `command` | `id`, `name`, `args` | 执行结构化 Runtime 命令 |

例如：

```json
{"id":"req-1","type":"command","name":"compact","args":{}}
```

### 7.2 即时控制消息

以下消息由 Python 输入线程立即处理，不排在普通命令队列后：

| `type` | 字段 | 行为 |
|---|---|---|
| `interaction_response` | `interaction_id`, `value` | 唤醒对应阻塞交互 |
| `cancel` | `request_id` | 取消匹配的活动请求 |
| `shutdown` | 无 | 取消交互、终止活动请求并关闭 Bridge |

必须即时处理的原因是：主线程可能正阻塞在审批或模型任务中，如果回复也进入普通队列，
主线程永远无法先完成当前请求来读取它。

### 7.3 `command.name`

| 名称 | 主要参数 | 结果 |
|---|---|---|
| `status` | 无 | 当前 `BridgeState` |
| `approval` | `mode?` | 查询或设置审批策略 |
| `provider` | `provider?` | 查询或切换 Provider，同时重置默认 Model |
| `model` | `model?` | 查询或切换 Model |
| `plan_enter` | 无 | 进入 Plan Mode |
| `plan_exit` | 无 | 退出 Plan Mode |
| `budget` | 无 | 上下文预算报告 |
| `compact` | 无 | 手动压缩 History |
| `remember` | `text` | 写入长期记忆候选 |
| `dream` | `background?` | 前台或后台整理长期记忆 |
| `review` | `focus?` | 组织 Review 请求并执行完整 Agent run |
| `session_current` | 无 | 当前 Session 信息 |
| `session_list` | 无 | 项目会话列表 |
| `session_rename` | `session_id?`, `title` | 修改 Session 标题 |
| `session_resume` | `session_id` | 恢复 Session 和可见历史 |
| `session_new` | 无 | 创建空 Session |
| `reset` | 无 | 清空当前会话状态 |
| `history` | 无 | 返回可见 Transcript 投影 |
| `change_undo` | `change_set_id` | 整轮撤销 |
| `change_redo` | `change_set_id` | 整轮还原 |

`review` 是特殊命令：它内部调用 `_ask()`，会产生完整 `run_started` 到
`run_finished` 生命周期，因此不会再额外发 `command_result`。

---

## 8. Python 到 Extension Host 事件

### 8.1 启动、会话与请求生命周期

| `type` | 主要字段 | 含义 |
|---|---|---|
| `ready` | `protocol_version`, `state`, `history` | Bridge 初始化完成 |
| `session_opened` | `request_id`, `session`, `history`, `state` | `new_ask` 已切换到新 Session |
| `checkpoint_restored` | `request_id`, `history`, `state` | 重试前状态和 Transcript 已恢复 |
| `run_started` | `request_id`, `text`, `response_annotations`, `state` | Agent run 开始 |
| `run_finished` | `request_id`, `status`, `final?`, `messages?`, `change_set?`, `state?` | Agent run 完成、取消或失败 |
| `command_result` | `request_id`, `name`, `status`, `value`, `state` | 普通结构化命令完成 |
| `closed` | 无 | Bridge 已完成关闭 |

`run_finished.final` 是最终文本兜底。流式或非流式路径通常已先发送 `final`，Webview 会
比较文本，避免重复显示。
`run_finished.messages` 返回当前轮的权威 Transcript 投影。Webview 只在此时把最终回答的
持久化 `id` 和 `content_hash` 绑定到 DOM，因而流式输出尚未完成时不会出现批注入口。

### 8.2 模型和文本事件

| `type` | 主要字段 | Webview 行为 |
|---|---|---|
| `model_status` | `status`, `kind?`, `metadata?` | `started` 时显示 Thinking |
| `stream_start` | `stream_id`, `phase` | 创建流式累计状态 |
| `text_delta` | `stream_id`, `phase`, `text` | 累积并按动画帧渲染 Markdown |
| `stream_end` | `stream_id`, `kind`, `metadata` | 刷新剩余文本并结束流 |
| `commentary` | `text` | 在 Process 中显示非流式进展 |
| `final` | `text` | 显示非流式最终回答 |

典型流式序列：

```text
model_status(started)
stream_start(stream_id, phase=commentary)
text_delta(...)
stream_end(kind=tool_calls)
tool_start(...)
tool_result(...)
model_status(finished)
```

最终回答流的 `phase` 是 `final_answer`，`stream_end.kind` 是 `final`。

### 8.3 工具事件

`tool_start`：

```json
{
  "type": "tool_start",
  "request_id": "req-1",
  "tool_id": "tool_xxx",
  "name": "read_file",
  "args": {"path":"src/app.py"},
  "risk_level": "low"
}
```

`tool_result`：

```json
{
  "type": "tool_result",
  "request_id": "req-1",
  "tool_id": "tool_xxx",
  "name": "read_file",
  "args": {"path":"src/app.py"},
  "result": "...",
  "metadata": {}
}
```

`write_file` 和 `patch_file` 的 `args` 在 UI 协议中只保留 `path`；完整参数仍在 Runtime
内部使用。修改片段通过 `tool_result.metadata.change_preview` 提供。

### 8.4 Runtime 状态事件

| `type` | 字段 | 含义 |
|---|---|---|
| `compact_status` | `status`, `reason?`, `metadata?` | History 压缩开始和结束 |
| `review_status` | `status`, `metadata?` | Review 子 Agent 开始和结束 |

`compact_status.status` 开始时为 `started`，结束常见为 `ok`、`skipped` 或错误状态。

### 8.5 阻塞交互事件

这些事件都包含 `request_id` 和 `interaction_id`。

#### `approval_request`

```text
name, tool_id, args, metadata, options
```

`options` 已由 Python 根据当前权限场景构造，例如 Allow Once、允许某类 shell、允许某个
目录和 Deny。Webview 不重新推导权限，只展示选项并原样返回选中项的 `value`。

#### `user_input_request`

```text
questions: 1 至 3 个结构化问题
```

Webview 为每个问题增加 Other 自定义输入，并以 question ID 为键返回答案。

#### `plan_review_request`

```text
title, plan
```

Webview 渲染 Markdown，并返回 `approved`、`revision_requested` 或 `cancelled`。

#### `editor_diagnostics_request`

```text
path, wait_for_update
```

该请求由 Extension Host 直接消费，不发送给 Webview。Host 返回目标文件最多 100 条
Error 快照；Runtime 只把相对修改前基线新增的最多 20 条 Error 写入工具结果。请求设置
4 秒 Broker 超时，确保编辑器或语言服务器失效时不会永久阻塞工具循环。

#### `session_select_request`

```text
sessions, current_id
```

`JsonUI` 已定义该事件，用于兼容 Runtime 的交互式 Session 菜单；当前 Webview 没有
对应 `handleBackendEvent` 分支。插件正常会话选择使用首页的 `session_list` 和
`session_resume`，因此不会依赖这个事件。若未来让 Bridge 直接触发 Session 菜单，必须
先补充 Webview 消费逻辑。

### 8.6 错误事件

| `type` | 主要字段 | 来源 |
|---|---|---|
| `error` | `request_id`, `code`, `message` | Python 命令执行异常 |
| `protocol_error` | `message`, `request_id?`, `interaction_id?` | 任一协议边界校验失败 |
| `startup_error` | `message` | Python Agent 构建失败 |

Python 普通命令异常会先发送 `error`，再发送 `run_finished(status=error)`，确保 Webview
退出运行态。Extension Host 自己还可能产生 `connection_error`、`disconnected` 和
`ui_error`。

---

## 9. 各层监听清单

### 9.1 Webview 监听

| 监听源 | 处理内容 |
|---|---|
| `window.message` | Host 发来的 backend 和附件消息 |
| 输入框 `input/keydown` | 自动高度、Slash/@ 补全、Enter 发送 |
| 表单 `submit` | 普通请求和新任务 |
| 按钮 `click` | 停止、返回、设置、附件、审批、Diff、Undo/Redo |
| `document.click/keydown` | 关闭弹出菜单和处理 Escape |
| `window.resize` | 关闭位置可能失效的运行时菜单 |

Webview 启动最后发送 `ready`，这是后端懒启动的入口。

### 9.2 Extension Host 监听

| 监听源 | 处理内容 |
|---|---|
| `webview.onDidReceiveMessage` | 浏览器声明式操作 |
| `backend.onEvent` | Python 或 Process 本地事件 |
| `child.stdout` 的 `line` | JSONL Bridge 事件 |
| `child.stderr` 的 `data` | Output Channel 诊断日志 |
| `child.error` | 进程启动错误 |
| `child.exit` | 清理活动状态并发布断开事件 |
| `vscode.languages.onDidChangeDiagnostics` | 更新 Changes 下的 Problems |
| `webviewView.onDidDispose` | 解除当前 View 的三个订阅 |

### 9.3 Python Backend 监听

| 监听源 | 处理内容 |
|---|---|
| stdin 读取线程 | 解析每行 JSON；即时处理交互、取消、关闭 |
| 主线程命令队列 | 串行执行 ask/new_ask/retry/command |
| Runtime `JsonUI` 回调 | 把模型、工具、压缩和 Review 状态写成事件 |
| `InteractionBroker` Queue | 等待匹配的用户交互回复 |

---

## 10. 关键完整时序

### 10.1 Webview 打开与 Backend 启动

```text
VS Code -> ChatViewProvider: resolveWebviewView()
ChatViewProvider -> Webview: 设置 HTML，加载 webview.js
Webview -> ChatViewProvider: ready
ChatViewProvider -> CodeMateProcess: start()
CodeMateProcess -> Python: spawn codemate-bridge
Python -> CodeMateProcess: ready(state, history)
CodeMateProcess -> ChatViewProvider: onEvent(ready)
ChatViewProvider -> Webview: backendEvent(ready)
Webview: 恢复状态和历史，显示会话首页
```

如果 Backend 已经运行，新 Webview 不会再次收到旧的 stdout `ready`，Provider 会重放
`CodeMateProcess.lastReadyEvent`。

### 10.2 普通请求与流式输出

```text
Webview -> Host: sendMessage(text, attachments)
Host -> Python: ask(id, text, attachments)
Python -> Host: run_started(request_id, state)
Host -> Webview: backendEvent(run_started)
Webview: 创建 Turn，显示 Thinking

Python -> Host: model_status(started)
Python -> Host: stream_start
Python -> Host: text_delta ...
Host -> Webview: 逐个转发
Webview: 每动画帧合并 Markdown 渲染

Python -> Host: final 或 final stream
Python -> Host: run_finished(final, change_set, state)
Webview: 最终文本兜底、折叠 Process、恢复输入框
```

### 10.3 工具调用和审批

```text
Python Runtime -> JsonUI: approval_request(tool, args)
JsonUI -> InteractionBroker: request()
InteractionBroker -> Host: approval_request(interaction_id, options)
Host -> Webview: backendEvent(approval_request)
Webview: 渲染审批按钮

用户点击 Allow Once
Webview -> Host: interactionResponse(interactionId, value)
Host -> Python: interaction_response(interaction_id, value)
Python 输入线程 -> InteractionBroker: deliver()
Python Runtime: 审批返回，继续执行工具

Python -> Webview: tool_result
```

审批事件已经创建并登记 `tool_id`，因此获批后不会再发送一条重复的 `tool_start`；最终
`tool_result` 使用该 ID 更新审批所在的工具卡片。无需审批的工具则正常发送
`tool_start -> tool_result`。交互回复绕过普通命令队列，因此即使主线程正在等待，也能
立即解除阻塞。

### 10.4 编辑器附件

```text
Webview -> Host: requestAttachment(kind=selection/file/problems)
Host: 调用 VS Code API 读取内容并校验
Host -> Webview: attachmentResult / attachmentError / attachmentCancelled
Webview: 保存为待发送附件标签

Webview -> Host: sendMessage(text, attachments)
Host -> Python: ask(..., attachments)
Python: 校验并渲染为 editor_context
Python Runtime: 将其作为仓库证据加入当前请求
```

### 10.5 Checkpoint 重试

```text
Webview -> Host: retryRequest(edited text)
Host -> Python: retry(id, text)
Python: 读取请求前 session checkpoint
Python: 将 transcript 截断到 checkpoint 字节位置
Python: 替换 Agent Session
Python -> Webview: checkpoint_restored(history, state)
Webview: 清除旧执行结果并恢复历史
Python -> Webview: run_started ... run_finished
```

工作区文件不会随 Session checkpoint 自动回滚，重试按钮会明确提示这一点。

### 10.6 ChangeSet、Diagnostics 和原生 Diff

```text
Python -> Host: run_finished(change_set)
Host: rememberChangeSets() 保存完整快照路径
Host -> Webview: 发送不含快照路径的 change_set
Webview: 渲染 Changes 文件列表

Host: 延迟读取 VS Code Diagnostics
Host -> Webview: changeDiagnostics(changeSetId, diagnostics)
Webview: 在 Changes 下显示 Problems

用户点击文件
Webview -> Host: openDiff(changeSetId, path)
Host -> ChangeDocumentProvider: openDiff()
ChangeDocumentProvider -> VS Code: vscode.diff(beforeUri, afterUri)
VS Code -> ChangeDocumentProvider: provideTextDocumentContent(uri)
VS Code: 显示原生 Diff
```

### 10.7 取消和关闭

```text
用户点击 Stop
Webview -> Host: cancel
Host -> Python: cancel(request_id)
Python 输入线程: cancel_all() + interrupt_main()
Python -> Host: run_finished(status=cancelled)
Webview: 退出运行态

用户请求重启 Backend
Host -> Python: shutdown
Python: 取消交互、关闭 Agent、发送 closed
Host: 等待退出，超时后依次升级 SIGTERM / SIGKILL

扩展停用
Host -> Python Process: SIGTERM
```

当前 `restart()` 使用优雅关闭和超时升级流程；扩展停用触发的 `dispose()` 则直接终止
子进程，不等待 `closed` 事件。

---

## 11. 安全、校验与可靠性边界

### 11.1 Webview 入站数据不可信

`ChatViewProvider` 首先用 `isRecord()` 和字符串类型检查收窄消息，再对白名单字段做
转换。`changeAction` 只允许 `undo/redo`，设置只允许 provider/model/approval。

### 11.2 Python 入站数据分层校验

`parse_message()` 只保证顶层是对象且 `type` 是字符串；`BridgeServer` 和 Runtime 再
校验 `id`、命令名、附件数量、附件类型和具体参数。

### 11.3 stdout 专用于 JSONL

Bridge 启动后把普通 `sys.stdout` 重定向到 stderr，只有预先保存的协议流由
`JsonLineWriter` 使用。写入锁保证后台线程事件不会互相拼接。

### 11.4 Webview 数据最小化

- ChangeSet 快照绝对路径只保留在 Host。
- 写入和补丁工具只向 UI 暴露目标路径。
- 可见历史过滤内部 context，但不会裁剪用户可见消息。
- Diagnostics、附件和工具展示都有数量或长度限制。

### 11.5 CSP 与本地资源

`localResourceRoots` 限制 VS Code 可以提供的本地目录，`asWebviewUri()` 转换具体资源，
CSP 再限制浏览器允许加载的来源和资源类别。Markdown 进入 DOM 前还会经过 DOMPurify。

### 11.6 请求结束保证

正常协议请求在收到 `run_finished` 或 `command_result` 后释放 `activeRequestId`。本地
`/help` 会在生成本地结果时直接释放，子进程退出也会清空该字段。Python 异常路径会发
`error` 后再发 `run_finished(status=error)`，防止 UI 永久停留在运行态。

---

## 12. 新增接口时的检查清单

新增一条跨层能力时，应按方向检查全部边界：

1. 明确谁是权威执行者，避免 Webview 复制 Runtime 逻辑。
2. 为消息选择唯一、稳定的 `type`，并定义必需字段和错误行为。
3. Webview 到 Host 的消息要在 `onDidReceiveMessage()` 中校验和分发。
4. 需要 Python 时，在 `CodeMateProcess` 中转换成 `ask`、`command` 或即时控制消息。
5. Python 入站消息要在 `BridgeServer` 中校验并保证请求最终有结束事件。
6. Runtime UI 能力应通过 `JsonUI` 转成结构化事件，不要向 stdout 打印自由文本。
7. Host 转发前检查是否包含绝对路径、密钥或大字段，并通过 `forWebview()` 投影。
8. Webview 在 `handleBackendEvent()` 或外层信封监听中增加消费逻辑。
9. 阻塞交互必须使用唯一 `interaction_id`，并允许取消时解除等待。
10. 增加协议测试，至少覆盖事件字段、关联 ID、成功响应和错误结束路径。

---

## 13. 当前协议注意事项

- `BridgeEvent` 在 TypeScript 中是开放结构，编译期不会穷举所有事件；运行时分支必须
  自行收窄字段。
- `session_select_request` 已由 `JsonUI` 定义，但当前 Webview 不消费；插件使用首页
  Session 列表替代该交互。
- Python 会发送 `closed`，当前页面主要依赖 Extension Host 在进程退出时产生的
  `disconnected` 更新连接状态。
- `npm run compile:webview` 使用 esbuild 转译和打包，不做完整类型检查；接口改动后还应
  执行 `npm run check`。
- `retainContextWhenHidden` 只保留被隐藏 Webview 的浏览器上下文，不替代 Session 和
  Transcript 持久化。

---

## 14. 插件使用的 VS Code 能力

CodeMate 没有把 Agent Runtime 重新实现在 TypeScript 中。Python Backend 仍负责模型、
工具、权限和持久化，扩展只使用 VS Code API 补充编辑器上下文、原生界面和宿主能力。

### 14.1 Activity Bar 和 Webview View

`package.json` 通过 `viewsContainers.activitybar` 注册 CodeMate 活动栏入口，通过
`views` 注册 `codemate.chatView`。Extension Host 再调用：

```ts
vscode.window.registerWebviewViewProvider(CHAT_VIEW_ID, provider)
```

把 `ChatViewProvider` 接到这个视图。Webview 负责会话首页、聊天内容、工具卡片、审批、
配置菜单和 Changes 面板。`retainContextWhenHidden` 可以在用户暂时切走侧边栏时保留
浏览器页面，但不能替代 Session 和 Transcript 持久化。

### 14.2 Webview 资源和安全边界

Extension Host 使用 `localResourceRoots` 限定 Webview 可加载的扩展目录，再通过
`webview.asWebviewUri()` 把 `dist/webview.js` 和 `media/chat.css` 转成 Webview URI。
CSP 限制脚本、样式和图片来源，动态 Markdown 还会经过 DOMPurify。

Webview 不能导入 `vscode`、读取任意本地文件或启动进程。所有宿主操作必须先发送
`postMessage`，再由 `ChatViewProvider` 调用对应 VS Code API。

### 14.3 当前代码选区

`@selection` 使用：

```ts
vscode.window.activeTextEditor
editor.document.getText(editor.selection)
```

读取当前活动编辑器中的非空选区，并记录工作区相对路径、起止行和文本。内容最多
30,000 字符，结果先返回 Webview 形成可删除的附件标签，发送请求时才进入 Backend。

### 14.4 工作区文件附件

`@file` 使用 `vscode.window.showOpenDialog()` 选择一个文件，再通过
`vscode.workspace.fs.readFile()` 读取。当前只允许工作区内的单个文本文件，不接受
二进制文件，内容同样限制为 30,000 字符。

这不是读取 Explorer 当前高亮项；它会打开 VS Code 文件选择框，只是默认位置通常来自
当前活动文档。

### 14.5 Diagnostics 和 Problems

`@problems` 使用 `vscode.languages.getDiagnostics(uri)` 读取当前活动文件最多 20 条
Error 和 Warning。这些诊断通常由 Pylance、TypeScript Language Service、ESLint 或
其他语言扩展发布，主要属于编辑器静态分析结果，不等于 `pytest` 输出或程序运行异常。

`write_file` 和 `patch_file` 还会使用一条运行时反馈链路：首次修改文件前记录 Error
基线，修改成功后等待该文件的 Diagnostics 更新，再把最多 20 条新增 Error 附加到
该工具结果。Runtime 根据诊断消息、来源和错误代码识别原有问题，因此编辑造成的
普通行号移动不会把旧错误误判为新增错误。若语言服务器不可用或请求超时，诊断链路
直接降级，文件修改仍保持原成功状态。

这条链路的事件流为：

```text
Python Runtime -> editor_diagnostics_request
Extension Host -> openTextDocument + getDiagnostics
Extension Host -> interaction_response
Python Runtime -> 比较基线并追加新增 Error 到 tool result
```

Agent 完成整轮修改后，Extension Host 还会读取 ChangeSet 所含文件最多 50 条诊断，
并监听：

```ts
vscode.languages.onDidChangeDiagnostics(...)
```

语言服务器稍后更新结果时，Changes 面板下的 Problems 会随之刷新。轮末诊断用于 UI
展示；修改工具后的新增 Error 会自动进入 Agent 上下文；用户主动添加 `@problems`
时，当前活动文件的 Error 和 Warning 也会作为请求附件进入模型上下文。

### 14.6 打开文件和跳转位置

用户点击 Changes 下的诊断时，Extension Host 调用：

```ts
vscode.workspace.openTextDocument(uri)
vscode.window.showTextDocument(document)
editor.revealRange(range)
```

打开工作区文件，把光标移动到诊断行，并滚动到可见位置。

### 14.7 原生文本 Diff

完整 Diff 使用内置命令：

```ts
vscode.commands.executeCommand('vscode.diff', beforeUri, afterUri, title)
```

由于 before/after 是 Backend 保存的快照，不一定是工作区真实文件，扩展使用
`registerTextDocumentContentProvider()` 注册 `codemate-change:` 虚拟文档协议。VS Code
打开虚拟 URI 时调用 `provideTextDocumentContent()`，Provider 再读取对应快照内容。

Webview 只提交 `changeSetId + 相对路径`，不会获得快照绝对路径或直接读取快照。

### 14.8 配置、命令和 Output Channel

插件使用 `vscode.workspace.getConfiguration('codemate')` 读取 Backend 命令、Provider、
Model、Approval、shell Python 和 Session 恢复设置；使用
`workbench.action.openSettings` 打开当前扩展设置页。

`vscode.window.createOutputChannel('CodeMate')` 单独记录 Backend 启动命令和 stderr，
不会把诊断日志混入聊天协议或用户会话。

### 14.9 工作区和生命周期

插件通过 `workspaceFolders` 确定 Backend 的 `--cwd`，使用 `asRelativePath()` 生成展示和
附件中的相对路径，并用 `getWorkspaceFolder()` 阻止附加工作区外文件。当前主要使用第一
个 Workspace Folder，多根工作区支持并不完整。

Provider、命令、Output Channel、虚拟文档 Provider 和 Python 进程都加入
`context.subscriptions`。扩展停用时 VS Code 会统一释放这些资源。

### 14.10 当前未使用的 VS Code 能力

当前没有使用 Terminal API、Task API、Git/SCM API、Testing API、`WorkspaceEdit`、
CodeLens、Tree View、Chat Participant 或 Debug Adapter。CodeMate 修改文件、执行 shell、
管理会话和执行 Undo/Redo 都由 Python Backend 完成，而不是调用 VS Code 编辑 API。

---

## 15. ChangeSet 完整生命周期

ChangeSet 是以一次主 Agent run 为边界、由 `write_file` 和 `patch_file` 驱动的文件修改
快照。它不依赖 Git，也不会扫描整个工作区，主要用于：

- 展示本轮最终修改了哪些文件及增删行数。
- 使用 before/after 快照打开 VS Code 原生 Diff。
- 在没有后续冲突时对本轮所有修改执行 Undo All 和 Redo All。

### 15.1 标识和归属

一次用户请求会创建 `conversation_id` 和 `run_id`。ChangeSet ID 当前与 `run_id` 相同：

```text
conversation_id -> 将 Changes 面板挂到对应聊天轮次
run_id/change_set_id -> 定位 run 目录、manifest 和快照
```

只有 `runtime_mode=agent`、`depth=0` 的主 Agent 建立 ChangeSetTracker。子 Agent 不单独
创建可恢复变更集。

### 15.2 延迟跟踪，不扫描工作区

请求开始时 `begin_change_tracking()` 只创建空 Tracker：

```python
self.initial = {}
self.available = True
```

只有获准执行 `write_file` 或 `patch_file` 后，Runtime 才在真正修改前调用
`track_file_edit(path)`。`run_shell`、用户手动编辑和外部程序造成的变化不被登记。
工作区外路径也不会进入 ChangeSet。

### 15.3 首次 before 快照

同一路径第一次被修改前，Tracker 记录：

```text
exists, sha256, mode, snapshot
```

普通文件内容写入当前 run 的 `changes/snapshots/before-NNNN.bin`。同一轮再次修改该路径
不会覆盖 before，因此连续发生 `A -> B -> C -> D` 时，整轮基线仍是 A。

文件原本不存在时，before 只记录 `exists=false`，不会创建 before 快照。空文件与不存在
不同：空文件存在，因此会保存一个 0 字节快照。

### 15.4 请求结束和最终 after

Agent 请求通过 `finally` 调用 `finish_change_tracking()`。Tracker 只遍历登记过的路径，
保存 `after-NNNN.bin` 并比较首次 before 与最终 after：

| before | after | 状态和快照 |
|---|---|---|
| 存在 | 存在且不同 | `modified`，两侧都有快照 |
| 不存在 | 存在 | `added`，只有 after 快照 |
| 存在 | 不存在 | `deleted`，只有 before 快照 |
| 两侧相同 | 两侧相同 | 从 ChangeSet 中省略 |

新建后又删除会回到“两侧都不存在”，不会形成最终变更；删除后按相同内容和权限恢复也会
被省略。工具执行失败且文件未改变时同样不会留下 ChangeSet 文件项。

### 15.5 Manifest 和快照目录

存在有效变化时，run 目录包含：

```text
runs/<run_id>/changes/
├── manifest.json
└── snapshots/
    ├── before-0000.bin
    ├── after-0000.bin
    └── ...
```

Manifest 保存工作区根目录、run/conversation ID、文件状态及两侧快照元数据：

```json
{
  "id": "run_xxx",
  "run_id": "run_xxx",
  "conversation_id": "conversation_xxx",
  "tracking_mode": "tool",
  "files": [{
    "path": "src/app.py",
    "status": "modified",
    "reversible": true,
    "before": {"exists": true, "sha256": "...", "mode": 420,
               "snapshot": "snapshots/before-0000.bin"},
    "after": {"exists": true, "sha256": "...", "mode": 420,
              "snapshot": "snapshots/after-0000.bin"}
  }]
}
```

Manifest 使用原子 JSON 写入。Session 只保存最多 50 条轻量引用，不复制文件内容；恢复
会话时再通过 `run_id` 加载完整 Manifest 和快照。

### 15.6 Materialize：从持久化记录生成 UI 数据

每次加载 ChangeSet 时，Backend 都根据当前工作区重新计算状态，并为 UTF-8 文本快照
计算 additions/deletions。完整 Host 版本还包含 before/after 的绝对快照路径。

Bridge 发出 `run_finished(change_set)` 后，Extension Host 先通过
`rememberChangeSets()` 保存完整版本，再通过 `forWebview()` 删除快照路径，只发送：

```text
id, run_id, conversation_id, state, message
files[path, status, reversible, additions, deletions]
```

Webview 根据 `conversation_id` 把文件列表挂到对应 Turn。恢复 Session 时先通过
Transcript 重建 Turn，再把 `state.change_sets` 逐个挂回。相同文件在一轮内的多次修改
合并为“首次 before -> 最终 after”；不同 run 之间不合并。

### 15.7 单次 change_preview 与整轮 ChangeSet

两者是独立但互补的机制：

| 数据 | 范围 | 展示 | 是否负责恢复 |
|---|---|---|---|
| `change_preview` | 单次 write/patch | 工具卡片中的带行号短 Diff | 否 |
| ChangeSet | 整个 run | Changes 文件列表、统计、完整 Diff | 是 |

工具执行前后会额外捕获最多 2MB 的文本，使用 `difflib.unified_diff(n=3)` 生成最多 24 行、
单行最多 240 字符的预览，并放入 `tool_result.metadata.change_preview`。它同时作为
`ui_metadata` 写入 Transcript，因此恢复会话后仍能重建工具卡片预览。

Webview 解析 `@@ -oldStart +newStart @@` 初始化旧、新行号：删除行显示并递增旧行号，
新增行显示并递增新行号，上下文行同时递增两者。例如：

```diff
@@ -3,3 +3,3 @@
 context
-old
+new
```

展示为：

```text
3   context
4 - old
4 + new
```

### 15.8 原生完整 Diff

用户点击 Changes 中的文件后，Webview 只发送 `changeSetId + path`。Extension Host 从
自己的完整 ChangeSet 映射中找到快照，生成两个 `codemate-change:` URI，再调用
`vscode.diff`。VS Code 随后向 `ChangeDocumentProvider` 请求虚拟文档内容。

新增文件以“空文档 -> after”展示；删除文件以“before -> 空文档”展示。Webview 不读取
快照，也不会收到宿主绝对路径。

### 15.9 动态状态机和冲突检测

ChangeSet 的 `state` 由当前文件与 Manifest 两侧指纹动态计算：

| 状态 | 条件 | 可用操作 |
|---|---|---|
| `applied` | 所有当前文件匹配 after | Undo All |
| `reverted` | 所有当前文件匹配 before | Redo All |
| `conflict` | 当前状态不能整体匹配任一侧 | 禁止 Undo/Redo |
| `unavailable` | 存在不可恢复文件或记录错误 | 禁止 Undo/Redo |

哈希、存在状态和权限都参与比较。若 Agent 修改后用户又手动编辑文件，当前内容既不匹配
before 也不匹配 after，ChangeSet 会进入 `conflict`，避免恢复操作覆盖新修改。

### 15.10 Undo All 和 Redo All

Webview 点击按钮后发送 `changeAction`，Extension Host 转成 `change_undo` 或
`change_redo` 命令。Python 首先确认 ChangeSet ID 属于当前 Session，然后加载 Manifest
并预检整个状态：Undo 只接受 `applied`，Redo 只接受 `reverted`。

恢复规则为：

```text
Undo modified -> 写回 before
Undo added    -> 删除文件
Undo deleted  -> 从 before 重建文件

Redo modified -> 写回 after
Redo added    -> 从 after 重建文件
Redo deleted  -> 再次删除文件
```

普通文件使用同目录临时文件、`fsync`、权限恢复和 `os.replace()` 原子替换。整组文件按序
恢复；中途失败时会反向恢复已经完成的文件，尽量回到操作前一侧。命令完成后 Backend
返回更新后的 ChangeSet，Host 更新快照映射，Webview 原位置重新渲染按钮和状态。

### 15.11 大文件、二进制和特殊文件

可恢复快照的单文件上限是 10MB。超过上限、符号链接和非普通文件只记录错误或指纹，
对应 ChangeSet 为 `unavailable`。10MB 内的二进制普通文件可以快照和恢复，但不计算文本
增删行，也不能通过文本型 `vscode.diff` 展示。

---

## 16. VS Code 扩展的底层启动机制

本节先脱离 CodeMate 业务，说明一个 VS Code 扩展为什么会被发现、何时加载，以及
`activate()`、Provider 和 Webview 中的宿主对象从哪里来。这里的“注入”不是修改源码，
而是 VS Code 在约定的生命周期节点调用扩展函数并传入宿主对象。

整个机制可以先简化为下面五个阶段：

```text
1. VS Code 发现扩展并读取 package.json
   -> 静态注册 contributes 中的命令、View、Activity Bar 和 Settings
   -> 此时扩展代码尚未运行

2. 用户操作触发 Activation Event
   -> 例如打开 codemate.chatView 触发 onView
   -> 执行 codemate.showWelcome 触发 onCommand

3. VS Code 激活扩展
   -> 加载 main 指向的 dist/extension.js
   -> 调用 activate(context)
   -> 扩展注册命令回调、View Provider 和其他动态处理者

4. VS Code 继续最初触发的操作
   -> 命令触发：调用已经注册的命令回调
   -> View 触发：调用 Provider.resolveWebviewView()

5. 进入扩展自己的业务逻辑
   -> 创建 Webview、读取配置、启动 Backend、处理用户请求
```

因此，更准确的表述不是“扩展启动时静态注册 Contribution”，而是：

> VS Code 发现扩展时先根据 Manifest 建立静态功能入口；用户触发入口后，VS Code 才加载
> 扩展并执行 `activate()` 注册具体实现，随后把最初的命令或 View 请求交给对应处理者。

“扩展被发现”“扩展被激活”“Webview 被创建”和“Python Backend 被启动”是四个不同的
生命周期节点，不能混为一次启动操作。

### 16.1 编译产物和扩展清单

VS Code 不直接运行项目中的 TypeScript 源码。开发或发布前，`tsc` 把 Extension Host
代码编译到 `dist/extension.js`，esbuild 把 Webview 代码打包到 `dist/webview.js`。

VS Code 扫描扩展时先读取 `package.json`，其中最关键的入口是：

```json
{
  "engines": {"vscode": "^1.95.0"},
  "main": "./dist/extension.js",
  "activationEvents": [],
  "contributes": {...}
}
```

`main` 告诉 Extension Host 激活时加载哪个 Node.js 模块；`contributes` 声明命令、活动栏
容器、View 和 Settings。Contribution 是静态声明，VS Code 可以在尚未执行扩展代码时
先把图标、视图标题、命令和配置项加入界面。

### 16.2 静态 Contribution 与动态实现

`package.json` 只声明“存在什么”，TypeScript 代码注册“由谁实现”：

```text
package.json: 存在 codemate.chatView
extension.ts: ChatViewProvider 实现 codemate.chatView

package.json: 存在 codemate.showWelcome
extension.ts: registerCommand() 注册处理函数
```

两边的 ID 必须完全一致。静态声明缺失时功能不会出现在 VS Code 中；动态注册缺失时，
用户触发后找不到实际处理者。

### 16.3 激活时机

扩展采用懒激活。VS Code 1.95 可以根据已声明的 View 和 Command contribution 自动建立
相应激活条件，因此这里不需要显式重复 `onView:codemate.chatView` 或
`onCommand:codemate.showWelcome`。

当用户打开 CodeMate View 或执行 CodeMate 命令时，Extension Host：

```text
读取 package.json 的 main
→ 加载 dist/extension.js
→ 找到导出的 activate
→ 调用 activate(extensionContext)
```

激活扩展不等于立即启动 Python Backend。CodeMate 的 Backend 还采用第二层懒加载，直到
Webview 发出 `ready` 或某个请求需要 Backend 时才启动。

### 16.4 VS Code 提供的宿主对象

几个看起来像“自动注入”的对象实际来自生命周期回调：

| 对象或 API | 由谁提供 | 提供时机 |
|---|---|---|
| `vscode` 模块 | Extension Host | 扩展模块执行 `require('vscode')` 时解析 |
| `ExtensionContext` | VS Code | 调用 `activate(context)` 时作为参数传入 |
| `WebviewView` | VS Code | 创建目标 View 后调用 `resolveWebviewView(view)` |
| `TextDocumentContentProvider` 的 URI | VS Code | 打开对应虚拟 URI 时调用 Provider |
| `acquireVsCodeApi()` | Webview 宿主 | Webview 页面脚本运行时提供 |

`ExtensionContext` 包含扩展安装 URI、订阅集合和持久化能力。`WebviewView` 代表本次实际
侧边栏实例。`acquireVsCodeApi()` 只存在于 Webview 页面，不是普通浏览器标准 API。

### 16.5 注册即建立回调关系

`registerWebviewViewProvider()`、`registerCommand()` 和
`registerTextDocumentContentProvider()` 都不会立即执行完整功能。它们先把“ID 到处理者”
的映射登记到 VS Code：

```text
注册 View Provider
→ 用户打开 View
→ VS Code 调用 resolveWebviewView()

注册虚拟文档 Provider
→ vscode.diff 打开 codemate-change: URI
→ VS Code 调用 provideTextDocumentContent()
```

这就是许多函数没有被项目代码直接调用，却能在运行时自动进入的原因。

### 16.6 Webview 页面创建

`resolveWebviewView()` 收到 VS Code 创建的 View 后：

```text
设置 enableScripts/localResourceRoots
→ 生成带 CSP 的 HTML
→ 把 HTML 赋给 webview.html
→ 浏览器解析 HTML
→ 加载 chat.css 和 dist/webview.js
→ chat.js 注册 DOM 与 window.message 监听
→ chat.js 调用 vscode.postMessage({type: 'ready'})
```

`webview.html` 不是交给 `chat.js` 处理；它先由 Webview 浏览器解析。HTML 中的 `<script>`
标签加载 `chat.js`，之后页面交互才由该脚本控制。

### 16.7 资源释放

所有注册返回的 Disposable 都应加入 `context.subscriptions`。扩展停用或 Extension Host
关闭时，VS Code 会调用它们的 `dispose()`，解除监听、销毁 Output Channel 并终止长期
运行的 Backend 进程。Webview 自身销毁时还会单独释放只属于该页面实例的监听器。

---

## 17. CodeMate 插件完整启动和运行流程

本节把上一节的通用机制映射到当前项目，按照实际发生时间从 VS Code 启动一直讲到一次
Agent 请求完成。

### 17.1 VS Code 发现 CodeMate

VS Code 读取 `vscode-extension/package.json` 后，在 Activity Bar 中注册 `codemate` 容器，
在容器中注册 `codemate.chatView`，并把 CodeMate Settings 加入配置系统。此时 Python
进程尚未启动，`extension.ts` 也可能尚未执行。

用户打开 CodeMate View 后，VS Code 加载 `dist/extension.js` 并调用：

```ts
activate(context: vscode.ExtensionContext)
```

### 17.2 `activate()` 组装 Extension Host

CodeMate 的 `activate()` 依次创建：

```text
OutputChannel('CodeMate')
→ CodeMateProcess(extensionUri, output)
→ ChangeDocumentProvider
→ 注册 codemate-change: 虚拟文档协议
→ 注册 codemate.showWelcome 命令
→ 注册 codemate.chatView 的 ChatViewProvider
→ 将全部 Disposable 加入 context.subscriptions
```

这是依赖装配阶段。`CodeMateProcess` 构造函数只保存依赖，不会在此时 spawn Python。
`ChatViewProvider` 通过构造参数拿到同一个 Backend 客户端和 ChangeDocumentProvider，
因此消息路由与 Diff 可以共享状态。

### 17.3 创建侧边栏页面

VS Code 创建 `codemate.chatView` 实例，并调用：

```ts
ChatViewProvider.resolveWebviewView(webviewView)
```

Provider 配置 Webview、设置 HTML，然后建立三组当前页面专属监听：

```text
backend.onEvent              Python/Process 事件 -> Webview
webview.onDidReceiveMessage  Webview 操作 -> Host/Backend
onDidChangeDiagnostics       VS Code 诊断变化 -> Changes UI
```

页面脚本加载完成后初始化本地控件，并发送 `ready`。Provider 收到该消息后才调用
`CodeMateProcess.start()`。

### 17.4 读取配置并启动 Python

`start()` 首先取得第一个 Workspace Folder；没有打开目录时直接报错。随后读取
`codemate.*` 配置，替换 `${extensionRoot}` 和 `${workspaceFolder}`，构造默认命令：

```text
uv run --project <扩展根目录>/.. codemate-bridge
  --cwd <工作区>
  --provider <provider>
  --approval <approval>
  [--model <model>]
  [--resume latest]
```

`shell.pythonPath` 不改变 Bridge 自身解释器，只通过 `CODEMATE_SHELL_PATH` 调整 Agent
后续 `run_shell` 看到的 PATH。Extension Host 使用 `spawn(..., shell=false)` 创建子进程，
并分别接管 stdin、stdout 和 stderr：

```text
stdin  -> Host 写给 Python 的 JSONL
stdout -> Python 发给 Host 的 JSONL
stderr -> CodeMate Output Channel
```

### 17.5 Python Bridge 初始化

`codemate-bridge` 解析与 CLI 共用的参数，保留原始 stdout 作为协议流，再把普通 stdout
重定向到 stderr，防止库日志破坏 JSONL。之后创建：

```text
JsonLineWriter
→ RequestContext
→ InteractionBroker
→ JsonUI
→ build_agent(args, ui=JsonUI)
→ BridgeServer
```

`BridgeServer.serve()` 启动 stdin 读取线程，然后立即发送：

```json
{"type":"ready","protocol_version":1,"state":{...},"history":[...]}
```

Extension Host 最多等待 15 秒。收到 `ready` 后缓存为 `lastReadyEvent`，再通过
`eventEmitter.fire()` 通知 `ChatViewProvider`。Provider 清理宿主私有字段后调用
`webview.postMessage()`，页面据此恢复 Session、Transcript、配置和 ChangeSet。

### 17.6 用户发送请求

用户提交输入后：

```text
chat.ts
→ vscode.postMessage({type: 'sendMessage', text, attachments})
→ ChatViewProvider.onDidReceiveMessage()
→ CodeMateProcess.sendInput()
```

`CodeMateProcess` 先确认 Backend 已 ready、当前没有其他活动请求，再生成 `request_id`。
Slash Command 会在 Host 中解析为结构化 `command`；普通文本则写入：

```json
{"id":"request-uuid","type":"ask","text":"...","attachments":[]}
```

Python 输入线程按行解析 JSON。普通 ask 进入命令队列，由 Bridge 主线程串行调用 `_ask()`；
审批回复、取消和关闭则由输入线程立即处理，避免被当前阻塞请求卡住。

### 17.7 Runtime 执行与流式事件返回

`_ask()` 先保存请求前 Checkpoint，再发送 `run_started` 并调用 `agent.ask()`。Runtime
创建 conversation/run、记录用户消息、组装上下文，然后进入模型和工具循环。

`JsonUI` 把 Runtime 回调转换为 JSONL：

```text
model_start  -> model_status(started)
stream_start -> stream_start
stream_delta -> text_delta
stream_end   -> stream_end
tool_start   -> tool_start
tool_result  -> tool_result
final_answer -> final
```

每条 stdout JSONL 先由 `readline` 还原成完整行，再经过：

```text
CodeMateProcess.handleLine()
→ JSON.parse + isBridgeEvent
→ eventEmitter.fire(parsed)
→ ChatViewProvider.backend.onEvent
→ rememberChangeSets + forWebview
→ webview.postMessage
→ chat.ts.handleBackendEvent
→ 更新 DOM
```

### 17.8 审批和其他阻塞交互

需要审批时，Runtime 调用 `JsonUI.approval_request()`。`InteractionBroker` 生成
`interaction_id`、发送 `approval_request`，并阻塞等待结果。用户在 Webview 中选择后：

```text
interactionResponse
→ CodeMateProcess.respond()
→ Python interaction_response
→ stdin 线程立即 deliver
→ InteractionBroker 唤醒 Runtime
→ 执行或拒绝工具
```

`request_user_input` 和 `submit_plan` 使用同一机制，只是交互数据结构不同。

### 17.9 请求完成、ChangeSet 和诊断

模型给出 final 后，Runtime 先持久化最终消息和 run 状态，再完成 ChangeSet。Bridge 从
Agent 取得 `latest_change_set()`，发送：

```text
run_finished(request_id, final, change_set, state)
```

Extension Host 收到后释放 `activeRequestId`，保存完整 ChangeSet 快照映射，把安全投影发给
Webview。页面补齐最终文本、折叠 Process、挂载 Changes，并重新启用输入框。随后 Host
延迟读取本轮修改文件的 Diagnostics；语言服务器继续更新时，监听器还会再次刷新。

### 17.10 Session 恢复和 Webview 重建

Webview 隐藏且上下文仍保留时，不需要重建页面。若 VS Code 重新创建 Webview，但 Python
仍在运行，Provider 会重放 `CodeMateProcess.lastReadyEvent`。若 Backend 重启，则 Python
根据 `--resume latest` 恢复 Session，并在新的 `ready` 中返回完整可见 Transcript 和
ChangeSet 列表。

Transcript 用来恢复用户看到的历史，不受模型 History compact 影响；ChangeSet 通过
Session 中的轻量引用重新加载 run 快照，再按 `conversation_id` 挂回对应 Turn。

### 17.11 停止、重启和扩展关闭

用户点击 Stop 时，Host 发送带当前 `request_id` 的 `cancel`。Python 输入线程取消等待中
的交互并中断主线程，Bridge 返回 `run_finished(status=cancelled)`，但进程继续服务后续
请求。

主动 Restart 使用：

```text
shutdown JSONL
→ 等待正常退出
→ 2 秒后 SIGTERM
→ 4 秒后 SIGKILL
→ 按当前配置重新 start
```

扩展停用时，VS Code 释放 `context.subscriptions`，`CodeMateProcess.dispose()` 终止 Python
子进程，EventEmitter、View Provider、命令、虚拟文档 Provider 和 Output Channel 一并
释放。

### 17.12 一次普通请求的总时序

```text
VS Code 读取 package.json
→ 用户打开 codemate.chatView
→ 激活扩展并调用 activate(context)
→ VS Code 调用 resolveWebviewView(view)
→ Webview 加载 chat.js 并发送 ready
→ Host 读取配置并 spawn codemate-bridge
→ Python 构建 Agent 并发送 ready(state, history)
→ Webview 恢复页面

用户发送请求
→ Host 生成 request_id 并写入 ask JSONL
→ Python 保存 checkpoint，发送 run_started
→ Runtime 调模型、流式输出、审批并执行工具
→ Runtime 持久化 final 和 ChangeSet
→ Bridge 发送 run_finished
→ Host 保存完整 ChangeSet，向 Webview 发送安全投影
→ Webview 展示最终回答和 Changes，恢复可输入状态
```
