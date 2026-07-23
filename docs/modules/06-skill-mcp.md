# Skill 和 MCP 模块笔记

## 1. 模块定位

Skill 和 MCP 都是 Codemate 的能力扩展机制，但二者解决的问题不同。

- **Skill** 扩展的是“怎么做事”的知识，比如某类项目的开发流程、代码审查方式、论文总结模板、特定技术栈的工作习惯。
- **MCP** 扩展的是“能调用什么外部工具”，比如搜索服务、数据库工具、浏览器工具、公司内部系统工具等。

二者共同的设计原则是：扩展能力不能绕过 Codemate 的上下文、工具、权限和 trace 体系。Skill 进入 working memory，MCP 进入 tool registry；它们都不是直接让模型获得无限能力。

## 2. Skill 的作用

Skill 用来保存某类任务的专门说明和资源。它更像一个可加载的工作手册，而不是长期记忆。

适合放进 skill 的内容包括：

- 特定任务的标准工作流。
- 某类项目的实现步骤。
- 代码生成或评审规范。
- 需要复用的脚本、模板、示例和参考资料。
- 针对某个领域的注意事项。

不适合放进 skill 的内容包括：

- 当前任务的临时状态。
- 某次对话中的短期发现。
- 用户跨项目偏好。
- 会频繁变化的项目运行结果。

这些信息分别应该进入 working memory、history、长期记忆或 trace。

## 3. Skill 存储位置

Codemate 当前支持两级 skill：

```text
项目级：
<workspace>/.codemate/skills/<skill-name>/SKILL.md

用户级：
~/.codemate/skills/<skill-name>/SKILL.md
```

启动时会自动创建这些目录：

- 项目内 `.codemate/skills`
- 用户级 `~/.codemate/skills`

发现 skill 时，Codemate 会同时扫描用户级和项目级目录。如果用户级和项目级存在同名 skill，项目级会覆盖用户级。这样既支持全局复用，也支持单项目定制。

## 4. SKILL.md 格式要求

每个 skill 目录下必须有 `SKILL.md`。文件开头需要 frontmatter，至少包含：

```markdown
---
name: ai-coding-backend
description: Structured workflow for Python backend/API projects.
---

具体 skill 指令...
```

当前发现规则包括：

- skill 目录名必须是合法名称。
- `SKILL.md` 必须存在。
- frontmatter 中必须有 `name`。
- frontmatter 的 `name` 必须和目录名一致。
- 必须有非空 `description`。
- description 最长 250 字符，超过会截断。

要求 `SKILL.md` 内部显式写 `name` 的原因是：目录名虽然可以作为本地实现的优先来源，但标准 skill 文件本身应该是自描述的。这样以后迁移到其他 agent 或工具生态时，不会依赖 Codemate 的目录约定。

## 5. Available Skills

模型每轮会看到一个 `Available skills` section，其中只包含 skill 的 name 和 description，例如：

```text
Available skills:
- ai-coding-backend: Structured workflow for Python backend/API projects.
- paper-summary: ...
```

这里不放完整 `SKILL.md` 正文。原因是 skill 内容可能很长，如果全部提前塞进上下文，会挤占 history 和 working memory。

这个列表只解决“模型知道有哪些 skill 可选”的问题。模型判断某个 skill 和当前任务明显相关时，再调用 `skill_load`。

## 6. Skill 生命周期

Skill 的生命周期是：

```text
扫描 skill 目录
  -> 渲染 Available skills
  -> 模型判断当前任务需要某个 skill
  -> skill_load
  -> 完整 SKILL.md 进入 working memory
  -> 模型按 skill 指令执行当前任务
  -> 用户切换到无关任务时 skill_unload
```

`skill_load` 会做几件事：

- 检查 skill 是否存在。
- 检查是否已经 active，防止重复加载。
- 检查 active skill 数量，当前最多 3 个。
- 读取完整 `SKILL.md`。
- 校验 frontmatter name 和目录名一致。
- 把 skill 存入 session 的 `active_skills`。
- 在 trace 中记录 `skill_loaded`。

`skill_unload` 会做几件事：

- 检查 skill 当前是否 active。
- 从 session 的 `active_skills` 删除。
- 记录卸载原因。
- 在 trace 中记录 `skill_unloaded`。

设计上不要求一个请求完成后立刻卸载 skill。更合理的规则是：当用户切换到无关任务、skill 加载错误、或当前任务方向变化导致 skill 不再适用时再卸载。

## 7. Active Skill 如何进入上下文

已加载 skill 会进入 working memory，而不是放在 available skills 列表里。

Working memory 中会展示：

