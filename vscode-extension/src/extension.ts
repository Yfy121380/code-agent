import * as vscode from 'vscode';

import { ChatViewProvider, CHAT_VIEW_ID } from './chatViewProvider';
import { CodeMateProcess } from './codemateProcess';
import { ChangeDocumentProvider } from './changeDocumentProvider';

// 命令 ID 是扩展公开声明的一部分。只有这里的值和 package.json 一致，VS Code
// 才能正确找到并调用该命令。
const SHOW_WELCOME_COMMAND = 'codemate.showWelcome';

/**
 * VS Code 激活扩展时，组装 CodeMate 在 Extension Host 中运行的部分。
 *
 * 该函数只负责依赖装配：Python 进程生命周期由 CodeMateProcess 管理，Webview
 * 消息路由由 ChatViewProvider 管理，虚拟 Diff 文档由 ChangeDocumentProvider
 * 管理。职责分开后，这个文件本身就是扩展顶层架构的入口图。
 */
export function activate(context: vscode.ExtensionContext): void {
  // OutputChannel 用于记录后端启动过程和 stderr，与用户看到的聊天记录分开。
  const output = vscode.window.createOutputChannel('CodeMate');
  const backend = new CodeMateProcess(context.extensionUri, output);
  const changeDocuments = new ChangeDocumentProvider();
  changeDocuments.register(context);

  // 这个简单命令也用于在开发时确认 package.json 命令声明和扩展激活已经连通。
  const showWelcome = vscode.commands.registerCommand(
    SHOW_WELCOME_COMMAND,
    async () => {
      const workspaceName = vscode.workspace.name ?? 'an empty window';
      await vscode.window.showInformationMessage(
        `CodeMate is active for ${workspaceName}.`,
      );
    },
  );
  // 将 ChatViewProvider 注册为 codemate.chatView 侧边栏视图的内容提供者。
  const chatView = vscode.window.registerWebviewViewProvider(
    CHAT_VIEW_ID,
    new ChatViewProvider(context.extensionUri, backend, changeDocuments),
    // 当用户切换到其他侧边栏、暂时隐藏 CodeMate 视图时，尽量保留 Webview 当前的浏览器上下文。
    { webviewOptions: { retainContextWhenHidden: true } },
  );

  // Extension Host 关闭时，subscriptions 中的对象会被统一释放，其中
  // CodeMateProcess 会负责终止 Python 子进程。
  context.subscriptions.push(output, backend, showWelcome, chatView);
}

/**
 * VS Code 停用扩展时调用该钩子。所有资源都已注册到 context.subscriptions，
 * 因此这里不需要再次手动释放。
 */
export function deactivate(): void {}
