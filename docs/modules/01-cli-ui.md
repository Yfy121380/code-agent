# CLI/UI 模块笔记

CLI/UI 是 Codemate 的终端交互层，主要负责四件事：

- 启动和配置 agent。
- 提供交互式输入和 slash command。
- 展示模型 commentary、工具调用、工具结果和最终回答。
- 处理用户审批、session 选择等交互菜单。

这一层不负责模型推理，也不负责工具真实执行。它更像是 agent runtime 的“前端”：runtime 产生事件，CLI/UI 把事件显示成用户能看懂、能操作的终端界面。

## 1. 支持的命令

Codemate 在交互式 REPL 中支持一组 slash commands，用于查看状态、切换配置、管理上下文和会话。

### 基础命令

- `/help`：显示所有命令帮助。
- `/exit`：退出 agent。
- `/reset`：清空当前 session 的历史和记忆。

### 审批模式

- `/approval`：查看当前审批模式。
- `/approval ask`：切换到 ask 模式，风险操作需要询问。
- `/approval auto`：切换到 auto 模式，普通低风险操作自动通过。
- `/approval read_only`：切换到 read_only 模式，只允许读类和非修改操作。
- `/approval full`：切换到 full 模式，当前进程中审批默认通过，主要用于测试。

切换审批模式后，顶部 banner 会重新展示当前状态。

### 模型提供商和模型切换

- `/provider`：查看当前 provider 和可用 provider。
- `/provider openai`：切换到 OpenAI-compatible provider。
- `/provider anthropic`：切换到 Anthropic-compatible provider。
- `/provider deepseek`：切换到 DeepSeek provider。
- `/model`：查看当前模型和当前 provider 下可选模型。
- `/model <name>`：切换当前模型。

切换 provider 或 model 后，会重新构建 model client，重置 token usage，并刷新 prefix。顶部 banner 会同步展示新的 `provider:model`。

### 上下文和记忆

- `/budget`：查看各上下文 section 的字符数、工具 schema 字符数、估算 token、最大上下文和当前占比。
- `/compact`：手动压缩较早的历史上下文。
- `/memory`：查看当前工作记忆。
- `/remember <text>`：向当日 daily log 追加一条记忆候选。
- `/dream`：前台运行长期记忆整理。
- `/dream --background`：后台运行长期记忆整理。

这些命令主要用于调试和管理长任务。比如 `/budget` 可以观察上下文是否接近压缩阈值，`/compact` 可以手动触发 history summary，`/remember` 可以直接记录用户希望 agent 记住的信息。

### 会话管理

- `/session`：查看当前 session 信息和 session 文件路径。
- `/session list`：列出当前项目的所有 session。
- `/session rename <title>`：重命名当前 session。
- `/session resume`：弹出选择框，选择并恢复另一个 session。

启动时也支持 `codemate --resume`，不带具体 id 时会弹出 session 选择菜单。

## 2. prompt_toolkit 的作用

Codemate 使用 `prompt_toolkit` 主要是为了改善终端输入体验。普通 `input()` 很难满足交互式 agent 的需求，比如方向键、输入历史、命令补全、选择菜单都不好做。

### PromptSession

`PromptSession` 提供交互式输入框。Codemate 的 REPL 每轮通过它读取用户输入：

```text
codemate> 
```

它支持历史记录、补全、样式和按键绑定，是整个输入体验的基础。

### FileHistory

`FileHistory` 用于保存用户输入历史。Codemate 将输入历史保存在项目的 `.codemate/input_history` 中。这样用户下次进入同一项目时，可以继续使用历史输入。

### Completer / Completion

`Completer` 和 `Completion` 用于实现 slash command 补全。

当用户输入 `/` 开头的内容时，补全器会根据已有命令给出候选项，并显示命令说明。例如：

```text
/remember <text>    Append a memory entry to today's daily log.
/session resume     Choose and resume another session.
```

这里有一个比较重要的细节：**显示内容和实际插入内容可以分开**。

例如 `/remember` 的候选项可以显示为：

```text
/remember <text>
```

但实际插入到输入框的是：

```text
/remember 
```

这样用户选择候选项后，可以直接继续输入要记住的内容，而不会把 `<text>` 这个占位符插入进去。

### KeyBindings

`KeyBindings` 用于自定义按键行为。Codemate 主要用它处理补全菜单打开时的 Enter：

- 如果补全菜单打开，Enter 用于选择当前候选项。
- 如果没有补全菜单，Enter 才提交当前输入。

这样可以避免用户选择补全项时，命令被过早提交。

### Application

`Application` 用于实现更复杂的终端选择菜单，例如：

- 审批菜单。
- session resume 菜单。

这些菜单支持：