- skill name。
- skill root。
- skill-relative 资源说明。
- 完整 `SKILL.md` 内容。

这样设计是为了让 skill 能在多轮复杂任务中持续生效。如果 skill 只作为一次 tool result 出现在 history，随着后续工具调用增加，它很快可能被挤掉或 compact 掉；放在 working memory 则说明它是当前任务仍然相关的操作指南。

同时，active skill 也不能永久保留。否则无关任务也会被旧 skill 影响，并且占用上下文。因此卸载由模型通过 `skill_unload` 显式完成，runtime 则负责防止重复加载和数量失控。

## 8. Skill Root 和资源定位

Skill root 是 skill 目录的绝对路径，这个路径会写在 Active Skill 中,例如：

```text
Name: ai-coding-backend
Root: /home/pea/.codemate/skill/ai-coding-backend
Skill-relative resources such as scripts/, references/, examples/, and templates/ are under this root.
Instructions:
```

`SKILL.md` 中提到的相对资源都基于这个 root 查找：

- `scripts/`
- `references/`
- `examples/`
- `templates/`

例如 `SKILL.md` 写：

```text
Run scripts/check_project.py before final review.
```

模型应该理解为：

```text
<skill-root>/scripts/check_project.py
```

而不是 workspace 下的 `scripts/check_project.py`。

这点很重要。Skill 是一个自带资源包的能力单元，不只是一个 markdown 文件。明确 root 可以避免模型把 skill 资源和项目文件混淆。

## 9. Skill 的设计取舍

Skill 加载策略的核心取舍是上下文成本和任务连续性。

如果只把 skill 作为工具结果放进 history：

- 优点是简单。
- 缺点是复杂任务多轮执行后容易丢失，不使用时还会占用上下文。


Codemate 采用折中方案：

- 未加载时只展示 name/description。
- 需要时加载完整 skill 到 working memory。
- 任务切换后由模型卸载。
- runtime 限制重复加载和 active 数量。

这样可以让 skill 在相关任务中持续可用，又避免长期污染上下文。

## 10. MCP 的作用

MCP 用来接入外部工具服务。它和内置工具不同：内置工具由 Codemate 自己定义 schema 和执行逻辑；MCP 工具由外部 server 提供 schema、description 和执行能力。

适合通过 MCP 接入的能力包括：

- 搜索或资料系统。
- 浏览器自动化。
- 数据库或 BI 查询。
- 公司内部系统。
- 第三方工具平台。

MCP 的价值是让 Codemate 不需要为每个外部系统写死一套工具，而是通过标准协议动态发现和调用。

## 11. MCP 配置

MCP 配置放在 settings 中：

```json
{
  "mcp": {
    "servers": {
      "tavily-mcp": {
        "command": "npx",
        "args": ["-y", "tavily-mcp"],
        "env": {
          "TAVILY_API_KEY": "..."
        }
      }
    }
  }
}
```

支持用户级和项目级 settings：

```text
用户级：
~/.codemate/settings.json

项目级：
<workspace>/.codemate/settings.json
```

配置合并时，用户级先加载，项目级后加载。同名 MCP server 会被项目级配置覆盖。这样可以在用户级放通用 MCP，在项目级放当前项目需要的特殊 MCP。

## 12. MCP 支持的连接方式

当前支持三种 transport：

### stdio

本地启动 MCP server，通过标准输入输出通信。

典型配置：

```json
{
  "command": "npx",
  "args": ["-y", "some-mcp-server"],
  "env": {
    "API_KEY": "..."
  }
}
```

如果没有显式写 `type` 或 `transport`，默认按 `stdio` 处理。

### http / streamable_http

连接远程或本地 HTTP MCP server。

典型配置：

```json
{
  "transport": "http",
  "url": "http://127.0.0.1:8000/mcp"
}
```

`streamable_http` 会被规范化为 `http`。

### sse

兼容旧版 SSE MCP server。

典型配置：

```json
{
  "transport": "sse",
  "url": "http://127.0.0.1:8000/sse"
}
```

SSE 已经偏旧，但保留支持有利于兼容已有 MCP server。

## 13. MCP 动态工具发现

MCP 工具加载流程是：

```text
读取 settings.json
  -> 合并 mcp.servers
  -> 为每个 server 建立 MCP session
  -> session.list_tools()
  -> 读取每个工具的 name / description / input_schema
  -> 包装成 Codemate 工具
  -> 合并进 tool registry
```

包装后的工具名规则是：

```text
mcp__<server-name>__<tool-name>
```

例如：

```text
mcp__tavily-mcp__tavily_search
```

模型看到的是这个 wrapper name。真正执行时，Codemate 会根据 wrapper 找到：

- 对应 MCP server。
- 原始 MCP tool name。
- 已连接的 MCP session。

然后调用：

```text
session.call_tool(original_tool_name, arguments)
```

MCP 返回结果后，再转换成普通文本 tool result 写回 history。

## 14. MCP 连接生命周期

MCP SDK 的 stdio/http/sse client 基于异步上下文管理器。连接创建、工具调用和关闭必须在稳定的事件循环中完成，否则容易出现 async context manager 在一个 loop/task 里进入、在另一个 loop/task 里退出的问题。

Codemate 的处理方式是：

- 为当前 agent 创建一个 MCP manager。
- MCP manager 启动一个后台 asyncio event loop。
- 所有 MCP 操作通过一个单 worker 队列串行提交到这个 loop。
- 发现工具时连接 server 并复用 session。
- 调用工具时优先复用已有 session。
- 如果调用失败，关闭该 server 的旧连接，重连后重试一次。
- agent close 时关闭所有 MCP session、transport 和后台 loop。

这样避免每次工具调用都重新连接，也避免异步资源跨 event loop 关闭导致的异常。

## 15. MCP 日志处理

stdio MCP server 的 stderr 常常会输出运行日志，例如：

```text
Tavily MCP server running on stdio
```

这些日志不是 MCP 协议内容，也不是 agent 的用户可见回答。如果直接继承终端 stderr，会污染交互界面。

因此 stdio transport 会把 server stderr 重定向到 `os.devnull`。真正的工具结果只来自 MCP 协议返回内容。

## 16. MCP 权限策略

MCP 工具默认比内置低风险工具更保守。

原因是：MCP 工具的能力来自外部 server，Codemate 不能仅凭名称判断它是否只读、是否访问网络、是否修改外部系统、是否读取本地敏感数据。

当前策略：

- `ask`：MCP 默认需要询问。
- `auto`：MCP 仍然需要询问。
- `full`：MCP 自动放行。
- `read_only`：MCP 直接拒绝。

MCP 工具虽然进入统一 tool registry，但不会因为“看起来像工具”就继承内置工具的低风险判断。

## 17. Skill 和 MCP 的关系

Skill 和 MCP 可以配合，但职责不同。

一个 skill 可以告诉模型：

- 当前任务应该使用哪个 MCP 工具。
- 使用某个 MCP 工具前应该准备什么参数。
- MCP 返回结果应该如何解读。
- 什么时候不应该调用 MCP。

但 skill 本身不执行 MCP。真正执行仍然要走工具调用、权限 gate、审批、trace 和 history。

这种分层可以避免 skill 绕过安全边界：skill 只是指导，MCP 才是外部动作。

## 18. 设计难点

### Skill 资源路径容易混淆

模型看到 `scripts/run.py` 时，可能误以为是 workspace 下的脚本。

解决方式是：active skill 在 working memory 中明确展示绝对 Root，并说明相对资源都在该 Root 下。

### MCP 工具能力不可预测

MCP 工具由外部 server 提供，Codemate 无法提前知道它的真实风险。

解决方式是：动态发现 schema，但权限默认 ask；read_only 下拒绝；full 才自动通过。

### MCP 异步生命周期容易出错

如果每次调用都创建新 event loop，或者跨 loop 关闭 async context manager，stdio/http/sse client 都可能报错。

解决方式是：MCP manager 使用后台长期 event loop 和单 worker 队列，保证连接、调用、关闭都在同一条异步执行链路中。

## 19. 面试复述版本

Codemate 的扩展能力分为 Skill 和 MCP 两类。Skill 解决“任务应该怎么做”，MCP 解决“外部工具怎么接入”。Skill 以 `SKILL.md` 形式放在项目级或用户级 skills 目录中，未加载时只把 name 和 description 展示给模型；模型判断相关后调用 `skill_load`，完整 skill 进入 working memory，并通过 skill root 定位 scripts、references、examples 等资源。任务切换到无关方向时再用 `skill_unload` 卸载，避免长期污染上下文。

MCP 则从 settings 中读取 server 配置，支持 stdio、http/streamable_http 和旧版 sse。启动后连接 server，调用 `tools/list` 动态发现工具，再包装成 `mcp__server__tool` 形式合并到工具注册表。调用时根据 wrapper 找回原始 server 和 tool name，通过 MCP session 执行。为了避免重复连接和异步关闭问题，MCP 连接由后台长期 event loop 管理，调用失败时重连重试一次。

二者都不会绕过 Codemate 的统一控制体系：Skill 进入 working memory，MCP 进入 tool registry；MCP 默认需要审批，trace/history 会记录加载、卸载和工具调用结果。
