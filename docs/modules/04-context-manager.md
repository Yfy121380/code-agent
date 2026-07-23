# Context Manager 模块笔记

## 1. 模块定位

Context Manager 负责决定模型每一轮请求时“能看到什么”。它不是简单把系统提示词、历史消息和当前问题拼起来，而是把不同生命周期、不同可靠性、不同重要程度的信息分层组织，然后配合预算统计和 history compact 机制，让 agent 能在长任务中继续保持上下文连续性。

这个模块的核心价值是解决三个问题：

- **信息分层**：系统规则、工作状态、长期记忆、历史对话、当前请求不是同一种信息，不能混在一起裁剪。
- **结构正确**：history 中的 `tool_call` 和 `tool_result` 必须保持配对，尤其要兼容 OpenAI 和 Anthropic 对工具消息的不同格式要求。
- **长任务续航**：旧 history 不能无限增长，需要在保留最近上下文的同时，把较早信息压缩成可继续使用的 summary。

设计上，Context Manager 更像模型请求的“上下文编排器”：它负责组织输入结构、记录各层长度、保留工具交互关系，并把真正容易膨胀的 history 交给 compact 机制处理。

## 2. 上下文分层总览

Codemate 当前按下面顺序组织上下文：

```text
prefix
available skills
working memory
relevant memory
history summary
recent history
current request
```

每层的作用不同：

- **prefix**：稳定系统规则，约束 agent 的行为边界。
- **available skills**：当前可加载的 skill 列表，只放名称和简短描述。
- **working memory**：当前任务最相关的短期状态，滚动更新。
- **relevant memory**：从长期记忆中召回的跨会话信息。
- **history summary**：已经被压缩过的较早历史。
- **recent history**：保留原始结构的最近对话和工具交互。

这样分层的原因是：不同信息的生命周期不同。`prefix` 比较稳定，不能轻易压缩；`working memory` 服务当前任务，要短而准；长期记忆跨会话存在，不能和本轮工具结果混在一起；`history` 是增长最快、最容易撑爆上下文的部分，因此需要专门处理。

## 3. Prefix

Prefix 是 agent 的稳定行为规则，可以理解为“工作手册”。它主要包含：

- 工具使用规则：什么时候使用工具、什么时候停止工具调用并返回 final。
- 工作流规则：读文件、改文件、验证代码、处理重复调用等基本流程。
- todo 规则：复杂多步骤任务如何规划，如何跟随 `current_todos` 执行。
- skill 规则：什么时候加载 skill、如何使用 skill root 下的资源、什么时候卸载。
- web 工具规则：什么时候搜索、什么时候抽取网页、如何看待网页内容。
- progress/commentary 规则：什么时候输出中间进展，如何在大型调查中沉淀关键发现。
- final answer 规则：最终回答应该如何组织，避免过短或无关。
- 权限和安全意识：工具不可用时不要假设，敏感内容不要发给网络工具等。

Prefix 通常不参与 history compact。它是相对稳定的运行规则，会随着工具签名、工作区事实或配置变化而刷新。这样做的好处是：模型每轮都能看到一致的行为约束，不会因为历史压缩而丢掉基本工作规范。

## 4. Available Skills

Available skills 只展示“有哪些 skill 可以加载”，格式上是 skill name 加 description。它不包含完整 `SKILL.md` 正文。

这样设计是为了控制上下文成本。完整 skill 可能很长，如果一开始全部塞进上下文，会挤占 history 和 working memory；但模型又需要知道当前有哪些可选能力。因此列表层只提供发现信息，真正需要某个 skill 时，再通过 `skill_load` 把它加载到 working memory。

技能列表有两个重要限制：

- description 会限制长度，避免 skill 列表过大。
- 列表总量受预算控制，避免大量 skill 把上下文撑大。

## 5. Working Memory

Working memory 是当前任务的短期工作区。它存放的不是完整历史，而是“下一轮模型最可能继续用到的状态”。这部分的设计理念是：不要让模型每次都从冗长 history 中重新找关键信息，而是把高价值、当前相关的信息滚动沉淀出来。

Working memory 当前包括几类信息。

### Runtime Context

Runtime context 提供当前运行时事实，例如：

- 当前本地时间。
- 当前日期。
- 时区。
- memory root。
- 当日 daily log 路径。

