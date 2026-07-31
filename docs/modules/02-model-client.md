# Model Client 适配层笔记

## 1. 模块定位

Model Client 是 Codemate 与大模型服务之间的协议适配层。它主要解决三个问题：

- 将 agent 内部统一的消息历史转换成不同模型接口要求的请求格式。
- 将不同模型接口返回的文本、工具调用和中间进展统一成 runtime 能处理的响应。
- 屏蔽工具 schema、结构化输出、usage 和缓存字段等 provider 差异。

Codemate 当前主要兼容 OpenAI-compatible 和 Anthropic-compatible 两类接口。两者对消息结构、工具调用、工具结果以及中间进展的表达方式都不同，因此不能让 runtime 直接依赖某一种 API 格式。

这一层的核心思想可以概括为：

```text
Codemate 内部统一格式
        ↓ 请求适配
OpenAI / Anthropic 原生格式
        ↓ 模型返回
OpenAI / Anthropic 原生响应
        ↓ 响应归一化
Codemate 内部统一响应
```

这里并不是直接把 OpenAI 消息转换成 Anthropic 消息，或者反过来转换。两类接口都只与 Codemate 的内部格式互相转换。内部格式相当于一个稳定的中间层，避免 provider 之间形成两两适配关系。

## 2. Agent 内部消息格式

Codemate 的历史消息使用统一的 `role + content + 扩展字段` 结构，主要有三种 role。

### user 消息

表示用户输入：

```json
{
  "role": "user",
  "content": "检查当前项目的权限设计"
}
```

### assistant 消息

assistant 消息通过 `kind` 区分用途。

仅包含中间进展：

```json
{
  "role": "assistant",
  "kind": "commentary",
  "content": "我已经找到权限判断入口，接下来检查 shell 路径校验。"
}
```

仅包含工具调用：

```json
{
  "role": "assistant",
  "kind": "tool_calls",
  "content": "",
  "tool_calls": [
    {
      "id": "call_001",
      "name": "read_file",
      "args": {"path": "codemate/tools/validators.py"}
    }
  ]
}
```

同时包含 commentary 和工具调用：

```json
{
  "role": "assistant",
  "kind": "tool_calls",
  "content": "我先读取权限校验和路径策略，确认两者如何组合。",
  "tool_calls": [
    {
      "id": "call_001",
      "name": "read_file",
      "args": {"path": "codemate/tools/validators.py"}
    },
    {
      "id": "call_002",
      "name": "read_file",
      "args": {"path": "codemate/tools/path_policy.py"}
    }
  ]
}
```

最终回答：

```json
{
  "role": "assistant",
  "kind": "final",
  "content": "当前权限系统的主要问题是……"
}
```

### tool 消息

表示某个工具调用的执行结果，通过 `tool_call_id` 与 assistant 发出的工具调用对应：

```json
{
  "role": "tool",
  "tool_call_id": "call_001",
  "name": "read_file",
  "content": "status: ok\n..."
}
```

一条 assistant 消息可以包含多个工具调用，因此后面也可能连续出现多条 tool 消息。内部 history 允许这些工具结果逐条保存，provider 适配层负责在请求时把它们整理成目标接口要求的结构。

## 3. 内部统一响应

不同 provider 的原始响应最终会被归一化成 `ModelResponse`。Runtime 只需要识别三种 `kind`：

- `commentary`：用户可见的阶段性进展，任务尚未结束。
- `tool_calls`：模型请求执行一个或多个工具，`text` 可以同时携带 commentary。
- `final`：最终回答，本轮 agent loop 结束。

工具调用本身统一为：

```json
{
  "id": "call_001",
  "name": "read_file",
  "args": {"path": "README.md"}
}
```

这个设计让 runtime 不必理解 OpenAI 的 `function_call`、Chat Completions 的 `tool_calls`，也不必理解 Anthropic 的 `tool_use` content block。它只处理统一的工具名称、参数和调用 ID。

## 4. OpenAI 请求转换

Codemate 优先使用 OpenAI Responses API，同时保留 Chat Completions fallback。

### Responses API

Responses API 的输入不是简单的传统 messages，而是一组 input items。主要转换关系如下：

| Codemate 内部消息 | OpenAI Responses API |
| --- | --- |
| system | `role: system` + `input_text` |
| user | `role: user` + `input_text` |
| assistant commentary | `role: assistant` + `output_text` + `phase: commentary` |
| assistant final | `role: assistant` + `output_text` + `phase: final_answer` |
| assistant tool calls | 一个或多个 `function_call` item |
| tool result | `function_call_output` |

