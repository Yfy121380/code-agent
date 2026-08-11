# Claude Code 与 Codex 默认提示词设计对比

本文对比 Claude Code 与 Codex 在**普通默认使用状态**下送给模型的提示词，重点解释下面这种使用感受从何而来：

- Claude Code 更容易指出用户方案的问题、澄清需求并与用户讨论实现路径。
- Codex 更容易接受任务目标，直接读取代码、修改文件并验证结果。

结论先行：这种差异确实有明显的提示词和工具设计依据，但不能完全归因于某一句 system prompt。两者的默认上下文都是由多层内容共同组成的，包括基础系统提示词、工具描述、模式附加提示、模型专属配置和项目级指令。模型训练与后训练风格也会放大或减弱这些倾向。

## 1. 对比范围

本文所说的“默认提示词”包括：

1. 启动普通会话时的基础 system/developer instructions。
2. 默认提供给模型的工具及其描述，因为工具描述同样属于模型上下文，会影响模型何时提问、规划或执行。
3. Codex 启动时自动附加的 `Collaboration Mode: Default` 提示。

本文不把以下内容当作默认提示词：

- 用户明确进入 Plan mode 后才注入的计划模式提示。
- `/plan` 等特定命令生成的临时提示。
- 用户自己的 `CLAUDE.md`、`AGENTS.md`、memory、skill、MCP 返回内容。
- 权限审批时临时生成的提示。

需要特别区分两件事：Claude Code 默认上下文中包含 `EnterPlanMode` 工具的描述，不等于当前已经处于 Plan mode。工具描述会先影响模型，使它倾向于建议进入 Plan mode；只有工具被调用并获批后，计划模式本身的附加提示才会生效。

## 2. 默认提示词的组成方式

| 层次 | Claude Code | Codex |
|---|---|---|
| 基础身份与工作规则 | `src/constants/prompts.ts` 动态拼装 system prompt | `codex-rs/models-manager/prompt.md`，也可由模型目录配置覆盖 |
| 默认工具行为 | `AskUserQuestion`、`EnterPlanMode` 等工具描述直接进入上下文 | 工具描述进入上下文，但 `request_user_input` 默认通常只允许在 Plan mode 使用 |
| 默认模式附加提示 | 没有与 Codex 完全对应的 Default mode 文件；行为主要由 system prompt 和工具描述共同决定 | 自动创建 `ModeKind::Default`，并附加 `templates/default.md` |
| 用户进入 Plan mode 后 | 额外注入只读探索、制定方案、等待批准等提示 | 切换为独立的 Plan collaboration mode |

这套结构意味着，比较单个 prompt 文件是不够的。尤其是 Claude Code，最强的“先讨论方案”信号位于工具描述中；Codex 最强的“先执行”信号则同时出现在基础提示词和 Default mode 附加提示中。

## 3. Claude Code 的默认提示词

### 3.1 基础身份

