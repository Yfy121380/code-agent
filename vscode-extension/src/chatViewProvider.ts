/** 连接 CodeMate Webview 和 Extension Host 中的后端进程。 */

import * as vscode from 'vscode';
import * as path from 'node:path';

import { ChangeDocumentProvider } from './changeDocumentProvider';
import { CodeMateProcess } from './codemateProcess';
import { EditorDiagnosticsBridge } from './editorDiagnostics';
import { isRecord } from './protocol';

export const CHAT_VIEW_ID = 'codemate.chatView';

/**
 * 管理侧边栏文档，并将隔离的浏览器环境连接到受信任服务。
 *
 * Webview 无法直接调用 VS Code API 或读取文件。本 Provider 接收浏览器消息，执行
 * 只能在 Host 中完成的操作，把 Agent 请求转发给 CodeMateProcess，并将清理后的
 * 事件发回 Webview。
 */
export class ChatViewProvider implements vscode.WebviewViewProvider {
  // 完整变更集包含私有快照路径，只能保留在 Extension Host。浏览器仅接收 ID、
  // 相对路径和展示状态。
  private readonly changeSets = new Map<string, Record<string, unknown>>();
  private currentChangeSetId = '';
  private currentSessionId = '';
  private readonly editorDiagnostics: EditorDiagnosticsBridge;

