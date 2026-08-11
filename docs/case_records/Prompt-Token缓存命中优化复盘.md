# Prompt Token 缓存命中优化复盘：从固定 7680 Token 到稳定上下文前缀

## 1. 问题概述

CodeMate 在长任务中会连续进行多轮模型请求：

```text
用户请求
  -> 模型决定调用工具
  -> 工具结果写入 history
  -> 重新组装上下文
  -> 再次请求模型
```

理论上，这些请求的大部分输入内容都是上一轮请求的前缀，因此非常适合使用模型服务端的 Prompt Cache。工具调用越多、history 越长，后续请求能够复用的输入 token 应该越多。

实际账单中却长期出现：

```text
输入 token:  20k、40k、80k、90k
缓存读取:    经常固定为 7680 token
```

偶尔缓存读取会增加到 18k 左右，但大部分请求无论输入多长，始终只能命中约 7680 token。

这个现象说明缓存功能并非完全没有生效，而是：

> 请求之间只有最前面约 7680 token 保持一致，后面的某个上下文块发生了变化，导致缓存共享前缀在这里中断。

## 2. Prompt Cache 的基本机制

Prompt Cache 复用的是两个请求之间从开头开始连续相同的 token 前缀，而不是在整段 Prompt 中搜索任意相同片段。

假设请求结构为：

```text
system prefix
tool schemas
runtime context
history
current request
```

第一次请求：

```text
A + B + C1 + D1
```

第二次请求：

```text
A + B + C2 + D1 + D2
```

即使 `D1` 完全相同，只要 `C1` 和 `C2` 不同，缓存也只能命中：

```text
A + B
```

后面的 `D1` 不会重新匹配。

因此，`prompt_cache_key` 只能帮助服务端识别和路由同一组缓存，不能让内容不同的 Prompt 强制复用缓存。真正决定缓存长度的是：

```text
从请求开头到第一次 token 差异之间的连续相同内容
```

## 3. 为什么缓存经常停在 7680 Token

### 3.1 动态 Working Memory 位于 history 之前

原来的上下文顺序大致为：

```text
prefix
available skills
working memory
relevant memory
history summary
history
current request
```

Working Memory 中包含：

- `task_summary`
- `recent_files`
- `file_summaries`
- `process_notes`
- `current_todos`
- `active_skills`

这些字段会在工具执行后频繁更新。例如：

```text
read_file
  -> 更新 recent_files
  -> 更新 file_summaries

工具失败
  -> 增加 process_notes

todo_write
  -> 更新 current_todos

skill_load
  -> 更新 active_skills
```

下一轮模型请求时，Working Memory 已经和上一轮不同。由于它位于 history 之前，缓存会在这里中断。

结果是：

```text
稳定 prefix + 稳定 tool schemas
    -> 可以命中

动态 Working Memory
    -> 第一次差异

后面的 history
    -> 即使大量内容相同，也无法继续命中
```

账单中固定出现的约 7680 cached tokens，基本对应了动态上下文之前的稳定系统提示词和工具 schema。

### 3.2 Prefix 曾包含不必要的动态信息

原来的 Prefix 构建还可能受到以下信息影响：

- 当前时间。
- Git status。
- recent commits。
- workspace fingerprint。
- 文件修改后的仓库状态。

如果每次模型请求前重新构建 Prefix，这些信息发生变化时，缓存会在 system prompt 内部更早失效。

特别是秒级时间，即使 Agent 什么都没有修改，两次请求的 Prefix 也可能不同。

### 3.3 Cache Key 的启用曾依赖服务地址

OpenAI-compatible client 曾根据 URL 判断是否发送显式缓存参数，只对少量已知服务地址启用 `prompt_cache_key`。

这会导致使用代理服务或其他兼容端点时：

```text
模型接口本身支持缓存
但客户端没有发送 cache key
```

服务端仍可能进行隐式缓存，因此账单中不是完全零命中，但缓存行为不稳定，也不利于排查。

## 4. 解决方案

这次优化不是简单地移动一个字段，而是重新区分：

```text
模型每轮必须看到的信息
Runtime 自己维护即可的信息
适合通过 history 自然传播的信息
```

### 4.1 Prefix 在 Agent 启动时只构建一次

Prefix 现在在 Agent 初始化阶段构建并保存：

```text
agent 启动
  -> build_prefix()
  -> 计算稳定 prefix hash
  -> 后续模型请求复用相同 prefix
```

运行过程中修改文件、更新 Todo 或执行工具，都不会重新生成 Prefix。

Prefix 中只保留真正稳定且每轮都需要的内容：

- Agent 身份。
- 工具使用规则。
- 工作流与验证规则。
- Commentary 和回答规则。
- 稳定的 workspace 路径。

以下信息不再进入稳定 Prefix：

