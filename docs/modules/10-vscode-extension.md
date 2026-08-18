# VS Code 扩展设计与源码阅读指南

> 本文按文件职责帮助阅读源码。需要查找 Webview、Extension Host 和 Python Backend
> 之间的完整消息类型、字段与交互时序时，请阅读
> [VS Code 扩展通信接口文档](./11-vscode-extension-interface.md)。

## 1. 文档目标

CodeMate 的 VS Code 扩展不是把 CLI 输出简单地嵌入一个终端，而是为现有 Python
Agent 增加了一层图形化客户端。它需要同时处理以下问题：

- 在 VS Code 中注册侧边栏和配置项。
- 启动并维护一个长期运行的 Python Agent 进程。
- 在 TypeScript 与 Python 之间传递请求、流式文本、工具调用和审批结果。
- 在隔离的 Webview 页面中渲染会话、Markdown、工具过程和交互控件。
- 读取当前选区、文件和 Problems 等只有 VS Code 才知道的信息。
- 使用 VS Code 原生 Diff 编辑器展示 Agent 当轮产生的文件变化。
- 在不覆盖用户后续修改的前提下撤销或还原整轮代码变更。

这份文档面向只掌握 TypeScript 基础语法的读者。阅读时不需要一次理解所有代码，
应先掌握运行环境和消息流，再逐步阅读每个功能分支。

---

## 2. 最重要的概念：三个运行环境

插件代码看起来复杂，主要原因不是 TypeScript 语法，而是同一个功能跨越了三个彼此
隔离的运行环境。

```text
┌──────────────────────────────────────────────────────────────┐
│ VS Code Extension Host                                      │
│                                                              │
│ extension.ts                                                 │
│ chatViewProvider.ts                                          │
│ codemateProcess.ts                                           │
│ changeDocumentProvider.ts                                    │
│                                                              │
│ 能调用 vscode API、Node.js fs/path/child_process             │
└───────────────────────┬───────────────────────┬──────────────┘
                        │ postMessage           │ stdin/stdout
                        │                       │ JSONL
┌───────────────────────▼────────────────┐      │
│ Webview 浏览器环境                     │      │
│                                        │      │
│ webview/chat.ts                        │      │
│ media/chat.css                         │      │
│                                        │      │
│ 只能使用 DOM 和浏览器 API              │      │
│ 不能直接调用 vscode、fs、child_process │      │
└────────────────────────────────────────┘      │
                                                │
┌───────────────────────────────────────────────▼──────────────┐
│ Python 后端进程                                              │
│                                                              │
│ codemate/bridge/server.py                                    │
│ codemate/bridge/ui.py                                        │
│ codemate/bridge/protocol.py                                  │
│ CodeMate runtime                                             │
│                                                              │
│ 负责 Agent loop、模型、工具、Session、权限和持久化           │
└──────────────────────────────────────────────────────────────┘
```

### 2.1 Extension Host

Extension Host 是 VS Code 为扩展提供的 Node.js 运行环境。这里可以：

- 调用 `vscode.window`、`vscode.workspace` 和 `vscode.languages`。
- 启动 Python 子进程。
- 读取本地文件。
- 打开编辑器、Diff 页面和文件选择框。
- 注册 Webview、命令和虚拟文档 Provider。

`src/` 下的 TypeScript 文件都运行在这里。

### 2.2 Webview

Webview 可以理解为 VS Code 侧边栏中的一个隔离浏览器页面。它能操作 HTML、CSS、
DOM，但不能直接使用 `vscode` 模块，也不能直接访问文件系统。

因此下面这种代码只能写在 Extension Host 中：

```ts
vscode.window.activeTextEditor
vscode.languages.getDiagnostics(uri)
vscode.workspace.fs.readFile(uri)
```

Webview 想读取选区时，只能发送消息：

```ts
vscode.postMessage({ type: 'requestAttachment', kind: 'selection' });
```

Extension Host 收到消息后读取选区，再把结果发回 Webview。

### 2.3 Python 后端

Python 进程是真正运行 CodeMate Agent 的地方。插件不重新实现模型调用、工具系统、
审批或上下文管理，而是通过 Bridge 调用已有 runtime。

这种设计避免了维护两套 Agent，同时让 CLI 和插件共享同一套核心能力。

---

## 3. 总体目录与阅读顺序

### 3.1 插件目录

```text
vscode-extension/
├── package.json
├── tsconfig.json
├── tsconfig.webview.json
├── src/
│   ├── extension.ts
│   ├── protocol.ts
│   ├── codemateProcess.ts
│   ├── chatViewProvider.ts
│   └── changeDocumentProvider.ts
├── webview/
│   └── chat.ts
├── media/
│   ├── chat.css
│   └── codemate.svg
└── dist/
    └── 编译后的 JavaScript
```

### 3.2 Python Bridge

```text
codemate/bridge/
├── protocol.py
├── server.py
└── ui.py
```

Diff 和撤销功能还会用到：

```text
codemate/storage/change_sets.py
codemate/runtime/changes.py
codemate/runtime/change_preview.py
```

### 3.3 推荐阅读顺序

1. `package.json`：知道扩展向 VS Code 声明了什么。
2. `src/extension.ts`：知道插件启动时创建了哪些对象。
3. `src/protocol.ts`：知道 TypeScript 侧的数据大致长什么样。
4. `src/codemateProcess.ts`：理解 Python 进程和 JSONL 通信。
5. `codemate/bridge/protocol.py`：理解 Python 侧如何收发 JSONL。
6. `codemate/bridge/server.py`：理解消息如何转成 `agent.ask()`。
7. `codemate/bridge/ui.py`：理解 runtime 事件如何返回插件。
8. `src/chatViewProvider.ts`：理解 Webview、后端和 VS Code API 如何连接。
9. `webview/chat.ts`：理解页面状态和渲染。
10. `src/changeDocumentProvider.ts`：最后理解 Diff 快照。