当一条内部 assistant 消息同时包含 commentary 和工具调用时，会被拆成：

```text
assistant commentary item
function_call item 1
function_call item 2
```

commentary 通过 `phase: commentary` 保留原有语义，工具调用则携带 `call_id`、工具名称和 JSON 参数。后续工具结果使用同一个 `call_id` 关联。

### Chat Completions fallback

部分 OpenAI-compatible 服务并不完整支持 Responses API，Codemate 在特定请求失败时可以退回 Chat Completions：

| Codemate 内部消息 | Chat Completions |
| --- | --- |
| system / user | 普通 role message |
| assistant commentary / final | assistant 文本消息 |
| assistant tool calls | assistant message 中的 `tool_calls` |
| tool result | role 为 `tool` 的消息 |

Chat Completions 没有 `phase` 字段，因此无法在协议层严格区分 commentary 和 final。它只能依靠“是否同时返回工具调用”和当前 agent loop 的位置来表达中间进展。也就是说，Responses API 对 commentary 的支持更完整，Chat Completions 主要承担兼容 fallback 的职责。

## 5. Anthropic 请求转换

Anthropic Messages API 使用 content blocks 表达一条消息中的不同内容。

| Codemate 内部消息 | Anthropic Messages API |
| --- | --- |
| system | 请求顶层独立的 `system` 字段 |
| user | user message 中的 `text` block |
| assistant commentary / final | assistant message 中的 `text` block |
| assistant tool calls | assistant message 中的 `tool_use` block |
| tool result | user message 中的 `tool_result` block |

当一条 assistant 消息同时包含 commentary 和工具调用时，转换结果类似于：

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "我先读取权限校验和路径策略。"
    },
    {
      "type": "tool_use",
      "id": "call_001",
      "name": "read_file",
      "input": {"path": "codemate/tools/validators.py"}
    },
    {
      "type": "tool_use",
      "id": "call_002",
      "name": "read_file",
      "input": {"path": "codemate/tools/path_policy.py"}
    }
  ]
}
```

### 多工具结果必须合并

Anthropic 对工具结果顺序有严格要求：

- assistant 消息中出现一个或多个 `tool_use` 后，下一条消息必须是 user。
- 对应的所有 `tool_result` 必须放在这条紧邻的 user 消息中。
- 不能把多个工具结果拆成多条连续 user 消息。
- `tool_result.tool_use_id` 必须对应前一条 assistant 消息中的 `tool_use.id`。

因此，内部 history 中连续保存的多条 tool 消息，在转换时必须合并：

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "call_001",
      "content": "第一个工具的结果"
    },
    {
      "type": "tool_result",
      "tool_use_id": "call_002",
      "content": "第二个工具的结果"
    }
  ]
}
```

这也是消息历史内部要保留 tool call ID，并且 history 裁剪必须以完整 tool interaction 为单位的原因。如果 assistant 的 `tool_use` 与对应的 `tool_result` 被拆散，Anthropic-compatible 接口会直接拒绝请求。

Codemate 默认不启用 Anthropic thinking blocks。历史回放只保留用户可见的 `text` 和工具调用所需的 `tool_use`，避免引入 thinking 签名、跨模型兼容和回传约束。

## 6. OpenAI 响应识别

OpenAI Responses API 可以在同一次响应的 `output` 中返回多种 item：

```text
commentary message
function call
function call
```

适配层分别提取：

- `phase: commentary` 的文本。
- `phase: final_answer` 的文本。
- 没有 phase 的 fallback 文本。
- 所有 `function_call` 或兼容形式的工具调用。

响应分类优先级是：

1. 只要存在工具调用，统一返回 `kind = tool_calls`；commentary 放在 `text` 中。
2. 没有工具调用但存在 `final_answer`，返回 `kind = final`。
3. 没有工具调用和 final，但存在独立 commentary，返回 `kind = commentary`。
4. 其他普通文本按 fallback final 处理。

这样可以正确处理四种情况：

- commentary-only。
- tool calls-only。
- commentary + tool calls。
- final answer。

Runtime 收到 commentary-only 后会展示并继续请求模型；收到 commentary + tool calls 后会先展示进展，再执行工具；只有收到 final 才结束本轮任务。

## 7. Anthropic 响应识别

Anthropic 的响应由 `content` blocks 组成，Codemate 当前关注两类：

- `text`
- `tool_use`

适配层先收集所有 text，再收集所有 tool_use：

- 如果存在 `tool_use`，返回 `kind = tool_calls`，同时把 text 作为 commentary。
- 如果不存在 `tool_use`，将 text 返回为 `kind = final`。

