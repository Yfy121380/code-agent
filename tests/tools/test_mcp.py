"""MCP 工具接入测试。

覆盖模块：tools.mcp、tools.registry、validators。
重点边界：动态工具注册、审批策略、read_only 拒绝、配置发现、session 复用、调用失败后重连。
"""

from dataclasses import dataclass
import asyncio
from unittest.mock import patch

import pytest

from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate.config import ensure_codemate_layout
from codemate.tools.mcp import McpConnection, McpManager, McpServerConfig, McpToolCallError, McpToolInfo


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    ensure_codemate_layout(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def fake_mcp_tool():
    server = McpServerConfig(name="notes", transport="http", url="http://localhost:3000/mcp")
    return McpToolInfo(
        server=server,
        original_name="create_note",
        wrapper_name="mcp__notes__create_note",
        description="Create a note in the external notes service.",
        input_schema={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    )


def test_mcp_tool_is_registered_and_exposed_to_model(tmp_path):
    with patch("codemate.tools.mcp.discover_mcp_tools", return_value=[fake_mcp_tool()]):
        agent = build_agent(tmp_path)

    assert "mcp__notes__create_note" in agent.tools
    model_tool = next(tool for tool in agent.model_tools() if tool["name"] == "mcp__notes__create_note")
    assert model_tool["description"] == "Create a note in the external notes service."
    assert model_tool["input_schema"]["properties"]["title"]["type"] == "string"
    assert model_tool["risky"] is True


def test_mcp_tool_auto_policy_still_asks_for_approval(tmp_path):
    with patch("codemate.tools.mcp.discover_mcp_tools", return_value=[fake_mcp_tool()]):
        agent = build_agent(tmp_path, approval_policy="auto")

    result = agent.run_tool("mcp__notes__create_note", {"title": "demo"})

    assert result == "error: approval denied for mcp__notes__create_note"
    assert agent._last_tool_result_metadata["approval_gate"] == "ask"
    assert agent._last_tool_result_metadata["approval_reason"] == "mcp_default_ask"
    assert agent._last_tool_result_metadata["mcp_server"] == "notes"
    assert agent._last_tool_result_metadata["mcp_tool"] == "create_note"


def test_mcp_tool_full_policy_runs_wrapper(tmp_path):
    with patch("codemate.tools.mcp.discover_mcp_tools", return_value=[fake_mcp_tool()]):
        agent = build_agent(tmp_path, approval_policy="full")

    async def fake_call_tool(self, server_name, tool_name, arguments):
        return "created"

    with patch("codemate.tools.mcp.McpManager.call_tool", fake_call_tool):
        result = agent.run_tool("mcp__notes__create_note", {"title": "demo"})

    assert result == "created"
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"
    assert agent._last_tool_result_metadata["approval_reason"] == "mcp_full"
    assert agent._last_tool_result_metadata["mcp_server"] == "notes"


def test_mcp_tool_read_only_policy_rejects_in_validation(tmp_path):
    with patch("codemate.tools.mcp.discover_mcp_tools", return_value=[fake_mcp_tool()]):
        agent = build_agent(tmp_path, approval_policy="read_only")

    result = agent.run_tool("mcp__notes__create_note", {"title": "demo"})

    assert result == "error: MCP tools are blocked in read-only mode"
    assert agent._last_tool_result_metadata["tool_error_code"] == "mcp_read_only_block"


@dataclass
class FakeMcpTool:
    name: str
    description: str
    inputSchema: dict


def test_mcp_config_discovery_builds_wrapped_tool(tmp_path):
    config_dir = tmp_path / ".codemate"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "settings.json").write_text(
        """
        {
          "mcp": {
            "servers": {
              "notes": {
                "type": "http",
                "url": "http://localhost:3000/mcp"
              }
            }
          },
          "permissions": {
            "read": {"allow": [], "deny": []},
            "write": {"allow": [], "deny": []}
          }
        }
        """,
        encoding="utf-8",
    )

    server_tool = FakeMcpTool(
        name="create-note",
        description="Create a note.",
        inputSchema={"type": "object", "properties": {}},
    )
    async def fake_list_tools(self, server_name):
        return [server_tool]

    with patch("codemate.tools.mcp.McpManager.list_tools", fake_list_tools):
        agent = build_agent(tmp_path)

    assert "mcp__notes__create-note" in agent.tools
    assert agent.tools["mcp__notes__create-note"]["mcp_server"] == "notes"
    assert agent.tools["mcp__notes__create-note"]["mcp_tool"] == "create-note"


class FakeSession:
    def __init__(self, tools=None, fail_once=False):
        self.tools = tools or []
        self.fail_once = fail_once
        self.calls = []

    async def initialize(self):
        return None

    async def list_tools(self):
        return type("ToolList", (), {"tools": self.tools})()

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("connection closed")
        return "called"


class FakeContextManager:
    def __init__(self, value):
        self.value = value
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self):
        self.enter_count += 1
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_count += 1


def test_mcp_manager_reuses_session_for_list_and_call():
    config = McpServerConfig(name="notes", transport="http", url="http://localhost:3000/mcp")
    manager = McpManager([config])
    session = FakeSession(tools=[FakeMcpTool("create", "Create.", {"type": "object"})])
    transport_cm = FakeContextManager(None)
    session_cm = FakeContextManager(None)

    async def fake_connect(server_name):
        connection = McpConnection(transport_cm=transport_cm, session_cm=session_cm, session=session)
        manager.connections[server_name] = connection
        return connection

    manager.connect = fake_connect  # type: ignore[method-assign]

    try:
        listed = manager.run(manager.list_tools, "notes")
        result = manager.run(manager.call_tool, "notes", "create", {"title": "demo"})
    finally:
        manager.close()

    assert listed[0].name == "create"
    assert result == "called"
    assert session.calls == [("create", {"title": "demo"})]
    assert session_cm.exit_count == 1
    assert transport_cm.exit_count == 1


def test_mcp_manager_does_not_retry_call_after_remote_failure():
    config = McpServerConfig(name="notes", transport="http", url="http://localhost:3000/mcp")
    manager = McpManager([config])
    first = FakeSession(fail_once=True)
    sessions = [first]
    transport_cm = FakeContextManager(None)
    session_cm = FakeContextManager(None)

    async def fake_connect(server_name):
        session = sessions.pop(0)
        connection = McpConnection(transport_cm=transport_cm, session_cm=session_cm, session=session)
        manager.connections[server_name] = connection
        return connection

    manager.connect = fake_connect  # type: ignore[method-assign]

    try:
        with pytest.raises(McpToolCallError, match="was not retried"):
            manager.run(manager.call_tool, "notes", "create", {"title": "demo"})
    finally:
        manager.close()

    assert first.calls == [("create", {"title": "demo"})]
    assert sessions == []
    assert session_cm.exit_count == 1
    assert transport_cm.exit_count == 1


def test_mcp_manager_bounds_async_operation_time():
    manager = McpManager([])

    async def hang():
        await asyncio.sleep(1)

    try:
        with pytest.raises(TimeoutError):
            manager.run(hang, timeout=0.01)
    finally:
        manager.close()
