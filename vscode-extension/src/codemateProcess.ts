/** 管理当前 VS Code 窗口对应的一个长期运行 CodeMate JSONL 后端。 */

import { ChildProcessWithoutNullStreams, spawn } from 'node:child_process';
import * as readline from 'node:readline';
import { randomUUID } from 'node:crypto';
import * as path from 'node:path';

import * as vscode from 'vscode';

import { BridgeEvent, isBridgeEvent } from './protocol';

const HELP_TEXT = `Available commands:
/approval [ask|auto|read_only|full]
/plan | /plan exit
/review [focus]
/provider [name]
/model [name]
/budget | /compact
/remember <text>
/dream [--background]
/session | /session list | /session rename <title> | /session resume <id> | /session new
/reset`;

/**
 * 管理一个 Python Bridge 进程，并将其封装为事件驱动客户端。
 *
 * 请求通过 stdin 以逐行 JSON 发送给 Python，事件通过 stdout 以逐行 JSON 返回，
 * 再由 onEvent 发布。stderr 专门用于诊断日志，防止普通日志污染协议数据流。
 */
export class CodeMateProcess implements vscode.Disposable {
  // 同一时刻只允许一个用户请求运行；但该请求在 Python 中等待时，审批等交互响应
  // 仍然可以通过独立消息发送。
  private child: ChildProcessWithoutNullStreams | undefined;
  private readyEvent: BridgeEvent | undefined;
  private activeRequestId = '';
  private stopping = false;
  private readonly eventEmitter = new vscode.EventEmitter<BridgeEvent>();

  readonly onEvent = this.eventEmitter.event;