- 上下键移动。
- Enter 确认。
- Ctrl+C 取消。
- 选择完成后清除菜单显示。

审批和 session 选择都复用了同一个选择菜单逻辑，只是传入的选项和返回值不同。

## 3. rich 的作用

Codemate 使用 `rich` 主要是为了改善终端输出体验。Agent 会产生大量结构化事件，如果直接 `print()`，很容易变成难读的日志流。

### Console

`Console` 是 rich 的统一输出入口。TerminalUI 通过它打印 commentary、工具调用、工具结果、compact 状态和最终回答。

### Panel

`Panel` 用于把重要输出放进边框中，例如：

- 普通工具调用。
- 工具结果。
- 审批请求。
- 最终回答。

不同类型的内容使用不同颜色边框：

- 工具调用：通常是 cyan。
- 工具结果成功：green。
- 工具结果异常或审批：yellow。
- 最终回答：green。

### Syntax

`Syntax` 用于展示等宽文本，例如工具参数、shell 命令、patch 预览、工具结果摘要。它支持自动换行，比直接打印长字符串更适合展示结构化文本。

### Markdown

`Markdown` 用于渲染最终回答。这样模型返回的列表、代码块、标题等 Markdown 内容可以在终端中更清楚地展示。

## 4. 工具调用和结果展示方式

CLI/UI 的展示原则是：**终端展示摘要，完整内容保存在 history 和 trace 中**。

这样做的原因是工具结果可能很长。比如读取一个大文件、grep 很多匹配、运行 pytest、web research 都可能产生大量内容。如果终端全量展示，用户反而看不到重点。

### 读类工具

读类工具包括：

- `list_files`
- `read_file`
- `grep`
- `web_search`
- `web_extract`
- `web_research`

这些工具在开始调用时通常只显示一行简短信息，例如：

```text
◇ read_file README.md
◇ grep 'pattern'
◇ web_search 'latest Python release'
```

成功后只显示结果规模，例如：

```text
-> ok, 32 lines, 1200 chars
-> ok, 4 dirs, 12 files
-> ok, 5 results, 38 lines, 2400 chars
```

这样用户能知道工具成功了、读到了多少内容，但不会被完整内容刷屏。

### run_shell

`run_shell` 展示两部分：

1. 工具调用时展示命令：

```text
run_shell
  $ pytest -q
```

2. 结果展示时压缩 stdout/stderr：

- stdout 前 4 行。
- stdout 后 4 行。
- stderr 前 4 行。
- stderr 后 4 行。

如果中间有很多行，会显示 omitted 行数。完整 shell 输出仍然保存在工具结果、history 和 trace 中。

### write_file

`write_file` 展示：

- 目标路径。
- 写入模式，例如 overwrite 或 append。
- 写入内容大小。
- 前若干行内容预览。

这样用户在审批或观察工具调用时，可以快速判断 agent 打算写哪个文件、写多少内容、大概写了什么。

### patch_file

`patch_file` 展示：

- 目标路径。
- old_text 字符数和预览。
- new_text 字符数和预览。

它不会展示完整 patch 大段内容，而是展示能帮助用户判断修改意图的关键片段。

### todo_write

`todo_write` 会按 phase/task 结构展示任务计划，例如：

```text
todo_write
  1. [in_progress] 调查权限系统
     - [completed] 定位权限入口
     - [in_progress] 检查 shell 风险分类
  2. [pending] 汇总设计问题
```

这样用户能直接看到 agent 当前计划和进度。

### delegate

`delegate` 调用时展示子任务列表和 focus。结果返回时展示每个子任务的状态和结果字符数，而不是把所有子 agent 输出全部展开。

### approval

审批请求使用黄色面板展示工具调用摘要和风险信息。如果目标路径在 workspace 外，会额外提示：

```text
Warning: target path is outside the current workspace.
```

审批菜单支持：

- `Allow once`
- `Allow read for <dir> this session`
- `Allow write for <dir> this session`
- `Deny`

是否出现 session allow 选项，取决于 runtime 传来的工具权限 metadata。

### final answer

最终回答使用 Markdown + Panel 展示，标题为 `codemate`。这部分是用户真正的任务结果，而不是中间工具日志。

## 5. 小结

CLI/UI 模块的重点可以简单概括为：

- `prompt_toolkit` 负责输入体验：历史、补全、按键绑定、选择菜单。
- `rich` 负责输出体验：面板、Markdown、等宽文本和颜色区分。
- Slash commands 负责运行期控制：模型切换、审批切换、上下文查看、记忆整理、会话管理。
- 工具展示默认采用摘要策略：终端只看重点，完整信息交给 history 和 trace。

这部分不直接决定 agent 的智能程度，但会显著影响用户是否能理解和信任 agent 的工作过程。
