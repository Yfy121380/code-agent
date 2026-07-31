# Runtime Loop 模块笔记

## 1. 模块定位

Runtime 是 Codemate 的运行中枢，负责把用户的一次请求推进成完整的 Agent 工作过程。

模型本身只负责给出下一步意图：

- 输出用户可见的阶段性进展。
- 请求调用一个或多个工具。
- 返回最终回答。

Runtime 则负责控制这些意图如何落地，包括：

- 构建模型上下文并调用模型。
- 识别 commentary、tool calls 和 final。
- 校验、审批并执行工具。
- 将模型消息和工具结果写回 history。
- 更新任务状态、工作记忆和 token usage。
- 判断任务是否继续、压缩上下文或结束。

整体循环可以概括为：

```text
用户请求
    ↓
构建最新上下文
    ↓
模型决定下一步
    ↓
展示 commentary / 执行工具 / 返回 final
    ↓
将新观察写回上下文
    ↓
继续下一轮，直到任务完成
```

核心设计思想是：**模型负责决策，Runtime 负责控制。** 模型可以提出工具调用，但不能绕过参数校验、权限审批、沙箱和运行状态管理。

## 2. Agent 初始化

Runtime 创建 Agent 时，主要完成以下初始化工作。

### 2.1 加载运行配置

保存模型客户端、工作区、审批策略、最大输出 token、运行模式、时区、UI、功能开关和子 Agent 深度等配置。

不同运行模式可以使用不同能力：

- 主 Agent 使用完整工具集合。
- Delegate 使用只读权限和工具白名单。
- Review 串行启动一个只读审查子 Agent，使用独立 prompt 和工具白名单。
- Dream 只允许长期记忆整理需要的工具。

### 2.2 初始化路径和 Settings

解析项目目录、用户目录以及项目状态目录，自动创建缺失的基础结构，并加载用户级和项目级 settings。

配置中包括：

- MCP server。
- 读写 allow/deny 规则。
- 用户级和项目级 skills。
- 当前项目的 session 和 memory 位置。

后续模块统一使用解析后的绝对路径，不再各自拼接目录。

### 2.3 创建或恢复 Session

新会话会创建初始 session，恢复会话则加载已有状态。Session 中主要保存：

- history 和 history summary。
- 已读文件版本状态。
- todos。
- 最近调用的 skills。
- temporary permissions。
- 会话标题和更新时间。

临时权限也会随 session 保存，因此恢复会话后，之前选择的“本会话允许”仍然有效。

### 2.4 聚合权限规则

Runtime 将默认规则、用户 settings、项目 settings、当前审批模式和 session 临时权限合并为当前实际使用的权限规则。

这份规则同时用于普通文件工具、Shell 路径审批和沙箱。只有权限发生变化时才重新聚合，不会在每次工具调用时重复读取配置。

### 2.5 初始化运行组件

最后创建：

- 工具注册表和模型可见的 tool schemas。
- Prefix。
- Context Manager。
- RunStore 和当前运行状态。
- Token usage、最近工具结果等临时状态。

初始化完成后，Agent 已经具备构建上下文、请求模型、执行工具和保存会话的完整能力。

## 3. ask() 主循环

`ask()` 表示让 Agent 完整处理一次用户请求。它不是单次模型调用，而是持续执行“决策—行动—观察”的循环。

### 3.1 创建本次 Run

收到用户请求后，Runtime 会：

1. 将 user message 写入 history。
2. 创建 TaskState、task ID 和 run ID。
3. 写入 run 开始事件。
4. 为当前请求召回一次长期记忆。

长期记忆只在请求开始时召回一次，后续工具循环直接复用，避免每轮都额外调用模型。

### 3.2 构建模型输入

每轮模型请求前都会重新构建上下文：

```text
prefix
available skills
runtime context
relevant memory
history summary
recent history
```

之所以每轮重新构建，是因为 history 和工具结果会在上一轮发生变化；Todo 和完整 Skill 指令通过工具消息留在 history，需要时可由只读工具或 compact 恢复。

构建完成后，Runtime 根据最近一次模型 usage 和新增工具结果的 token 估算检查上下文预算。

如果达到阈值：

1. 压缩较旧的 history。
2. 保留最新消息。
3. 保存结构化 history summary。
4. 重新构建模型输入。