这里与 OpenAI 有一个本质区别：Anthropic 没有与 Responses API `phase` 等价的显式 commentary/final 标识。因此同样是一个单独的 text block，适配层无法可靠判断它是“说完一段进展，随后还要继续”还是“最终回答”。

Codemate 采用的兼容策略是：

- text 和 tool_use 同时出现：text 明确属于工具调用前的可见进展。
- 只有 text：视为最终回答。

这种策略符合 Anthropic 常见的工具调用语义，也避免仅凭文本内容猜测响应是否结束。代价是 Anthropic-compatible 接口不能像 OpenAI Responses API 一样原生表达 commentary-only。

## 8. Commentary 的跨 Provider 设计

Commentary 是这一层最重要的跨 provider 抽象之一。它不是隐藏思维过程，而是用户可见的工作进展，例如：

```text
我已经确认工具注册没有问题，报错发生在 Anthropic 消息转换阶段。
接下来检查多个 tool result 是否被合并到同一条 user message。
```

OpenAI Responses API 可以显式返回：

```json
{
  "role": "assistant",
  "phase": "commentary",
  "content": [...]
}
```

Anthropic 则通常把进展文本和 `tool_use` 放在同一个 assistant content 数组中：

```json
{
  "role": "assistant",
  "content": [
    {"type": "text", "text": "我先检查相关实现。"},
    {"type": "tool_use", "...": "..."}
  ]
}
```

为了兼容这两种表达，Codemate 内部没有直接保存 `phase` 或 content block，而是使用：

- `kind = commentary` 表示独立中间进展。
- `kind = tool_calls` 且 `text` 非空，表示工具调用附带中间进展。
- `kind = final` 表示最终回答；如果同一次 OpenAI response 在 final 前还包含 commentary，则保存在独立的 `commentary_text` 中。

因此，commentary 的协议差异只存在于 Model Client 边界，UI 和 runtime 始终使用同一套语义。

## 9. 流式输出适配

Codemate 支持在终端中流式展示模型文本，但内部 history 不保存流式碎片。Model Client 会把 provider 的 SSE 事件转换成统一的 `ModelStreamEvent`：

- `text_delta`：用户可见的文本增量，只用于 UI 临时展示。
- `done`：一次模型响应结束，携带完整 `ModelResponse`。
- `error`：流式请求失败。

Runtime 只消费这三个统一事件，不直接读取 OpenAI 或 Anthropic 的原始字段。

OpenAI Responses API 的主要文本事件是 `response.output_text.delta`，工具参数事件是 `response.function_call_arguments.delta/done`，最终以 `response.completed` 中的完整 response 为准。这样可以避免只靠 delta 拼接时漏掉 usage、phase 或完整工具调用信息。

Anthropic Messages API 的主要文本事件是 `content_block_delta` 中的 `text_delta`，工具参数来自 `input_json_delta`。由于工具参数是分片 JSON，Model Client 必须等流结束后再把它组装成完整 `tool_use`，然后返回 `ModelResponse(kind="tool_calls")`。

OpenAI Responses 的 delta 可以携带 `commentary` 或 `final_answer` phase，终端会据此选择 commentary 或普通正文样式。Anthropic 和 OpenAI Chat Completions 的 delta 没有 phase，因此终端不再尝试提前判断文本类别：所有可见 assistant 文本都先显示统一的 `◆ Codemate` 标识，再按原有 Markdown 流式渲染。这样不需要在响应结束后重放正文，也不会因 provider 是否提供 phase 而缺少标识。

Runtime 分别记录“commentary 是否已经流式展示”和“final 是否已经流式展示”，不能只用总流式字符数判断。否则如果模型先流出 commentary、最终正文只出现在 completed response 中，最终回答会被错误跳过。

这个设计保持了一个关键边界：**流式输出只影响终端展示，工具审批、工具执行、history 存储和上下文压缩仍然只处理完整消息**。

## 10. 工具 Schema 和结构化输出

Codemate 内部工具统一使用：

```json
{
  "name": "read_file",
  "description": "读取指定文件……",
  "input_schema": {
    "type": "object",
    "properties": {}
  }
}
```

发送请求时再转换：

- OpenAI Responses：`type + name + description + parameters`
- OpenAI Chat Completions：外层 `type: function`，具体定义放进 `function`
- Anthropic：`name + description + input_schema`

结构化输出也采用相同思路。调用方提供统一 JSON Schema，适配层分别转换成 OpenAI 的 `text.format` / `response_format`，或 Anthropic 的 `output_config.format`。当前主要用于长期记忆召回等需要稳定 JSON 结果的场景，普通对话默认不启用。