  /**
   * 保存扩展安装目录和诊断输出通道；构造阶段不会立即启动 Python，真正启动采用
   * start() 中的懒加载方式。
   */
  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly output: vscode.OutputChannel,
  ) {}

  /** 最近一次完整 ready 状态，用于 Webview 被重新创建后的状态恢复。 */
  get lastReadyEvent(): BridgeEvent | undefined {
    return this.readyEvent;
  }

  /**
   * 按需启动 Bridge，并等待其 ready 事件。
   *
   * 启动命令可以配置，因此开发环境可以使用 `uv run ...`，打包后也可以指定其他
   * 启动器。workspace、provider 和 model 设置会在这里转换为后端 CLI 参数。
   */
  async start(): Promise<void> {
    if (this.child) {
      await this.waitUntilReady();
      return;
    }
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!workspaceRoot) {
      throw new Error('Open a workspace folder before starting CodeMate.');
    }

    const config = vscode.workspace.getConfiguration('codemate');
    const command = config.get<string>('backend.command', 'uv').trim();
    if (!command) {
      throw new Error('CodeMate backend command cannot be empty.');
    }
    const configuredArgs = config.get<string[]>('backend.arguments', []);
    const extensionRoot = this.extensionUri.fsPath;
    const substitutions: Record<string, string> = {
      '${extensionRoot}': extensionRoot,
      '${workspaceFolder}': workspaceRoot,
    };
    const substitute = (value: string): string =>
      Object.entries(substitutions).reduce(
        (result, [token, replacement]) => result.replaceAll(token, replacement),
        value,
      );
    const commandArgs = configuredArgs.map(substitute);
    commandArgs.push(
      '--cwd',
      workspaceRoot,
      '--provider',
      config.get<string>('provider', 'openai'),
      '--approval',
      config.get<string>('approval', 'ask'),
    );
    const model = config.get<string>('model', '').trim();
    if (model) {
      commandArgs.push('--model', model);
    }
    if (config.get<boolean>('session.resumeLatest', true)) {
      commandArgs.push('--resume', 'latest');
    }

    const childEnvironment = this.buildBackendEnvironment(config, substitute);
    this.output.appendLine(`Starting backend: ${command} ${commandArgs.join(' ')}`);
    const child = spawn(command, commandArgs, {
      cwd: workspaceRoot,
      env: childEnvironment,
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.child = child;

    // JSONL 以换行作为边界，因此 stdout 的每一行都应是一个完整协议事件。
    const lines = readline.createInterface({ input: child.stdout });
    lines.on('line', (line) => this.handleLine(line));
    child.stderr.on('data', (chunk: Buffer) => this.output.append(chunk.toString()));
    child.on('error', (error) => {
      this.emitLocal('connection_error', { message: error.message });
    });
    child.on('exit', (code, signal) => {
      const expected = this.stopping;
      lines.close();
      if (this.child === child) {
        this.child = undefined;
        this.activeRequestId = '';
        this.readyEvent = undefined;
        this.stopping = false;
      }
      this.emitLocal('disconnected', { code, signal, expected });
    });

    try {
      await this.waitUntilReady();
    } catch (error) {
      if (this.child === child) {
        child.kill('SIGTERM');
      }
      throw error;
    }
  }

  /** 将 Bridge 自身解释器和 run_shell 使用的命令环境分开。 */
  private buildBackendEnvironment(
    config: vscode.WorkspaceConfiguration,
    substitute: (value: string) => string,
  ): NodeJS.ProcessEnv {
    const environment = { ...process.env };
    const originalPath = process.env.PATH || '';
    const configuredPython = substitute(
      config.get<string>('shell.pythonPath', '').trim(),
    );
    let shellPath = originalPath;
    if (configuredPython) {
      if (!path.isAbsolute(configuredPython)) {
        throw new Error(
          'CodeMate shell Python path must be absolute or start with ${workspaceFolder}.',
        );
      }
      const pythonDirectory = path.dirname(configuredPython);
      const remaining = originalPath
        .split(path.delimiter)
        .filter((entry) => entry && entry !== pythonDirectory);
      shellPath = [pythonDirectory, ...remaining].join(path.delimiter);
      this.output.appendLine(`Shell Python: ${configuredPython}`);
    }
    if (shellPath) {
      // uv 启动 Bridge 时可能重写 PATH，因此通过私有环境变量保留预期的工具环境，
      // runtime 启动后只读取一次。
      environment.CODEMATE_SHELL_PATH = shellPath;
    }
    return environment;
  }

  /** 停止现有 Bridge，并使用当前配置启动新进程。 */
  async restart(): Promise<void> {
    this.emitLocal('connecting', {});
    await this.stop();
    await this.start();
  }

  /**
   * 先请求优雅关闭，再依次升级为 SIGTERM 和 SIGKILL。
   * 超时控制可以避免扩展停用时永久等待卡住的后端。
   */
  async stop(): Promise<void> {
    const child = this.child;
    if (!child) {
      return;
    }
    this.stopping = true;
    if (child.stdin.writable) {
      this.sendRaw({ type: 'shutdown' });
    } else {
      child.kill('SIGTERM');
    }
    await new Promise<void>((resolve) => {
      const forceTimeout = setTimeout(() => {
        child.kill('SIGTERM');
      }, 2000);
      const finishTimeout = setTimeout(() => {
        child.kill('SIGKILL');
        if (this.child === child) {
          this.child = undefined;
          this.readyEvent = undefined;
          this.activeRequestId = '';
          this.stopping = false;
        }
        resolve();
      }, 4000);
      child.once('exit', () => {
        clearTimeout(forceTimeout);
        clearTimeout(finishTimeout);
        resolve();
      });
    });
  }

  /** 将普通文本发送给 ask，或在本地把斜杠命令转换为结构化命令。 */
  async sendInput(
    text: string,
    attachments: unknown[] = [],
    responseAnnotations: unknown[] = [],
  ): Promise<string> {
    const requestId = await this.beginRequest();
    // 带批注的输入始终是 Agent 请求，避免把 `/compact` 等文字误解释为命令后
    // 丢掉批注上下文。
    const command = responseAnnotations.length === 0
      ? parseSlashCommand(text)
      : undefined;
    if (command) {
      if (command.name === 'help') {
        this.activeRequestId = '';
        this.emitLocal('command_result', {
          request_id: requestId,
          name: 'help',
          status: 'ok',
          value: { report: HELP_TEXT },
        });
      } else {
        this.sendRaw({ id: requestId, type: 'command', ...command });
      }
    } else {
      this.sendRaw({
        id: requestId,
        type: 'ask',
        text,
        attachments,
        response_annotations: responseAnnotations,
      });
    }
    return requestId;
  }

  /** 创建新后端会话，并将文本作为该会话的第一个任务。 */
  async startSession(text: string, attachments: unknown[] = []): Promise<string> {
    const requestId = await this.beginRequest();
    this.sendRaw({ id: requestId, type: 'new_ask', text, attachments });
    return requestId;
  }

  /** 恢复最近请求前的 checkpoint，并执行替换后的请求文本。 */
  async retryLastRequest(
    text: string,
    attachments?: unknown[],
    responseAnnotations?: unknown[],
  ): Promise<string> {
    const requestId = await this.beginRequest();
    const message: Record<string, unknown> = { id: requestId, type: 'retry', text };
    if (attachments) message.attachments = attachments;
    if (responseAnnotations) message.response_annotations = responseAnnotations;
    this.sendRaw(message);
    return requestId;
  }

  /** 通过同一请求生命周期发送结构化的非聊天命令。 */
  async sendCommand(
    name: string,
    args: Record<string, unknown> = {},
  ): Promise<string> {
    const requestId = await this.beginRequest();
    this.sendRaw({ id: requestId, type: 'command', name, args });
    return requestId;
  }

  /** 将用户审批、问题回答或计划决策返回给 Python。 */
  respond(interactionId: string, value: unknown): void {
    this.sendRaw({ type: 'interaction_response', interaction_id: interactionId, value });
  }

  /** 请求 Python 中断当前正在执行的 Agent 请求。 */
  cancel(): void {
    if (this.activeRequestId) {
      this.sendRaw({ type: 'cancel', request_id: this.activeRequestId });
    }
  }

  /** 扩展关闭时释放子进程和 VS Code 事件发射器。 */
  dispose(): void {
    this.child?.kill('SIGTERM');
    this.child = undefined;
    this.eventEmitter.dispose();
  }

  /** 解析并校验 Python stdout 返回的一条完整 JSONL 事件。 */
  private handleLine(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      this.output.appendLine(`Invalid backend JSON: ${line}`);
      this.emitLocal('protocol_error', { message: 'The backend returned invalid JSON.' });
      return;
    }
    if (!isBridgeEvent(parsed)) {
      this.emitLocal('protocol_error', { message: 'The backend returned an invalid event.' });
      return;
    }
    if (parsed.type === 'ready') {
      this.readyEvent = parsed;
    }
    if (parsed.type === 'run_finished' || parsed.type === 'command_result') {
      if (!parsed.request_id || parsed.request_id === this.activeRequestId) {
        this.activeRequestId = '';
      }
    }
    this.eventEmitter.fire(parsed);
  }

  /** 将一条命令序列化为一行 JSON，并写入子进程 stdin。 */
  private sendRaw(message: Record<string, unknown>): void {
    if (!this.child?.stdin.writable) {
      throw new Error('CodeMate backend is not connected.');
    }
    this.child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  /**
   * 确保 Bridge 已就绪，并占用唯一的活动请求槽位。
   * UUID 用于关联整轮事件，也可防止两个请求同时更新同一聊天界面。
   */
  private async beginRequest(): Promise<string> {
    await this.start();
    if (this.activeRequestId) {
      throw new Error('CodeMate is already processing a request.');
    }
    const requestId = randomUUID();
    this.activeRequestId = requestId;
    return requestId;
  }

  /** 通过统一事件 API 发布由扩展本地产生的事件。 */
  private emitLocal(type: string, payload: Record<string, unknown>): void {
    this.eventEmitter.fire({ type, ...payload });
  }

  /** 等待启动成功；若超时或收到终止类启动事件则拒绝。 */
  private waitUntilReady(): Promise<void> {
    if (this.readyEvent) {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        subscription.dispose();
        reject(new Error('CodeMate backend did not become ready within 15 seconds.'));
      }, 15000);
      const subscription = this.onEvent((event) => {
        if (event.type === 'ready') {
          clearTimeout(timeout);
          subscription.dispose();
          resolve();
        } else if (
          event.type === 'startup_error'
          || event.type === 'connection_error'
          || event.type === 'disconnected'
        ) {
          clearTimeout(timeout);
          subscription.dispose();
          reject(new Error(String(event.message || 'CodeMate backend failed to start.')));
        }
      });
    });
  }
}