如果压缩多次失败，则停止当前请求，避免继续发送明显超出预算的上下文。

### 3.3 调用模型

Runtime 将 system、messages、工具 schemas 和输出 token 限制交给 Model Client。

模型返回后，Runtime 会：

- 更新 input/output/cache token usage。
- 记录模型耗时和响应类型。
- 将 Provider 差异屏蔽在 Model Client 内部。
- 根据统一的 `kind` 进入对应分支。

Runtime 只需要处理三种响应：

```text
commentary
tool_calls
final
```

### 3.4 Commentary 分支

Commentary 是用户可见的阶段进展，不代表任务结束。

Runtime 会：

1. 在终端展示 commentary。
2. 保存为 assistant commentary 消息。
3. 继续下一轮模型请求。

这样复杂调查中的关键发现能够及时进入 history，而不会只存在于容易被清理的旧工具结果里。

### 3.5 Tool Calls 分支

模型可以一次返回多个工具调用，并同时附带 commentary。

Runtime 会先将这一轮的 commentary 和所有工具调用保存为一条 assistant 消息，然后逐个处理工具：

```text
assistant:
  commentary
  tool_call 1
  tool_call 2

tool:
  result 1

tool:
  result 2
```

同一轮工具调用不能拆成多个 assistant 消息，否则会破坏工具调用与结果的对应关系，也会影响 Anthropic 多工具结果的格式转换。

每个工具执行后，Runtime 会：

- 更新工具步数和最近工具。
- 展示工具结果摘要。
- 将完整结果写入 history。
- 更新工具结果 token 估算。
- `read_file` 或文件修改成功后更新文件版本状态。
- 写入工具执行 trace。

所有工具完成后重新构建上下文，让模型根据新观察决定下一步。

### 3.6 Final 分支

收到 final 后，Runtime 会：

1. 保存 assistant final 消息。
2. 将 TaskState 标记为完成。
3. 写入 run 结束事件。
4. 展示最终回答。
5. 判断是否需要后台 Dream。
6. 在首轮结束后尝试生成 session title。
7. 结束本次 `ask()`。

主 Agent 不设置固定工具步数上限，避免大型任务在即将完成时被强制停止。Delegate 和 Dream 等目标明确的子流程仍然保留步数限制。

## 4. 工具校验与执行流程

模型返回工具调用只代表执行意图。真实工具必须经过 Runtime 的统一执行闸口：

```text
检查工具是否存在
    ↓
校验参数和工具语义
    ↓
解析路径 / 分析 Shell 命令
    ↓
生成 allow / ask / deny gate
    ↓
应用 Runtime 额外约束
    ↓
拦截连续重复调用
    ↓
必要时请求用户审批
    ↓
执行真实工具
    ↓
分类结果并更新状态
```

### 4.1 检查工具和参数

首先确认工具存在于当前 Agent 的注册表中，然后执行对应的参数校验，例如：

- 文件路径、行号范围和写入模式是否合法。
- patch 的 `old_text` 是否精确出现一次。
- grep 的模式和上下文行数是否合法。
- todo 的 phase/task 状态是否一致。
- Web URL 是否为公开 HTTP/HTTPS 地址。
- Delegate 的任务数量和最大步数是否在限制内。
- Skill 是否存在、是否已经加载。

参数不合法会直接返回错误，不会进入审批。

### 4.2 路径标准化

文件工具涉及的路径会先转换为真实绝对路径：

- 相对路径以工作区为基准。
- 展开 `~`。
- 处理 `..`。
- 解析符号链接。
- 判断目标是文件还是目录。
- 判断命中了哪些 allow/deny 规则。

权限审批和实际执行使用同一个规范路径，避免路径穿越和符号链接造成判断目标与真实目标不一致。

### 4.3 Shell 命令分析

Shell 工具会先分析命令：

- 识别命令主体、管道和重定向。
- 提取读写路径。
- 将命令分类为 read、modify、dangerous 或 unknown。
- 检查危险目标和修改类通配符。

明确危险的目标或不可控写通配符会直接拒绝。其余命令根据命令类别和路径权限生成 gate。

静态分析负责判断执行意图，沙箱负责限制真实系统调用。即使命令通过审批，运行时也不能突破沙箱最终允许的读写范围。

