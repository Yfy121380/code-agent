"""路径权限与临时审批规则测试。

覆盖模块：tools/path_policy、tools/validators、runtime/approvals。
重点边界：workspace 外读写、symlink 解析、ask/auto 差异、临时 allow 持久化、deny 优先。
"""

import json

from unittest.mock import patch

from codemate import FakeModelClient, MiniAgent
from tests.helpers import RememberingApprovalUI, build_agent, build_workspace


def test_outside_workspace_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": f"../../{outside.name}"})

    assert "outside" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"

def test_symlink_to_outside_workspace_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "outside" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"

def test_grep_outside_workspace_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside-grep"
    outside.mkdir(exist_ok=True)
    (outside / "note.txt").write_text("abc\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("grep", {"pattern": "abc", "path": f"../../{outside.name}", "mode": "content"})

    assert "abc" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"

def test_outside_workspace_home_read_auto_policy_allows(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-home.txt"
    outside.write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.path_policy.Path.home", return_value=tmp_path.parent):
        result = agent.run_tool("read_file", {"path": f"../{outside.name}", "start": 1, "end": 1})

    assert "outside" in result
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"

def test_outside_workspace_home_write_auto_policy_asks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-write.txt"
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("codemate.tools.path_policy.Path.home", return_value=tmp_path.parent):
        result = agent.run_tool("write_file", {"path": f"../{outside.name}", "content": "outside\n"})

    assert result == "error: approval denied for write_file"
    assert not outside.exists()
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "ask"

def test_outside_workspace_write_uses_rules_and_approval_instead_of_path_denial(tmp_path):
    outside = tmp_path.parent.parent / f"{tmp_path.name}-outside-write.txt"
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("write_file", {"path": f"../../{outside.name}", "content": "outside\n"})

    assert result == "error: approval denied for write_file"
    assert not outside.exists()
    assert agent._last_tool_result_metadata["outside_workspace"] is True
    assert agent._last_tool_result_metadata["approval_gate"] == "ask"
    assert agent._last_tool_result_metadata["tool_error_code"] == "approval_denied"

def test_approval_can_add_temporary_write_allow_for_session(tmp_path):
    ui = RememberingApprovalUI()
    agent = build_agent(tmp_path, [], approval_policy="ask", ui=ui)

    first = agent.run_tool("write_file", {"path": "first.txt", "content": "one\n"})
    second = agent.run_tool("write_file", {"path": "second.txt", "content": "two\n"})

    assert first == "wrote first.txt (4 chars)"
    assert second == "wrote second.txt (4 chars)"
    assert len(ui.calls) == 1
    assert ui.calls[0]["metadata"]["approval_access"] == "write"
    assert ui.calls[0]["metadata"]["suggested_allow_dir"] == str(tmp_path.resolve())

def test_temporary_permission_persists_when_session_resumes(tmp_path):
    first_ui = RememberingApprovalUI()
    agent = build_agent(tmp_path, [], approval_policy="ask", ui=first_ui)

    first = agent.run_tool("write_file", {"path": "first.txt", "content": "one\n"})
    session_id = agent.session["id"]
    saved = json.loads(agent.session_store.path(session_id).read_text(encoding="utf-8"))

    second_ui = RememberingApprovalUI()
    resumed = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session=agent.session_store.load(session_id),
        approval_policy="ask",
        ui=second_ui,
    )
    second = resumed.run_tool("write_file", {"path": "second.txt", "content": "two\n"})

    assert first == "wrote first.txt (4 chars)"
    assert second == "wrote second.txt (4 chars)"
    assert saved["temporary_permissions"]["permissions"]["write"]["allow"] == [str(tmp_path.resolve())]
    assert len(first_ui.calls) == 1
    assert second_ui.calls == []

def test_approval_can_add_temporary_read_allow_for_session(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-read-dir"
    outside_dir.mkdir()
    (outside_dir / "one.txt").write_text("one\n", encoding="utf-8")
    (outside_dir / "two.txt").write_text("two\n", encoding="utf-8")
    ui = RememberingApprovalUI()
    agent = build_agent(tmp_path, [], approval_policy="ask", ui=ui)

    with patch("codemate.tools.path_policy.Path.home", return_value=tmp_path.parent):
        first = agent.run_tool("read_file", {"path": f"../{outside_dir.name}/one.txt", "start": 1, "end": 1})
        second = agent.run_tool("read_file", {"path": f"../{outside_dir.name}/two.txt", "start": 1, "end": 1})

    assert "one" in first
    assert "two" in second
    assert len(ui.calls) == 1
    assert ui.calls[0]["metadata"]["approval_access"] == "read"
    assert ui.calls[0]["metadata"]["suggested_allow_dir"] == str(outside_dir.resolve())

def test_permission_deny_overrides_temporary_allow(tmp_path):
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "note.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".codemate").mkdir(exist_ok=True)
    (tmp_path / ".codemate" / "settings.json").write_text(
        '{"mcp":{"servers":{}},"permissions":{"read":{"allow":[],"deny":["secret"]},"write":{"allow":[],"deny":[]}}}\n',
        encoding="utf-8",
    )
    agent = build_agent(tmp_path, [], approval_policy="full")
    agent.add_temporary_permission("read", secret_dir)

    result = agent.run_tool("read_file", {"path": "secret/note.txt", "start": 1, "end": 1})

    assert "read denied by permission rules" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "read_permission_denied"
