# MCP 工具接入模块：把项目配置中的 MCP server 动态转换成 codemate 工具。
#
# 配置文件位于当前工作区 `.codemate/mcp.json`。启动时读取 server 配置，
# 为每个可用 server 调用 tools/list，然后把每个 MCP tool 包装成
# `mcp__server__tool` 形式的普通工具。MCP 连接运行在独立后台事件循环中，
# 后续工具调用复用已有 session；如果调用失败，则关闭旧连接并重连重试一次。

import asyncio
import concurrent.futures
import json
import os
import re
import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path


MCP_TOOL_PREFIX = "mcp__"
MCP_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str = ""


@dataclass(frozen=True)
class McpToolInfo:
    server: McpServerConfig
    original_name: str
    wrapper_name: str
    description: str
    input_schema: dict


@dataclass
class McpConnection:
    transport_cm: object
    session_cm: object
    session: object
    errlog: object = None


def is_mcp_tool_name(name):
    return str(name or "").startswith(MCP_TOOL_PREFIX)


def _normalize_name(value):
    text = MCP_NAME_RE.sub("_", str(value or "").strip())
    text = text.strip("._-")
    return text or "unnamed"


def _tool_attr(tool, name, default=None):
    if isinstance(tool, dict):
        return tool.get(name, default)
    return getattr(tool, name, default)


def _result_to_text(result):
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if content:
        parts = []
        for item in content:
            text = _tool_attr(item, "text")
            if text is not None:
                parts.append(str(text))
                continue
            data = _tool_attr(item, "data")
            if data is not None:
                parts.append(str(data))
                continue
            parts.append(str(item))
        return "\n".join(parts)
    return str(result)


def load_mcp_config(root):
    path = Path(root) / ".codemate" / "mcp.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError(".codemate/mcp.json must contain an object field named mcpServers")

    configs = []
    for raw_name, raw_config in servers.items():
        if not isinstance(raw_config, dict):
            raise ValueError(f"MCP server {raw_name!r} config must be an object")
        if raw_config.get("disabled") is True:
            continue
        name = _normalize_name(raw_name)
        transport = str(raw_config.get("type") or raw_config.get("transport") or "stdio").strip().lower()
        if transport == "streamable_http":
            transport = "http"
        if transport not in {"stdio", "http", "sse"}:
            raise ValueError(f"MCP server {raw_name!r} transport must be one of: stdio, http, sse")
        if transport == "stdio":
            command = str(raw_config.get("command", "")).strip()
            if not command:
                raise ValueError(f"MCP stdio server {raw_name!r} requires command")
            args = tuple(str(item) for item in raw_config.get("args", []) or [])
            env = raw_config.get("env")
            if env is not None:
                if not isinstance(env, dict):
                    raise ValueError(f"MCP stdio server {raw_name!r} env must be an object")
                env = {str(key): str(value) for key, value in env.items()}
            configs.append(McpServerConfig(name=name, transport=transport, command=command, args=args, env=env))
            continue
        url = str(raw_config.get("url", "")).strip()
        if not url:
            raise ValueError(f"MCP {transport} server {raw_name!r} requires url")
        configs.append(McpServerConfig(name=name, transport=transport, url=url))
    return configs


