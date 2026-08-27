# settings.json 配置加载与权限规则聚合。
# 本文件负责创建默认配置文件、合并用户级和项目级 settings，
# 并把 read/write allow/deny 路径归一化为绝对路径规则。
# 具体工具是否放行由后续 path policy 使用这些聚合规则来判断。

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from .paths import CodematePaths, ensure_codemate_layout


DEFAULT_READ_DENY_PREFIXES = (
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.azure",
    "~/.gcloud",
    "~/.kube",
    "~/.docker",
    "~/.config/gh",
    "~/.password-store",
)
DEFAULT_READ_DENY_EXACT = (
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.git-credentials",
)
DEFAULT_WRITE_DENY_EXACT = (
    "~/.bashrc",
    "~/.zshrc",
    "~/.profile",
    "~/.bash_profile",
    "~/.zprofile",
    "~/.zshenv",
    "~/.gitconfig",
)

SETTINGS_TEMPLATE = {
    "mcp": {
        "servers": {},
    },
    "sandbox": {
        "mode": "required",
    },
    "memory": {
        "backend": "legacy",
    },
    "permissions": {
        "read": {
            "allow": [],
            "deny": [],
        },
        "write": {
            "allow": [],
            "deny": [],
        },
    },
}


@dataclass(frozen=True)
class PermissionRules:
    read_allow: tuple[Path, ...]
    read_deny: tuple[Path, ...]
    write_allow: tuple[Path, ...]
    write_deny: tuple[Path, ...]


@dataclass(frozen=True)
class CodemateSettings:
    user: dict
    project: dict
    merged: dict
    mcp_servers: dict
    sandbox: dict
    memory: dict
    permission_rules: PermissionRules


def default_settings():
    return copy.deepcopy(SETTINGS_TEMPLATE)


