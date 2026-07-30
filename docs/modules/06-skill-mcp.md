# Skill 和 MCP 模块笔记

## 1. 模块定位

Skill 和 MCP 都扩展 Codemate 的能力，但边界不同：

- Skill 提供“任务应该怎么做”的工作说明和配套资源。
- MCP 提供“能够调用什么外部服务”的动态工具。

Skill 通过 `skill_load` 进入 history；MCP 通过工具注册表进入模型 schema。二者都经过统一的 runtime、trace 和权限流程。

## 2. Skill 目录

支持项目级和用户级 Skill：

```text
<workspace>/.codemate/skills/<name>/SKILL.md
~/.codemate/skills/<name>/SKILL.md
```

同名时项目级覆盖用户级。目录会在初始化时自动创建。

`SKILL.md` 使用 frontmatter：

```markdown
---
name: ai-coding-backend
description: Structured workflow for Python backend/API projects.
---

具体指令...
```

目录名必须与 frontmatter `name` 一致，description 不能为空且最多展示 250 字符。

## 3. Skill 发现与调用

模型首先看到：

```text
Available skills:
- ai-coding-backend: Structured workflow for Python backend/API projects.
```

这里只放名称和 description，不提前发送完整正文。匹配任务后，模型调用：

```json
{"name": "ai-coding-backend"}
```

`skill_load` 返回：

```text
Skill loaded: ai-coding-backend
Root: /absolute/path/to/ai-coding-backend

Instructions:
<完整 SKILL.md>
```

完整结果直接进入 history。Skill 中的 `scripts/`、`references/`、`examples/` 和 `templates/` 均相对于返回的 root。

## 4. Skill 状态与 Compact

Session 保存最近调用的三个不同 Skill，包括 name、root 和完整正文。重复调用同名 Skill 会刷新内容和顺序；调用第四个时淘汰最早记录。

History compact 后：

- retained recent history 中仍有成功 `skill_load` 的 Skill 不重复恢复。
- 只恢复 recent history 已经缺失的 Skill。
- 最多恢复三个，正文不截断。
- 重复 compact 会删除旧 `skill_context` 后重新计算，避免累积。

## 5. Skill 的设计理由

把完整 Skill 每轮重复放进固定上下文会破坏缓存，也会长期占用 token；只保留一次工具结果又可能在 compact 后丢失。当前方案采用“历史追加 + compact 按需恢复”：

- 平时只有一次正文成本。
- 动态内容位于 history 尾部，更有利于缓存稳定。
- Compact 后仍能恢复当前任务需要的说明。
- 最近三个的边界防止无限增长。

## 6. MCP 配置

MCP 配置位于用户级或项目级 `settings.json` 的 `mcp` 字段。项目配置覆盖同名用户配置。

stdio 示例：

```json
{
  "mcp": {
    "tavily": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "tavily-mcp"],
      "env": {
        "TAVILY_API_KEY": "${TAVILY_API_KEY}"
      }
    }
  }
}
```

HTTP 示例：

```json
{
  "mcp": {
    "demo": {
      "transport": "streamable_http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

配置中的 secret 环境变量在 trace 和日志中脱敏。

## 7. MCP Transport

### stdio

Runtime 启动子进程，通过标准输入输出传输 MCP 消息。服务端业务日志必须写 stderr，否则会污染协议 stdout。

### streamable_http

Runtime 连接 HTTP MCP endpoint，适合独立运行或远程部署的服务。

### sse

保留旧版 SSE 兼容，用于尚未迁移到 Streamable HTTP 的服务。

## 8. 动态工具发现

Agent 启动时对每个启用的 MCP server：

1. 建立连接并初始化 session。
2. 调用 `list_tools`。
3. 将 schema 转为模型工具。
4. 注册为 `mcp__<server>__<tool>`。
5. 保存连接供后续调用复用。

发现失败只记录到 `mcp_load_errors`，不会阻止 Agent 使用内置工具启动。

## 9. MCP 连接生命周期

MCP session、异步上下文管理器和底层 transport 必须始终运行在同一个长期事件循环和线程中。Runtime 使用专用后台 loop：

```text
connect
list_tools
call_tool
close
```

同步 runtime 通过 `asyncio.run_coroutine_threadsafe()` 把异步操作提交到该 loop。连接断开时按需重连，正常退出时由 `agent.close()` 统一关闭 session、transport 和事件循环。

这避免了异步上下文在一个 task 中进入、却在另一个 task 中退出导致的 AnyIO cancel scope 错误。

## 10. MCP 日志

stdio server 的 stderr 由 runtime 接收。为了避免服务端启动提示反复刷终端：

- 默认写入日志文件。
- 只有加载失败等必要错误进入用户界面。
- stdout 严格保留给 MCP 协议。

## 11. MCP 权限

MCP 工具能力由外部 server 定义，runtime 无法仅根据名称证明是否只读，因此默认采用保守策略：

- MCP 工具标为较高风险。
- 按审批策略决定 allow 或 ask。
- 工具调用和结果写入 history、trace。
- 配置中禁用的 server 不连接、不发现工具。

Web 内置工具与 MCP 分开管理；Web 工具具有已知参数和行为边界，不需要套用所有 MCP 的未知能力策略。

## 12. 设计难点

### Skill 资源路径

`SKILL.md` 中的相对资源属于 Skill 包，而不是 workspace。工具结果显式返回绝对 root，让模型能够稳定定位配套脚本和参考文件。

### Skill 与缓存

完整正文每轮重复注入会让缓存前缀在加载后永久变化。正文改为一次工具结果，并在 compact 后按需恢复，减少重复 token。

### MCP 能力不可预测

内置工具可以人工分类风险，MCP 工具来自外部服务。统一命名、保守审批和完整 trace 提供了可审计边界。

### MCP 异步生命周期

连接不能跨临时 event loop 使用。专用长期 loop 统一承担建立、调用、重连和关闭。

## 13. 面试复述

Codemate 使用 Skill 扩展工作方法，使用 MCP 扩展外部工具。Skill 未调用时只展示 name 和 description；`skill_load` 返回完整正文和绝对 root，结果进入 history，compact 后仅恢复 recent history 缺失的最近三个 Skill。

MCP 支持 stdio、Streamable HTTP 和旧版 SSE。Agent 启动时连接服务并动态发现工具，后续复用同一 session；所有异步生命周期固定在专用后台事件循环中，退出时统一关闭。动态 MCP 工具采用保守审批并写入 trace，避免外部能力绕过 runtime 控制。