这些信息主要服务长期记忆和 dream。比如模型要写 daily log 时，不需要猜当前日期和路径，而是直接使用 runtime context 给出的绝对信息。

### Task Summary

Task summary 是当前任务的简短摘要，用于帮助模型保持“我现在在做什么”的意识。它不会替代 history，而是作为任务状态提示，防止长工具链执行过程中偏离当前目标。

### Recent Files

Recent files 记录最近访问或修改过的文件。它解决的是“模型刚才处理过哪些文件”的问题。

设计上只保留有限数量的最近文件，因为这类信息越旧越不可靠。比如文件已经被修改过，旧的读取结果和摘要可能失效；保留太多也会让 working memory 变成另一个 history。

### File Summaries

File summaries 是对最近读过文件的短摘要。它不是完整文件内容，而是轻量提示，例如文件大致用途、关键入口或最近看到的重要片段。

这部分和 history 中的 `read_file` 工具结果职责不同：

- history 保存原始工具观察，用于当前上下文内继续推理。
- file summary 保存轻量状态，用于下一轮快速恢复“这个文件大概是什么”。

文件摘要会做新鲜度校验。如果文件被 `write_file` 或 `patch_file` 修改，旧摘要会失效，避免模型继续相信过期信息。

### Process Notes

Process notes 记录工具异常调用，例如：

- 参数错误。
- 重复调用被拦截。
- 审批被拒绝。
- 工具执行失败。

它的目的不是长期记录错误，而是避免模型在后续几轮继续犯同样错误。比如某个工具参数格式不对，或者某个重复读取已经被拦截，process note 会提醒模型换参数、换工具或停止重复尝试。

Process notes 有 TTL 和成功清理策略：

- 重复调用类错误在任意成功工具调用后清理。
- 参数错误、审批拒绝、普通错误等在同工具成功后清理。
- 过了若干 turn 后自动过期。

### Active Skills

当模型调用 `skill_load` 后，完整 skill 内容进入 working memory。它包含：

- skill 名称。
- skill root。
- skill-relative 资源位置说明。
- `SKILL.md` 中的具体指令。

这样设计是为了让 skill 能跨多轮任务持续生效，而不是只在调用工具那一轮出现。与此同时，skill 不会永久占用上下文；当用户切换到无关任务时，模型应调用 `skill_unload` 卸载不再相关的 skill。

### Current Todos

Todo 也放在 working memory 中。它代表当前任务计划，而不是普通历史记录。

当前设计支持阶段式 todo：

- 外层是 phase。
- phase 内部可以有更细的 tasks。
- 简单 phase 可以不展开 tasks。
- 复杂 phase 可以在执行到该阶段后渐进式展开。

这样做的原因是：复杂任务的后续步骤往往依赖前面调查得到的信息。如果一开始把所有细节都列满，容易规划错误；如果一直只列粗粒度阶段，长任务执行到后面又容易混乱。阶段式、渐进式展开是在规划准确性和上下文成本之间的折中。

## 6. Relevant Memory

Relevant memory 是长期记忆召回结果，当前分为三类：

- `user_profile`：用户画像，包括身份、知识背景、偏好。
- `feedback_workflow`：用户对 agent 工作方式的反馈。
- `project_context`：项目背景、目标、约束、关键设计。

这部分和 working memory 不同。Working memory 面向当前任务，滚动变化；relevant memory 来自长期记忆，服务跨会话连续性。

召回结果会按类别展示，并限制数量。这样模型能知道这条记忆属于用户偏好、工作流反馈还是项目背景，减少不同类型记忆混在一起导致的误用。

## 7. History Summary

History summary 是已经压缩过的旧历史。它会以固定 wrapper 放在 recent history 之前：

```text
This session is being continued from a previous conversation that ran out of context.
The summary below covers the earlier portion of the conversation.

Summary:
...
```

这个 wrapper 的作用是告诉模型：下面不是新的用户请求，而是较早对话的摘要。这样 summary 既可以作为主 agent 的上下文，也可以在下一次 compact 时作为“之前已经压缩过的信息”重新参与合并。

Session 内部只保存 summary 的正文；真正组装上下文时再统一加 wrapper。这样可以避免多次 compact 后 wrapper 重复嵌套。

## 8. Recent History

Recent history 保存最近一段原始消息，包括：

- user 消息。
- assistant commentary。
- assistant final。
- assistant tool calls。
- tool results。

