# CodeMate VS Code Extension

This directory contains the VS Code frontend for the local CodeMate runtime.
The extension starts `codemate-bridge` as a child process and communicates with
it using one JSON object per stdin/stdout line.

> 源码中包含面向学习的详细中文注释。`package.json` 和 `tsconfig*.json` 等文件
> 不添加注释，因为标准 JSON 不支持注释，添加伪字段还可能破坏 VS Code 的 manifest
> 校验。相关字段统一在 `docs/modules/10-vscode-extension.md` 中讲解。

## Development

Install dependencies and compile both Extension Host and Webview code:

```bash
cd vscode-extension
npm install
npm run check
npm run compile
```

Open the repository root in VS Code and press `F5`. In the Extension Development
Host window, select the CodeMate icon in the Activity Bar.

The default development command is equivalent to:

```bash
uv run --project <repository-root> codemate-bridge
```

Use the `codemate.backend.command` and `codemate.backend.arguments` settings when
the Python package is installed elsewhere. By default, the backend resumes the
most recently used session for the workspace; disable
`codemate.session.resumeLatest` to start a new session after each backend start.

The bridge runs in CodeMate's Python environment, but shell tools use the PATH
inherited by the VS Code Extension Host. Set `codemate.shell.pythonPath` to an
absolute project interpreter, such as
`${workspaceFolder}/.venv/bin/python`, when tests should use a specific virtual
environment. Restart the backend after changing this setting.

## Responsibilities

- `src/extension.ts` registers the sidebar and owns extension resources.
- `src/codemateProcess.ts` manages the Python process and JSONL transport.
- `src/chatViewProvider.ts` connects Extension Host events to the Webview.
- `src/changeDocumentProvider.ts` supplies read-only snapshots to VS Code's
  native diff editor without exposing snapshot paths to the Webview.
- `src/protocol.ts` defines data crossing the process and Webview boundaries.
- `webview/chat.ts` renders the session home, chat turns, streaming text, tools,
  and interactive approvals.
- `media/chat.css` uses VS Code theme variables for the sidebar layout.

## Session workflow

The sidebar opens on a project-scoped session list. Selecting a title resumes
that session; submitting the home composer creates a session and starts its
first request atomically. The latest request can be edited and retried from its
persisted pre-request session checkpoint. Retry restores Agent state and chat
history, but intentionally does not undo file, shell, or network side effects.

During a request, progress remains visible while individual tool events are
collapsed. Once the request finishes, the complete process section collapses
and the final answer remains visible.

Each completed coding turn can show a Changes panel. Selecting a file opens a
native Before/After diff. Undo and redo operate on the whole turn only and are
refused if any affected file no longer matches the recorded hash, so later user
edits are not overwritten. Errors and warnings reported by VS Code for changed
files appear below the panel and open at their source location.

Use `@selection`, `@file`, or `@problems` in the chat composer, or the adjacent
`+` menu, to attach editor evidence to the next request. Attachments are bounded
text context and do not grant the backend additional filesystem permissions.

## Current commands

The input supports normal CodeMate requests and `/help`, `/approval`, `/plan`,
`/review`, `/provider`, `/model`, `/budget`, `/compact`, `/remember`, `/dream`,
`/session`, and `/reset`. Use `/help` in the sidebar for exact forms.