  /** 注入资源根目录、共享后端客户端和原生 Diff 文档 Provider。 */
  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly backend: CodeMateProcess,
    private readonly changeDocuments: ChangeDocumentProvider,
  ) {
    this.editorDiagnostics = new EditorDiagnosticsBridge(backend);
  }

  /**
   * 初始化一个 Webview 实例，并接通双向消息。
   *
   * 侧边栏隐藏或移动后，VS Code 可能重新创建 Webview，因此所有订阅都归属于当前
   * 实例，并在该实例释放时一并注销。
   * 整体流程:
   * → 配置 Webview
   * → 加载 HTML
   * → 接通 Python 到 Webview
   * → 接通 Webview 到 Extension Host
   * → 监听诊断变化
   * → 在视图销毁时释放监听
   */
  resolveWebviewView(webviewView: vscode.WebviewView): void {
    const mediaRoot = vscode.Uri.joinPath(this.extensionUri, 'media');
    const distRoot = vscode.Uri.joinPath(this.extensionUri, 'dist');
    webviewView.webview.options = {
      enableScripts: true,
      /*
        允许 Webview 请求：
        vscode-extension/media/
        vscode-extension/dist/
        它只是目录白名单，不会主动遍历目录，也不会生成 URI。
      */
      localResourceRoots: [mediaRoot, distRoot],
    };
    webviewView.webview.html = this.buildHtml(webviewView.webview);

    // Python -> Extension Host -> Webview。事件清理前先保存变更快照；run 完成后
    // 延迟刷新诊断，因为语言服务器可能稍后才发布最新结果。
    const backendEvents = this.backend.onEvent((event) => {
      if (event.type === 'editor_diagnostics_request') {
        void this.editorDiagnostics.handle(event);
        return;
      }
      this.rememberChangeSets(event);
      void webviewView.webview.postMessage({
        type: 'backendEvent',
        event: this.forWebview(event),
      });
      if (event.type === 'run_finished' && isRecord(event.change_set)) {
        const changeSetId = String(event.change_set.id || '');
        if (changeSetId) {
          setTimeout(() => {
            void this.publishDiagnostics(webviewView.webview, changeSetId);
          }, 500);
        }
      }
    });
    // Agent 完成文件修改后，语言服务器可能继续更新诊断；这里保持 Changes 面板下的
    // Problems 区域同步。
    const diagnostics = vscode.languages.onDidChangeDiagnostics(() => {
      const latest = this.latestChangeSetId();
      if (latest) void this.publishDiagnostics(webviewView.webview, latest);
    });
    // Webview -> Extension Host。这是浏览器操作访问后端请求、编辑器 API、设置和
    // 原生 Diff 视图的唯一可信入口。
    const webviewMessages = webviewView.webview.onDidReceiveMessage(
      async (message: unknown) => {
        if (!isRecord(message) || typeof message.type !== 'string') {
          return;
        }
        try {
          if (message.type === 'ready') {
            const wasAlreadyReady = Boolean(this.backend.lastReadyEvent);
            await this.backend.start();
            const readyEvent = this.backend.lastReadyEvent;
            if (wasAlreadyReady && readyEvent) {
              await webviewView.webview.postMessage({
                type: 'backendEvent',
                event: this.forWebview(readyEvent),
              });
            }
          } else if (message.type === 'sendMessage' && typeof message.text === 'string') {
            await this.backend.sendInput(
              message.text,
              this.attachments(message),
              this.responseAnnotations(message),
            );
          } else if (message.type === 'newTask' && typeof message.text === 'string') {
            await this.backend.startSession(message.text, this.attachments(message));
          } else if (message.type === 'retryRequest' && typeof message.text === 'string') {
            const attachments = this.attachments(message);
            await this.backend.retryLastRequest(
              message.text,
              attachments.length > 0 ? attachments : undefined,
              this.responseAnnotations(message),
            );
          } else if (message.type === 'listSessions') {
            await this.backend.sendCommand('session_list');
          } else if (message.type === 'openSession') {
            await this.backend.sendCommand('session_resume', {
              session_id: String(message.sessionId || ''),
            });
          } else if (message.type === 'renameSession') {
            await this.backend.sendCommand('session_rename', {
              session_id: String(message.sessionId || ''),
              title: String(message.title || ''),
            });
          } else if (message.type === 'setRuntimeSetting') {
            await this.setRuntimeSetting(message);
          } else if (message.type === 'requestAttachment') {
            await this.attachEditorContext(webviewView.webview, String(message.kind || ''));
          } else if (message.type === 'openDiff') {
            await this.changeDocuments.openDiff(
              String(message.changeSetId || ''),
              String(message.path || ''),
            );
          } else if (message.type === 'changeAction') {
            const action = String(message.action || '');
            if (!['undo', 'redo'].includes(action)) return;
            await this.backend.sendCommand(`change_${action}`, {
              change_set_id: String(message.changeSetId || ''),
            });
          } else if (message.type === 'openLocation') {
            await this.openLocation(
              String(message.path || ''),
              Number(message.line || 1),
            );
          } else if (message.type === 'interactionResponse') {
            const interactionId = String(message.interactionId || '');
            if (interactionId) {
              this.backend.respond(interactionId, message.value);
            }
          } else if (message.type === 'cancel') {
            this.backend.cancel();
          } else if (message.type === 'restart') {
            await this.backend.restart();
          } else if (message.type === 'openSettings') {
            await vscode.commands.executeCommand(
              'workbench.action.openSettings',
              '@ext:xidiannss.codemate-vscode',
            );
          }
        } catch (error) {
          await webviewView.webview.postMessage({
            type: 'backendEvent',
            event: {
              type: 'ui_error',
              message: error instanceof Error ? error.message : String(error),
            },
          });
        }
      },
    );
    webviewView.onDidDispose(() => {
      backendEvents.dispose();
      diagnostics.dispose();
      webviewMessages.dispose();
    });
  }

  /** 读取并规范化 Webview 请求中可选的附件数组。 */
  private attachments(message: Record<string, unknown>): unknown[] {
    return Array.isArray(message.attachments) ? message.attachments : [];
  }

  /** 读取 Webview 已根据最终回答选区构造的待发送批注。 */
  private responseAnnotations(message: Record<string, unknown>): unknown[] {
    return Array.isArray(message.responseAnnotations)
      ? message.responseAnnotations
      : [];
  }

  /**
   * 通过 VS Code API 解析 @selection、@file 或 @problems。
   * 结果先返回 Webview，让用户在内容进入模型请求前能够看到并移除附件。
   */
  private async attachEditorContext(webview: vscode.Webview, kind: string): Promise<void> {
    try {
      const attachment = kind === 'selection'
        ? this.selectionAttachment()
        : kind === 'file'
          ? await this.fileAttachment()
          : kind === 'problems'
            ? this.problemsAttachment()
            : undefined;
      if (kind === 'file' && !attachment) {
        await webview.postMessage({ type: 'attachmentCancelled' });
        return;
      }
      if (!attachment) throw new Error(`No ${kind || 'editor'} context is available.`);
      await webview.postMessage({ type: 'attachmentResult', attachment });
    } catch (error) {
      await webview.postMessage({
        type: 'attachmentError',
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }

  /** 捕获当前编辑器的非空选区，并附带从 1 开始的行号元数据。 */
  private selectionAttachment(): Record<string, unknown> | undefined {
    const editor = vscode.window.activeTextEditor;
    if (!editor || editor.selection.isEmpty) return undefined;
    const content = editor.document.getText(editor.selection);
    this.requireAttachmentSize(content);
    const path = vscode.workspace.asRelativePath(editor.document.uri, false);
    return {
      id: `${Date.now()}-selection`, kind: 'selection', path,
      label: `${path}:${editor.selection.start.line + 1}-${editor.selection.end.line + 1}`,
      start_line: editor.selection.start.line + 1,
      end_line: editor.selection.end.line + 1,
      content,
    };
  }

  /**
   * 让用户选择一个工作区文本文件，并通过 workspace.fs 读取。
   * 内容进入 Webview 前会校验工作区归属、二进制特征和载荷大小。
   */
  private async fileAttachment(): Promise<Record<string, unknown> | undefined> {
    const selection = await vscode.window.showOpenDialog({
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: false,
      defaultUri: vscode.window.activeTextEditor?.document.uri,
      openLabel: 'Attach file',
    });
    const uri = selection?.[0];
    if (!uri) return undefined;
    if (!vscode.workspace.getWorkspaceFolder(uri)) {
      throw new Error('Only files inside the current workspace can be attached.');
    }
    const data = await vscode.workspace.fs.readFile(uri);
    if (data.includes(0)) throw new Error('Binary files cannot be attached as text context.');
    const content = Buffer.from(data).toString('utf8');
    this.requireAttachmentSize(content);
    const path = vscode.workspace.asRelativePath(uri, false);
    return { id: `${Date.now()}-file`, kind: 'file', path, label: path, content };
  }

  /** 收集当前编辑器文档最多 20 条错误或警告。 */
  private problemsAttachment(): Record<string, unknown> | undefined {
    const uri = vscode.window.activeTextEditor?.document.uri;
    if (!uri) return undefined;
    const path = vscode.workspace.asRelativePath(uri, false);
    const diagnostics = vscode.languages.getDiagnostics(uri)
      .filter((item) => [vscode.DiagnosticSeverity.Error, vscode.DiagnosticSeverity.Warning].includes(item.severity))
      .slice(0, 20);
    if (diagnostics.length === 0) return undefined;
    const content = diagnostics.map((item) => {
      const severity = item.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning';
      return `${path}:${item.range.start.line + 1}:${item.range.start.character + 1} [${severity}] ${item.message}`;
    }).join('\n');
    return { id: `${Date.now()}-problems`, kind: 'problems', path, label: `Problems in ${path}`, content };
  }

  /** 对文件和选区附件执行统一的提示词上下文大小限制。 */
  private requireAttachmentSize(content: string): void {
    if (content.length > 30_000) {
      throw new Error('Editor context exceeds 30,000 characters. Select a smaller range.');
    }
  }

  /**
   * 索引直接事件、命令结果或状态中出现的所有变更集。
   * 会话切换时重置当前指针，避免把诊断附加到之前会话的变更集。
   */
  private rememberChangeSets(event: Record<string, unknown>): void {
    let explicitChangeSetId = '';
    if (isRecord(event.change_set)) {
      const id = String(event.change_set.id || '');
      if (id) {
        this.changeSets.set(id, event.change_set);
        explicitChangeSetId = id;
      }
      this.changeDocuments.remember(event.change_set);
    }
    const value = isRecord(event.value) ? event.value : {};
    if (isRecord(value.change_set)) {
      const id = String(value.change_set.id || '');
      if (id) {
        this.changeSets.set(id, value.change_set);
        explicitChangeSetId = id;
      }
      this.changeDocuments.remember(value.change_set);
    }
    const state = isRecord(event.state) ? event.state : {};
    const session = isRecord(state.session) ? state.session : {};
    const sessionId = String(session.id || '');
    if (sessionId && sessionId !== this.currentSessionId) {
      this.currentSessionId = sessionId;
      this.currentChangeSetId = '';
    }
    this.changeDocuments.rememberAll(state.change_sets);
    if (Array.isArray(state.change_sets)) {
      for (const raw of state.change_sets) {
        if (!isRecord(raw)) continue;
        const id = String(raw.id || '');
        if (id) {
          this.changeSets.set(id, raw);
        }
      }
    }
    if (explicitChangeSetId) {
      this.currentChangeSetId = explicitChangeSetId;
    } else if (!this.currentChangeSetId && Array.isArray(state.change_sets)) {
      const latest = state.change_sets.at(-1);
      if (isRecord(latest)) this.currentChangeSetId = String(latest.id || '');
    }
  }

  /** 事件进入 Webview 前，移除仅允许 Host 使用的快照路径。 */
  private forWebview(event: Record<string, unknown>): Record<string, unknown> {
    const visible = { ...event };
    if (isRecord(visible.change_set)) {
      visible.change_set = this.visibleChangeSet(visible.change_set);
    }
    if (isRecord(visible.value) && isRecord(visible.value.change_set)) {
      visible.value = {
        ...visible.value,
        change_set: this.visibleChangeSet(visible.value.change_set),
      };
    }
    if (isRecord(visible.state) && Array.isArray(visible.state.change_sets)) {
      visible.state = {
        ...visible.state,
        change_sets: visible.state.change_sets.map((item) => this.visibleChangeSet(item)),
      };
    }
    return visible;
  }

  /** 将完整 Host 变更集投影为 UI 渲染所需且可安全公开的字段。 */
  private visibleChangeSet(raw: unknown): Record<string, unknown> {
    const changeSet = isRecord(raw) ? raw : {};
    const files = Array.isArray(changeSet.files)
      ? changeSet.files.map((rawFile) => {
          const file = isRecord(rawFile) ? rawFile : {};
          return {
            path: String(file.path || ''),
            status: String(file.status || ''),
            reversible: Boolean(file.reversible),
            additions: Number(file.additions || 0),
            deletions: Number(file.deletions || 0),
          };
        })
      : [];
    return {
      id: String(changeSet.id || ''),
      run_id: String(changeSet.run_id || ''),
      conversation_id: String(changeSet.conversation_id || ''),
      state: String(changeSet.state || ''),
      message: String(changeSet.message || ''),
      files,
    };
  }

  /** 返回当前打开会话所关联的变更集。 */
  private latestChangeSetId(): string {
    return this.currentChangeSetId;
  }

  /**
   * 读取一次 Agent run 所修改文件的 VS Code 诊断。
   * 诊断进入 Webview 前会限制数量，从而控制 UI 工作量，并避免超大的 Problems
   * 集合形成巨型消息。
   */
  private async publishDiagnostics(webview: vscode.Webview, changeSetId: string): Promise<void> {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (!workspace) return;
    const changeSet = this.changeSets.get(changeSetId);
    if (!isRecord(changeSet) || !Array.isArray(changeSet.files)) return;
    const results: Record<string, unknown>[] = [];
    for (const rawFile of changeSet.files) {
      const item = isRecord(rawFile) ? rawFile : {};
      const path = String(item.path || '');
      if (!path) continue;
      const uri = vscode.Uri.joinPath(workspace.uri, path);
      for (const diagnostic of vscode.languages.getDiagnostics(uri)) {
        if (diagnostic.severity > vscode.DiagnosticSeverity.Warning) continue;
        results.push({
          path,
          line: diagnostic.range.start.line + 1,
          character: diagnostic.range.start.character + 1,
          severity: diagnostic.severity === vscode.DiagnosticSeverity.Error ? 'error' : 'warning',
          message: diagnostic.message,
        });
      }
    }
    await webview.postMessage({
      type: 'backendEvent',
      event: {
        type: 'changeDiagnostics',
        changeSetId,
        diagnostics: results.slice(0, 50),
      },
    });
  }

  /**
   * 打开模型或诊断引用的本地代码位置。
   *
   * 相对路径始终以当前工作区为基准且不得通过 `..` 越界；绝对路径保留原语义，
   * 以支持用户要求模型解释工作区外文件。实际行号会收敛到文档范围内。
   */
  private async openLocation(requestedPath: string, line: number): Promise<void> {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    const rawPath = requestedPath.trim();
    if (!workspace || !rawPath || rawPath.includes('\0')) return;

    const workspaceRoot = workspace.uri.fsPath;
    const absolutePath = path.isAbsolute(rawPath)
      ? path.resolve(rawPath)
      : path.resolve(workspaceRoot, rawPath);
    if (!path.isAbsolute(rawPath)) {
      const relative = path.relative(workspaceRoot, absolutePath);
      if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        throw new Error('Relative code location is outside the current workspace.');
      }
    }

    const uri = vscode.Uri.file(absolutePath);
    const document = await vscode.workspace.openTextDocument(uri);
    const editor = await vscode.window.showTextDocument(document);
    const requestedLine = Number.isFinite(line) ? Math.floor(line) : 1;
    const lineIndex = Math.min(
      Math.max(0, requestedLine - 1),
      Math.max(0, document.lineCount - 1),
    );
    const position = new vscode.Position(lineIndex, 0);
    editor.selection = new vscode.Selection(position, position);
    editor.revealRange(new vscode.Range(position, position));
  }

  /**
   * 应用本地弹出菜单选择的 Provider、model 或 approval 值。
   * 后端命令仍是最终状态来源，并负责返回更新后的状态。
   */
  private async setRuntimeSetting(message: Record<string, unknown>): Promise<void> {
    const setting = String(message.setting || '');
    const value = String(message.value || '');
    if (!['provider', 'model', 'approval'].includes(setting) || !value) {
      return;
    }
    const key = setting === 'approval' ? 'mode' : setting;
    await this.backend.sendCommand(setting, { [key]: value });
  }

  /**
   * 构造受 CSP 保护的侧边栏静态 HTML 外壳。
   *
   * 该方法只声明稳定的 DOM 挂载点，所有动态渲染由编译后的 webview.js 完成。
   * 每个文档独立的 nonce 只允许当前脚本运行，localResourceRoots 则限制可加载的
   * 扩展资源目录。
   */
  private buildHtml(webview: vscode.Webview): string {
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, 'dist', 'webview.js'),
    );
    const styleUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, 'media', 'chat.css'),
    );
    const nonce = createNonce();

    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8">
    <meta
      http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src ${webview.cspSource} data: https:; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';"
    >
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="${styleUri}" rel="stylesheet">
    <title>CodeMate</title>
  </head>
  <body>
    <main class="app-shell">
      <header class="status-bar">
        <div class="runtime-status">
          <strong id="connection-label">Connecting</strong>
          <span id="runtime-label"></span>
        </div>
        <button id="settings-button" class="icon-button" type="button" title="Settings" aria-label="Settings">⚙</button>
      </header>

      <section id="home-page" class="page home-page">
        <div class="home-heading">
          <h1>CodeMate</h1>
          <p>Continue a session or start a new task.</p>
        </div>
        <div id="session-list" class="session-list" aria-live="polite"></div>
        <form id="new-task-form" class="composer home-composer">
          <label class="sr-only" for="new-task-input">New task</label>
          <textarea id="new-task-input" rows="3" placeholder="Start a new task" aria-label="New task"></textarea>
          <div class="composer-footer">
            <div id="home-runtime-controls" class="runtime-controls"></div>
            <button id="new-task-button" class="send-icon" type="submit" disabled title="Start session" aria-label="Start session">↑</button>
          </div>
        </form>
      </section>

      <section id="chat-page" class="page chat-page" hidden>
        <header class="chat-header">
          <button id="back-button" class="icon-button" type="button" title="Back to sessions" aria-label="Back to sessions">←</button>
          <strong id="session-title">Session</strong>
          <button id="rename-current-button" class="icon-button" type="button" title="Rename session" aria-label="Rename session">✎</button>
        </header>
        <section id="messages" class="messages" aria-live="polite"></section>
        <form id="composer" class="composer chat-composer">
          <label class="sr-only" for="message-input">Message</label>
          <div class="input-wrap">
            <textarea id="message-input" rows="2" placeholder="Ask CodeMate or enter /" aria-label="Message"></textarea>
            <div id="command-menu" class="command-menu" hidden></div>
          </div>
          <div class="composer-footer">
            <div id="chat-runtime-controls" class="runtime-controls"></div>
            <div class="attachment-control">
              <button id="attachment-button" class="icon-button" type="button" title="Attach editor context" aria-label="Attach editor context">+</button>
              <div id="attachment-menu" class="attachment-menu" hidden></div>
            </div>
            <button id="stop-button" class="stop-icon" type="button" hidden title="Stop current request" aria-label="Stop current request">■</button>
            <button id="send-button" class="send-icon" type="submit" disabled title="Send" aria-label="Send">↑</button>
          </div>
          <div id="attachment-chips" class="attachment-chips" hidden></div>
        </form>
      </section>
    </main>
    <script nonce="${nonce}" src="${scriptUri}"></script>
  </body>
</html>`;
  }
}

/** 生成附加到 Webview script 标签上的随机 CSP nonce。 */
function createNonce(): string {
  const alphabet =
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let nonce = '';
  for (let index = 0; index < 32; index += 1) {
    nonce += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
  }
  return nonce;
}
