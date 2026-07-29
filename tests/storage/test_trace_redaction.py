"""trace 脱敏测试。

覆盖模块：RunStore trace 写入、runtime secret redaction。
重点边界：配置的 secret 名称在嵌套 dict/list 中也会被替换。
"""

import os

from unittest.mock import patch

from codemate.storage import TaskState
from tests.helpers import build_agent, isolated_env


def test_configured_secret_env_names_are_redacted_in_trace(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, isolated_env(tmp_path, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}), clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Mask configured secrets")
        agent.run_store.start_run(state)

        assert set(agent.secret_env_summary()["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}

        payload = {
            "GITHUB_PAT": github_pat,
            "GH_PAT": gh_pat,
            "nested": {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
            "list": [github_pat, gh_pat],
        }
        agent.emit_trace(state, "tool_executed", payload)

    run_dir = agent.run_store.run_dir(state.run_id)
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")

    assert github_pat not in trace_text
    assert gh_pat not in trace_text
    assert trace_text.count("<redacted>") >= 4
