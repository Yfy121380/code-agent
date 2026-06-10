import os
from unittest.mock import patch

from pico.config import find_project_env, load_project_env


def test_find_project_env_only_checks_start_directory(tmp_path):
    parent_env = tmp_path / ".env"
    child = tmp_path / "child"
    child.mkdir()
    parent_env.write_text("PICO_OPENAI_API_KEY=sk-parent\n", encoding="utf-8")

    assert find_project_env(child) is None


def test_load_project_env_loads_exact_directory_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PICO_OPENAI_API_KEY=sk-root\n", encoding="utf-8")

    with patch.dict(os.environ, {}, clear=True):
        loaded = load_project_env(tmp_path)

        assert loaded == {"PICO_OPENAI_API_KEY": "sk-root"}
        assert os.environ["PICO_OPENAI_API_KEY"] == "sk-root"
