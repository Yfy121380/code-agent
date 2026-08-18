/** 将 Python 的无界面诊断请求映射到 VS Code Diagnostics API。 */

import * as path from 'node:path';
import * as vscode from 'vscode';

import { CodeMateProcess } from './codemateProcess';
import { BridgeEvent } from './protocol';

const BASELINE_REFRESH_TIMEOUT_MS = 800;
const POST_EDIT_REFRESH_TIMEOUT_MS = 1500;
const DIAGNOSTIC_SETTLE_MS = 120;
const MAX_EDITOR_ERRORS_PER_SNAPSHOT = 100;

/**
 * 在 Extension Host 内处理诊断请求。
 *
 * 请求不会转发给 Webview，也不会显示交互控件。它只读取 VS Code 已聚合的静态
 * 诊断，并通过既有 interaction_response 通道返回给等待中的 Python 工具线程。
 */
export class EditorDiagnosticsBridge {
  constructor(private readonly backend: CodeMateProcess) {}

  async handle(event: BridgeEvent): Promise<void> {
    if (event.type !== 'editor_diagnostics_request') return;
    const interactionId = String(event.interaction_id || '');
    if (!interactionId) return;
    try {
      const uri = this.workspaceFileUri(String(event.path || ''));
      if (!uri) {
        this.backend.respond(interactionId, { status: 'unavailable', diagnostics: [] });
        return;
      }
      const waitForUpdate = Boolean(event.wait_for_update);
      const diagnostics = await this.collect(uri, waitForUpdate);
      this.backend.respond(interactionId, { status: 'ok', diagnostics });
    } catch {
      // 诊断只是可选的编辑器辅助，语言服务器或文件读取失败不能把成功修改改成失败。
      this.backend.respond(interactionId, { status: 'unavailable', diagnostics: [] });
    }
  }

  /** 解析请求路径，并拒绝访问所有已打开工作区之外的文件。 */
  private workspaceFileUri(requestedPath: string): vscode.Uri | undefined {
    const roots = vscode.workspace.workspaceFolders || [];
    if (!requestedPath.trim() || requestedPath.includes('\0') || roots.length === 0) {
      return undefined;
    }
    if (path.isAbsolute(requestedPath)) {
      const resolved = path.resolve(requestedPath);
      return roots.some((root) => this.isWithin(root.uri.fsPath, resolved))
        ? vscode.Uri.file(resolved)
        : undefined;
    }
    const resolved = path.resolve(roots[0].uri.fsPath, requestedPath);
    return this.isWithin(roots[0].uri.fsPath, resolved) ? vscode.Uri.file(resolved) : undefined;
  }

  private isWithin(root: string, candidate: string): boolean {
    const relative = path.relative(path.resolve(root), path.resolve(candidate));
    return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
  }

  /**
   * 在不显示编辑器的情况下打开文档，并按需等待语言服务器发布新诊断。
   */
  private async collect(uri: vscode.Uri, waitForUpdate: boolean): Promise<Record<string, unknown>[]> {
    try {
      await vscode.workspace.fs.stat(uri);
    } catch {
      // write_file 创建新文件之前不存在，因此它的诊断基线为空。
      if (!waitForUpdate) return [];
      throw new Error('VS Code 无法读取修改后的文件。');
    }
    const wasOpen = vscode.workspace.textDocuments.some((document) => document.uri.toString() === uri.toString());
    const refresh = waitForUpdate || !wasOpen
      ? this.waitForRefresh(uri, waitForUpdate ? POST_EDIT_REFRESH_TIMEOUT_MS : BASELINE_REFRESH_TIMEOUT_MS)
      : Promise.resolve();
    try {
      await vscode.workspace.openTextDocument(uri);
    } catch {
      throw new Error('VS Code 无法打开修改后的文件。');
    }
    await refresh;
    const relativePath = vscode.workspace.asRelativePath(uri, false);
    return vscode.languages.getDiagnostics(uri)
      .filter((item) => item.severity === vscode.DiagnosticSeverity.Error)
      .slice(0, MAX_EDITOR_ERRORS_PER_SNAPSHOT)
      .map((item) => ({
        path: relativePath,
        line: item.range.start.line + 1,
        column: item.range.start.character + 1,
        severity: 'error',
        message: item.message,
        source: item.source || '',
        code: typeof item.code === 'object' && item.code !== null
          ? String(item.code.value)
          : String(item.code || ''),
      }));
  }

  /** 等待目标文件的诊断事件短暂稳定，同时设置硬超时防止工具循环卡住。 */
  private waitForRefresh(uri: vscode.Uri, timeoutMs: number): Promise<void> {
    return new Promise((resolve) => {
      let settled = false;
      let settleTimer: NodeJS.Timeout | undefined;
      const finish = () => {
        if (settled) return;
        settled = true;
        if (settleTimer) clearTimeout(settleTimer);
        clearTimeout(deadline);
        subscription.dispose();
        resolve();
      };
      const subscription = vscode.languages.onDidChangeDiagnostics((change) => {
        if (!change.uris.some((changed) => changed.toString() === uri.toString())) return;
        if (settleTimer) clearTimeout(settleTimer);
        settleTimer = setTimeout(finish, DIAGNOSTIC_SETTLE_MS);
      });
      const deadline = setTimeout(finish, timeoutMs);
    });
  }
}