Claude Code 的基础身份在 [`src/constants/prompts.ts`](../claude-code-analysis/src/constants/prompts.ts#L175) 中生成：

> You are an interactive agent that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.

这里使用的是 “interactive agent”，但仅凭这一句还无法得出它更偏讨论的结论。更关键的是任务规则与默认工具描述。

### 3.2 默认仍然要求实际执行

同一文件的 `Doing tasks` 部分明确要求把普通指令理解为代码任务。例如用户要求把某个方法名改为 snake case 时，不应只回复转换后的名字，而应在代码库中找到它并完成修改：

> When given an unclear or generic instruction, consider it in the context of these software engineering tasks and the current working directory. [...] do not reply with just "method_name", instead find the method in the code and modify the code.

来源：[`src/constants/prompts.ts:221`](../claude-code-analysis/src/constants/prompts.ts#L221)

它还要求失败后先调查原因，只有真正调查到无法继续时才向用户升级问题：

> Escalate to the user with AskUserQuestion only when you're genuinely stuck after investigation, not as a first response to friction.

来源：[`src/constants/prompts.ts:233`](../claude-code-analysis/src/constants/prompts.ts#L233)

因此，Claude Code 的基础 system prompt 并不是“只讨论、不执行”。它同样把读取代码、直接修改和自主排查作为默认行为。

### 3.3 “指出用户问题”存在显式提示，但有构建分支限制

最接近用户观察的原文是：

> If you notice the user's request is based on a misconception, or spot a bug adjacent to what they asked about, say so. You're a collaborator, not just an executor—users benefit from your judgment, not just your compliance.

来源：[`src/constants/prompts.ts:224`](../claude-code-analysis/src/constants/prompts.ts#L224)

这句话会直接鼓励模型：

- 不要机械服从存在错误前提的需求。
- 主动指出相邻问题。
- 把自己的工程判断当作交付的一部分。

但源码显示它只在 `process.env.USER_TYPE === 'ant'` 时加入。文件注释还表明这是模型发布阶段的 assertiveness counterweight，并计划在外部 A/B 验证后再解除条件。因此，不能把它不加限定地描述成所有公开 Claude Code 构建的默认提示词。

这一区别非常重要：

- 如果实际运行环境命中 `USER_TYPE=ant`，这句提示可以直接解释“会挑战用户方案”的行为。
- 如果是外部默认分支，则不能用这句作为唯一解释，需要继续看工具提示和模型自身行为。

### 3.4 `AskUserQuestion` 是默认工具

Claude Code 的基础工具列表默认包含 `AskUserQuestionTool` 和 `EnterPlanModeTool`：

来源：[`src/tools.ts:193`](../claude-code-analysis/src/tools.ts#L193)

`AskUserQuestion` 的提示词明确告诉模型可以在执行过程中：

> 1. Gather user preferences or requirements  
> 2. Clarify ambiguous instructions  
> 3. Get decisions on implementation choices as you work  
> 4. Offer choices to the user about what direction to take.

来源：[`src/tools/AskUserQuestionTool/prompt.ts:32`](../claude-code-analysis/src/tools/AskUserQuestionTool/prompt.ts#L32)

也就是说，即使用户没有开启 Plan mode，模型仍然能在普通执行过程中使用结构化问题与用户讨论偏好和实现选择。工具 schema 和描述给“共同决策”提供了一个显眼、低摩擦的动作入口。

### 3.5 `EnterPlanMode` 的外部默认工具提示非常积极

外部构建使用的 `EnterPlanMode` 工具描述写道：

> Use this tool proactively when you're about to start a non-trivial implementation task. Getting user sign-off on your approach before writing code prevents wasted effort and ensures alignment.

随后又规定，只要满足新功能、多种合理方案、修改现有行为、架构决策、多文件变更、需求不清或用户偏好重要等任一条件，就倾向使用该工具；只有简单且明确的任务才跳过。末尾进一步要求：

> If unsure whether to use it, err on the side of planning.  
> Users appreciate being consulted before significant changes are made to their codebase.

来源：[`src/tools/EnterPlanModeTool/prompt.ts:16`](../claude-code-analysis/src/tools/EnterPlanModeTool/prompt.ts#L16)

这是 Claude Code 更容易先讨论方案的最直接默认上下文证据之一。它在模型尚未进入 Plan mode 时就已经存在，并影响模型是否发起模式切换。

不过这里同样存在分支差异。`USER_TYPE=ant` 使用另一版提示词，它只在存在真正的架构歧义、需求不清或高影响重构时建议进入 Plan mode，并明确写道：

> When in doubt, prefer starting work and using AskUserQuestion for specific questions over entering a full planning phase.

来源：[`src/tools/EnterPlanModeTool/prompt.ts:101`](../claude-code-analysis/src/tools/EnterPlanModeTool/prompt.ts#L101)

所以 Claude Code 源码内部同时存在两种调节方向：

- 外部工具提示更偏“重要修改前先对齐”。
- `ant` 工具提示更偏“能推断就先做，有具体歧义再问”。
- `ant` 基础任务提示又额外加入“不是单纯执行者，要指出误解”的要求。

它们并不矛盾：前者决定是否进入完整规划流程，后者决定执行过程中是否应表达独立判断。

### 3.6 Claude Code 默认行为小结

Claude Code 的默认路径可以概括为：

```text
收到任务
  -> 先按软件工程任务理解，并读取相关代码
  -> 简单明确：直接执行
  -> 存在偏好或实现选择：可用 AskUserQuestion 与用户共同决定
  -> 外部构建中的非简单实现：工具提示积极建议先请求进入 Plan mode
  -> 若命中 ant 分支：发现用户误解或相邻问题时主动指出
```

因此，即使用户没有手动开启 Plan mode，Claude Code 仍可能表现出较强的讨论倾向。原因不是“计划模式已默认开启”，而是默认上下文已经把提问、方案对齐和表达工程判断设计成常规动作。

## 4. Codex 的默认提示词

### 4.1 基础 prompt 的核心是持续执行到完成

Codex 的基础提示词由 [`prompt.md`](../codex/codex-rs/models-manager/prompt.md) 提供，源码通过 `include_str!` 将它作为本地默认基础指令：

> You are a coding agent. Please keep going until the query is completely resolved, before ending your turn and yielding back to the user. Only terminate your turn when you are sure that the problem is solved. Autonomously resolve the query to the best of your ability, using the tools available to you, before coming back to the user.

来源：[`prompt.md:123`](../codex/codex-rs/models-manager/prompt.md#L123)，加载位置见 [`model_info.rs:17`](../codex/codex-rs/models-manager/src/model_info.rs#L17)

这段话把默认交互节奏定义得很清楚：先尽可能自主解决，再回到用户处汇报，而不是在每个可讨论的实现选择前暂停。

### 4.2 对现有代码库强调精确服从任务范围

`Ambition vs. precision` 部分要求：

> If you're operating in an existing codebase, you should make sure you do exactly what the user asks with surgical precision. [...] don't overstep.

同时允许模型根据需求使用审慎的主动性，但不得过度扩展范围。

来源：[`prompt.md:165`](../codex/codex-rs/models-manager/prompt.md#L165)

它鼓励的工程判断主要用于决定交付细节和复杂度，并没有发现一条与 Claude Code `ant` 分支中“发现用户误解就指出、你不是单纯执行者”完全对应的默认指令。

### 4.3 Codex 的 plan 工具不等于 Plan mode

基础 prompt 也鼓励复杂任务使用 `update_plan`，以展示步骤、进度和检查点：

> Plans can help to make complex, ambiguous, or multi-phase work clearer and more collaborative for the user.

来源：[`prompt.md:52`](../codex/codex-rs/models-manager/prompt.md#L52)

但 `update_plan` 主要是执行清单和进度展示工具。它不要求在写代码前等待用户批准，也不自动把会话切换到只读讨论状态。因此它和 Claude Code 的 `EnterPlanMode` 不是同一种行为约束。

### 4.4 启动会话时自动选择 Default mode

Codex 创建会话时明确构造：

```rust
let collaboration_mode = CollaborationMode {
    mode: ModeKind::Default,
    // ...
};
```

来源：[`codex-rs/core/src/session/mod.rs:662`](../codex/codex-rs/core/src/session/mod.rs#L662)

`ModeKind` 的默认枚举值也是 `Default`，并将 `code`、`pair_programming`、`execute` 和 `custom` 作为兼容别名：

来源：[`codex-rs/protocol/src/config_types.rs:643`](../codex/codex-rs/protocol/src/config_types.rs#L643)

所以用户不选择 Plan mode 时，运行时确实处于独立定义的 Default collaboration mode，而不是某种未命名的中间状态。

### 4.5 Default mode 直接要求“作合理假设并执行”

Default mode 的附加提示是解释 Codex 行为的关键：

> In Default mode, strongly prefer making reasonable assumptions and executing the user's request rather than stopping to ask questions.

只有当答案无法从本地上下文发现，而且自行假设会带来风险时，才应该用简短的普通文本提问。

来源：[`codex-rs/collaboration-mode-templates/templates/default.md:7`](../codex/codex-rs/collaboration-mode-templates/templates/default.md#L7)

这与 Claude Code 外部 `EnterPlanMode` 工具提示形成近乎正面的对照：

- Claude Code：非简单实现应主动考虑先取得方案认可；不确定时偏向规划。
- Codex Default：应强烈偏向合理假设并执行；只有无法安全推断时才停下来提问。

### 4.6 默认模式通常不给结构化提问工具

`ModeKind::allows_request_user_input` 默认只对 `Plan` 返回 true：

来源：[`codex-rs/protocol/src/config_types.rs:681`](../codex/codex-rs/protocol/src/config_types.rs#L681)

相关测试也明确验证：默认可用模式配置下，Default mode 会得到：

> request_user_input is unavailable in Default mode

来源：[`request_user_input_spec_tests.rs:157`](../codex/codex-rs/core/src/tools/handlers/request_user_input_spec_tests.rs#L157)

某个 feature flag 可以把 `request_user_input` 开放给 Default mode，因此这不是绝对不变的产品限制。但源码默认值清楚地表明：结构化采访用户主要属于 Plan mode，而不是普通执行模式。

Codex 仍然可以直接用自然语言提问；区别在于默认提示要求尽量不因可合理推断的问题而中断执行，同时没有给普通模式一个同样突出的结构化选择工具。

### 4.7 Codex 默认行为小结

```text
收到任务
  -> 判断任务范围并读取代码
  -> 复杂任务可建立 update_plan 执行清单
  -> 强烈偏向从本地上下文推断并直接实施
  -> 持续工作、测试和修正，直到任务解决
  -> 只有无法发现答案且冒险假设不安全时才暂停提问
  -> 用户明确切到 Plan mode 后，才进入系统性的需求访谈和方案定稿流程
```

## 5. 逐项差异

| 比较维度 | Claude Code 默认状态 | Codex Default mode | 预期行为影响 |
|---|---|---|---|
| 首要身份 | Interactive agent | Coding agent | Claude 的表述更突出互动，Codex 更突出完成编码任务，但身份句本身不是决定因素 |
| 模糊但可推断的需求 | 结合代码上下文理解；可使用提问工具 | 强烈偏向作合理假设并执行 | Codex 更少因小歧义暂停 |
| 结构化提问 | `AskUserQuestion` 是基础工具，可询问偏好、需求和实现选择 | `request_user_input` 默认通常仅 Plan mode 可用 | Claude 更容易把选择交还用户 |
| 非简单实现 | 外部 `EnterPlanMode` 工具提示要求主动考虑先取得方案认可 | `update_plan` 可记录步骤，但不要求等待认可 | Claude 更容易先讨论；Codex 更容易边规划边执行 |
| 对用户错误前提的态度 | `ant` 分支明确要求指出误解和相邻 bug | 未发现完全对应的默认句；以精确完成用户任务为主 | 命中该分支时，Claude 更容易挑战方案 |
| 持续执行 | 要求实际修改代码，失败后自主诊断 | 明确要求持续到完全解决后再返回用户 | 两者都执行，但 Codex 的持续执行指令更集中、更强 |
| 询问阈值 | 基础 prompt 说调查后真正卡住再问；工具层又鼓励就偏好和方案提问 | 无法从本地发现答案且合理假设有风险时才问 | Claude 的工具层扩大了“值得问”的范围 |
| 计划模式边界 | 默认提供进入 Plan mode 的工具，并用工具描述影响选择 | Default 与 Plan 是明确分离的 collaboration modes | 用户没手动开 Plan mode 时，Codex 的执行边界更稳定 |
| 风险操作 | 不可逆、共享或破坏性动作先确认 | 由 approval、sandbox 和指令共同约束 | 两者都会在高风险外部动作前确认，这不是主要风格差异 |

## 6. 为什么在没有开启 Plan mode 时仍能观察到差异

### Claude Code

用户没有手动开启 Plan mode，并不意味着普通上下文没有讨论倾向：

1. `AskUserQuestion` 已经是默认工具，可以在执行中询问偏好和实现决策。
2. `EnterPlanMode` 的工具描述在进入模式之前就可见；外部版本主动建议非简单任务先获取用户认可。
3. 命中 `USER_TYPE=ant` 时，基础任务提示直接要求指出用户误解或相邻问题。
4. 即使最终没有进入 Plan mode，模型也可能先用自然语言表达异议或讨论方案。

### Codex

Codex 的普通会话则有反方向的约束：

1. 会话启动时明确选择 `ModeKind::Default`。
2. Default mode 要求强烈偏向合理假设并执行。
3. `request_user_input` 默认通常不对 Default mode开放。
4. 基础 prompt 要求持续工作到问题解决，再把结果交还用户。

因此，你在日常默认使用中观察到的差别，与源码中的默认上下文设计是吻合的，不需要以“用户主动开启 Plan mode”为前提。

## 7. 不能只归因于提示词

提示词能解释很大一部分行为，但不能证明全部因果。还应考虑：

- **模型后训练差异**：Claude 与 GPT/Codex 模型可能对服从、质疑、提问和工具调用有不同的训练偏好。
- **工具可供性**：一个专门的多选提问工具会让“与用户共同决策”比纯文本提问更容易被模型选择。
- **运行时实验分支**：Claude Code 的 `USER_TYPE=ant`、feature flag 和 A/B 配置会改变实际提示词。
- **Codex 模型目录配置**：Codex 可以使用模型目录提供的 `instructions_template`，本地 `prompt.md` 是默认/回退来源，不保证所有线上模型在所有版本中收到逐字相同的文本。
- **用户级指令**：`CLAUDE.md`、`AGENTS.md`、skills、memory 和组织策略都可能覆盖或强化默认倾向。
- **任务类型**：架构选择多、产品偏好强的任务自然会诱发更多讨论；明确的修复任务则会让两者都偏向执行。

所以更准确的因果表述是：

> 默认提示词与工具设计为这种行为差异提供了清晰、方向一致的激励；模型训练和运行时配置决定这些激励最终表现得有多强。

## 8. 最终结论

Claude Code 与 Codex 并不是简单的“一个会思考、一个只服从”。两者都要求读取代码、实际执行、避免越界并在失败后调查原因。真正的设计差异是默认交互策略：

- Claude Code 把**询问偏好、讨论实现选择、重要修改前取得对齐、表达独立工程判断**放进默认可见的行为工具箱。外部 `EnterPlanMode` 提示尤其积极；`ant` 分支则直接强调“collaborator, not just an executor”。
- Codex 把**合理推断、直接执行、持续工作到解决、精确遵守现有代码库任务范围**设为 Default mode 的主路径，把系统性访谈和方案定稿主要留给显式 Plan mode。

因此，用户感受到 Claude Code 更愿意指出问题并讨论，而 Codex 更愿意按指示落地，并非纯粹的主观印象；在这两个源码版本中，可以找到与之高度一致的默认提示词和工具机制证据。

## 9. 主要源码索引

Claude Code：

- [`src/constants/prompts.ts`](../claude-code-analysis/src/constants/prompts.ts)
- [`src/tools.ts`](../claude-code-analysis/src/tools.ts)
- [`src/tools/AskUserQuestionTool/prompt.ts`](../claude-code-analysis/src/tools/AskUserQuestionTool/prompt.ts)
- [`src/tools/EnterPlanModeTool/prompt.ts`](../claude-code-analysis/src/tools/EnterPlanModeTool/prompt.ts)

Codex：

- [`codex-rs/models-manager/prompt.md`](../codex/codex-rs/models-manager/prompt.md)
- [`codex-rs/models-manager/src/model_info.rs`](../codex/codex-rs/models-manager/src/model_info.rs)
- [`codex-rs/core/src/session/mod.rs`](../codex/codex-rs/core/src/session/mod.rs)
- [`codex-rs/collaboration-mode-templates/templates/default.md`](../codex/codex-rs/collaboration-mode-templates/templates/default.md)
- [`codex-rs/protocol/src/config_types.rs`](../codex/codex-rs/protocol/src/config_types.rs)
- [`codex-rs/core/src/tools/handlers/request_user_input_spec_tests.rs`](../codex/codex-rs/core/src/tools/handlers/request_user_input_spec_tests.rs)