def ensure_settings_file(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default_settings(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_settings_file(path):
    ensure_settings_file(path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid codemate settings JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"codemate settings must be an object: {path}")
    return data


def normalize_rule_path(value, workspace_root):
    # 权限规则必须落到真实绝对路径上。
    # 相对路径按当前 workspace 解析，避免同一条规则在不同 cwd 下含义变化。
    text = str(value or "").strip()
    if not text:
        raise ValueError("permission rule path must not be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(workspace_root) / path
    return path.resolve(strict=False)


def _permission_list(settings, access, kind):
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        return []
    section = permissions.get(access, {})
    if not isinstance(section, dict):
        return []
    values = section.get(kind, [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"permissions.{access}.{kind} must be a list")
    return values


def _compact_paths(paths):
    # 同一类规则内，父目录已经覆盖子目录。
    # 这里先按路径层级从短到长排序，再丢弃已经被父目录包含的子路径。
    result = []
    for path in sorted({Path(path) for path in paths}, key=lambda item: (len(item.parts), str(item))):
        if any(path == parent or path.is_relative_to(parent) for parent in result):
            continue
        result.append(path)
    return tuple(result)


def build_permission_rules(paths: CodematePaths, user_settings, project_settings, temporary_settings=None):
    # 默认规则、用户 settings、项目 settings 和本进程临时 allow 在这里聚合。
    # write allow 自动带来 read allow；read deny 自动带来 write deny。
    workspace = paths.workspace_root
    read_allow_raw = [
        paths.workspace_root,
        paths.project_config_root,
        paths.project_state_root,
    ]
    write_allow_raw = [
        paths.project_config_root,
        paths.project_state_root,
    ]
    read_deny_raw = [*DEFAULT_READ_DENY_PREFIXES, *DEFAULT_READ_DENY_EXACT]
    write_deny_raw = [*DEFAULT_WRITE_DENY_EXACT]

    for settings in (user_settings, project_settings, temporary_settings):
        if not settings:
            continue
        read_allow_raw.extend(_permission_list(settings, "read", "allow"))
        read_deny_raw.extend(_permission_list(settings, "read", "deny"))
        write_allow_raw.extend(_permission_list(settings, "write", "allow"))
        write_deny_raw.extend(_permission_list(settings, "write", "deny"))

    read_allow = [normalize_rule_path(path, workspace) for path in read_allow_raw]
    read_deny = [normalize_rule_path(path, workspace) for path in read_deny_raw]
    write_allow = [normalize_rule_path(path, workspace) for path in write_allow_raw]
    write_deny = [normalize_rule_path(path, workspace) for path in write_deny_raw]

    read_allow.extend(write_allow)
    write_deny.extend(read_deny)

    return PermissionRules(
        read_allow=_compact_paths(read_allow),
        read_deny=_compact_paths(read_deny),
        write_allow=_compact_paths(write_allow),
        write_deny=_compact_paths(write_deny),
    )


def _merged_mcp_servers(user_settings, project_settings):
    merged = {}
    for settings in (user_settings, project_settings):
        mcp = settings.get("mcp", {})
        if not isinstance(mcp, dict):
            continue
        servers = mcp.get("servers", {})
        if servers is None:
            continue
        if not isinstance(servers, dict):
            raise ValueError("mcp.servers must be an object")
        merged.update(copy.deepcopy(servers))
    return merged


def _merged_permissions(user_settings, project_settings):
    permissions = default_settings()["permissions"]
    for access in ("read", "write"):
        for kind in ("allow", "deny"):
            values = []
            for settings in (user_settings, project_settings):
                values.extend(_permission_list(settings, access, kind))
            permissions[access][kind] = values
    return permissions


def _merged_sandbox(user_settings, project_settings):
    mode = str(default_settings()["sandbox"]["mode"])
    for settings in (user_settings, project_settings):
        value = settings.get("sandbox", {})
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("sandbox must be an object")
        if "mode" in value and "enabled" in value:
            raise ValueError("sandbox cannot define both mode and legacy enabled")
        if "mode" in value:
            candidate = value["mode"]
            if not isinstance(candidate, str) or candidate not in {
                "required",
                "optional",
                "disabled",
            }:
                raise ValueError(
                    "sandbox.mode must be one of: required, optional, disabled"
                )
            mode = candidate
        elif "enabled" in value:
            # Existing settings used a boolean. Normalize them at load time so
            # upgrades preserve the old fail-closed/disabled behavior.
            enabled = value["enabled"]
            if not isinstance(enabled, bool):
                raise ValueError("sandbox.enabled must be a boolean")
            mode = "required" if enabled else "disabled"
    return {"mode": mode}


def _merged_memory(user_settings, project_settings):
    """Resolve the default memory backend, with project settings taking precedence."""
    backend = str(default_settings()["memory"]["backend"])
    for settings in (user_settings, project_settings):
        value = settings.get("memory", {})
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("memory must be an object")
        if "backend" not in value:
            continue
        candidate = value["backend"]
        if not isinstance(candidate, str) or candidate not in {
            "legacy",
            "progressive",
            "disabled",
        }:
            raise ValueError(
                "memory.backend must be one of: legacy, progressive, disabled"
            )
        backend = candidate
    return {"backend": backend}


def load_codemate_settings(paths_or_workspace_root):
    paths = paths_or_workspace_root
    if not isinstance(paths_or_workspace_root, CodematePaths):
        paths = ensure_codemate_layout(paths_or_workspace_root)
    user_settings = load_settings_file(paths.user_settings)
    project_settings = load_settings_file(paths.project_settings)
    merged = default_settings()
    merged["mcp"]["servers"] = _merged_mcp_servers(user_settings, project_settings)
    merged["sandbox"] = _merged_sandbox(user_settings, project_settings)
    merged["memory"] = _merged_memory(user_settings, project_settings)
    merged["permissions"] = _merged_permissions(user_settings, project_settings)
    return CodemateSettings(
        user=user_settings,
        project=project_settings,
        merged=merged,
        mcp_servers=merged["mcp"]["servers"],
        sandbox=merged["sandbox"],
        memory=merged["memory"],
        permission_rules=build_permission_rules(paths, user_settings, project_settings),
    )