interface ParsedCommand {
  name: string;
  args: Record<string, unknown>;
}

/**
 * 将 Webview 中的斜杠命令语法转换为 Bridge 命令结构。
 * 返回 undefined 表示输入是普通 Agent 请求。
 */
function parseSlashCommand(input: string): ParsedCommand | undefined {
  const text = input.trim();
  if (!text.startsWith('/')) {
    return undefined;
  }
  if (text === '/help') return { name: 'help', args: {} };
  if (text === '/approval') return { name: 'approval', args: {} };
  if (text.startsWith('/approval ')) return { name: 'approval', args: { mode: text.slice(10).trim() } };
  if (text === '/plan') return { name: 'plan_enter', args: {} };
  if (text === '/plan exit') return { name: 'plan_exit', args: {} };
  if (text === '/review' || text.startsWith('/review ')) return { name: 'review', args: { focus: text.slice(7).trim() } };
  if (text === '/provider') return { name: 'provider', args: {} };
  if (text.startsWith('/provider ')) return { name: 'provider', args: { provider: text.slice(10).trim() } };
  if (text === '/model') return { name: 'model', args: {} };
  if (text.startsWith('/model ')) return { name: 'model', args: { model: text.slice(7).trim() } };
  if (text === '/budget') return { name: 'budget', args: {} };
  if (text === '/compact') return { name: 'compact', args: {} };
  if (text.startsWith('/remember ')) return { name: 'remember', args: { text: text.slice(10).trim() } };
  if (text === '/dream') return { name: 'dream', args: {} };
  if (text === '/dream --background') return { name: 'dream', args: { background: true } };
  if (text === '/session') return { name: 'session_current', args: {} };
  if (text === '/session list') return { name: 'session_list', args: {} };
  if (text === '/session new') return { name: 'session_new', args: {} };
  if (text.startsWith('/session rename ')) return { name: 'session_rename', args: { title: text.slice(16).trim() } };
  if (text.startsWith('/session resume ')) return { name: 'session_resume', args: { session_id: text.slice(16).trim() } };
  if (text === '/reset') return { name: 'reset', args: {} };
  return { name: '__unknown__', args: { input: text } };
}
