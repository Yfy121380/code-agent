"""文件读取与目录浏览工具测试。

覆盖模块：list_files、read_file、工具结果截断出口。
重点边界：隐藏状态目录、文本/二进制/大文件展示、read_all 与范围参数、超大工具结果截断。
"""

from pathlib import Path

from PIL import Image

from codemate.tools.constants import LIST_FILE_LINE_COUNT_MAX_BYTES, MAX_TOOL_RESULT_CHARS
from tests.helpers import build_agent


def test_memory_directory_is_explicitly_accessible_but_not_default_listed(tmp_path):
    agent = build_agent(tmp_path, [])

    root_listing = agent.run_tool("list_files", {"path": "."})
    memory_listing = agent.run_tool("list_files", {"path": str(agent.paths.memory_root)})
    ignored_listing = agent.run_tool("list_files", {"path": ".codemate"})

    assert ".codemate" not in root_listing
    assert "user_profile.md" in memory_listing
    assert "path is ignored" in ignored_listing

def test_list_files_shows_text_line_count_binary_and_large_file(tmp_path):
    (tmp_path / "small.txt").write_text("one\ntwo\nthree", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    Image.new("RGB", (8, 6), color="red").save(tmp_path / "preview.png")
    (tmp_path / "large.txt").write_bytes(b"a" * (LIST_FILE_LINE_COUNT_MAX_BYTES + 1))
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("list_files", {"path": "."})

    assert "[F] small.txt  3 lines" in result
    assert "[F] preview.png  image file" in result
    assert "[F] binary.bin  binary file" in result
    assert "[F] large.txt  large file" in result

def test_read_file_read_all_ignores_range(tmp_path):
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "notes.txt", "start": 2, "end": 2, "read_all": True})

    assert "   1: one" in result
    assert "   2: two" in result
    assert "   3: three" in result

def test_tool_result_is_truncated_before_history_and_trace_use(tmp_path):
    (tmp_path / "huge.txt").write_text("x" * (MAX_TOOL_RESULT_CHARS + 10_000), encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "huge.txt", "read_all": True})

    assert len(result) <= MAX_TOOL_RESULT_CHARS
    assert result.startswith("Tool result truncated from ")
    assert agent._last_tool_result_metadata["tool_result_truncated"] is True
    assert agent._last_tool_result_metadata["tool_result_max_chars"] == MAX_TOOL_RESULT_CHARS


def test_read_file_image_returns_metadata_and_cached_content_block(tmp_path):
    Image.new("RGB", (12, 10), color="blue").save(tmp_path / "shot.png")
    agent = build_agent(tmp_path, [])
    agent.model_client.supports_images = True

    result = agent.run_tool("read_file", {"path": "shot.png", "start": 1, "end": 1, "read_all": True})

    assert "Image file: shot.png" in result
    assert "dimensions: 12x10" in result
    assert "base64" not in result
    assert agent._last_tool_result_metadata["image_result"] is True
    blocks = agent._last_tool_result_content_blocks
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"
    assert blocks[0]["media_type"] == "image/png"
    assert (agent.session_store.media_dir(agent.session["id"]) / Path(blocks[0]["path"]).name).is_file()


def test_read_file_image_requires_image_capable_model(tmp_path):
    Image.new("RGB", (4, 4), color="green").save(tmp_path / "shot.png")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "shot.png"})

    assert "current model does not support image input" in result
    assert agent._last_tool_result_content_blocks == []
