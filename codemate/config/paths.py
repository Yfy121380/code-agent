# Codemate 本地路径布局。
# 本文件根据当前工作区计算项目内配置目录、用户级配置目录、
# 以及绑定到该项目的 session 和长期记忆目录。
# 所有路径都会转为真实绝对路径，避免后续模块重复拼接路径。

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodematePaths:
    workspace_root: Path
    project_id: str
    project_config_root: Path
    project_settings: Path
    project_skills: Path
    home_root: Path
    user_memory_root: Path
    progressive_core_memory: Path
    user_settings: Path
    user_skills: Path
    project_state_root: Path
    sessions_root: Path
    memory_root: Path
    progressive_memory_root: Path
    skill_evolution_root: Path


def codemate_home():
    value = os.environ.get("CODEMATE_HOME", "~/.codemate")
    return Path(value).expanduser().resolve()


def project_id_for_root(workspace_root):
    root = Path(workspace_root).expanduser().resolve()
    text = str(root).replace("\\", "/")
    return text.replace("/", "-") or "workspace"


def codemate_paths(workspace_root, home_root=None):
    workspace = Path(workspace_root).expanduser().resolve()
    home = Path(home_root).expanduser().resolve() if home_root is not None else codemate_home()
    project_config_root = workspace / ".codemate"
    project_id = project_id_for_root(workspace)
    project_state_root = home / "projects" / project_id
    return CodematePaths(
        workspace_root=workspace,
        project_id=project_id,
        project_config_root=project_config_root,
        project_settings=project_config_root / "settings.json",
        project_skills=project_config_root / "skills",
        home_root=home,
        user_memory_root=home / "memory",
        progressive_core_memory=home / "memory" / "progressive" / "core.json",
        user_settings=home / "settings.json",
        user_skills=home / "skills",
        project_state_root=project_state_root,
        sessions_root=project_state_root / "sessions",
        memory_root=project_state_root / "memory",
        progressive_memory_root=project_state_root / "memory" / "progressive",
        skill_evolution_root=project_state_root / "skill-evolution",
    )


def ensure_codemate_layout(workspace_root, home_root=None):
    # 统一创建 codemate 运行所需的基础目录。
    # settings.json 文件内容由 settings 模块负责创建，这里只保证目录存在。
    paths = codemate_paths(workspace_root, home_root=home_root)
    for directory in (
        paths.project_config_root,
        paths.project_skills,
        paths.home_root,
        paths.user_memory_root,
        paths.progressive_core_memory.parent,
        paths.user_skills,
        paths.project_state_root,
        paths.sessions_root,
        paths.memory_root,
        paths.progressive_memory_root,
        paths.skill_evolution_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths
