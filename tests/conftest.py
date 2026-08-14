"""测试环境全局配置。

这里隔离 codemate 的用户级状态目录，避免测试把 sessions、memory、
settings 或 projects 写入真实的 ~/.codemate。每个测试都会得到独立
CODEMATE_HOME，测试结束后随 pytest 临时目录一起清理。普通测试默认关闭
shell sandbox，专门的 sandbox 测试会在项目 settings 中显式打开。
"""

import json

import pytest


@pytest.fixture(autouse=True)
def isolated_codemate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    codemate_home = tmp_path.parent / f"{tmp_path.name}-home" / ".codemate"
    monkeypatch.setenv("CODEMATE_HOME", str(codemate_home))
    codemate_home.mkdir(parents=True, exist_ok=True)
    settings = {
        "mcp": {"servers": {}},
        "sandbox": {"mode": "disabled"},
        "permissions": {"read": {"allow": [], "deny": []}, "write": {"allow": [], "deny": []}},
    }
    (codemate_home / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    project_config = tmp_path / ".codemate"
    project_config.mkdir(parents=True, exist_ok=True)
    (project_config / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