这里最重要的是结构正确。工具调用不是普通文本，必须保证 assistant 的 `tool_calls` 和后续对应 `tool_result` 不被拆散。

History 会按 group 处理：

- 普通 user/assistant 消息单独成为一个 message group。
- assistant tool_calls 和后续同 id 的 tool results 绑定成一个 tool interaction group。
- 如果出现孤立 tool result，会跳过，避免产生不符合模型 API 的消息结构。

这个分组策略是为了兼容 OpenAI 和 Anthropic。尤其是 Anthropic 要求 `tool_use` 后面必须紧跟对应的 `tool_result`，否则请求会直接报错。因此 history 裁剪、去重和 compact 切分都必须以 group 为单位，不能只按单条 message 做滑动窗口。

## 9. 预算管理

现在的设计是：

- prefix 基本不裁剪，依靠规则本身保持稳定。
- available skills 控制 description 长度和列表规模。
- working memory 通过条目数量、摘要长度、TTL 等机制控制大小。
- relevant memory 限制召回条数。
- history summary 是 compact 后的结构化摘要。
- recent history 是主要增长项，通过 microcompact 和 history compact 控制规模。

预算统计使用模型上下文 token 上限作为参考。系统会维护最近一次模型请求 usage，并把工具结果按粗略 token 估算追加到 token 使用状态中。`/budget` 会展示：

- 各 section 的字符数。
- 工具 schema 数量和字符数。
- 当前模型最大上下文 token。
- compact 触发阈值。
- 当前估算上下文 token 和占比。

这个估算不是 tokenizer 级别的精确计算，但足够用于判断上下文是否接近压缩阈值。真实请求前，如果估算达到阈值，就会触发 history compact。

## 10. Microcompact

Microcompact 不是完整 history 压缩，而是对旧观察结果做轻量清理。

它主要处理这些工具结果：

- `list_files`
- `read_file`
- `grep`
- `web_search`
- `web_extract`
- `web_research`

规则包括：

- 只处理成功的观察类工具结果。
- 失败、拒绝、报错结果不清理，因为这些信息可能影响后续决策。
- 对完全**相同参数的只读工具调用做去重**，只保留最新一次。
- 只**保留最新20条观察类工具结果**，旧结果替换为：

```text
Old tool result content cleared.
```

这样做的原因是：读文件、搜索和网页结果通常很长，会大量占用上下文；而且旧文件内容可能已经因为后续修改而变得不可靠。清理旧结果可以显著降低 history 的体积。

Microcompact 的风险是：旧工具结果被清理后，早期调查发现可能丢失。因此 prefix 中要求模型在大型调查任务中，阶段性输出有信息量的 commentary，把关键发现、证据位置和下一步判断及时沉淀到 history 的 assistant 文本中。

## 11. History Compact

History compact 是真正的长历史压缩。当预算接近阈值，或者用户手动执行 `/compact` 时，会启动一次压缩流程。

整体流程是：

1. 将 history 按 group 切分。
2. 从新到旧保留 recent history，至少保留一定消息数或字符数。
3. 剩余较旧 history 作为待压缩内容。
4. 如果已有 history summary，把它用固定 wrapper 放到待压缩内容前面，说明这是之前已经压缩过的早期信息。
5. fork 一个无工具的 compact 子请求。
6. compact 子请求只接收 system prompt、待压缩 history、compact request。
7. 模型返回结构化 Markdown summary。
8. 校验 summary 是否包含固定 section。
9. 成功后 session 中只保留新的 summary 和 recent history。
10. 失败最多重试 3 次，仍失败则恢复原 history 和原 summary。

Compact 子请求的工具列表为空，因此它不能继续执行用户任务，也不能读取文件或调用其他工具。它唯一任务就是根据给定消息生成摘要。

## 12. Compact Prompt 结构

Compact 使用独立 system prompt，里面定义了子请求身份和输出规则：

- 它是一个 conversation summarizer。
- 它只负责总结旧上下文，不继续执行 coding task。
- 它不能调用工具。
- 它必须输出 Markdown。
- 它必须使用固定字段。
- 它要保留用户约束、技术决策、代码状态、验证结果和未解决问题。

待压缩信息放在 messages 中，而不是揉进 system prompt。最后追加一个 compact request，明确要求：

- 只总结上面的待压缩消息。
- 最新消息会单独保留，不需要总结 recent history。
- 返回固定结构的 Markdown summary。