不要首先逐行阅读 `chat.ts`。它是最终汇集所有 UI 功能的地方，在不知道消息从哪里来
之前，很容易只看到大量互不相关的 DOM 操作。

---

## 4. `package.json`：扩展清单

`package.json` 不只是 npm 配置，也是 VS Code 扩展的声明文件。

### 4.1 入口

```json
"main": "./dist/extension.js"
```

VS Code 加载的是编译后的 `dist/extension.js`，不是直接执行 TypeScript。

### 4.2 VS Code 版本

```json
"engines": {
  "vscode": "^1.95.0"
}
```

这表示插件依赖 VS Code 1.95 及其兼容版本提供的 API。

### 4.3 Activity Bar 和侧边栏

`viewsContainers.activitybar` 声明 CodeMate 左侧图标和容器，`views.codemate` 声明
容器中的 `codemate.chatView` Webview。

这个 ID 必须和代码中的常量一致：

```ts
export const CHAT_VIEW_ID = 'codemate.chatView';
```

之后 `extension.ts` 才能为这个 View 注册 `ChatViewProvider`。

### 4.4 Settings

`contributes.configuration` 声明插件设置，例如：

- `codemate.backend.command`：启动后端使用的命令，默认是 `uv`。
- `codemate.backend.arguments`：传给后端命令的参数。
- `codemate.provider`：默认模型 Provider。
- `codemate.model`：模型覆盖值。
- `codemate.approval`：审批策略。
- `codemate.shell.pythonPath`：工具运行时优先使用的 Python。
- `codemate.session.resumeLatest`：启动时是否恢复最近会话。

声明在这里后，VS Code Settings 页面才能识别并展示这些配置。

### 4.5 构建命令

```text
npm run check
  ├── 检查 Extension Host TypeScript
  └── 检查 Webview TypeScript

npm run compile
  ├── tsc 编译 src/
  └── esbuild 打包 webview/chat.ts
```

Extension Host 和 Webview 使用两套 TypeScript 配置，因为前者运行在 Node.js，后者
运行在浏览器。

### 4.6 其他工程文件

- `package-lock.json`：锁定 npm 依赖的精确版本，由 npm 维护。学习业务逻辑时通常不需要
  阅读，也不应手工编辑。
- `.vscodeignore`：控制打包扩展时排除哪些开发文件，作用类似 npm 包的忽略清单。
- `dist/`：TypeScript 和 Webview 打包后的 JavaScript。它是构建产物，调试逻辑时应阅读
  `src/` 和 `webview/`，修改后再运行编译命令生成。
- `README.md`：面向扩展开发者的启动和配置说明，不参与运行。
- `media/codemate.svg`：Activity Bar 中 CodeMate 容器使用的图标。

---

## 5. 两套 TypeScript 配置为什么不同

### 5.1 `tsconfig.json`

它编译 `src/**/*.ts`，模块系统为 Node16，输出到 `dist/`。这些代码可以使用：

- Node.js API，例如 `child_process`、`fs`、`path`。
- VS Code API。
- `Buffer` 和 `NodeJS.ProcessEnv` 等 Node 类型。

### 5.2 `tsconfig.webview.json`

它检查 `webview/**/*.ts`，包含 DOM 类型但不加载 Node 类型。这里可以使用：

- `document`、`window`、`HTMLElement`。
- `addEventListener`、`querySelector`。
- 浏览器事件。

这里不能直接使用 `fs`、`child_process` 或导入 `vscode`。

这个限制不是项目人为增加的麻烦，而是 Webview 的真实安全边界。

---

## 6. `src/extension.ts`：装配入口

这个文件只有一个核心函数：

```ts
export function activate(context: vscode.ExtensionContext): void
```

VS Code 激活扩展时调用它。函数完成四件事。

### 6.1 创建 Output Channel

```ts
const output = vscode.window.createOutputChannel('CodeMate');
```

它用于记录 Python 后端启动命令、stderr 和连接错误。用户可以在 VS Code 的 Output
面板选择 CodeMate 查看。

### 6.2 创建长期后端管理器

```ts
const backend = new CodeMateProcess(context.extensionUri, output);
```

`CodeMateProcess` 不会在构造时立即启动 Python；第一次调用 `start()` 或发送请求时
才启动。

### 6.3 注册虚拟 Diff 文档

```ts
const changeDocuments = new ChangeDocumentProvider();
changeDocuments.register(context);
```

它为 `codemate-change:` URI 提供只读文本，后续 VS Code 原生 Diff 编辑器会读取这些
虚拟文档。

### 6.4 注册 Webview Provider

```ts
vscode.window.registerWebviewViewProvider(
  CHAT_VIEW_ID,
  new ChatViewProvider(context.extensionUri, backend, changeDocuments),
)
```

当用户打开 CodeMate 侧边栏时，VS Code 调用 Provider 的 `resolveWebviewView()`，
Provider 再构造 HTML 并建立消息监听。

### 6.5 `context.subscriptions`

```ts
context.subscriptions.push(output, backend, showWelcome, chatView);
```

这些对象都实现或关联 `dispose()`。扩展停用时 VS Code 会统一释放它们，避免事件
监听、Output Channel 和 Python 进程泄漏。

---

## 7. `src/protocol.ts`：TypeScript 数据边界

这个文件定义跨进程或跨 Webview 使用的基础结构。

### 7.1 `BridgeEvent`

```ts
export interface BridgeEvent {
  type: string;
  request_id?: string;
  state?: BridgeState;
  [key: string]: unknown;
}
```

所有后端事件至少有 `type`。不同事件还可能携带：

- `text`：commentary 或 final 文本。
- `tool_id`、`name`、`args`：工具调用。
- `result`：工具结果。
- `interaction_id`：等待用户回答的交互。
- `state`：当前 Provider、Model、Session 和审批状态。