class McpManager:
    """当前 agent 的 MCP 连接和后台事件循环管理器。

    MCP SDK 的 stdio/http/sse 客户端依赖异步上下文管理器，连接创建、工具调用和关闭必须
    在稳定的事件循环里完成。这里启动一个后台 loop，并用单 worker 队列串行执行所有 MCP
    协程，保证同一个连接的 enter/use/exit 都发生在同一个 asyncio task 中。
    """

    def __init__(self, configs):
        self.configs = {config.name: config for config in configs}
        self.connections = {}
        self.closed = False
        self.loop = asyncio.new_event_loop()
        self.queue = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, name="codemate-mcp-loop", daemon=True)
        self.thread.start()
        self.ready.wait()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.queue = asyncio.Queue()
        self.loop.create_task(self._worker())
        self.ready.set()
        self.loop.run_forever()

    async def _worker(self):
        while True:
            async_fn, args, future = await self.queue.get()
            if async_fn is None:
                future.set_result(None)
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = await async_fn(*args)
            except Exception as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def run(self, async_fn, *args, timeout=None):
        if self.closed:
            raise RuntimeError("MCP manager is closed")
        future = concurrent.futures.Future()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (async_fn, args, future))
        return future.result(timeout=timeout)

    async def connect(self, server_name):
        try:
            from mcp import ClientSession, StdioServerParameters
        except ImportError as exc:
            raise RuntimeError("MCP support requires the 'mcp' Python package") from exc

        config = self.configs[server_name]
        errlog = None
        if config.transport == "stdio":
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=config.command, args=list(config.args), env=config.env)
            # MCP server 的 stderr 是运行日志，不属于协议内容；不要让它污染交互式终端。
            errlog = open(os.devnull, "w", encoding="utf-8")
            transport_cm = stdio_client(params, errlog=errlog)
        elif config.transport == "http":
            from mcp.client.streamable_http import streamablehttp_client

            transport_cm = streamablehttp_client(config.url)
        else:
            from mcp.client.sse import sse_client

            transport_cm = sse_client(config.url)

        transport_entered = False
        session_cm = None
        try:
            streams = await transport_cm.__aenter__()
            transport_entered = True
            read_stream, write_stream = streams[0], streams[1]
            session_cm = ClientSession(read_stream, write_stream)
            session = await session_cm.__aenter__()
            await session.initialize()
        except Exception:
            if session_cm is not None:
                await session_cm.__aexit__(None, None, None)
            if transport_entered:
                await transport_cm.__aexit__(None, None, None)
            if errlog is not None:
                errlog.close()
            raise

        connection = McpConnection(transport_cm=transport_cm, session_cm=session_cm, session=session, errlog=errlog)
        self.connections[server_name] = connection
        return connection

    async def ensure_connected(self, server_name):
        if server_name not in self.configs:
            raise ValueError(f"MCP server not configured: {server_name}")
        connection = self.connections.get(server_name)
        if connection is not None:
            return connection.session
        connection = await self.connect(server_name)
        return connection.session

    async def list_tools(self, server_name):
        session = await self.ensure_connected(server_name)
        result = await session.list_tools()
        return list(getattr(result, "tools", []) or [])

    async def call_tool(self, server_name, tool_name, arguments):
        try:
            session = await self.ensure_connected(server_name)
            return await session.call_tool(tool_name, arguments or {})
        except Exception:
            await self.close_server(server_name)
            session = await self.ensure_connected(server_name)
            return await session.call_tool(tool_name, arguments or {})

    async def close_server(self, server_name):
        connection = self.connections.pop(server_name, None)
        if connection is None:
            return
        try:
            await connection.session_cm.__aexit__(None, None, None)
        finally:
            try:
                await connection.transport_cm.__aexit__(None, None, None)
            finally:
                if connection.errlog is not None:
                    connection.errlog.close()

    async def close_all(self):
        for server_name in list(self.connections):
            await self.close_server(server_name)

    def close(self):
        if self.closed:
            return
        try:
            self.run(self.close_all)
        finally:
            self.closed = True
            future = concurrent.futures.Future()
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (None, (), future))
            future.result(timeout=5)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()


def get_mcp_manager(agent):
    manager = getattr(agent, "_mcp_manager", None)
    if manager is None:
        manager = McpManager(load_mcp_config(agent.root))
        agent._mcp_manager = manager
    return manager


def discover_mcp_tools(agent):
    manager = get_mcp_manager(agent)
    tools = []
    errors = []
    for config in manager.configs.values():
        try:
            server_tools = manager.run(manager.list_tools, config.name)
        except Exception as exc:
            errors.append(f"{config.name}: {exc}")
            continue
        for tool in server_tools:
            original_name = str(_tool_attr(tool, "name", "")).strip()
            if not original_name:
                continue
            wrapper_name = f"{MCP_TOOL_PREFIX}{config.name}__{_normalize_name(original_name)}"
            description = str(_tool_attr(tool, "description", "") or "").strip()
            input_schema = _tool_attr(tool, "inputSchema") or _tool_attr(tool, "input_schema") or {}
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}
            tools.append(
                McpToolInfo(
                    server=config,
                    original_name=original_name,
                    wrapper_name=wrapper_name,
                    description=description or f"MCP tool {original_name} from server {config.name}.",
                    input_schema=input_schema,
                )
            )
    agent.mcp_load_errors = errors
    return tools


def run_mcp_tool(agent, tool_info, args):
    manager = get_mcp_manager(agent)
    result = manager.run(manager.call_tool, tool_info.server.name, tool_info.original_name, args or {})
    return _result_to_text(result)


def close_mcp_connections(agent):
    manager = getattr(agent, "_mcp_manager", None)
    if manager is not None:
        manager.close()


def build_mcp_tool_registry(agent):
    registry = {}
    for tool_info in discover_mcp_tools(agent):
        registry[tool_info.wrapper_name] = {
            "input_schema": tool_info.input_schema,
            "description": tool_info.description,
            "risky": True,
            "mcp_server": tool_info.server.name,
            "mcp_tool": tool_info.original_name,
            "run": partial(run_mcp_tool, agent, tool_info),
        }
    return registry