## 11. Usage 和请求元数据

### Prompt 缓存

OpenAI 和 Anthropic 使用不同的缓存控制方式。OpenAI 请求携带稳定的 `prompt_cache_key`；Anthropic 没有对应的 key，而是在可缓存内容上设置 `cache_control`。

Anthropic provider 默认启用缓存，当前使用 5 分钟 TTL。请求同时设置两层缓存边界：

```text
tools
system + explicit cache_control
messages
request-level automatic cache_control
```

system 上的显式断点用于稳定缓存工具定义和系统提示词；请求级自动缓存继续在增长的消息历史中选择可复用前缀。这样既能复用稳定 prefix，也能在长工具循环中逐步扩大缓存命中范围。

缓存只在 runtime 为主 agent 请求提供稳定 `prompt_cache_key` 时启用。Compact、记忆提取、记忆召回、标题生成等一次性模型请求不携带该键，因此不会为了几乎不会复用的输入创建缓存。DeepSeek 虽然复用 Anthropic-compatible 协议适配器，但默认不发送 Anthropic 缓存字段。

Client 还保留 1 小时 TTL 支持，但默认不启用。并发子 agent 通过 `fork()` 创建 client 时会继承缓存开关和 TTL。

### Usage 归一化

不同接口对 token 使用量的字段命名不一致：

- OpenAI 常见为 `input_tokens` / `output_tokens`，兼容接口也可能使用 `prompt_tokens` / `completion_tokens`。
- 缓存 token 可能位于 `input_tokens_details` 或 `prompt_tokens_details`。
- Anthropic 将未缓存输入、缓存创建和缓存读取分别放在 `input_tokens`、`cache_creation_input_tokens` 和 `cache_read_input_tokens`。

适配层将它们统一为：

- `input_tokens`
- `output_tokens`
- `total_tokens`
- `cached_tokens`
- `cache_creation_input_tokens`
- `uncached_input_tokens`
- `cache_hit`

Anthropic 的 `input_tokens` 在归一化后是三类输入 token 之和，而 `uncached_input_tokens` 保留接口原始的未缓存部分。Runtime 因而能用完整输入规模更新上下文预算，不会因为大量 cache read 而低估当前上下文。

## 12. Runtime 如何消费响应

Model Client 返回统一响应后，runtime loop 的处理非常直接：

```text
commentary
  -> 展示给用户
  -> 作为 assistant commentary 写入 history
  -> 继续下一次模型请求

tool_calls
  -> 展示附带 commentary
  -> 保存 assistant 工具调用消息
  -> 校验、审批并执行每个工具
  -> 逐条保存 tool result
  -> 继续下一次模型请求

final
  -> 展示最终回答
  -> 作为 assistant final 写入 history
  -> 结束当前 run
```

模型协议转换与 agent 行为控制由此分离：Model Client 判断“模型返回了什么”，Runtime 决定“接下来做什么”。

## 13. 主要难点与解决方式

### 难点一：Anthropic 工具调用配对严格

内部 history 逐条记录工具结果更适合执行和持久化，但 Anthropic 要求多个结果合并到紧邻的同一条 user 消息。解决方法是在请求边界集中合并连续 tool 消息，同时依靠稳定的 tool call ID 保持对应关系。

### 难点二：两类接口对 commentary 的表达不同

OpenAI 有显式 phase，Anthropic 只有 text block 与 tool_use 的组合语义。解决方法是设计 provider 无关的 `ModelResponse.kind`，并允许 `tool_calls` 响应同时携带可见文本。


### 难点三：历史必须能跨 Provider 恢复

会话可能先使用 OpenAI，恢复后切换到 Anthropic，或者反过来。因此持久化层不能保存只能由某个 provider 理解的原始消息。统一内部 history 使历史可以在每次请求时重新转换成当前 provider 所需格式。

## 14. 面试表达要点

可以把这一层概括为：

> 我没有让 agent runtime 直接依赖 OpenAI 或 Anthropic 的原生协议，而是设计了一套统一的内部消息和响应结构。请求时，适配层把内部的 user、commentary、tool calls、tool results 和 final 转成目标 provider 格式；返回时，再统一解析成 commentary、tool_calls 和 final 三种状态。这里最难的是 Anthropic 对多个 tool_use/tool_result 的紧邻和合并要求，以及 OpenAI 有显式 commentary phase、Anthropic 只能根据 text 与 tool_use 的组合判断语义。通过把这些差异收敛在 Model Client 边界，runtime、上下文管理和 UI 都不需要感知 provider 细节，也支持会话在不同模型提供商之间恢复和切换。