之所以保留 `[key: string]: unknown`，是因为事件类型很多，第一版没有为每一种事件
建立独立的联合类型。

### 7.2 为什么使用 `unknown`

`unknown` 表示“现在还不知道它是什么”，使用前必须检查。它比 `any` 更安全：

```ts
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
```

`isRecord()` 是一个类型守卫。检查成功后，TypeScript 才允许把输入当对象使用。

### 7.3 `DisplayHistoryItem`

这是 Python Bridge 返回给 UI 的历史投影，不等同于完整 Session History。Bridge 会：

- 限制条目数量和内容长度。
- 隐藏内部 context 消息。
- 保留 UI 恢复对话所需的 role、kind、tool call 和 conversation ID。

---

## 8. `src/codemateProcess.ts`：Python 进程和 JSONL 客户端

`CodeMateProcess` 是 Extension Host 中的后端客户端。它不理解 Agent 逻辑，只负责
进程生命周期、请求互斥和协议转发。

### 8.1 重要字段

```ts
private child: ChildProcessWithoutNullStreams | undefined;
private readyEvent: BridgeEvent | undefined;
private activeRequestId = '';
private readonly eventEmitter = new vscode.EventEmitter<BridgeEvent>();
```

- `child`：当前 Python 子进程。
- `readyEvent`：后端完成初始化后返回的状态。
- `activeRequestId`：当前正在执行的请求，非空时禁止再发一个请求。
- `eventEmitter`：把后端事件广播给 `ChatViewProvider`。

### 8.2 `start()` 如何启动后端

流程如下：

```text
读取当前 workspace
  -> 读取 VS Code Settings
  -> 替换 ${extensionRoot} 和 ${workspaceFolder}
  -> 拼接 --cwd / --provider / --approval / --model
  -> spawn(command, args)
  -> 按行读取 stdout
  -> 等待 ready 事件
```

默认命令近似于：

```bash
uv run --project <CodeMate仓库> codemate-bridge --cwd <当前项目>
```

### 8.3 为什么 stdout 必须按行读取

Bridge 协议使用 JSON Lines：每行是一个完整 JSON 对象。

```json
{"type":"commentary","request_id":"req-1","text":"正在查看入口。"}
{"type":"tool_start","request_id":"req-1","name":"read_file"}
```

`readline.createInterface()` 把 stdout 流切成行，`handleLine()` 再执行
`JSON.parse()`。这样不需要自己处理一个 JSON 被拆成多个字节块的问题。

### 8.4 stderr 为什么不走 JSON

stdout 专门用于机器可解析协议；stderr 可能包含 Python warning、traceback 或库日志，
因此直接写入 CodeMate Output Channel，不能混入 stdout。

### 8.5 请求 ID 和请求互斥

`beginRequest()` 使用 `randomUUID()` 创建请求 ID，并检查 `activeRequestId`：

```text
没有活动请求 -> 允许发送
已有活动请求 -> 报 CodeMate is already processing a request
```

收到 `run_finished` 或 `command_result` 后，活动 ID 才会被清空。

### 8.6 普通请求和 Slash Command

`sendInput()` 先调用 `parseSlashCommand()`：

- 普通文本发送 `{type: "ask", text, attachments}`。
- `/compact` 发送 `{type: "command", name: "compact"}`。
- `/review xxx` 发送 `{type: "command", name: "review", args: {focus}}`。

因此 Slash Command 不会先交给模型理解，而是由 Bridge 直接执行确定的 runtime 操作。

### 8.7 取消

```ts
cancel(): void {
  this.sendRaw({ type: 'cancel', request_id: this.activeRequestId });
}
```

取消消息不创建新请求，而是引用当前请求 ID。Python Bridge 收到后中断主线程中的
Agent 操作。

### 8.8 后端环境与 shell 环境

扩展可能通过项目自己的 `.venv` 启动 Bridge，但用户希望 `run_shell` 使用另一个
终端 Python。`buildBackendEnvironment()` 将工具 PATH 保存到
`CODEMATE_SHELL_PATH`，runtime 构造 shell 环境时再读取它。

因此需要区分：

- Bridge 使用哪个 Python 运行 CodeMate。
- Agent 的 `run_shell` 在哪个 PATH 中找 `python`、`pytest` 等命令。

---

## 9. Python `bridge/protocol.py`：JSONL 与交互关联

### 9.1 `JsonLineWriter`

`emit()` 把事件编码为单行 JSON，并使用锁保证多个线程不会把输出交错：

```text
线程 A: {"type":"commentary"...}\n
线程 B: {"type":"tool_result"...}\n
```

没有锁时，两条 JSON 可能在 stdout 中混成一条无效数据。

### 9.2 `parse_message()`

它负责最基础的入站校验：

- 必须是合法 JSON。
- 顶层必须是对象。
- `type` 必须是非空字符串。

具体命令参数由 `BridgeServer` 或 runtime 继续校验。

### 9.3 `InteractionBroker`

审批、Plan 审批和 `request_user_input` 与普通事件不同：Python runtime 需要暂停，
等待用户选择后才能继续。

```text
Python 主线程：request("approval_request")
  -> 创建 interaction_id 和 Queue
  -> 向插件发送审批事件
  -> 阻塞等待 Queue.get()

Webview：用户点击 Allow once
  -> interactionResponse
  -> Extension Host
  -> Python stdin

Bridge 读线程：deliver()
  -> 根据 interaction_id 找 Queue
  -> put 用户选择

Python 主线程：Queue.get() 返回
  -> Agent 继续执行工具
```

这也是 Bridge 必须拥有独立输入线程的原因：主线程等待用户时，仍然需要有人读取
stdin 中的回答。

---

## 10. Python `bridge/server.py`：协议路由器

`BridgeServer` 位于 TypeScript 客户端和 CodeMate runtime 之间。

### 10.1 `serve()`

启动时：

