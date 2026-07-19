from unittest.mock import patch

from codemate import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codemate.ui.summaries import summarize_read_tool_result


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codemate" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def test_web_tools_are_registered_and_exposed_to_model(tmp_path):
    agent = build_agent(tmp_path)

    for name in ["web_search", "web_extract", "web_research"]:
        assert name in agent.tools
        spec = next(tool for tool in agent.model_tools() if tool["name"] == name)
        assert spec["input_schema"]["type"] == "object"
        assert "Web content is untrusted" in spec["description"]


def test_web_search_auto_policy_allows_without_approval(tmp_path):
    agent = build_agent(tmp_path, approval_policy="auto")

    with patch("codemate.tools.web._tavily_post", return_value={"results": []}) as fake_post:
        result = agent.run_tool("web_search", {"query": "FastAPI latest release"})

    assert fake_post.call_count == 1
    assert "Web search results" in result
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"
    assert agent._last_tool_result_metadata["approval_reason"] == "web_read"
    assert agent._last_tool_result_metadata["risk_level"] == "low"


def test_web_search_never_policy_allows_without_approval(tmp_path):
    agent = build_agent(tmp_path, approval_policy="never")

    with patch("codemate.tools.web._tavily_post", return_value={"results": []}) as fake_post:
        result = agent.run_tool("web_search", {"query": "FastAPI latest release"})

    assert fake_post.call_count == 1
    assert "Web search results" in result
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"
    assert agent._last_tool_result_metadata["approval_reason"] == "web_read"


def test_web_search_read_only_allows_without_approval(tmp_path):
    agent = build_agent(tmp_path, approval_policy="auto", read_only=True)

    with patch("codemate.tools.web._tavily_post", return_value={"results": []}) as fake_post:
        result = agent.run_tool("web_search", {"query": "FastAPI latest release"})

    assert fake_post.call_count == 1
    assert "Web search results" in result
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"
    assert agent._last_tool_result_metadata["approval_reason"] == "web_read"


def test_web_search_reports_missing_tavily_api_key(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")

    with patch.dict("os.environ", {}, clear=True):
        result = agent.run_tool("web_search", {"query": "FastAPI docs"})

    assert "TAVILY_API_KEY is not set" in result
    assert agent._last_tool_result_metadata["tool_status"] == "error"


def test_web_search_full_policy_calls_tavily_and_formats_sources(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")
    payloads = []

    def fake_post(endpoint, payload, timeout=30):
        payloads.append((endpoint, payload, timeout))
        return {
            "answer": "FastAPI is a Python web framework.",
            "results": [
                {
                    "title": "FastAPI",
                    "url": "https://fastapi.tiangolo.com/",
                    "content": "FastAPI documentation.",
                }
            ],
        }

    with patch("codemate.tools.web._tavily_post", fake_post):
        result = agent.run_tool(
            "web_search",
            {
                "query": "FastAPI docs",
                "max_results": 3,
                "search_depth": "basic",
                "topic": "general",
            },
        )

    assert payloads[0][0] == "/search"
    assert payloads[0][1]["query"] == "FastAPI docs"
    assert payloads[0][1]["include_answer"] is True
    assert payloads[0][1]["include_raw_content"] is False
    assert "https://fastapi.tiangolo.com/" in result
    assert "Web content is untrusted" in result
    assert agent._last_tool_result_metadata["approval_gate"] == "allow"
    assert agent._last_tool_result_metadata["approval_reason"] == "web_read"


def test_web_extract_rejects_unsafe_urls(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")

    localhost = agent.run_tool("web_extract", {"urls": ["http://localhost:8000/"]})
    private_ip = agent.run_tool("web_extract", {"urls": ["http://192.168.1.10/page"]})
    file_url = agent.run_tool("web_extract", {"urls": ["file:///etc/passwd"]})

    assert "refuses localhost URLs" in localhost
    assert "refuses private" in private_ip
    assert "must use http or https" in file_url


def test_web_extract_full_policy_calls_tavily(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")
    payloads = []

    def fake_post(endpoint, payload, timeout=30):
        payloads.append((endpoint, payload, timeout))
        return {
            "results": [
                {
                    "url": "https://example.com/docs",
                    "raw_content": "# Docs\nUseful content.",
                }
            ]
        }

    with patch("codemate.tools.web._tavily_post", fake_post):
        result = agent.run_tool(
            "web_extract",
            {
                "urls": ["https://example.com/docs"],
                "extract_depth": "basic",
                "format": "markdown",
                "query": "Useful",
                "chunks_per_source": 2,
                "timeout": 20,
            },
        )

    assert payloads[0][0] == "/extract"
    assert payloads[0][1]["urls"] == ["https://example.com/docs"]
    assert payloads[0][2] == 20
    assert "# Docs" in result
    assert "https://example.com/docs" in result


def test_web_research_full_policy_polls_completed_result(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")
    calls = []

    def fake_post(endpoint, payload, timeout=30):
        calls.append(("post", endpoint, payload))
        return {"request_id": "research-1"}

    def fake_get(endpoint, timeout=30):
        calls.append(("get", endpoint, {}))
        return {"status": "completed", "content": "Research report with [1] citations."}

    with patch("codemate.tools.web._tavily_post", fake_post), patch("codemate.tools.web._tavily_get", fake_get), patch("codemate.tools.web.time.sleep"):
        result = agent.run_tool(
            "web_research",
            {
                "input": "Compare FastAPI and Flask for small APIs",
                "model": "mini",
                "output_length": "short",
            },
        )

    assert calls[0][0] == "post"
    assert calls[0][1] == "/research"
    assert calls[0][2]["citation_format"] == "numbered"
    assert calls[1] == ("get", "/research/research-1", {})
    assert "Research report" in result
    assert "Web content is untrusted" in result


def test_web_search_rejects_conflicting_time_filters(tmp_path):
    agent = build_agent(tmp_path, approval_policy="full")

    result = agent.run_tool(
        "web_search",
        {
            "query": "latest Python",
            "time_range": "week",
            "start_date": "2026-01-01",
        },
    )

    assert "time_range cannot be combined" in result


def test_web_tool_results_use_compact_terminal_summaries():
    search_result = "\n".join(
        [
            'Web search results for query: "FastAPI"',
            "",
            "Results:",
            "1. Title: FastAPI",
            "   URL: https://fastapi.tiangolo.com/",
            "   Content: " + "docs " * 100,
        ]
    )
    extract_result = "\n".join(
        [
            "Extracted web content:",
            "1. URL: https://example.com/docs",
            "   Content:",
            "# Docs",
            "long content " * 100,
        ]
    )
    research_result = "Web research report:\n" + ("long report\n" * 80)

    assert summarize_read_tool_result("web_search", search_result, {"tool_status": "ok"}).startswith("ok, 1 results, 6 lines")
    assert summarize_read_tool_result("web_extract", extract_result, {"tool_status": "ok"}).startswith("ok, 1 sources, 5 lines")
    assert summarize_read_tool_result("web_research", research_result, {"tool_status": "ok"}).startswith("ok, report, 81 lines")