### 4.4 权限 Gate

工具校验最终返回三种结果：

- `allow`：直接执行。
- `ask`：请求用户审批。
- `deny`：直接拒绝。

`deny` 在校验阶段结束，不会显示审批菜单；只有 `ask` 才进入用户审批。

审批时可以选择：

- Allow once。
- Allow read/write for a specific directory this session。
- Deny。

选择本会话允许后，Runtime 会保存临时目录权限或 Shell 命令主体。目录变化会触发路径规则重新聚合；Shell 主体只影响后续命令风险审批。

### 4.5 Runtime 额外约束

权限 gate 通过后，还要检查 Agent 工作流约束。

#### Read-only 模式

`read_only` 模式会拒绝真正修改外部状态的工具，但允许读工具、读类 Shell、Web、todo 和 skill 操作。

#### 编辑前读取

修改已有文件前必须使用 `read_file` 读取该文件。grep 和 list_files 不能代替完整读取。

以下情况不要求提前读取：

- 创建不存在的新文件。
- 使用 append 模式追加内容。

文件修改后旧摘要会失效，再次修改前需要重新读取。

#### Delegate 深度

Delegate 必须满足最大子 Agent 深度，避免递归委派失控。

### 4.6 重复调用拦截

如果模型连续多次使用完全相同的工具名称和参数，Runtime 会拒绝第三次相同调用，并要求模型：

- 修改参数。
- 更换工具。
- 或返回最终回答。

这种拦截可以防止模型在没有新观察时陷入重复读取循环。

### 4.7 执行和结果处理

校验、权限和审批全部通过后，Runtime 才调用真实工具。

执行结果统一转为文本，并标记为：

- `ok`
- `error`
- `rejected`
- 部分成功或子任务失败

工具失败通常不会让 Agent 进程直接崩溃，而是作为 tool result 返回模型。模型可以根据错误调整参数、选择其他方案或结束任务。

工具执行后还会更新：

- 最近访问文件和文件摘要。
- 已失效的文件摘要。
- 工具错误过程笔记。
- 工具执行 metadata。
- TaskState 和 trace。

## 5. 其他配套机制

Runtime 还负责少量跨模块协调：

- Session 保存可恢复的对话、记忆、todo、skills 和临时权限。
- TaskState 记录单次请求的状态、模型请求次数、工具步数和停止原因。
- Trace 保存运行中的关键事件，并在落盘前脱敏 secret。
- History compact 负责长任务的上下文续航。
- Dream 在任务结束后按条件后台整理长期记忆。
- Delegate 使用独立只读子 Agent 隔离大规模调查上下文。
- Runtime 关闭时统一释放 MCP 等长期连接。

这些能力都围绕同一个目标：让主循环能够长期、可控、可恢复地运行。

## 6. 设计难点

### 模型行为不稳定

模型可能参数错误、重复调用、完成后不收尾或返回空响应。Runtime 通过参数校验、重复调用拦截、过程笔记和明确的响应分支降低这些问题。

### 工具调用必须安全但不能中断推理

工具失败和审批拒绝需要阻止真实动作，但仍要把错误返回模型，使 Agent 可以继续调整，而不是整个任务直接崩溃。

### 长任务需要持续运行

主 Agent 取消固定步数上限，并通过 token 预算和 history compact 控制上下文；受控子流程则使用工具白名单和步数上限控制成本。

### 多 Provider 响应必须统一

OpenAI 和 Anthropic 的原生消息格式不同，但 Runtime 只处理 commentary、tool calls 和 final。Provider 差异全部由 Model Client 消化，主循环保持稳定。

## 7. 面试表达

> Runtime 是 coding agent 的控制中枢。模型负责决定下一步是输出 commentary、调用工具还是返回 final，Runtime 负责构建最新上下文、调用模型、维护 session 和任务状态，并将工具调用变成受控执行。每个工具都要经过工具存在性检查、参数校验、路径标准化、Shell 风险分析、权限 gate、Runtime 工作流约束、重复调用拦截和用户审批。执行结果无论成功还是失败，都会作为新的观察写回 history，再由模型继续决策。主 Agent 不设置固定步数上限，而是通过上下文预算和 history compact 支持长任务；Delegate 和 Dream 等子流程则使用工具白名单和步数限制控制风险与成本。