1. 创建后台输入线程 `_read_commands()`。
2. 输出 `ready`，附带状态和可展示历史。
3. 主线程从命令队列中依次取请求。
4. 调用 `_dispatch()`。
5. 关闭时取消交互、关闭 Agent 并输出 `closed`。

命令串行执行，所以同一个 Agent Session 不会同时被两个普通请求修改。

### 10.2 `_read_commands()`

这个线程持续读取 stdin，并区分三类消息：

- `interaction_response`：直接交给 `InteractionBroker`，不能排到普通命令后面。
- `cancel`：中断当前主线程任务。
- 普通 ask/command：放入 `_commands` 队列，等待串行执行。

### 10.3 `_dispatch()`

它按 `type` 分发：

- `ask` -> `_ask()`。
- `new_ask` -> 创建 Session 后执行 `_ask()`。
- `retry` -> 恢复 checkpoint 后执行 `_ask()`。
- `command` -> `_command()`。

所有异常最终会转换成 `error` 和 `run_finished` 事件，防止前端永远停留在运行中。

### 10.4 `_ask()`

普通请求的桥接流程：

```text
校验 text
  -> 校验并渲染 editor attachments
  -> 保存请求前 checkpoint
  -> 发送 run_started
  -> agent.ask(text, editor_context=...)
  -> 获取当轮 change_set
  -> 发送 run_finished(final, state, change_set)
```

附件被渲染为内部 `editor_context`，其中会注明它们是仓库证据，不是附加指令。

### 10.5 `_execute_command()`

Slash Command 在这里映射到 runtime 方法，例如：

```text
compact       -> agent.compact_history()
budget        -> agent.budget_report()
plan_enter    -> agent.enter_plan_mode()
review        -> 组织 review 请求后进入 _ask()
session_list  -> SessionStore.list_sessions()
change_undo   -> apply_whole_change_set(..., "undo")
```

### 10.6 `_state()`

每次重要事件会附带轻量状态：

- workspace。
- provider 和 model。
- 可选 provider/model。
- approval policy。
- workflow mode。
- 当前 Session。
- retry 是否可用。
- 最近 change sets。

Webview 不需要自己猜测后端状态，只需以最新的 `state` 更新界面。

### 10.7 `_display_history()`

模型 History 会被 compact，不能直接作为聊天界面的持久化数据源。每次可见消息还会
追加到独立的 `transcript.jsonl`；compact 只修改模型上下文，不修改 transcript。
Bridge 从 transcript 返回 UI 投影：

- 恢复 compact 前后的完整可见对话。
- 保留用户可见消息的完整文本；模型上下文限制不作用于 UI Transcript。
- 隐藏 `editor_context` 等内部 user context。
- 保留 tool call/result 配对和修改预览所需字段。
- 对 `write_file`/`patch_file` 只公开目标路径，不传输大段修改参数。

请求前 checkpoint 同时保存 transcript 的字节位置。重新执行最近请求时，runtime
恢复 session snapshot 并截断 transcript，因此侧边栏也会回到同一个请求边界。

---

## 11. Python `bridge/ui.py`：把 runtime UI 调用变成事件

CLI 使用 `TerminalUI` 把内容打印到终端；插件使用 `JsonUI` 把同样的语义转成 JSON
事件。

例如 runtime 调用：

```python
self.ui.commentary("我先检查入口。")
```

CLI 会打印文本，`JsonUI` 会输出：

```json
{"type":"commentary","text":"我先检查入口。"}
```

主要映射如下：

| runtime UI 方法 | Bridge 事件 | Webview 用途 |
|---|---|---|
| `model_start/end` | `model_status` | Thinking 状态 |
| `stream_start/delta/end` | 流式事件 | 增量 Markdown |
| `commentary` | `commentary` | 中间进展 |
| `final_answer` | `final` | 最终回答 |
| `tool_start/result` | 工具事件 | 折叠工具卡片 |
| `compact_start/end` | `compact_status` | 压缩状态和简短结果 |
| `review_start/end` | `review_status` | Review 状态 |
| `approval_request` | 审批事件 | Allow/Deny 按钮 |
| `request_user_input` | 问题事件 | 选项表单 |
| `plan_review` | Plan 审批事件 | Approve/Revise/Cancel |

`JsonUI` 不负责画页面，它只负责提供结构化事件。

---

## 12. `src/chatViewProvider.ts`：Extension Host 中的总桥梁

这个文件连接三部分：

```text
Python CodeMateProcess <-> ChatViewProvider <-> Webview chat.ts
                              |
                              +-> VS Code API
```

### 12.1 `resolveWebviewView()`

当侧边栏第一次显示时，该函数：

1. 设置 Webview 可以加载的本地资源目录。
2. 通过 `buildHtml()` 设置页面 HTML。
3. 监听后端事件并转发到 Webview。
4. 监听 Webview 消息并执行对应操作。
5. 监听 VS Code Diagnostics 变化。
6. 在 View 销毁时释放监听器。

### 12.2 `webview.html` 和 `chat.ts` 的关系

```ts
webviewView.webview.html = this.buildHtml(webviewView.webview);
```

这行把完整 HTML 文档交给 Webview 浏览器。HTML 最后包含：

```html
<script src=".../dist/webview.js"></script>
```

`dist/webview.js` 是 `webview/chat.ts` 编译后的结果。因此不是 Provider 把 HTML
“传给 chat.ts 处理”，而是：

1. Provider 创建浏览器页面。
2. 浏览器加载 HTML。
3. HTML 加载编译后的 `chat.ts`。
4. `chat.ts` 获取 DOM 元素并控制页面交互。

### 12.3 Webview -> Provider

Webview 中的：

```ts
vscode.postMessage({ type: 'sendMessage', text: '你好' });
```

会触发 Provider 注册的：

```ts
webview.webview.onDidReceiveMessage((message) => { ... });
```

