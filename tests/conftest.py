"""测试环境全局配置。

这里隔离 codemate 的用户级状态目录，避免测试把 sessions、memory、
settings 或 projects 写入真实的 ~/.codemate。每个测试都会得到独立
CODEMATE_HOME，测试结束后随 pytest 临时目录一起清理。
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_codemate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    monkeypatch.setenv("CODEMATE_HOME", str(tmp_path.parent / f"{tmp_path.name}-home" / ".codemate"))
