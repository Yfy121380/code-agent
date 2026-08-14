# Shell 沙箱构造模块。
#
# 本文件只负责把已经通过审批的 run_shell 命令包装成 bubblewrap 参数。
# 权限模型保持简单：根文件系统默认只读，read_deny 用空挂载遮蔽，
# write_allow 和本次审批通过的写目标重新 bind 成可写，网络默认继承宿主机。

from pathlib import Path
import shutil
import subprocess


_SANDBOX_PREFLIGHT_ERROR = None


def sandbox_enabled(agent):
    return sandbox_mode(agent) != "disabled"


def sandbox_mode(agent):
    """Return the normalized shell sandbox mode for this runtime."""
    sandbox = getattr(getattr(agent, "settings", None), "sandbox", {}) or {}
    mode = sandbox.get("mode")
    if mode in {"required", "optional", "disabled"}:
        return str(mode)
    # Keep programmatically constructed legacy settings safe by default.
    if "enabled" in sandbox:
        return "required" if bool(sandbox.get("enabled")) else "disabled"
    return "required"


def bwrap_path():
    return shutil.which("bwrap")


def sandbox_preflight_error():
    """检查当前机器是否能启动 bwrap。

    有些系统安装了 bwrap，但禁用了非特权 user namespace。这里做一次轻量
    探测并缓存结果，避免真正执行命令时才得到难读的底层报错。
    """
    global _SANDBOX_PREFLIGHT_ERROR
    if _SANDBOX_PREFLIGHT_ERROR is not None:
        return _SANDBOX_PREFLIGHT_ERROR
    executable = bwrap_path()
    if not executable:
        _SANDBOX_PREFLIGHT_ERROR = "shell sandbox requires bubblewrap (bwrap), but it is not installed"
        return _SANDBOX_PREFLIGHT_ERROR
    try:
        result = subprocess.run(
            [
                executable,
                "--unshare-all",
                "--share-net",
                "--die-with-parent",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
                "/bin/sh",
                "-lc",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        _SANDBOX_PREFLIGHT_ERROR = "shell sandbox preflight timed out"
        return _SANDBOX_PREFLIGHT_ERROR
    except OSError as exc:
        _SANDBOX_PREFLIGHT_ERROR = f"shell sandbox failed to start: {exc}"
        return _SANDBOX_PREFLIGHT_ERROR
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        _SANDBOX_PREFLIGHT_ERROR = "shell sandbox failed to start" + (f": {detail}" if detail else "")
        return _SANDBOX_PREFLIGHT_ERROR
    _SANDBOX_PREFLIGHT_ERROR = ""
    return ""


def _is_relative_to(path, base):
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _under_any(path, roots):
    return any(path == root or _is_relative_to(path, root) for root in roots)


def _compact_dirs(paths):
    result = []
    for path in sorted({Path(item).resolve(strict=False) for item in paths}, key=lambda item: (len(item.parts), str(item))):
        if any(path == parent or _is_relative_to(path, parent) for parent in result):
            continue
        result.append(path)
    return result


def _writable_dir(path):
    path = Path(path).resolve(strict=False)
    return path if path.exists() and path.is_dir() else path.parent


def approved_write_dirs(agent):
    # write_allow 是持久/settings/会话临时规则。
    # gate.paths 是本次工具调用审批通过的路径，只对当前 shell 执行生效，
    # 不会写回 permission_rules。
    rules = agent.permission_rules
    dirs = [_writable_dir(path) for path in rules.write_allow]
    gate = getattr(agent, "_last_tool_gate", None)
    if gate is not None and getattr(gate, "access", "") == "write":
        dirs.extend(_writable_dir(path) for path in getattr(gate, "paths", ()) or ())
    return _compact_dirs(path for path in dirs if path.exists())


def build_shell_sandbox_command(agent, command):
    """构造 run_shell 的 bwrap 命令行参数。

    默认 `--ro-bind / /` 让文件系统只读可见；read_deny 目录用 tmpfs 遮蔽，
    read_deny 文件用 /dev/null 覆盖；write_allow 和本次审批写目录再 bind
    成可写。deny 优先，位于 read_deny 下的写目录不会被重新开放。
    """
    rules = agent.permission_rules
    read_denies = _compact_dirs(path for path in rules.read_deny if Path(path).exists())
    args = [
        bwrap_path() or "bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
    ]

    for path in read_denies:
        if path.is_dir():
            args.extend(["--tmpfs", str(path)])
        elif path.is_file():
            args.extend(["--ro-bind", "/dev/null", str(path)])

    for path in approved_write_dirs(agent):
        if _under_any(path, read_denies):
            continue
        args.extend(["--bind", str(path), str(path)])

    args.extend(
        [
            "--chdir",
            str(Path(agent.root).resolve()),
            "--setenv",
            "HOME",
            str(Path.home().resolve()),
            "/bin/sh",
            "-lc",
            str(command),
        ]
    )
    return args