这是 VS Code 为当前 Webview 自动建立的消息通道，不是普通网页的网络请求。

### 12.4 Provider -> Webview

Provider 使用：

```ts
webview.postMessage({ type: 'backendEvent', event });
```

Webview 使用浏览器事件接收：

```ts
window.addEventListener('message', (event) => { ... });
```

Provider 不直接绘制最终文本。页面 DOM 存在于 Webview 中，所以最终仍由 `chat.ts`
接收消息并创建 HTML 元素。

### 12.5 为什么编辑器附件必须在这里读取

`@selection`、`@file` 和 `@problems` 都依赖 VS Code API：

- `selectionAttachment()` 读取当前 `activeTextEditor.selection`。
- `fileAttachment()` 使用 `showOpenDialog()` 和 `workspace.fs.readFile()`。
- `problemsAttachment()` 使用 `languages.getDiagnostics()`。

Webview 只负责发起请求和显示附件标签，实际数据必须由 Provider 读取。

附件限制：

- 单个最多 30,000 字符。
- 文件必须位于工作区。
- 二进制文件拒绝作为文本附件。
- Problems 只取当前文件的 error/warning，最多 20 条。

### 12.6 变更集为什么要在 Provider 中缓存

Python 返回的变更集包含 before/after 快照路径。Webview 不应该知道宿主文件系统中的
这些绝对路径，因此 Provider 做两件事：

1. 把完整变更集交给 `ChangeDocumentProvider` 保存。
2. 使用 `forWebview()` 删除快照路径，只发送 ID、状态、文件名等展示字段。

这是一个典型的权限边界：页面只表达“打开某个 change set 的某个文件”，宿主决定
这个 ID 实际对应哪个文件。

### 12.7 Diagnostics

Diagnostics 现在有两条用途不同的链路。

第一条位于工具执行过程中。`write_file` 或 `patch_file` 首次修改某个文件前，Python
通过 Bridge 请求该文件的 Error 基线；修改成功后再次请求，并等待语言服务器刷新。
Extension Host 使用 `vscode.languages.getDiagnostics(uri)` 返回结构化结果，Runtime
只把相对基线新增的 Error 追加到工具结果，使 Agent 下一次模型调用可以立即发现并
修正静态错误。原有 Error、Warning、超时和不可用状态不会改变写工具的成功结果。

第二条位于一轮结束后，用于用户界面。当一轮结束或 VS Code Diagnostics 变化时，
Provider：

1. 找到本轮变更文件。
2. 调用 `vscode.languages.getDiagnostics(uri)`。
3. 只取 error 和 warning。
4. 把最多 50 条结果发给 Webview。

点击诊断后，`openLocation()` 使用 VS Code API 打开文件并定位行号。

即时诊断请求由 Extension Host 直接处理并通过 `interaction_response` 返回 Python，
不会经过 Webview，也不会弹出审批或选择界面。

### 12.8 CSP 和 nonce

Webview HTML 使用 Content Security Policy，默认禁止任意资源和脚本。脚本标签带有随机
`nonce`，只有本次 HTML 中明确允许的脚本能执行。

这能减少用户内容或 Markdown 中的恶意脚本在 Webview 中运行的风险。

---

## 13. `src/changeDocumentProvider.ts`：原生 Diff 的数据源

VS Code 的原生 Diff 命令需要两个 URI：左侧文档和右侧文档。但 before/after 快照位于
Run 目录，不应该当普通工作区文件打开，因此实现了虚拟文档 Provider。

### 13.1 注册自定义 URI Scheme

```ts
vscode.workspace.registerTextDocumentContentProvider(
  'codemate-change',
  this,
)
```

之后 VS Code 遇到：

```text
codemate-change:/<change-set-id>/before/src/app.py
```

会调用 `provideTextDocumentContent()` 获取文本。

### 13.2 `remember()`

它接收完整变更集，把：

```text
changeSetId + 相对文件路径
```

映射到 before/after 快照路径。这个映射只存在 Extension Host 内存中。

### 13.3 `openDiff()`

流程：

```text
根据 changeSetId 和 filePath 找 ChangeFile
  -> 创建 before 虚拟 URI
  -> 创建 after 虚拟 URI
  -> vscode.commands.executeCommand('vscode.diff', before, after)
```

这样打开的是 VS Code 自带 Diff 编辑器，不需要在 Webview 中重新实现代码高亮、滚动
同步和差异标记。

### 13.4 添加和删除文件

- 新增文件的 before 快照为空，左侧显示空文档。
- 删除文件的 after 快照为空，右侧显示空文档。
- 二进制快照不作为文本 Diff 展示。

### 13.5 Python 侧的变更追踪文件

`ChangeDocumentProvider` 只负责“读取已经生成的快照并打开 Diff”，快照本身由 Python
侧生成。

#### `codemate/runtime/changes.py`

`ChangeTrackingMixin` 把变更追踪接入一次 Agent run 的生命周期：

```text
begin_change_tracking(task_state)
  -> 请求开始时创建 ChangeSetTracker

finish_change_tracking()
  -> 请求成功、失败或取消后比较工作区
  -> 把轻量 change set 索引写入 Session
```

Session 只保存 change set ID、conversation ID、文件路径和状态，不保存文件正文或快照
绝对路径。真正的快照放在对应 Run 目录中，避免 `session.json` 持续膨胀。

#### `codemate/storage/change_sets.py`

`ChangeSetTracker` 负责工作区基线、文件 hash 和 before/after 快照：

- Git 仓库请求开始时记录 HEAD 和已有未提交文件，使用 Git 对象作为未修改文件的
  before 基线，避免复制整个仓库。
- 非 Git 工作区以及尚无 commit 的新仓库使用文件系统基线：扫描普通工作区文件并
  保存 before 快照，请求结束后再次扫描以识别新增、修改和删除。
