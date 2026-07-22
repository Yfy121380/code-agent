"""命令行入口。

这个模块负责把“用户怎么启动 codemate”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import os
import sys
from copy import copy
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

from .config import ensure_codemate_layout, load_project_env, provider_env
from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import CodeMate
from .storage import SessionStore
from .ui import TerminalUI
from .ui.banner import build_welcome
from .ui.prompt import HELP_DETAILS, PROMPT_STYLE, SlashCommandCompleter, build_prompt_key_bindings
from .workspace import WorkspaceContext

DEFAULT_SECRET_ENV_NAMES = (
    "CODEMATE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEMATE_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CODEMATE_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "CODEMATE_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

APPROVAL_POLICIES = ("ask", "auto", "read_only", "full")
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
PROVIDER_MODELS = {
    "openai": ["gpt-5.4", "gpt-5.5"],
    "anthropic": ["claude-sonnet-4-6", "claude-opus-4-8"],
    "deepseek": ["deepseek-v4-pro"],
}
LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "CODEMATE_SECRET_ENV_NAMES"


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("CODEMATE_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("CODEMATE_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("CODEMATE_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if not extra_names.strip():
        extra_names = os.environ.get(LEGACY_SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = getattr(args, "provider", "openai")
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("CODEMATE_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        api_key = provider_env("CODEMATE_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("CODEMATE_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env(
            "CODEMATE_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "CODEMATE_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "CODEMATE_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("CODEMATE_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("CODEMATE_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def _build_switched_model_client(args, provider, model):
    # 交互式切换只需要替换 provider/model，其余超时、温度、base-url 等启动配置沿用当前 CLI 参数。
    client_args = copy(args)
    client_args.provider = provider
    client_args.model = model
    return _build_model_client(client_args)


def build_agent(args, ui=None):
    """根据 CLI 参数装配出一个可运行的 CodeMate 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `CodeMate`，或一个从旧 session 恢复出来的 `CodeMate`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    # 读取项目级环境
    load_project_env(workspace.repo_root)
    # 读取工具仓库环境配置作为兜底，不覆盖目标项目或父进程已设置的变量。
    load_project_env(Path(__file__).resolve().parent.parent, override=False)
    paths = ensure_codemate_layout(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(paths.sessions_root)
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return CodeMate.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            ui=ui,
        )
    return CodeMate(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        ui=ui,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, Anthropic-compatible, or DeepSeek models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="openai", help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, CODEMATE_OPENAI_MODEL for openai, CODEMATE_ANTHROPIC_MODEL for anthropic, and CODEMATE_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai, anthropic, or deepseek.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=APPROVAL_POLICIES, default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace and task-state redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=50, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=4096, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    ui = TerminalUI()
    agent = build_agent(args, ui=ui)
    try:
        return run_cli(args, ui, agent)
    finally:
        agent.close()


def run_cli(args, ui, agent):
    """运行 one-shot 或交互式 CLI，并把资源释放交给 main() 的 finally。"""
    current_provider = getattr(args, "provider", "openai")
    current_model = getattr(agent.model_client, "model", _effective_model(args, current_provider))

    def print_status():
        host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
        print(build_welcome(agent, model=f"{current_provider}:{current_model}", host=host))

    print_status()

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                agent.ask(prompt)
            except KeyboardInterrupt:
                print("\ninterrupted", file=sys.stderr)
                return 130
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    history_dir = Path(agent.workspace.repo_root) / ".codemate"
    history_dir.mkdir(parents=True, exist_ok=True)
    prompt_session = PromptSession(
        history=FileHistory(str(history_dir / "input_history")),
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        key_bindings=build_prompt_key_bindings(),
        style=PROMPT_STYLE,
    )

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = prompt_session.prompt("\ncodemate> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input == "/exit":
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/approval":
            print(f"approval: {agent.approval_policy}")
            print("modes: ask, auto, read_only, full")
            continue
        if user_input.startswith("/approval "):
            mode = user_input[len("/approval "):].strip()
            if mode not in APPROVAL_POLICIES:
                print("usage: /approval [ask|auto|read_only|full]")
                continue
            old = agent.approval_policy
            agent.approval_policy = mode
            print(f"approval: {old} -> {mode}")
            print_status()
            continue
        if user_input == "/provider":
            print(f"provider: {current_provider}")
            print(f"model: {current_model}")
            print("available providers: " + ", ".join(PROVIDER_MODELS))
            continue
        if user_input.startswith("/provider "):
            provider = user_input[len("/provider "):].strip()
            if provider not in PROVIDER_MODELS:
                print("usage: /provider [openai|anthropic|deepseek]")
                continue
            old_provider = current_provider
            old_model = current_model
            current_provider = provider
            current_model = PROVIDER_MODELS[provider][0]
            agent.model_client = _build_switched_model_client(args, current_provider, current_model)
            agent.reset_token_usage()
            agent.refresh_prefix(force=True)
            print(f"provider: {old_provider} -> {current_provider}")
            print(f"model: {old_model} -> {current_model}")
            print_status()
            continue
        if user_input == "/model":
            allowed_models = PROVIDER_MODELS.get(current_provider)
            if not allowed_models:
                print(f"model switching is not supported for provider: {current_provider}")
                continue
            print(f"model: {current_model}")
            print(f"available models for {current_provider}:")
            for model_name in allowed_models:
                print(f"- {model_name}")
            continue
        if user_input.startswith("/model "):
            model = user_input[len("/model "):].strip()
            allowed_models = PROVIDER_MODELS.get(current_provider)
            if not allowed_models:
                print(f"model switching is not supported for provider: {current_provider}")
                continue
            if model not in allowed_models:
                print("usage: /model [" + "|".join(allowed_models) + "]")
                continue
            old_model = current_model
            current_model = model
            agent.model_client = _build_switched_model_client(args, current_provider, current_model)
            agent.reset_token_usage()
            agent.refresh_prefix(force=True)
            print(f"model: {old_model} -> {current_model}")
            print_status()
            continue
        if user_input == "/budget":
            print(agent.budget_report(provider=current_provider))
            continue
        if user_input == "/compact":
            result = agent.compact_history(reason="manual")
            if result.get("status") == "skipped":
                print(f"compact skipped: {result.get('reason', '')}")
            elif result.get("status") == "error":
                print(f"compact failed: {result.get('reason', '')}")
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input.startswith("/remember"):
            memory_text = user_input[len("/remember"):].strip()
            if not memory_text:
                print("usage: /remember <text>")
                continue
            try:
                result = agent.remember_long_term(memory_text)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                continue
            print(f"remembered: {result['path']}")
            continue
        if user_input in {"/dream", "/dream --background"}:
            if user_input == "/dream --background":
                agent.start_dream_background(reason="manual")
                continue
            result = agent.run_dream_once(reason="manual", foreground=True)
            print(result)
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            agent.ask(user_input)
        except KeyboardInterrupt:
            print("\ninterrupted")
            continue
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