- Git status。
- recent commits。
- 秒级当前时间。
- 随文件修改变化的 workspace 状态。

当前日期和时区仍可作为低频变化的 Runtime Context 提供，但不会导致同一任务工具循环中的 Prefix 变化。

### 4.2 删除 Working Memory 上下文层

Working Memory 被完全移除，不再在每次请求中渲染以下内容：

- `task_summary`
- `recent_files`
- `file_summaries`
- `process_notes`
- `current_todos`
- `active_skills`

这些字段原本既与 history 重复，又会频繁变化，是破坏缓存共享前缀的主要来源。

删除后，上下文结构变为：

```text
system:
  stable prefix

user runtime message:
  available skills
  runtime context
  relevant memory

messages:
  history summary
  recent history
  current request
```

### 4.3 文件读取状态只保存在 Session

仍然需要防止 Agent 在没有读取文件，或文件已被外部修改的情况下直接覆盖文件。

这个安全需求不需要模型看到，因此改为在 Session 中维护：

```json
{
  "read_files": {
    "/absolute/path/to/file.py": {
      "mtime_ns": 1785390123456789000,
      "size": 12840
    }
  }
}
```

流程为：

```text
read_file 成功
  -> 记录真实绝对路径、mtime_ns 和 size

write_file / patch_file 修改已有文件之前
  -> 再次读取当前 mtime_ns 和 size
  -> 与 session 中状态比较

一致
  -> 允许修改

不存在或不一致
  -> 要求重新 read_file
```

这样既保留了“修改前必须读取最新文件”的安全约束，也不再把文件摘要和读取列表反复发送给模型。

### 4.4 Skill 通过 Tool Result 进入 History

`skill_load` 不再把 Skill 写进每轮都重建的 Working Memory，而是直接返回：

```text
Skill loaded: backend
Root: /absolute/path/to/backend

Instructions:
<完整 SKILL.md>
```

这个工具结果会自然进入 history：

```text
skill_load tool call
  -> tool result 中包含完整指令
  -> 后续请求复用同一段 history
```

Session 只保存最近调用的 3 个 Skill，用于 history compact 后恢复。恢复时：

- recent history 已保留对应 `skill_load`：不重复添加。
- 对应调用已经被压缩：在 summary 后恢复完整 Skill 指令。
- 多次 compact：先清理旧恢复消息，避免重复累积。

`skill_unload` 的内部实现暂时保留，但不暴露给模型，也不出现在默认工具 schema 和 Prefix 中。

### 4.5 Todo 存储在 Session，按需读取

Todo 不再被每轮注入上下文。

现在的职责是：

```text
todo_write
  -> 替换 Session 中的完整 Todo

todo_list
  -> 按需读取当前 Todo

所有阶段完成
  -> 自动清空 Todo
```

history compact 后：

- recent history 中已有当前 `todo_write`：不恢复。
- recent history 中已有能反映当前计划的 `todo_list`：不恢复。
- 当前 Todo 只存在于被压缩的旧 history：在 summary 后恢复一次。

这样 Todo 仍然可以跨工具步骤和会话恢复使用，但不会在每次 Prompt 前制造动态差异。

### 4.6 Relevant Memory 每个用户请求只召回一次

长期记忆仍然放在 Runtime Context 中，但其生命周期被限制为：

```text
新用户请求开始
  -> 召回一次 relevant memory
  -> 同一轮所有工具循环复用相同结果
```

因此，单个长任务内部：

- relevant memory 不会因每次工具调用重新变化。
- Runtime Context 保持稳定。
- 后续请求可以继续复用不断增长的 history 前缀。

不同用户请求可能召回不同记忆，因此跨用户轮次的缓存共享仍可能在 Runtime Context 处中断。这是当前设计接受的权衡：优先优化一个复杂任务内部最频繁、成本最高的工具循环。

### 4.7 所有 OpenAI-Compatible 地址都发送 Cache Key

OpenAI client 不再根据 URL 白名单决定是否启用显式缓存参数。

只要 client 声明支持 Prompt Cache，并且 Runtime 提供了稳定 key，就发送：

```text
prompt_cache_key = prefix_hash
```

`prefix_hash` 由稳定 Prefix 计算。由于 Prefix 不再在工具循环中变化，不需要额外拼接 workspace fingerprint 或动态状态。

## 5. 改造后的缓存行为

一个典型工具循环现在接近：

```text
请求 1:
stable prefix
+ tool schemas
+ stable runtime context
+ user request

请求 2:
stable prefix
+ tool schemas
+ stable runtime context
+ user request
+ assistant tool call
+ tool result

请求 3:
stable prefix
+ tool schemas
+ stable runtime context
+ user request
+ assistant tool call
+ tool result
+ assistant commentary/tool call
+ next tool result
```