- Git 模式请求结束时重新读取 Git 状态并比较内容；文件系统模式则重新扫描文件清单。
- 用户请求前已经存在的脏文件以当时内容为 before，而不是使用 HEAD 内容。
- 单文件快照上限为 10 MB，超限或非普通文件会标为不可安全恢复。
- 文件系统基线跳过 `.git`、虚拟环境、`node_modules` 和常见缓存目录，并限制单轮
  before/after 快照总量，避免大型依赖目录拖慢每次请求。

Change set 状态：

| 状态 | 含义 |
|---|---|
| `applied` | 当前全部文件仍匹配本轮 after |
| `reverted` | 当前全部文件匹配本轮 before |
| `conflict` | 文件已被再次修改或处于混合状态 |
| `unavailable` | 快照不完整、文件不支持或 HEAD 在运行中改变 |

`apply_change_set()` 执行整轮 Undo/Redo。它先对所有文件进行 hash 预检，全部满足后才
开始恢复；单个文件使用临时文件加 `os.replace()` 原子替换。如果中途失败，会尝试把
已经处理的文件回滚到操作前状态。

---

## 14. `webview/chat.ts`：页面状态和渲染

这是最大的文件，但它本质上由若干独立功能区组成。阅读时按下面顺序，而不是从第一行
一路读到最后。

### 14.1 基础类型和全局状态

重要类型：

- `BackendEvent`：后端或宿主发来的事件。
- `TurnView`：一轮对话在 DOM 中对应的元素引用。
- `StreamView`：一个流式响应当前累计的文本和目标节点。
- `EditorAttachment`：输入框中尚未发送的编辑器上下文。
- `CompactView`：当前压缩状态块。

重要状态：

- `state`：后端最新运行状态。
- `running`：是否正在处理请求。
- `activeTurn`：当前尚未完成的一轮。
- `turns`：conversation/turn 对应的 UI。
- `streams`：流 ID 对应的增量渲染状态。
- `tools`：tool ID 对应的折叠节点。
- `attachments`：下一次请求携带的附件。

### 14.2 `requiredElement()`

它根据 ID 获取 DOM 元素。如果 HTML 中缺少对应节点就立即报错，避免后续出现更难排查
的 `undefined` 错误。

### 14.3 Markdown 渲染

```ts
marked.parse(text)
DOMPurify.sanitize(html)
```

`marked` 把 Markdown 转成 HTML，`DOMPurify` 清理危险标签和属性。不能直接把模型输出
赋给 `innerHTML`，否则模型或工具结果中的 HTML 可能执行脚本。

### 14.4 页面与运行状态

- `setPage()`：切换 Session 首页和聊天页。
- `setRunning()`：禁用输入、显示 Stop、禁止切换 Session。
- `updateState()`：同步模型、审批、Session 标题和 workflow mode。
- `updateRuntimeControls()`：重绘 Provider/Model/Approval 按钮。

### 14.5 会话首页

- `renderSessions()`：显示项目下的 Session。
- `openRenameDialog()`：修改 Session title。
- 点击 Session 后发送 `openSession`。
- 首页输入新任务后发送 `newTask`。

### 14.6 一轮对话的 DOM 结构

`createTurn()` 创建：

```text
turn
├── user message
├── details: Process
│   ├── commentary
│   ├── tool calls
│   ├── approval
│   └── thinking
├── final answer
└── Changes panel
```

任务运行时 Process 展开；结束后自动折叠，只保留 Final Answer 作为主要内容。

### 14.7 工具调用

`appendTool()` 根据 `tool_id` 创建折叠 `<details>`。`appendToolResult()` 使用相同 ID
找到节点，再添加状态和结果。

这种映射避免并行工具调用时把结果放到错误的工具卡片中。

### 14.8 流式输出

事件顺序通常是：

```text
stream_start
text_delta
text_delta
...
stream_end
```

`handleTextDelta()` 累加文本，`requestAnimationFrame()` 控制 Markdown 重绘频率。
如果每个 token 都立即完整解析 Markdown，页面会频繁布局并变慢。

### 14.9 `handleBackendEvent()`

这是 Webview 的事件总路由：

```text
run_started       -> 创建 Turn 和 Thinking
commentary        -> 添加 Process 文本
tool_start/result -> 更新工具节点
final             -> 渲染最终答案
run_finished      -> 结束并折叠 Process
approval_request  -> 显示审批按钮
compact_status    -> 显示压缩过程和简短结果
command_result    -> 处理 Slash Command
```

它和 Python 的 `BridgeServer._dispatch()` 类似，都是根据 `type` 做路由，但职责不同：

- Bridge 路由“要执行什么后端操作”。
- Webview 路由“要如何改变页面”。

### 14.10 交互式审批

`appendApproval()` 根据后端给出的 options 创建按钮。用户点击后发送：

```ts
{
  type: 'interactionResponse',
  interactionId,
  value: option.value,
}
```

`appendQuestions()` 和 `appendPlanReview()` 使用相同思路，只是表单结构不同。

### 14.11 Slash Command 和 `/compact`

`handleCommandResult()` 处理不进入 Agent loop 的命令结果。

`/compact` 的 UI 会先创建：

```text
Compacting history...
```

收到 `compact_status` 或 `command_result` 后更新同一节点：

```text
-> history compacted: 120 -> 24 messages, summary 3821 chars
```

这样既不会展示完整结果 JSON，也不会因为 runtime 事件和命令结果各返回一次而重复。

### 14.12 输入补全与附件

输入 `/` 时，`updateCommandMenu()` 显示 Slash Command 候选。

输入以下内容时会调用 Extension Host：

- `@selection`：当前选区。
- `@file`：选择文件。
- `@problems`：当前文件诊断。

附件返回后显示为可删除 chip；发送请求时作为 `attachments` 字段一并提交，然后清空
输入区附件状态。

### 14.13 Changes、Undo 和 Redo

每次成功的 `write_file` 或 `patch_file` 都会在对应工具卡片中附带紧凑修改预览：

