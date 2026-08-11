/** 为 VS Code 原生 Diff 编辑器提供只读虚拟文档。 */

import { promises as fs } from 'node:fs';

import * as vscode from 'vscode';

import { isRecord } from './protocol';

const CHANGE_SCHEME = 'codemate-change';

/** 单个变更文件在 Extension Host 内部使用的快照位置。 */
interface ChangeFile {
  path: string;
  beforeSnapshot: string;
  afterSnapshot: string;
}

/** 快照路径只保留在 Extension Host，Webview 只传递稳定 ID。 */
export class ChangeDocumentProvider implements vscode.TextDocumentContentProvider {
  // VS Code 打开的是虚拟 URI；该映射再把 URI 还原为仅受信任 Extension Host
  // 可见的本地快照文件。
  private readonly documents = new Map<string, string>();
  private readonly changeSets = new Map<string, Map<string, ChangeFile>>();

  /** 将 codemate-change: scheme 注册为只读文档来源。 */
  register(context: vscode.ExtensionContext): void {
    context.subscriptions.push(
      vscode.workspace.registerTextDocumentContentProvider(CHANGE_SCHEME, this),
    );
  }

  /**
   * 缓存一个后端变更集中的快照路径。
   *
   * Webview 后续只使用变更集 ID 和相对路径标识 Diff，绝对快照路径不需要进入
   * 浏览器沙箱。
   */
  remember(rawChangeSet: unknown): void {
    const changeSet = isRecord(rawChangeSet) ? rawChangeSet : {};
    const id = String(changeSet.id || '').trim();
    if (!id || !Array.isArray(changeSet.files)) return;
    const files = new Map<string, ChangeFile>();
    for (const rawFile of changeSet.files) {
      const item = isRecord(rawFile) ? rawFile : {};
      const filePath = String(item.path || '').trim();
      if (!filePath) continue;
      files.set(filePath, {
        path: filePath,
        beforeSnapshot: String(item.before_snapshot || ''),
        afterSnapshot: String(item.after_snapshot || ''),
      });
    }
    this.changeSets.set(id, files);
  }

  /** 缓存恢复会话状态中包含的所有变更集。 */
  rememberAll(rawChangeSets: unknown): void {
    if (!Array.isArray(rawChangeSets)) return;
    for (const changeSet of rawChangeSets) this.remember(changeSet);
  }

  /**
   * VS Code 打开 codemate-change: 虚拟文档时提供其文本内容。
   * 原生文本 Diff 无法正确表示二进制内容，因此二进制快照会被拒绝。
   */
  async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const snapshotPath = this.documents.get(uri.toString());
    if (!snapshotPath) return '';
    const data = await fs.readFile(snapshotPath);
    if (data.includes(0)) {
      throw new Error('Binary snapshots cannot be displayed as text diffs.');
    }
    return data.toString('utf8');
  }

  /** 在 VS Code 原生 Diff 编辑器中打开选定文件的 before/after 快照。 */
  async openDiff(changeSetId: string, filePath: string): Promise<void> {
    const file = this.changeSets.get(changeSetId)?.get(filePath);
    if (!file) throw new Error('The requested change snapshot is unavailable.');
    const before = this.snapshotUri(changeSetId, file, 'before');
    const after = this.snapshotUri(changeSetId, file, 'after');
    await vscode.commands.executeCommand(
      'vscode.diff',
      before,
      after,
      `${file.path} (Before ↔ After)`,
      { preview: true },
    );
  }

  /**
   * 构造稳定的虚拟 URI，并将其绑定到对应本地快照。
   * 新增或删除文件的一侧没有快照，此时有意返回空文档用于 Diff 展示。
   */
  private snapshotUri(
    changeSetId: string,
    file: ChangeFile,
    side: 'before' | 'after',
  ): vscode.Uri {
    const uri = vscode.Uri.from({
      scheme: CHANGE_SCHEME,
      path: `/${encodeURIComponent(changeSetId)}/${side}/${file.path}`,
      query: `side=${side}`,
    });
    const snapshot = side === 'before' ? file.beforeSnapshot : file.afterSnapshot;
    if (snapshot) this.documents.set(uri.toString(), snapshot);
    return uri;
  }
}