这种组织方式比较清晰：system prompt 定义“怎么总结”，history messages 提供“总结什么”，compact request 触发“现在开始总结”。

## 13. Compact Summary 字段

压缩后的 summary 使用六个固定字段：

```text
## Working Directory
## User Preferences And Constraints
## Current State
## Key Decisions
## Changed Files
## Validation And Issues
```

各字段作用如下。

### Working Directory

记录工作目录、项目身份、分支、环境或运行时事实。它帮助后续 agent 知道当前上下文属于哪个项目。

### User Preferences And Constraints

记录用户偏好和明确限制，例如：

- 使用中文。
- 回答要直接务实。
- 不要过度封装。
- 不要修改某些函数名或文件。
- 修改后要运行哪些验证。

这部分非常重要，因为用户约束一旦在 compact 中丢失，后续 agent 很容易做出违背用户意图的修改。

### Current State

记录当前任务进展、正在讨论的设计、最近做到哪一步、下一步自然应该做什么。

它用于恢复“现在任务进行到哪儿了”，避免 compact 后只剩历史决策但不知道当前要继续做什么。

### Key Decisions

记录已经确认的设计决策、技术选择、被否定的方案、接口约定、权限规则、行为规则和重要结论。

这部分用于避免重复讨论已经定下来的方案。

### Changed Files

记录创建、修改、删除或重点查看过的文件，以及每个文件的简短改动摘要。

它不保存完整 diff，而是保存未来继续工作时需要知道的文件级状态。

### Validation And Issues

记录运行过的测试、语法检查、命令结果、失败原因、未解决问题和风险。

它能帮助后续 agent 判断哪些东西已经验证过，哪些还需要继续验证。

## 14. 设计难点和解决方式

Context 管理的主要难点不在“拼 prompt”，而在如何让信息长期可靠地留在正确位置。

### 难点一：滑动窗口容易丢约束

如果只保留最近 N 条消息，早期用户约束、架构决策和验证结论很容易丢失。

解决方式是：recent history 只负责保留最近原文，旧 history 通过结构化 summary 保存关键事实。

### 难点二：工具结果太长

`read_file`、`grep`、web 搜索结果经常很长，而且很多内容只在当时有用。

解决方式是：观察类工具结果做 microcompact，只保留近期结果；同时要求模型在大型调查中把关键发现写入 commentary。

### 难点三：工具消息结构不能被破坏

OpenAI 和 Anthropic 都要求工具调用和工具结果有正确关系，Anthropic 对顺序尤其严格。

解决方式是：history 以 group 为单位处理，assistant tool_calls 和对应 tool results 绑定在一起，裁剪和 compact 切分时不拆散。

### 难点四：working memory 不能变成第二份 history

如果把所有工具结果都沉淀到 working memory，它会越来越长，最后和 history 一样难管理。

解决方式是：working memory 只保存短期高价值状态，例如最近文件、短摘要、todo、active skill 和工具错误笔记，并通过数量、长度、TTL 控制规模。

### 难点五：压缩失败不能破坏会话

Compact 本身也是模型调用，可能失败、返回空内容、缺 section 或错误地产生 tool calls。

解决方式是：compact 结果必须校验固定字段；失败最多重试 3 次；全部失败时恢复原 history 和原 summary，不破坏 session。

## 15. 面试复述版本

Context Manager 的核心设计是分层上下文管理。Codemate 不把所有内容当成一段聊天历史处理，而是分成 prefix、available skills、working memory、relevant memory、history summary、recent history 和 current request。

Prefix 保存稳定行为规则；skills 只放可加载技能列表；working memory 保存当前任务最相关的滚动状态，包括任务摘要、最近文件、文件摘要、工具错误笔记、active skills 和 todos；relevant memory 保存长期记忆召回结果；history summary 保存旧历史压缩结果；recent history 保留最近原始结构化消息。

预算管理上，不再对所有层做统一字符裁剪，而是让稳定层通过自身规则限制规模，把主要增长压力集中在 history 上。History 先通过 microcompact 清理旧的读类和搜索类工具结果，再在接近模型上下文阈值时 fork 一个无工具 compact 子请求，把旧 history 压缩成固定六字段 summary，同时原样保留最近 history。

这样做的重点是兼顾三件事：长任务不中断、工具消息结构不被破坏、重要约束和决策不会因为滑动窗口裁剪而丢失。