- 折叠标题显示文件路径和 `+/-` 行数。
- 展开后使用自绘的行级 Diff 卡片显示行号、增删背景和最多 24 行预览。
- 大文件、二进制文件或读取失败时只显示无法预览的原因，不影响工具实际执行。
- 预览作为 UI metadata 保存，不会混入模型读取的工具结果正文。

单次工具预览用于解释“这一次工具调用改了什么”，不负责整轮撤销和完整 Diff。

`renderChangeSet()` 展示当轮文件列表：

- `A`：added。
- `M`：modified。
- `D`：deleted。
- `+N -N`：从本轮第一次修改前到最终结果的净增删行数。

点击文件发送 `openDiff`，由 Extension Host 打开原生 Diff。

Undo/Redo 只支持整轮操作。Python 后端会先验证全部文件 hash：

- 所有文件仍匹配 after 才能 Undo。
- 所有文件仍匹配 before 才能 Redo。
- 任意文件之后被修改则整个操作拒绝，不做部分恢复。

### 14.14 最后的事件监听

文件末尾注册 DOM 事件，例如：

- form submit -> `sendMessage()`。
- Enter -> 发送，Shift+Enter -> 换行。
- Stop -> `cancel`。
- Back -> Session 首页。
- `window.message` -> 接收 Provider 消息。

这些监听注册完成后，Webview 发送 `ready`，通知 Provider 可以启动或恢复后端。

---

## 15. `media/chat.css`：布局和 VS Code 主题

CSS 主要使用 VS Code 提供的变量，而不是写死颜色：

```css
color: var(--vscode-foreground);
background: var(--vscode-sideBar-background);
border-color: var(--vscode-widget-border);
```

这样用户切换浅色、深色或高对比主题时，插件会自动适配。

主要布局：

- `.app-shell`：顶部状态栏和主体。
- `.home-page`：Session 列表与新任务输入。
- `.chat-page`：标题、可滚动消息区和固定输入区。
- `.turn-process`：可折叠执行过程。
- `.tool-event`：工具卡片。
- `.runtime-menu`：Provider/Model/Approval 菜单。
- `.attachment-*`：附件菜单和标签。
- `.changes-*`：Diff 文件与 Diagnostics。

遇到滚动问题时，重点检查 CSS Grid 子项是否设置了 `min-height: 0`，以及真正需要滚动
的元素是否使用 `overflow-y: auto`。Grid/Flex 子项默认最小尺寸可能阻止它缩小，从而
导致整个页面滚动错位。

---

## 16. 一次普通请求的完整流动

以用户输入“检查权限系统”为例：

```text
1. chat.ts
   sendMessage()
   vscode.postMessage({type: "sendMessage", text: "检查权限系统"})

2. chatViewProvider.ts
   onDidReceiveMessage()
   backend.sendInput(text)

3. codemateProcess.ts
   beginRequest() 创建 request_id
   sendRaw({type: "ask", id, text})
   写入 Python stdin

4. bridge/protocol.py
   parse_message() 解析 JSON

5. bridge/server.py
   _read_commands() 放入队列
   _dispatch() -> _ask()
   writer.emit("run_started")
   agent.ask(text)

6. CodeMate runtime
   模型返回 commentary / tool calls / final
   调用 JsonUI 对应方法

7. bridge/ui.py
   输出 commentary、tool_start、tool_result 等 JSONL

8. codemateProcess.ts
   stdout line -> handleLine()
   eventEmitter.fire(event)

9. chatViewProvider.ts
   backend.onEvent()
   webview.postMessage({type: "backendEvent", event})

10. chat.ts
    window.message
    handleBackendEvent()
    修改 DOM
```

这里经历两套不同的消息机制：

- Webview 与 Extension Host：VS Code `postMessage`。
- Extension Host 与 Python：子进程 stdin/stdout JSONL。

---

## 17. 一次审批的完整流动

```text
Agent 准备执行 run_shell
  -> JsonUI.approval_request()
  -> InteractionBroker 创建 interaction_id 并阻塞
  -> approval_request JSONL
  -> CodeMateProcess
  -> ChatViewProvider
  -> chat.ts 显示按钮
  -> 用户点击 Allow once
  -> interactionResponse
  -> ChatViewProvider.backend.respond()
  -> Python stdin
  -> Bridge 读线程 deliver()
  -> Queue 返回选择
  -> Agent 继续执行 run_shell
```

理解这条流程后，Plan 审批和 `request_user_input` 就不再是新的架构，只是不同的 UI。

---

## 18. Diff 和整轮撤销的完整流动

CodeMate 同时维护两种粒度的 Diff：

- `change_preview.py` 在每次 `write_file`/`patch_file` 前后比较目标文件，立即生成有界预览。
- `ChangeSetTracker` 只汇总本轮被修改工具触碰的路径，生成最终文件列表、完整快照和 Undo/Redo 数据。

前者服务运行过程展示，后者作为整轮变化的权威结果；两者不能互相替代。

### 18.1 请求前

`ChangeSetTracker.begin()` 不扫描 Git，也不遍历工作区。每个 `write_file` 或
`patch_file` 真正执行前，runtime 调用 `track_path()`，只在该路径首次被修改时保存
before 快照。后续再次修改同一文件不会覆盖基线。

因此普通目录和 Git 仓库使用同一套逻辑，成本只与修改工具实际触碰的文件有关。
只由 `run_shell` 修改、且没有被修改工具登记的文件不会进入 Changes。

### 18.2 请求后

`finish()` 只读取已登记路径的最终状态，省略最终内容与 before 相同的净零变化，然后
保存 after 快照和 manifest。Bridge 将变更集随 `run_finished` 返回。

### 18.3 查看 Diff

```text
chat.ts: openDiff(changeSetId, path)
  -> ChatViewProvider
  -> ChangeDocumentProvider.openDiff()
  -> vscode.diff
```