同一用户任务内，前面的内容只追加、不改写，因此缓存读取应随着 history 增长，而不是长期固定在 7680 token。

这并不意味着每次请求都能命中全部旧输入：

- 模型服务端可能按固定 token block 计费或展示缓存。
- 第一次请求没有可复用内容。
- 更换模型、服务地址或 cache key 会重新建立缓存。
- history compact 会改写旧 history，compact 后需要重新建立部分缓存。
- 跨用户请求的 relevant memory 可能变化。
- Tool schema、Prefix 或 Available Skills 真正变化时，缓存也会失效。

## 6. 如何验证优化是否生效

### 6.1 检查 Prefix 是否稳定

连续工具循环中检查 trace 的：

```text
prompt_metadata.prefix_hash
prompt_metadata.prompt_cache_key
prompt_metadata.tool_signature
```

同一 Agent 运行期间，这三个字段应保持稳定，除非工具集合确实发生变化。

### 6.2 检查上下文是否只追加

查看连续 `prompt_build` 事件：

- `system` 应保持一致。
- Runtime Context 在同一用户请求内应保持一致。
- history 应增加新的 assistant/tool 消息。
- 不应再出现 Working Memory、`current_todos`、`file_summaries` 或 `process_notes`。

### 6.3 检查服务商账单

不要只观察一次请求。应选择一个至少包含 3 到 5 次模型调用的长任务，记录：

| 请求 | Input tokens | Cached tokens |
| --- | ---: | ---: |
| 首次决策 | 较小 | 0 或较小 |
| 第一次工具结果后 | 增加 | 应开始命中稳定前缀 |
| 第二次工具结果后 | 继续增加 | 应高于或接近上一轮 |
| 后续工具循环 | 持续增加 | 不应长期固定在 7680 |

如果输入已经达到数万 token，缓存仍持续固定在同一个较小数值，应比较相邻两次实际请求，定位第一次不同的消息块，而不是继续修改 cache key。

## 7. 排查过程中容易误判的额外请求

在测试“你好”时，服务商页面显示了两个时间接近的请求：

```text
主请求:
  流式
  输入约 8502 tokens
  输出约 14 tokens

小请求:
  非流式
  输入约 1677 tokens
  输出约 8 tokens
```

这个小请求不是 Prompt Cache 预热，也不是长期记忆召回，而是首次回答完成后的 Session Title 生成。

执行顺序是：

```text
主请求完成
  -> maybe_generate_session_title()
  -> 使用首个用户请求和最终回答生成短标题
```

它的特点是：

- 只在会话第一轮完成后运行一次。
- 不提供工具。
- 非流式请求。
- 最大输出 128 tokens。
- 对“你好”这类请求通常生成“临时对话”。

该次 trace 中长期记忆召回状态为：

```text
status: direct_small
selected_count: 10
duration_ms: 0
```

说明记忆条目数量较少，Runtime 直接在本地加载，没有为召回发起模型请求。

服务商页面可能按完成时间排序，而主请求和标题请求又在同一秒附近完成，所以视觉上容易误以为小请求发生在主请求之前。

## 8. 设计经验

### 8.1 Cache Key 不是缓存正确性的替代品

稳定 key 只能帮助服务端定位缓存。Prompt 内容在前部发生变化时，再稳定的 key 也无法复用后面的 history。

优化顺序应是：

```text
先稳定 Prompt 前缀
  -> 再设置稳定 cache key
  -> 最后通过账单和 trace 验证
```

### 8.2 Runtime 状态不等于模型上下文

文件 freshness、Todo 持久化、Skill 恢复信息都属于 Runtime 必须维护的状态，但不代表它们必须在每轮完整发送给模型。

判断一个字段是否应进入 Prompt，可以问：

```text
模型现在做下一步决策必须看到它吗？
它是否已经存在于 recent history？
它能否由工具按需读取？
它是否只是 Runtime 的安全检查状态？
```

如果后两项成立，通常不应把它放进每轮动态上下文。

### 8.3 对频繁循环优先优化

Relevant Memory 在不同用户请求之间可能变化，但同一复杂任务中的工具循环通常远多于用户轮次。

因此当前设计优先保证：

```text
同一用户请求内 Runtime Context 稳定
history 只追加
缓存命中随工具循环增长
```

这是相较于追求所有请求间绝对稳定，更符合 coding agent 实际成本结构的优化方向。

### 8.4 用 Trace 判断根因，不要只看账单数字

固定命中 7680 token 只能说明“前缀在某处断了”，不能直接说明：

- cache key 无效。
- 服务商不支持缓存。
- Tool schema 太大。
- history 没有被缓存。

必须结合相邻请求的：

- Prefix hash。
- Tool signature。
- Runtime Context。
- 消息顺序。
- 首个不同内容块。

才能判断真正的缓存断点。