### 18.4 Undo/Redo

```text
chat.ts: changeAction
  -> backend command change_undo/change_redo
  -> BridgeServer
  -> agent.apply_whole_change_set()
  -> 校验全部文件 hash
  -> 预检后逐文件原子替换或删除，失败时尝试回滚
  -> 返回新的 applied/reverted/conflict 状态
```

这里的安全原则是：宁可拒绝，也不能覆盖用户在任务结束后的手动修改。

---

## 19. 初学者需要补充的 TypeScript 和 VS Code 知识

### 19.1 TypeScript

优先掌握：

- `interface` 和可选字段 `field?: string`。
- `unknown`、`any` 和类型守卫的区别。
- `Record<string, unknown>`。
- 泛型，例如 `get<string>()`、`requiredElement<T>()`。
- `async/await` 与 `Promise`。
- 可选链 `value?.field` 和空值合并。
- `Map`、`Set` 和数组的 `map/filter/find`。
- 类构造参数属性，例如 `private readonly backend: CodeMateProcess`。

不需要先深入类型体操或高级泛型。

### 19.2 Node.js

需要了解：

- `child_process.spawn()`。
- stdin/stdout/stderr。
- 流不保证一次 data 事件对应一条完整消息。
- `readline` 如何按行读取流。
- 环境变量和 PATH。
- `fs.promises.readFile()`。

### 19.3 VS Code API

需要了解：

- `ExtensionContext` 和 `subscriptions`。
- `WebviewViewProvider`。
- `postMessage` / `onDidReceiveMessage`。
- `EventEmitter`。
- `Disposable`。
- `workspace.getConfiguration()`。
- `window.activeTextEditor`。
- `languages.getDiagnostics()`。
- `TextDocumentContentProvider`。
- `commands.executeCommand('vscode.diff', ...)`。

### 19.4 浏览器 DOM

需要了解：

- `document.createElement()`。
- `append()`、`replaceChildren()`、`remove()`。
- `addEventListener()`。
- `dataset`。
- `<details>/<summary>`。
- form submit 与 `preventDefault()`。

---

## 20. 调试与验证方法

### 20.1 编译检查

```bash
cd vscode-extension
npm run check
npm run compile
```

`check` 发现类型问题，`compile` 同时生成 Extension Host 和 Webview JavaScript。

### 20.2 启动 Extension Development Host

1. 用 VS Code 打开 CodeMate 仓库根目录。
2. 按 `F5`。
3. 在新窗口打开一个测试项目。
4. 点击 Activity Bar 中的 CodeMate。

### 20.3 查看 Extension Host 日志

插件后端启动和 stderr 写在 Output -> CodeMate。

### 20.4 查看 Webview 日志

使用 VS Code 命令 `Developer: Open Webview Developer Tools`，可以查看：

- `console.log()`。
- DOM 结构。
- CSS 实际计算值。
- Webview JavaScript 异常。

### 20.5 查看 Python Bridge

Bridge 本身可以在终端启动并手工发送一行 JSON，但正常调试更适合结合：

- CodeMate Output Channel。
- Session 下的 `trace.jsonl`。
- `tests/bridge/`。

---

## 21. 建议的分阶段学习练习

### 阶段一：只理解启动

阅读：

- `package.json`
- `extension.ts`
- `ChatViewProvider.buildHtml()`

目标：能回答“点击 CodeMate 图标后，HTML 是谁创建的”。

### 阶段二：理解一条消息

阅读：

- `chat.ts: sendMessage()`
- `chatViewProvider.ts: onDidReceiveMessage`
- `codemateProcess.ts: sendInput()/sendRaw()`
- `BridgeServer._ask()`

目标：能画出 user request 从输入框到 `agent.ask()` 的路径。

### 阶段三：理解返回结果

阅读：

- `JsonUI.commentary()/tool_start()/final_answer()`
- `CodeMateProcess.handleLine()`
- `ChatViewProvider.backend.onEvent()`
- `chat.ts: handleBackendEvent()`

目标：能解释为什么 Provider 不负责最终文本的 DOM 展示。

### 阶段四：理解阻塞交互

阅读：

- `InteractionBroker`
- `JsonUI.approval_request()`
- `chat.ts: appendApproval()`

目标：能解释 Agent 等待审批时为什么 Bridge 仍能收到用户选择。

### 阶段五：理解 VS Code 专属能力

阅读：

- `attachEditorContext()`
- `publishDiagnostics()`
- `ChangeDocumentProvider`

目标：能解释哪些功能不能直接在 Webview 或 Python runtime 中实现。

---

## 22. 当前结构的复杂度来源与后续阅读建议

当前复杂度主要集中在 `webview/chat.ts`，因为它同时维护：

- Session 首页。
- 对话页面。
- 流式 Markdown。
- 工具调用。
- 审批和 Plan 表单。
- Slash Command 补全。
- 编辑器附件。
- Changes、Diagnostics、Undo/Redo。

这不等于所有代码必须同时理解。学习时应把它看成若干 UI 模块共用一个状态和事件入口。

如果未来继续扩展，可以在功能稳定后按职责拆分为：

```text
webview/
├── state.ts
├── transport.ts
├── sessions.ts
├── turns.ts
├── interactions.ts
├── attachments.ts
├── changes.ts
└── chat.ts
```

但拆分只是降低文件级认知负担，不会消除三层运行环境和异步消息流本身的复杂度。
在没有理解当前数据流前直接重构，反而容易破坏事件状态和工具结果配对。

学习这套代码时，最有效的方法不是记住每个函数，而是始终追踪三个问题：

1. 这段代码运行在哪个环境？
2. 它收到的输入事件从哪里来？
3. 它产生的结果交给谁继续处理？

只要这三个问题清楚，后续增加一个按钮、一个 Bridge 命令或一种 Agent 事件，都可以放回
同一套架构中理解。
