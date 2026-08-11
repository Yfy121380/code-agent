/**
 * CodeMate Webview 中运行的会话浏览器和聊天渲染器。
 *
 * 该脚本运行在隔离浏览器中，而不是 VS Code Extension Host。它负责 DOM 状态，
 * 并通过 postMessage 发送声明式消息；文件、进程和编辑器操作必须交给
 * ChatViewProvider 处理。
 */

import DOMPurify from 'dompurify';
import { marked } from 'marked';

/** VS Code 注入 Webview 的最小消息 Bridge API。 */
interface VsCodeApi {
  postMessage(message: unknown): void;
}

// 调用一次即可建立页面消息通道，同时不会向浏览器代码暴露受信任 Extension Host
// 或完整的 `vscode` 模块。
declare function acquireVsCodeApi(): VsCodeApi;

/** 后端数据经过 Extension Host 清理后使用的通用事件结构。 */
interface BackendEvent {
  type: string;
  [key: string]: unknown;
}

/** 一轮用户/助手交互所对应的 DOM 引用和请求文本。 */
interface TurnView {
  id: string;
  requestText: string;
  root: HTMLElement;
  process: HTMLDetailsElement;
  processBody: HTMLElement;
  thinking?: HTMLElement;
  final?: HTMLElement;
  finalArticle?: HTMLElement;
  retryButton?: HTMLButtonElement;
  changes?: HTMLElement;
}

/** 用户针对一条已完成最终回答创建的待发送批注。 */
interface ResponseAnnotation {
  id: string;
  source_message_id: string;
  source_content_hash: string;
  selected_text: string;
  surrounding_text: string;
  comment: string;
  anchor_top?: number;
}

/** 用户已选择、但尚未发送给 Agent 的编辑器上下文。 */
interface EditorAttachment {
  id: string;
  kind: string;
  label: string;
  path?: string;
  content: string;
  start_line?: number;
  end_line?: number;
}

/** 一段流式 commentary 或 final answer 的累积状态。 */
interface StreamView {
  phase: string;
  text: string;
  finalText: string;
  article?: HTMLElement;
  content?: HTMLElement;
  renderFrame?: number;
}

/** 跟踪 compact 展示行，使后续状态事件更新同一个 DOM 节点。 */
interface CompactView {
  result: HTMLElement;
  complete: boolean;
}

interface CommandSuggestion {
  command: string;
  description: string;
}

// 斜杠命令元数据用于生成输入补全菜单；真正的解析和执行仍由 Extension Host 与
// Python Bridge 完成。
const COMMANDS: CommandSuggestion[] = [
  { command: '/plan', description: 'Enter Plan Mode' },
  { command: '/plan exit', description: 'Exit Plan Mode' },
  { command: '/review', description: 'Review current changes' },
  { command: '/budget', description: 'Show context budget' },
  { command: '/compact', description: 'Compact history' },
  { command: '/remember ', description: 'Remember project context' },
  { command: '/dream', description: 'Consolidate long-term memory' },
  { command: '/reset', description: 'Clear current conversation' },
  { command: '/help', description: 'Show available commands' },
];

// 启动时一次性解析固定 HTML 挂载点。缺少 ID 属于编程错误，因此应立即失败，而不是
// 稍后才出现难以定位的空引用。
const vscode = acquireVsCodeApi();
const homePage = requiredElement<HTMLElement>('home-page');
const chatPage = requiredElement<HTMLElement>('chat-page');
const sessionList = requiredElement<HTMLElement>('session-list');
const newTaskForm = requiredElement<HTMLFormElement>('new-task-form');
const newTaskInput = requiredElement<HTMLTextAreaElement>('new-task-input');
const newTaskButton = requiredElement<HTMLButtonElement>('new-task-button');
const composer = requiredElement<HTMLFormElement>('composer');
const input = requiredElement<HTMLTextAreaElement>('message-input');
const sendButton = requiredElement<HTMLButtonElement>('send-button');
const stopButton = requiredElement<HTMLButtonElement>('stop-button');
const backButton = requiredElement<HTMLButtonElement>('back-button');
const renameCurrentButton = requiredElement<HTMLButtonElement>('rename-current-button');
const settingsButton = requiredElement<HTMLButtonElement>('settings-button');
const messages = requiredElement<HTMLElement>('messages');
const connectionLabel = requiredElement<HTMLElement>('connection-label');
const runtimeLabel = requiredElement<HTMLElement>('runtime-label');
const sessionTitle = requiredElement<HTMLElement>('session-title');
const commandMenu = requiredElement<HTMLElement>('command-menu');
const attachmentButton = requiredElement<HTMLButtonElement>('attachment-button');
const attachmentMenu = requiredElement<HTMLElement>('attachment-menu');
const attachmentChips = requiredElement<HTMLElement>('attachment-chips');
const homeRuntimeControls = requiredElement<HTMLElement>('home-runtime-controls');
const chatRuntimeControls = requiredElement<HTMLElement>('chat-runtime-controls');

// 将异步后端 ID 和对应 DOM 节点关联起来，以便后续流式增量、工具结果或恢复状态能
// 更新正确位置。
const streams = new Map<string, StreamView>();
const tools = new Map<string, HTMLDetailsElement>();
const turns = new Map<string, TurnView>();
let state: Record<string, any> = {};
let activeTurn: TurnView | undefined;
let running = false;
let retryMode = false;
let commandSelection = 0;
let currentPage: 'home' | 'chat' = 'home';
let lastFinalText = '';
let runtimeMenu: HTMLElement | undefined;
let runtimeMenuTrigger: HTMLButtonElement | undefined;
let attachments: EditorAttachment[] = [];
let pendingAnnotations: ResponseAnnotation[] = [];
let annotationAction: HTMLElement | undefined;
let attachmentMenuOpen = false;
let attachmentPending = false;
let compactView: CompactView | undefined;

marked.setOptions({ gfm: true, breaks: true });

/** 查找必需的 HTML 元素，并保留调用方指定的具体 DOM 类型。 */
function requiredElement<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing Webview element: ${id}`);
  return element as T;
}

/** 将不可信消息转换为可读取的键值对象，失败时返回空对象。 */
function asRecord(value: unknown): Record<string, any> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

/** 修改工具的参数很大；界面只需要展示用户将要授权的目标文件。 */
function editToolTarget(name: unknown, rawArgs: unknown): string {
  if (!['write_file', 'patch_file'].includes(String(name || ''))) return '';
  return String(asRecord(rawArgs).path || '').trim();
}

/**
 * 将模型 Markdown 清理后渲染到目标节点。
 * DOMPurify 是模型可控标记的安全边界；外部链接还会隔离 opener 后才允许点击。
 */
function renderMarkdown(target: HTMLElement, text: string): void {
  target.innerHTML = DOMPurify.sanitize(marked.parse(text, { async: false }) as string);
  for (const link of target.querySelectorAll('a')) {
    link.setAttribute('target', '_blank');
    link.setAttribute('rel', 'noopener noreferrer');
  }
}

/** 在会话浏览首页和当前聊天页面之间切换。 */
function setPage(page: 'home' | 'chat'): void {
  currentPage = page;
  homePage.hidden = page !== 'home';
  chatPage.hidden = page !== 'chat';
  if (page === 'home') {
    clearRetryMode();
    clearPendingAnnotations();
    setRunning(true);
    vscode.postMessage({ type: 'listSessions' });
  } else {
    input.focus();
  }
}

/**
 * 将全局请求运行状态同步到所有控件。
 * 请求运行时禁止冲突的导航和提交操作，同时为唯一活动请求显示停止按钮。
 */
function setRunning(value: boolean): void {
  running = value;
  if (value) hideAnnotationAction();
  input.disabled = value;
  newTaskInput.disabled = value;
  sendButton.hidden = value;
  stopButton.hidden = !value;
  backButton.disabled = value;
  renameCurrentButton.disabled = value;
  attachmentButton.disabled = value;
  if (value) closeAttachmentMenu();
  updateComposers();
  updateRuntimeControls();
  if (!value) {
    updateRetryAction();
    (currentPage === 'chat' ? input : newTaskInput).focus();
  }
}

/** 合并后端状态快照，并刷新所有依赖该状态的标签。 */
function updateState(nextState: Record<string, unknown>): void {
  state = { ...state, ...nextState };
  const session = asRecord(state.session);
  const title = String(session.title || session.id || 'Session');
  sessionTitle.textContent = title;
  runtimeLabel.textContent = [
    `${String(state.provider || '')}:${String(state.model || '')}`,
    String(state.workflow_mode || 'agent').toUpperCase(),
    String(state.approval_policy || ''),
  ].filter(Boolean).join(' · ');
  updateRuntimeControls();
  updateRetryAction();
}

/** 根据后端权威状态重建 Provider、model 和 approval 控件。 */
function updateRuntimeControls(): void {
  closeRuntimeMenu();
  for (const container of [homeRuntimeControls, chatRuntimeControls]) {
    container.replaceChildren(
      runtimeButton('provider', String(state.provider || 'Provider'), state.available_providers),
      runtimeButton('model', String(state.model || 'Model'), state.available_models),
      runtimeButton(
        'approval',
        String(state.approval_policy || 'Approval'),
        ['ask', 'auto', 'read_only', 'full'],
      ),
    );
  }
}

/** 创建一个运行时选择按钮，并连接到 Webview 内部弹出菜单。 */
function runtimeButton(setting: string, label: string, rawOptions: unknown): HTMLElement {
  const control = document.createElement('div');
  control.className = 'runtime-control';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'runtime-button';
  button.textContent = label;
  button.title = `Change ${setting}`;
  const options = Array.isArray(rawOptions) ? rawOptions.map(String) : [];
  button.disabled = running
    || options.length === 0
    || (setting === 'approval' && state.workflow_mode === 'plan');
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    toggleRuntimeMenu(button, setting, label, options);
  });
  control.append(button);
  return control;
}

/** 在控件旁显示运行时选项，而不是调用 VS Code 顶部的全局 QuickPick。 */
function toggleRuntimeMenu(
  trigger: HTMLButtonElement,
  setting: string,
  current: string,
  options: string[],
): void {
  if (runtimeMenuTrigger === trigger) {
    closeRuntimeMenu();
    return;
  }
  closeRuntimeMenu();
  const menu = document.createElement('div');
  menu.className = 'runtime-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', `Select ${setting}`);
  for (const option of options) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'runtime-menu-item';
    item.setAttribute('role', 'menuitemradio');
    const selected = option === current;
    item.setAttribute('aria-checked', String(selected));
    if (selected) item.classList.add('selected');
    const label = document.createElement('span');
    label.textContent = option;
    const check = document.createElement('span');
    check.className = 'runtime-menu-check';
    check.textContent = selected ? '✓' : '';
    item.append(label, check);
    item.addEventListener('click', (event) => {
      event.stopPropagation();
      closeRuntimeMenu();
      if (!selected) {
        vscode.postMessage({ type: 'setRuntimeSetting', setting, value: option });
      }
    });
    menu.append(item);
  }
  const control = trigger.parentElement;
  if (!control) return;
  control.append(menu);
  runtimeMenu = menu;
  runtimeMenuTrigger = trigger;
  positionRuntimeMenu(menu, trigger);
}

/** 调整运行时菜单位置，确保它不超出狭窄的 Webview 视口。 */
function positionRuntimeMenu(menu: HTMLElement, trigger: HTMLElement): void {
  const rect = trigger.getBoundingClientRect();
  const margin = 6;
  const availableWidth = Math.max(80, window.innerWidth - margin * 2);
  const width = Math.min(Math.max(rect.width, 150), availableWidth);
  let left = 0;
  if (rect.left + width > window.innerWidth - margin) {
    left = window.innerWidth - margin - rect.left - width;
  }
  if (rect.left + left < margin) left = margin - rect.left;
  menu.style.width = `${width}px`;
  menu.style.left = `${left}px`;
}

/** 移除当前运行时菜单，并清空其所属按钮引用。 */
function closeRuntimeMenu(): void {
  runtimeMenu?.remove();
  runtimeMenu = undefined;
  runtimeMenuTrigger = undefined;
}

/** 使用当前项目的最新会话替换首页会话列表。 */
function renderSessions(rawSessions: unknown): void {
  sessionList.replaceChildren();
  const sessions = Array.isArray(rawSessions) ? rawSessions.map(asRecord) : [];
  if (sessions.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = 'No sessions yet.';
    sessionList.append(empty);
    return;
  }
  for (const session of sessions) {
    const row = document.createElement('article');
    row.className = 'session-row';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'session-open';
    const title = document.createElement('strong');
    title.textContent = String(session.title || session.id || 'Untitled session');
    const time = document.createElement('span');
    time.textContent = formatTimestamp(String(session.updated_at || session.created_at || ''));
    open.append(title, time);
    open.addEventListener('click', () => {
      setRunning(true);
      vscode.postMessage({ type: 'openSession', sessionId: String(session.id || '') });
    });
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'icon-button session-rename';
    rename.textContent = '✎';
    rename.title = 'Rename session';
    rename.setAttribute('aria-label', 'Rename session');
    rename.addEventListener('click', () => {
      openRenameDialog(String(session.id || ''), String(session.title || ''));
    });
    row.append(open, rename);
    sessionList.append(row);
  }
}

/** 按用户 VS Code/浏览器区域设置格式化 ISO 时间。 */
function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(date);
}

/** 显示会话改名弹窗，并把确认后的标题发送给 Extension Host。 */
function openRenameDialog(sessionId: string, currentTitle: string): void {
  const overlay = document.createElement('div');
  overlay.className = 'dialog-overlay';
  const form = document.createElement('form');
  form.className = 'dialog';
  const heading = document.createElement('strong');
  heading.textContent = 'Rename session';
  const field = document.createElement('input');
  field.type = 'text';
  field.value = currentTitle;
  field.maxLength = 20;
  const actions = document.createElement('div');
  actions.className = 'inline-actions';
  const cancel = actionButton('Cancel', () => overlay.remove());
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = 'Save';
  actions.append(cancel, save);
  form.append(heading, field, actions);
  overlay.append(form);
  document.body.append(overlay);
  field.focus();
  field.select();
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const title = field.value.trim();
    if (!title) return;
    overlay.remove();
    setRunning(true);
    vscode.postMessage({ type: 'renameSession', sessionId, title });
  });
}

/**
 * 为一轮对话创建稳定的 DOM 容器。
 * commentary 和工具过程放在可折叠 Process 区域中，最终回答单独渲染在其下方。
 */
function createTurn(id: string, requestText: string, rawAnnotations: unknown = []): TurnView {
  const root = document.createElement('section');
  root.className = 'turn';
  root.dataset.turnId = id;
  const user = document.createElement('article');
  user.className = 'message message-user';
  const userHeader = document.createElement('div');
  userHeader.className = 'message-header';
  const label = document.createElement('span');
  label.className = 'message-label';
  label.textContent = 'You';
  userHeader.append(label);
  const userContent = document.createElement('div');
  userContent.className = 'message-content';
  if (requestText) renderMarkdown(userContent, requestText);
  user.append(userHeader, userContent);
  appendSentAnnotations(user, rawAnnotations);

  const process = document.createElement('details');
  process.className = 'turn-process';
  process.open = true;
  process.hidden = true;
  const summary = document.createElement('summary');
  summary.textContent = 'Process';
  const processBody = document.createElement('div');
  processBody.className = 'process-body';
  process.append(summary, processBody);
  root.append(user, process);
  messages.append(root);
  const turn = { id, requestText, root, process, processBody };
  turns.set(id, turn);
  scrollToBottom();
  return turn;
}

/** 向某一轮的 Process 区域追加一段用户可见的进度说明。 */
function appendProcessMessage(turn: TurnView, text = ''): { article: HTMLElement; content: HTMLElement } {
  hideThinking(turn);
  turn.process.hidden = false;
  const article = document.createElement('article');
  article.className = 'process-message';
  const content = document.createElement('div');
  content.className = 'message-content';
  if (text) renderMarkdown(content, text);
  article.append(content);
  turn.processBody.append(article);
  return { article, content };
}

/** 在真正文本或工具事件到达前显示临时 Thinking 指示。 */
function showThinking(turn: TurnView): void {
  if (turn.thinking) return;
  turn.process.hidden = false;
  turn.process.open = true;
  const indicator = document.createElement('div');
  indicator.className = 'thinking-indicator';
  indicator.textContent = 'Thinking...';
  turn.processBody.append(indicator);
  turn.thinking = indicator;
  scrollToBottom();
}

/** 从指定轮次或当前轮次中移除临时 Thinking 指示。 */
function hideThinking(turn = activeTurn): void {
  if (!turn?.thinking) return;
  turn.thinking.remove();
  turn.thinking = undefined;
}

/** 按需创建并返回某一轮的最终回答 Markdown 容器。 */
function ensureFinal(turn: TurnView): HTMLElement {
  if (turn.final) return turn.final;
  const article = document.createElement('article');
  article.className = 'message message-assistant final-message';
  const label = document.createElement('div');
  label.className = 'message-label';
  label.textContent = 'CodeMate';
  const content = document.createElement('div');
  content.className = 'message-content';
  article.append(label, content);
  turn.root.append(article);
  turn.final = content;
  turn.finalArticle = article;
  return content;
}

/** 将后端确认的持久化消息标识绑定到最终回答，使其可以安全创建批注。 */
function bindFinalMessage(turn: TurnView, rawMessage: unknown): void {
  const message = asRecord(rawMessage);
  const messageId = String(message.id || '');
  const contentHash = String(message.content_hash || '');
  if (!messageId || !contentHash) return;
  ensureFinal(turn);
  if (!turn.finalArticle) return;
  turn.finalArticle.dataset.messageId = messageId;
  turn.finalArticle.dataset.contentHash = contentHash;
  turn.finalArticle.dataset.conversationId = String(message.conversation_id || turn.id);
  turn.finalArticle.classList.add('annotatable');
}

/** 在历史用户消息下显示已经发送的批注，但不暴露注入给模型的内部提示词。 */
function appendSentAnnotations(user: HTMLElement, rawAnnotations: unknown): void {
  if (!Array.isArray(rawAnnotations) || rawAnnotations.length === 0) return;
  const container = document.createElement('details');
  container.className = 'sent-annotations';
  const summary = document.createElement('summary');
  const icon = document.createElement('span');
  icon.className = 'sent-annotations-icon';
  icon.setAttribute('aria-hidden', 'true');
  const count = document.createElement('span');
  count.textContent = `${rawAnnotations.length} 条批注`;
  summary.append(icon, count);
  const list = document.createElement('div');
  list.className = 'sent-annotations-list';
  for (const [index, raw] of rawAnnotations.entries()) {
    const annotation = asRecord(raw);
    const card = document.createElement('section');
    card.className = 'sent-annotation';
    const number = document.createElement('span');
    number.className = 'sent-annotation-number';
    number.textContent = `${index + 1}.`;
    const body = document.createElement('div');
    body.className = 'sent-annotation-body';
    const selectionLabel = document.createElement('div');
    selectionLabel.className = 'sent-annotation-label';
    selectionLabel.textContent = '所选文本：';
    const selection = document.createElement('blockquote');
    selection.textContent = String(annotation.selected_text || '');
    selection.setAttribute('aria-label', `Selected response text ${index + 1}`);
    body.append(selectionLabel, selection);
    const comment = String(annotation.comment || '');
    if (comment) {
      const commentLabel = document.createElement('div');
      commentLabel.className = 'sent-annotation-label';
      commentLabel.textContent = '用户评论：';
      const commentText = document.createElement('p');
      commentText.textContent = comment;
      body.append(commentLabel, commentText);
    }
    card.append(number, body);
    list.append(card);
  }
  container.append(summary, list);
  user.append(container);
}

/** 渲染默认折叠的工具调用，并保存引用以等待对应结果事件。 */
function appendTool(event: BackendEvent, turn = activeTurn): HTMLDetailsElement {
  const details = document.createElement('details');
  details.className = 'tool-event';
  const summary = document.createElement('summary');
  summary.textContent = String(event.name || 'tool');
  const target = editToolTarget(event.name, event.args);
  const body = target
    ? createFileTarget(target)
    : document.createElement('pre');
  if (!target) body.textContent = JSON.stringify(event.args || {}, null, 2);
  details.append(summary, body);
  const parent = turn?.processBody || messages;
  if (turn) turn.process.hidden = false;
  parent.append(details);
  tools.set(String(event.tool_id || `tool-${Date.now()}`), details);
  scrollToBottom();
  return details;
}

/** 将工具结果匹配到对应调用，并追加状态和输出内容。 */
function appendToolResult(event: BackendEvent, turn = activeTurn): void {
  const details = tools.get(String(event.tool_id || '')) || appendTool(event, turn);
  const metadata = asRecord(event.metadata);
  const status = String(metadata.tool_status || 'ok');
  details.classList.add(status === 'ok' ? 'tool-ok' : 'tool-error');
  const statusLine = document.createElement('div');
  statusLine.className = 'tool-status';
  statusLine.textContent = status;
  const result = editToolTarget(event.name, event.args)
    ? document.createElement('div')
    : document.createElement('pre');
  result.className = editToolTarget(event.name, event.args)
    ? 'tool-result tool-result-compact'
    : 'tool-result';
  result.textContent = String(event.result || '(empty)');
  details.append(statusLine, result);
  appendToolChangePreview(details, metadata.change_preview);
  scrollToBottom();
}

/** 在对应 write_file/patch_file 工具卡片内展示有界修改片段。 */
function appendToolChangePreview(details: HTMLDetailsElement, rawPreview: unknown): void {
  const preview = asRecord(rawPreview);
  const path = String(preview.path || '');
  if (!path) return;
  const section = document.createElement('section');
  section.className = 'tool-change-preview';
  const header = document.createElement('div');
  header.className = 'tool-change-header';
  const label = document.createElement('strong');
  label.textContent = path;
  const stats = document.createElement('span');
  stats.textContent = `+${Number(preview.additions || 0)} -${Number(preview.deletions || 0)}`;
  header.append(label, stats);
  section.append(header);
  const summary = details.querySelector('summary');
  if (summary) {
    summary.textContent = `${summary.textContent || 'tool'} · ${path} ${stats.textContent}`;
  }
  const diff = String(preview.diff || '');
  if (diff) {
    const content = document.createElement('div');
    content.className = 'tool-change-diff';
    for (const row of parseDiffRows(diff)) {
      const line = document.createElement('div');
      line.className = `diff-row diff-${row.kind}`;
      const number = document.createElement('span');
      number.className = 'diff-line-number';
      number.textContent = row.lineNumber === undefined ? '' : String(row.lineNumber);
      const code = document.createElement('code');
      code.textContent = row.text || ' ';
      line.append(number, code);
      content.append(line);
    }
    section.append(content);
  } else {
    const message = document.createElement('p');
    message.className = 'tool-change-message';
    message.textContent = String(preview.message || 'No textual preview available.');
    section.append(message);
  }
  details.append(section);
}

interface DiffDisplayRow {
  kind: 'added' | 'deleted' | 'context' | 'hunk' | 'omitted';
  lineNumber?: number;
  text: string;
}

/** 将 unified diff 转成可单独着色、带行号的 Webview 行。 */
function parseDiffRows(diff: string): DiffDisplayRow[] {
  const rows: DiffDisplayRow[] = [];
  let oldLine = 0;
  let newLine = 0;
  for (const rawLine of diff.split('\n')) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(rawLine);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      rows.push({ kind: 'hunk', text: rawLine });
      continue;
    }
    if (rawLine === '... diff preview truncated ...') {
      rows.push({ kind: 'omitted', text: rawLine });
      continue;
    }
    if (rawLine.startsWith('+')) {
      rows.push({ kind: 'added', lineNumber: newLine, text: rawLine.slice(1) });
      newLine += 1;
      continue;
    }
    if (rawLine.startsWith('-')) {
      rows.push({ kind: 'deleted', lineNumber: oldLine, text: rawLine.slice(1) });
      oldLine += 1;
      continue;
    }
    rows.push({ kind: 'context', lineNumber: newLine, text: rawLine.slice(1) });
    oldLine += 1;
    newLine += 1;
  }
  return rows;
}

/** 生成审批和工具卡片共用的紧凑文件目标行。 */
function createFileTarget(path: string): HTMLDivElement {
  const target = document.createElement('div');
  target.className = 'tool-file-target';
  const label = document.createElement('span');
  label.textContent = 'File';
  const value = document.createElement('code');
  value.textContent = path;
  target.append(label, value);
  return target;
}

/**
 * 根据后端返回的有界展示历史重建聊天记录。
 * 工具调用与结果按 ID 配对，已完成 Process 默认折叠，持久化变更集重新挂到对应轮次。
 */
function renderHistory(rawHistory: unknown, rawChangeSets: unknown = state.change_sets): void {
  clearRetryMode();
  resetTranscript();
  if (!Array.isArray(rawHistory)) return;
  let turn: TurnView | undefined;
  for (const rawItem of rawHistory) {
    const item = asRecord(rawItem);
    const role = String(item.role || '');
    const kind = String(item.kind || '');
    const content = String(item.content || '');
    const conversationId = String(item.conversation_id || item.id || `history-${turns.size}`);
    if (kind === 'history_summary') {
      appendStandaloneMessage('Earlier summary', content);
      continue;
    }
    if (role === 'user') {
      turn = turns.get(conversationId)
        || createTurn(conversationId, content, item.response_annotations);
      continue;
    }
    turn = turn || turns.get(conversationId);
    if (!turn) continue;
    if (role === 'assistant' && kind === 'commentary') {
      appendProcessMessage(turn, content);
    } else if (role === 'assistant' && Array.isArray(item.tool_calls)) {
      if (content) appendProcessMessage(turn, content);
      for (const rawCall of item.tool_calls) {
        const call = asRecord(rawCall);
        appendTool({
          type: 'tool_start',
          tool_id: String(call.id || ''),
          name: call.name,
          args: call.args || call.arguments || {},
        }, turn);
      }
    } else if (role === 'assistant' && content) {
      renderMarkdown(ensureFinal(turn), content);
      if (kind === 'final') bindFinalMessage(turn, item);
      lastFinalText = content.trim();
    } else if (role === 'tool') {
      appendToolResult({
        type: 'tool_result',
        tool_id: item.tool_call_id,
        name: item.name,
        result: content,
        metadata: { tool_status: 'ok', ...asRecord(item.metadata) },
      }, turn);
    }
  }
  for (const renderedTurn of turns.values()) {
    renderedTurn.process.open = false;
  }
  if (Array.isArray(rawChangeSets)) {
    for (const rawChangeSet of rawChangeSets) {
      const changeSet = asRecord(rawChangeSet);
      const turn = turns.get(String(changeSet.conversation_id || ''));
      if (turn) renderChangeSet(turn, changeSet);
    }
  }
  activeTurn = undefined;
  updateRetryAction();
}

/** 追加不属于某个用户请求轮次的消息，例如错误或命令报告。 */
function appendStandaloneMessage(label: string, text: string, error = false): void {
  const article = document.createElement('article');
  article.className = `message message-system${error ? ' error-message' : ''}`;
  const heading = document.createElement('div');
  heading.className = 'message-label';
  heading.textContent = label;
  const content = document.createElement('div');
  content.className = 'message-content';
  renderMarkdown(content, text);
  article.append(heading, content);
  messages.append(article);
  scrollToBottom();
}

/** 打开其他会话前清空聊天 DOM 以及全部 ID 到视图的缓存。 */
function resetTranscript(): void {
  clearPendingAnnotations();
  hideAnnotationAction();
  messages.replaceChildren();
  streams.clear();
  tools.clear();
  turns.clear();
  activeTurn = undefined;
  lastFinalText = '';
  compactView = undefined;
}

/**
 * 处理 Extension Host 发来的全部事件，是后端事件到 DOM 状态的统一入口。
 * 每种事件只更新一小部分 UI；集中分发便于完整查看两端协议的映射关系。
 */
function handleBackendEvent(event: BackendEvent): void {
  if (event.state && typeof event.state === 'object') updateState(asRecord(event.state));
  switch (event.type) {
    case 'ready':
      connectionLabel.textContent = 'Connected';
      connectionLabel.className = 'connected';
      setRunning(false);
      setPage('home');
      break;
    case 'session_opened':
      updateSessionFromEvent(event);
      setPage('chat');
      renderHistory(event.history, asRecord(event.state).change_sets);
      break;
    case 'checkpoint_restored':
      renderHistory(event.history, asRecord(event.state).change_sets);
      break;
    case 'run_started': {
      const id = String(event.request_id || `turn-${Date.now()}`);
      activeTurn = createTurn(id, String(event.text || ''), event.response_annotations);
      clearPendingAnnotations();
      lastFinalText = '';
      connectionLabel.textContent = 'Working';
      setRunning(true);
      setPage('chat');
      showThinking(activeTurn);
      break;
    }
    case 'model_status':
      if (event.status === 'started' && activeTurn) showThinking(activeTurn);
      break;
    case 'stream_start':
      streams.set(String(event.stream_id || ''), {
        phase: String(event.phase || ''), text: '', finalText: '',
      });
      break;
    case 'text_delta':
      handleTextDelta(event);
      break;
    case 'stream_end':
      handleStreamEnd(event);
      break;
    case 'commentary':
      if (activeTurn) appendProcessMessage(activeTurn, String(event.text || ''));
      break;
    case 'final':
      if (activeTurn) {
        hideThinking(activeTurn);
        lastFinalText = String(event.text || '').trim();
        renderMarkdown(ensureFinal(activeTurn), lastFinalText);
      }
      break;
    case 'tool_start':
      hideThinking();
      appendTool(event);
      break;
    case 'tool_result':
      hideThinking();
      appendToolResult(event);
      break;
    case 'approval_request':
      hideThinking();
      appendApproval(event);
      break;
    case 'user_input_request':
      hideThinking();
      appendQuestions(event);
      break;
    case 'plan_review_request':
      hideThinking();
      appendPlanReview(event);
      break;
    case 'compact_status':
      if (event.status === 'started') {
        showCompactStart();
        connectionLabel.textContent = 'Compacting';
      } else {
        showCompactResult(String(event.status || ''), asRecord(event.metadata));
        connectionLabel.textContent = 'Working';
      }
      break;
    case 'review_status':
      connectionLabel.textContent = event.status === 'started' ? 'Reviewing' : 'Working';
      break;
    case 'command_result':
      handleCommandResult(event);
      connectionLabel.textContent = 'Connected';
      setRunning(false);
      break;
    case 'run_finished':
      finishRun(event);
      break;
    case 'changeDiagnostics':
      renderChangeDiagnostics(event);
      break;
    case 'ui_error':
      appendVisibleError(String(event.message || 'The editor action failed.'));
      break;
    case 'error':
    case 'protocol_error':
    case 'connection_error':
    case 'startup_error':
      hideThinking();
      if (compactView && !compactView.complete) {
        showCompactResult('error', { reason: event.message });
      }
      appendVisibleError(String(event.message || 'Unknown backend error.'));
      connectionLabel.textContent = 'Error';
      setRunning(false);
      break;
    case 'disconnected':
      connectionLabel.textContent = 'Disconnected';
      connectionLabel.className = 'disconnected';
      setRunning(false);
      if (!event.expected) appendReconnect();
      break;
    case 'connecting':
      connectionLabel.textContent = 'Connecting';
      connectionLabel.className = '';
      break;
  }
}

/** 合并没有放在常规 state 信封中的会话元数据。 */
function updateSessionFromEvent(event: BackendEvent): void {
  const session = asRecord(event.session);
  if (Object.keys(session).length > 0) updateState({ ...state, session });
}

/**
 * 累积一段流式文本增量，并按照 phase 路由。
 * final_answer 写入最终回答节点，commentary 写入 Process。渲染按动画帧调度，避免
 * 大量细碎网络增量反复解析 Markdown 并触发侧边栏重排。
 */
function handleTextDelta(event: BackendEvent): void {
  const streamId = String(event.stream_id || '');
  const stream = streams.get(streamId) || {
    phase: '', text: '', finalText: '',
  };
  streams.set(streamId, stream);
  const phase = String(event.phase || stream.phase || '');
  if (stream.content && phase && phase !== stream.phase) {
    flushStreamRender(stream);
    stream.article = undefined;
    stream.content = undefined;
    stream.text = '';
  }
  stream.phase = phase;
  if (!stream.content && activeTurn) {
    if (phase === 'final_answer') {
      stream.content = ensureFinal(activeTurn);
    } else {
      const processMessage = appendProcessMessage(activeTurn);
      stream.article = processMessage.article;
      stream.content = processMessage.content;
    }
  }
  const delta = String(event.text || '');
  if (delta) hideThinking();
  stream.text += delta;
  if (phase === 'final_answer') stream.finalText += delta;
  scheduleStreamRender(stream);
}

/** 立即刷新已完成流，并将其最终文本同步到最终回答节点。 */
function handleStreamEnd(event: BackendEvent): void {
  const id = String(event.stream_id || '');
  const stream = streams.get(id);
  if (!stream) return;
  flushStreamRender(stream);
  if (event.kind === 'final' && activeTurn) {
    const final = (stream.finalText || stream.text).trim();
    lastFinalText = final;
    const finalTarget = ensureFinal(activeTurn);
    if (stream.content !== finalTarget) {
      renderMarkdown(finalTarget, final);
      stream.article?.remove();
    }
  }
  streams.delete(id);
}

/**
 * Python 返回 run_finished 后结束当前轮次。
 * 该过程补充最终文本兜底、挂载本轮变更集、折叠 Process，并重新启用输入控件。
 */
function finishRun(event: BackendEvent): void {
  hideThinking();
  const final = String(event.final || '').trim();
  if (activeTurn && final && final !== lastFinalText) {
    renderMarkdown(ensureFinal(activeTurn), final);
    lastFinalText = final;
  }
  if (activeTurn) {
    const completedMessages = Array.isArray(event.messages) ? event.messages : [];
    const finalMessage = completedMessages
      .map(asRecord)
      .find((item) => item.role === 'assistant' && item.kind === 'final');
    if (finalMessage) bindFinalMessage(activeTurn, finalMessage);
    if (event.change_set && typeof event.change_set === 'object') {
      renderChangeSet(activeTurn, asRecord(event.change_set));
    }
    activeTurn.process.open = false;
    if (activeTurn.processBody.childElementCount === 0) activeTurn.process.hidden = true;
  }
  activeTurn = undefined;
  connectionLabel.textContent = event.status === 'completed'
    ? 'Connected'
    : String(event.status || 'Stopped');
  setRunning(false);
  updateRetryAction();
}

/**
 * 只在最新且可重试的用户请求上放置重试按钮。
 * 重试只恢复后端会话状态，提示文本会明确说明工作区文件修改不会自动撤销。
 */
function updateRetryAction(): void {
  for (const turn of turns.values()) turn.retryButton?.remove();
  const retry = asRecord(state.retry);
  // 新批注依赖当前最终回答；重试会恢复到该回答产生前，二者不能同时提交。
  if (!retry.available || running || pendingAnnotations.length > 0 || turns.size === 0) return;
  const latest = Array.from(turns.values()).at(-1);
  if (!latest) return;
  const header = latest.root.querySelector('.message-header');
  if (!header) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'icon-button retry-button';
  button.textContent = '↻';
  button.title = 'Edit and retry from the previous session state; file changes are not reverted';
  button.setAttribute('aria-label', 'Edit and retry this request');
  button.addEventListener('click', () => {
    retryMode = true;
    input.value = String(retry.user_request || latest.requestText);
    pendingAnnotations = normalizeAnnotations(retry.response_annotations);
    renderPendingAnnotations();
    input.dataset.retry = 'true';
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    updateComposers();
  });
  header.append(button);
  latest.retryButton = button;
}

/** 退出 checkpoint 重试编辑状态，并将输入框恢复为普通输入。 */
function clearRetryMode(): void {
  retryMode = false;
  delete input.dataset.retry;
  input.value = '';
}

/** 在当前页面追加错误，同时保留已有页面内容。 */
function appendVisibleError(text: string): void {
  if (currentPage === 'chat') {
    appendStandaloneMessage('Error', text, true);
    return;
  }
  const error = document.createElement('div');
  error.className = 'home-error';
  const label = document.createElement('strong');
  label.textContent = 'Error';
  const content = document.createElement('div');
  renderMarkdown(content, text);
  error.append(label, content);
  sessionList.prepend(error);
}

/**
 * 在当前 Process 区域中渲染阻塞式工具审批。
 * 用户选择后先禁用所有同级按钮，再针对后端 interaction ID 返回唯一答案。
 */
function appendApproval(event: BackendEvent): void {
  if (!activeTurn) return;
  const block = appendProcessMessage(activeTurn);
  const heading = document.createElement('strong');
  heading.textContent = `Approval · ${String(event.name || 'tool')}`;
  const target = editToolTarget(event.name, event.args);
  const args = target ? createFileTarget(target) : document.createElement('pre');
  if (!target) args.textContent = JSON.stringify(event.args || {}, null, 2);
  const actions = document.createElement('div');
  actions.className = 'inline-actions approval-actions';
  for (const rawOption of Array.isArray(event.options) ? event.options : []) {
    const option = asRecord(rawOption);
    const button = actionButton(String(option.label || 'Select'), () => {
      for (const candidate of actions.querySelectorAll('button')) {
        candidate.classList.toggle('selected', candidate === button);
        candidate.classList.toggle('not-selected', candidate !== button);
      }
      disableButtons(actions);
      vscode.postMessage({
        type: 'interactionResponse',
        interactionId: String(event.interaction_id || ''),
        value: option.value,
      });
    });
    button.classList.add('approval-option');
    actions.append(button);
  }
  block.content.append(heading, args, actions);
  scrollToBottom();
}

/** 渲染一至三个结构化规划问题，并按问题 ID 收集答案。 */
function appendQuestions(event: BackendEvent): void {
  if (!activeTurn) return;
  const block = appendProcessMessage(activeTurn);
  const form = document.createElement('form');
  form.className = 'question-form';
  for (const question of (Array.isArray(event.questions) ? event.questions.map(asRecord) : [])) {
    const fieldset = document.createElement('fieldset');
    const legend = document.createElement('legend');
    legend.textContent = String(question.question || question.header || 'Choose an option');
    const select = document.createElement('select');
    select.name = String(question.id || 'question');
    for (const rawOption of Array.isArray(question.options) ? question.options : []) {
      const option = asRecord(rawOption);
      const element = document.createElement('option');
      element.value = String(option.label || '');
      element.textContent = `${String(option.label || '')} — ${String(option.description || '')}`;
      select.append(element);
    }
    const other = document.createElement('option');
    other.value = '__other__';
    other.textContent = 'Other';
    select.append(other);
    const custom = document.createElement('input');
    custom.type = 'text';
    custom.placeholder = 'Custom answer';
    custom.hidden = true;
    select.addEventListener('change', () => { custom.hidden = select.value !== '__other__'; });
    fieldset.append(legend, select, custom);
    form.append(fieldset);
  }
  const submit = document.createElement('button');
  submit.type = 'submit';
  submit.textContent = 'Submit';
  form.append(submit);
  form.addEventListener('submit', (submitEvent) => {
    submitEvent.preventDefault();
    const answers: Record<string, unknown> = {};
    for (const fieldset of form.querySelectorAll('fieldset')) {
      const select = fieldset.querySelector('select');
      const custom = fieldset.querySelector('input');
      if (!select) continue;
      const isOther = select.value === '__other__';
      const value = isOther ? custom?.value.trim() || '' : select.value;
      if (!value) return;
      answers[select.name] = { type: isOther ? 'custom' : 'option', value };
    }
    disableButtons(form);
    vscode.postMessage({
      type: 'interactionResponse',
      interactionId: String(event.interaction_id || ''),
      value: { status: 'answered', answers },
    });
  });
  block.content.append(form);
}

/** 展示已提交的 Markdown 计划，并返回批准、修改或取消决策。 */
function appendPlanReview(event: BackendEvent): void {
  if (!activeTurn) return;
  const block = appendProcessMessage(activeTurn);
  const heading = document.createElement('strong');
  heading.textContent = `Plan · ${String(event.title || '')}`;
  const plan = document.createElement('div');
  renderMarkdown(plan, String(event.plan || ''));
  const actions = document.createElement('div');
  actions.className = 'inline-actions';
  const respond = (value: unknown): void => {
    disableButtons(actions);
    vscode.postMessage({
      type: 'interactionResponse',
      interactionId: String(event.interaction_id || ''), value,
    });
  };
  actions.append(
    actionButton('Approve and implement', () => respond({ decision: 'approved' })),
    actionButton('Revise', () => {
      const feedback = document.createElement('textarea');
      feedback.placeholder = 'Revision feedback';
      const submit = actionButton('Submit revision', () => {
        if (feedback.value.trim()) {
          respond({ decision: 'revision_requested', feedback: feedback.value.trim() });
        }
      });
      actions.replaceChildren(feedback, submit);
      feedback.focus();
    }),
    actionButton('Cancel', () => respond({ decision: 'cancelled' })),
  );
  block.content.append(heading, plan, actions);
}

/**
 * 处理具有专门 UI 行为的斜杠命令结果。
 * session、compact 和 change 命令会更新现有视图，普通报告则回退为独立的
 * Markdown/JSON 消息。
 */
function handleCommandResult(event: BackendEvent): void {
  const value = asRecord(event.value);
  const name = String(event.name || 'Command');
  if (name === 'compact') {
    showCompactResult(String(value.status || ''), value);
    return;
  }
  if (name === 'change_undo' || name === 'change_redo') {
    const changeSet = asRecord(value.change_set);
    const turn = Array.from(turns.values()).find(
      (item) => item.changes?.dataset.changeSetId === String(changeSet.id || ''),
    );
    if (turn) renderChangeSet(turn, changeSet);
    return;
  }
  if (name === 'session_list') {
    renderSessions(value.sessions);
    return;
  }
  if (name === 'session_resume' || name === 'session_new') {
    updateSessionFromEvent(value as BackendEvent);
    setPage('chat');
    renderHistory(value.history, asRecord(event.state).change_sets);
    return;
  }
  if (name === 'session_rename') {
    const renamed = value;
    const current = asRecord(state.session);
    if (String(renamed.session_id || '') === String(current.id || '')) {
      updateState({ ...state, session: { ...current, title: renamed.title } });
    }
    renderSessions(value.sessions);
    return;
  }
  if (['provider', 'model', 'approval'].includes(name)) return;
  if (Array.isArray(value.history)) {
    renderHistory(value.history, asRecord(event.state).change_sets);
    return;
  }
  const report = typeof value.report === 'string'
    ? value.report
    : `\`\`\`json\n${JSON.stringify(event.value ?? {}, null, 2)}\n\`\`\``;
  appendStandaloneMessage(name, report);
}

/** Python Bridge 意外断开后添加重新连接按钮。 */
function appendReconnect(): void {
  const button = actionButton('Restart backend', () => {
    button.disabled = true;
    connectionLabel.textContent = 'Connecting';
    vscode.postMessage({ type: 'restart' });
  });
  const wrapper = document.createElement('div');
  wrapper.className = 'reconnect';
  wrapper.append(button);
  (currentPage === 'chat' ? messages : sessionList).append(wrapper);
}

/** 创建绑定单个本地 UI 操作的标准文本按钮。 */
function actionButton(label: string, action: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.addEventListener('click', action);
  return button;
}

/** 禁用容器中的按钮，防止交互请求被重复提交。 */
function disableButtons(container: ParentNode): void {
  for (const button of container.querySelectorAll('button')) button.disabled = true;
}

/** 重新计算发送可用性、文本框高度和补全菜单状态。 */
function updateComposers(): void {
  sendButton.disabled = running
    || attachmentPending
    || (input.value.trim().length === 0 && pendingAnnotations.length === 0);
  newTaskButton.disabled = running || newTaskInput.value.trim().length === 0;
  resizeTextarea(input, 180);
  resizeTextarea(newTaskInput, 220);
  updateCommandMenu();
}

/** 根据内容自动增高输入框，但不超过指定最大高度。 */
function resizeTextarea(element: HTMLTextAreaElement, maxHeight: number): void {
  element.style.height = 'auto';
  element.style.height = `${Math.min(element.scrollHeight, maxHeight)}px`;
}

/** 将当前输入作为普通请求或 checkpoint 重试请求发送。 */
function sendMessage(): void {
  const text = input.value.trim();
  if ((!text && pendingAnnotations.length === 0) || running) return;
  setRunning(true);
  connectionLabel.textContent = 'Starting';
  if (text === '/compact' && pendingAnnotations.length === 0) showCompactStart();
  vscode.postMessage({
    type: retryMode ? 'retryRequest' : 'sendMessage',
    text,
    attachments: attachments.map((item) => ({ ...item })),
    responseAnnotations: pendingAnnotations.map(({ anchor_top: _anchorTop, ...item }) => item),
  });
  retryMode = false;
  delete input.dataset.retry;
  input.value = '';
  clearAttachments();
  hideCommandMenu();
  updateComposers();
}

/** 创建唯一 compact 进度行，并放入当前轮次或聊天记录。 */
function showCompactStart(): void {
  if (compactView && !compactView.complete) return;
  const root = document.createElement('article');
  root.className = 'compact-message';
  const status = document.createElement('div');
  status.textContent = 'Compacting history...';
  const result = document.createElement('div');
  result.className = 'compact-result';
  root.append(status, result);
  if (activeTurn) {
    activeTurn.process.hidden = false;
    activeTurn.processBody.append(root);
  } else {
    messages.append(root);
  }
  compactView = { result, complete: false };
  scrollToBottom();
}

/** 使用简洁的成功、跳过或失败摘要结束 compact 进度行。 */
function showCompactResult(status: string, metadata: Record<string, any>): void {
  if (compactView?.complete) return;
  if (!compactView) showCompactStart();
  if (!compactView) return;
  if (status === 'ok') {
    compactView.result.textContent = `→ history compacted: ${Number(metadata.history_before_messages || 0)} → ${Number(metadata.history_after_messages || 0)} messages, summary ${Number(metadata.summary_chars || 0)} chars`;
    compactView.result.className = 'compact-result success';
  } else if (status === 'skipped') {
    compactView.result.textContent = `→ compact skipped: ${String(metadata.reason || 'not needed')}`;
    compactView.result.className = 'compact-result skipped';
  } else {
    compactView.result.textContent = `→ history compact failed: ${String(metadata.reason || 'unknown error')}`;
    compactView.result.className = 'compact-result error';
  }
  compactView.complete = true;
  scrollToBottom();
}

/** 清空旧聊天记录，并请求后端创建新会话。 */
function startNewTask(): void {
  const text = newTaskInput.value.trim();
  if (!text || running) return;
  resetTranscript();
  setPage('chat');
  setRunning(true);
  sessionTitle.textContent = 'New session';
  connectionLabel.textContent = 'Starting';
  vscode.postMessage({ type: 'newTask', text });
  newTaskInput.value = '';
  updateComposers();
}

/**
 * 根据当前输入重建斜杠命令和 @ 附件补全。
 * 候选项只是本地 UI 元数据；选择后要么修改输入框，要么请求 Extension Host
 * 收集编辑器上下文。
 */
function updateCommandMenu(): void {
  const query = input.value;
  if (pendingAnnotations.length > 0) {
    hideCommandMenu();
    return;
  }
  const attachmentMatch = query.match(/(^|\s)@(selection|file|problems)?$/i);
  if (attachmentMatch) {
    const token = String(attachmentMatch[2] || '').toLowerCase();
    const matches = ['selection', 'file', 'problems'].filter((kind) => kind.startsWith(token));
    renderAttachmentSuggestions(matches);
    return;
  }
  if (!query.startsWith('/') || query.includes('\n')) {
    hideCommandMenu();
    return;
  }
  const matches = COMMANDS.filter((item) => item.command.startsWith(query));
  if (matches.length === 0 || matches.some((item) => item.command === query)) {
    hideCommandMenu();
    return;
  }
  commandSelection = Math.min(commandSelection, matches.length - 1);
  commandMenu.replaceChildren();
  matches.forEach((item, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = index === commandSelection ? 'selected' : '';
    const command = document.createElement('strong');
    command.textContent = item.command;
    const description = document.createElement('span');
    description.textContent = item.description;
    button.append(command, description);
    button.addEventListener('click', () => applyCommandSuggestion(item.command));
    commandMenu.append(button);
  });
  commandMenu.hidden = false;
}

/** 渲染支持键盘选择的 @selection、@file 和 @problems 候选项。 */
function renderAttachmentSuggestions(kinds: string[]): void {
  if (kinds.length === 0) {
    hideCommandMenu();
    return;
  }
  commandSelection = Math.min(commandSelection, kinds.length - 1);
  commandMenu.replaceChildren();
  for (const [index, kind] of kinds.entries()) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = index === commandSelection ? 'selected' : '';
    const label = document.createElement('strong');
    label.textContent = `@${kind}`;
    const description = document.createElement('span');
    description.textContent = attachmentDescription(kind);
    button.append(label, description);
    button.addEventListener('click', () => requestAttachment(kind, true));
    commandMenu.append(button);
  }
  commandMenu.hidden = false;
}

/** 返回两个附件菜单共用的简短用途说明。 */
function attachmentDescription(kind: string): string {
  if (kind === 'selection') return 'Attach the active editor selection';
  if (kind === 'file') return 'Choose a workspace file';
  return 'Attach errors and warnings for the active file';
}

/**
 * 请求 Extension Host 捕获一种编辑器上下文。
 * 在结果、错误或取消返回前，attachmentPending 会阻止提交，避免请求先于附件发送。
 */
function requestAttachment(kind: string, replaceToken = false): void {
  if (replaceToken) input.value = input.value.replace(/(^|\s)@(selection|file|problems)?$/i, '$1');
  hideCommandMenu();
  closeAttachmentMenu();
  attachmentPending = true;
  vscode.postMessage({ type: 'requestAttachment', kind });
  updateComposers();
}

/** 打开或关闭输入框按钮上方的附件选择菜单。 */
function toggleAttachmentMenu(): void {
  if (attachmentMenuOpen) {
    closeAttachmentMenu();
    return;
  }
  attachmentMenu.replaceChildren();
  for (const kind of ['selection', 'file', 'problems']) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = `@${kind}`;
    button.title = attachmentDescription(kind);
    button.addEventListener('click', () => requestAttachment(kind));
    attachmentMenu.append(button);
  }
  attachmentMenu.hidden = false;
  attachmentMenuOpen = true;
}

/** 隐藏附件选择菜单，并同步其布尔状态。 */
function closeAttachmentMenu(): void {
  attachmentMenu.hidden = true;
  attachmentMenuOpen = false;
}

/** 将待发送附件渲染为输入框下方可移除的标签。 */
function renderAttachmentChips(): void {
  attachmentChips.replaceChildren();
  for (const attachment of attachments) {
    const chip = document.createElement('span');
    chip.className = 'attachment-chip';
    const label = document.createElement('span');
    label.textContent = attachment.label;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = `Remove ${attachment.label}`;
    remove.addEventListener('click', () => {
      attachments = attachments.filter((item) => item.id !== attachment.id);
      renderAttachmentChips();
    });
    chip.append(label, remove);
    attachmentChips.append(chip);
  }
  attachmentChips.hidden = attachments.length === 0;
}

/** 请求消费附件后，清除全部待发送上下文。 */
function clearAttachments(): void {
  attachments = [];
  renderAttachmentChips();
}

/** 将后端或 checkpoint 中的批注转换成 Webview 可编辑的本地结构。 */
function normalizeAnnotations(rawAnnotations: unknown): ResponseAnnotation[] {
  if (!Array.isArray(rawAnnotations)) return [];
  return rawAnnotations.slice(0, 10).map((raw, index) => {
    const item = asRecord(raw);
    return {
      id: String(item.id || `annotation-${index + 1}`),
      source_message_id: String(item.source_message_id || ''),
      source_content_hash: String(item.source_content_hash || ''),
      selected_text: String(item.selected_text || ''),
      surrounding_text: String(item.surrounding_text || item.selected_text || ''),
      comment: String(item.comment || ''),
      anchor_top: Number.isFinite(Number(item.anchor_top))
        ? Number(item.anchor_top)
        : undefined,
    };
  }).filter((item) => item.source_message_id && item.source_content_hash && item.selected_text);
}

/** 重绘批注编号气泡，并同步发送与重试控件状态。 */
function renderPendingAnnotations(): void {
  removeAnnotationMarkers();
  renderAnnotationMarkers();
  updateComposers();
  updateRetryAction();
}

/** 请求被后端接受或切换会话时清空尚未发送的批注。 */
function clearPendingAnnotations(): void {
  pendingAnnotations = [];
  hideAnnotationAction();
  removeAnnotationMarkers();
  updateComposers();
}

/** 为每条待发送批注创建一个编号气泡，点击后可修改评论或删除。 */
function renderAnnotationMarkers(): void {
  for (const [index, annotation] of pendingAnnotations.entries()) {
    const article = finalArticleByMessageId(annotation.source_message_id);
    if (!article) continue;
    const marker = document.createElement('button');
    marker.type = 'button';
    marker.className = 'annotation-marker';
    marker.textContent = String(index + 1);
    marker.title = `Edit annotation ${index + 1}`;
    marker.setAttribute('aria-label', `Edit annotation ${index + 1}`);
    marker.style.top = `${annotation.anchor_top ?? 34 + index * 32}px`;
    marker.addEventListener('click', (event) => {
      event.stopPropagation();
      openAnnotationEditor(annotation, marker.getBoundingClientRect());
    });
    article.append(marker);
  }
}

/** 清除旧气泡，避免重绘或会话切换后留下重复标记。 */
function removeAnnotationMarkers(): void {
  for (const marker of messages.querySelectorAll('.annotation-marker')) marker.remove();
}

/** 根据持久化消息 ID 找到批注所属的最终回答节点。 */
function finalArticleByMessageId(messageId: string): HTMLElement | undefined {
  return Array.from(messages.querySelectorAll<HTMLElement>('.final-message.annotatable'))
    .find((article) => article.dataset.messageId === messageId);
}

const ANNOTATION_BLOCK_SELECTOR = 'p, li, blockquote, pre, h1, h2, h3, h4, h5, h6, td, th';

/** 根据当前 DOM 选区直接打开轻量评论输入框。 */
function offerAnnotationForSelection(): void {
  hideAnnotationAction();
  if (running || pendingAnnotations.length >= 10) return;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return;
  const range = selection.getRangeAt(0);
  const startArticle = closestFinalArticle(range.startContainer);
  const endArticle = closestFinalArticle(range.endContainer);
  if (!startArticle || startArticle !== endArticle) return;
  const messageId = String(startArticle.dataset.messageId || '');
  const contentHash = String(startArticle.dataset.contentHash || '');
  const content = startArticle.querySelector<HTMLElement>('.message-content');
  const selectedText = selection.toString().trim();
  if (!messageId || !contentHash || !content || !selectedText || selectedText.length > 2000) return;

  const surroundingText = selectionContext(range, content, selectedText);
  const articleRect = startArticle.getBoundingClientRect();
  const selectionRect = range.getBoundingClientRect();
  const annotation: ResponseAnnotation = {
    id: `annotation-${Date.now()}-${pendingAnnotations.length + 1}`,
    source_message_id: messageId,
    source_content_hash: contentHash,
    selected_text: selectedText,
    surrounding_text: surroundingText,
    comment: '',
    anchor_top: Math.max(8, selectionRect.top - articleRect.top),
  };
  pendingAnnotations.push(annotation);
  renderPendingAnnotations();
  openAnnotationEditor(annotation, selectionRect);
}

/** 创建与选区或编号气泡相邻的评论编辑器。评论允许为空。 */
function openAnnotationEditor(
  annotation: ResponseAnnotation,
  anchor: DOMRect,
): void {
  hideAnnotationAction();
  const editor = document.createElement('div');
  editor.className = 'annotation-editor';
  const comment = document.createElement('textarea');
  comment.rows = 1;
  comment.maxLength = 2000;
  comment.placeholder = '添加可选评论...';
  comment.value = annotation.comment;
  comment.addEventListener('input', () => { annotation.comment = comment.value; });
  const submit = document.createElement('button');
  submit.type = 'button';
  submit.className = 'annotation-submit';
  submit.textContent = '↑';
  submit.title = 'Save annotation';
  submit.setAttribute('aria-label', submit.title);
  const commit = (): void => {
    annotation.comment = comment.value.trim();
    hideAnnotationAction();
    renderPendingAnnotations();
  };
  submit.addEventListener('mousedown', (event) => event.preventDefault());
  submit.addEventListener('click', commit);
  comment.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      commit();
    }
  });
  editor.append(comment, submit);
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'annotation-remove';
  remove.textContent = '×';
  remove.title = 'Remove annotation';
  remove.setAttribute('aria-label', 'Remove annotation');
  remove.addEventListener('click', () => {
    pendingAnnotations = pendingAnnotations.filter((item) => item.id !== annotation.id);
    hideAnnotationAction();
    renderPendingAnnotations();
  });
  editor.prepend(remove);
  document.body.append(editor);
  positionAnnotationEditor(editor, anchor);
  annotationAction = editor;
  comment.focus({ preventScroll: true });
}

/** 将评论编辑器限制在 Webview 可见区域内，并优先放在选区下方。 */
function positionAnnotationEditor(editor: HTMLElement, anchor: DOMRect): void {
  const bounds = editor.getBoundingClientRect();
  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - bounds.width - 8));
  const below = anchor.bottom + 8;
  const top = below + bounds.height <= window.innerHeight - 8
    ? below
    : Math.max(8, anchor.top - bounds.height - 8);
  editor.style.left = `${left}px`;
  editor.style.top = `${top}px`;
}

/** 返回选区节点所属、且已经由后端确认完成的最终回答。 */
function closestFinalArticle(node: Node): HTMLElement | null {
  const element = node instanceof Element ? node : node.parentElement;
  return element?.closest<HTMLElement>('.final-message.annotatable') || null;
}

/**
 * 使用渲染后的语义块生成纯文本上下文。短块完整保留，长块只保留选区附近内容，
 * 避免把 HTML 或整篇长回答重新注入模型。
 */
function selectionContext(range: Range, content: HTMLElement, selectedText: string): string {
  const blocks: HTMLElement[] = [];
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    if (range.intersectsNode(node)) {
      const element = (node.parentElement?.closest(ANNOTATION_BLOCK_SELECTOR) as HTMLElement | null)
        || content;
      if (content.contains(element) && !blocks.includes(element)) blocks.push(element);
    }
    node = walker.nextNode();
  }
  const blockText = blocks.map((block) => block.innerText.trim()).filter(Boolean).join('\n\n');
  if (!blockText || blockText.length <= 1500) return blockText || selectedText;
  const selectedAt = blockText.indexOf(selectedText);
  if (selectedAt < 0) {
    return `${blockText.slice(0, 490)}\n\n${selectedText}\n\n${blockText.slice(-490)}`;
  }
  const before = blockText.slice(Math.max(0, selectedAt - 500), selectedAt);
  const afterStart = selectedAt + selectedText.length;
  const after = blockText.slice(afterStart, afterStart + 500);
  return `${before}${selectedText}${after}`;
}

/** 移除悬浮批注按钮，避免滚动或重新选择后按钮停留在旧位置。 */
function hideAnnotationAction(): void {
  annotationAction?.remove();
  annotationAction = undefined;
}

/**
 * 渲染一次 run 的文件变更摘要和整轮 Undo/Redo 操作。
 * 单个文件行会请求 VS Code 原生 Diff；conflict/unavailable 状态不会显示恢复按钮，
 * 因为后端无法确认当前文件 hash 与快照一致。
 */
function renderChangeSet(turn: TurnView, changeSet: Record<string, any>): void {
  turn.changes?.remove();
  const files = Array.isArray(changeSet.files) ? changeSet.files.map(asRecord) : [];
  if (files.length === 0) return;
  const panel = document.createElement('section');
  panel.className = 'changes-panel';
  panel.dataset.changeSetId = String(changeSet.id || '');
  const header = document.createElement('div');
  header.className = 'changes-header';
  const title = document.createElement('strong');
  title.textContent = `Changes (${files.length})`;
  const stateValue = String(changeSet.state || 'unavailable');
  const changeSetId = String(changeSet.id || '');
  const actions = document.createElement('div');
  actions.className = 'changes-actions';
  if (stateValue === 'applied' || stateValue === 'reverted') {
    const action = stateValue === 'applied' ? 'undo' : 'redo';
    actions.append(actionButton(action === 'undo' ? 'Undo all' : 'Redo all', () => {
      setRunning(true);
      connectionLabel.textContent = action === 'undo' ? 'Undoing changes' : 'Restoring changes';
      vscode.postMessage({ type: 'changeAction', action, changeSetId });
    }));
  }
  header.append(title, actions);
  const list = document.createElement('div');
  list.className = 'change-list';
  for (const file of files) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'change-file';
    const status = document.createElement('span');
    status.className = `change-status ${String(file.status || '')}`;
    status.textContent = ({ added: 'A', deleted: 'D', modified: 'M' } as Record<string, string>)[String(file.status)] || 'M';
    const path = document.createElement('span');
    path.textContent = String(file.path || '');
    const stats = document.createElement('span');
    stats.className = 'change-stats';
    stats.textContent = `+${Number(file.additions || 0)} -${Number(file.deletions || 0)}`;
    row.append(status, path, stats);
    row.addEventListener('click', () => {
      vscode.postMessage({ type: 'openDiff', changeSetId, path: file.path });
    });
    list.append(row);
  }
  panel.append(header, list);
  if (stateValue === 'conflict' || stateValue === 'unavailable') {
    const warning = document.createElement('p');
    warning.className = 'change-warning';
    warning.textContent = stateValue === 'conflict'
      ? `${String(changeSet.message || 'Files changed after this run')}. Undo/redo is unavailable.`
      : String(changeSet.message || 'This change set cannot be restored safely.');
    panel.append(warning);
  }
  turn.root.append(panel);
  turn.changes = panel;
}

/** 将当前 VS Code Problems 添加到对应 run 的变更面板下方。 */
function renderChangeDiagnostics(event: BackendEvent): void {
  const changeSetId = String(event.changeSetId || '');
  const panel = Array.from(document.querySelectorAll<HTMLElement>('.changes-panel'))
    .find((item) => item.dataset.changeSetId === changeSetId);
  if (!panel) return;
  panel.querySelector('.change-diagnostics')?.remove();
  const diagnostics = Array.isArray(event.diagnostics) ? event.diagnostics.map(asRecord) : [];
  if (diagnostics.length === 0) return;
  const section = document.createElement('div');
  section.className = 'change-diagnostics';
  const title = document.createElement('strong');
  title.textContent = `Problems (${diagnostics.length})`;
  section.append(title);
  for (const diagnostic of diagnostics) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'diagnostic-row';
    row.textContent = `${String(diagnostic.path || '')}:${String(diagnostic.line || '')} ${String(diagnostic.message || '')}`;
    row.addEventListener('click', () => vscode.postMessage({
      type: 'openLocation', path: diagnostic.path, line: diagnostic.line,
    }));
    section.append(row);
  }
  panel.append(section);
}

/** 插入选中的斜杠命令，同时允许用户继续编辑参数。 */
function applyCommandSuggestion(command: string): void {
  input.value = command;
  input.focus();
  input.setSelectionRange(command.length, command.length);
  hideCommandMenu();
  updateComposers();
}

/** 清空补全菜单 DOM，并将键盘选择重置到第一项。 */
function hideCommandMenu(): void {
  commandMenu.hidden = true;
  commandMenu.replaceChildren();
  commandSelection = 0;
}

/** 让聊天滚动区域始终显示最新追加的输出。 */
function scrollToBottom(): void {
  messages.scrollTop = messages.scrollHeight;
}

/** 将多个流式增量合并为每帧最多一次 Markdown 渲染。 */
function scheduleStreamRender(stream: StreamView): void {
  if (stream.renderFrame !== undefined) return;
  stream.renderFrame = requestAnimationFrame(() => {
    stream.renderFrame = undefined;
    if (stream.content) {
      renderMarkdown(stream.content, stream.text);
      scrollToBottom();
    }
  });
}

/** 取消延迟渲染，并同步绘制已经累积的全部流式文本。 */
function flushStreamRender(stream: StreamView): void {
  if (stream.renderFrame !== undefined) {
    cancelAnimationFrame(stream.renderFrame);
    stream.renderFrame = undefined;
  }
  if (stream.content) renderMarkdown(stream.content, stream.text);
}

// DOM 事件绑定统一放在文件末尾，便于先阅读上方的 UI 操作，再理解哪些用户动作会
// 触发这些操作。
composer.addEventListener('submit', (event) => { event.preventDefault(); sendMessage(); });
newTaskForm.addEventListener('submit', (event) => { event.preventDefault(); startNewTask(); });
input.addEventListener('input', () => { commandSelection = 0; updateComposers(); });
newTaskInput.addEventListener('input', updateComposers);
input.addEventListener('keydown', (event) => {
  if (!commandMenu.hidden && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
    event.preventDefault();
    const count = commandMenu.childElementCount;
    commandSelection = (commandSelection + (event.key === 'ArrowDown' ? 1 : -1) + count) % count;
    updateCommandMenu();
  } else if (!commandMenu.hidden && event.key === 'Tab') {
    event.preventDefault();
    (commandMenu.children.item(commandSelection) as HTMLButtonElement | null)?.click();
  } else if (!commandMenu.hidden && event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    (commandMenu.children.item(commandSelection) as HTMLButtonElement | null)?.click();
  } else if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  } else if (event.key === 'Escape') {
    hideCommandMenu();
  }
});
document.addEventListener('click', (event) => {
  const target = event.target;
  if (target instanceof Node && !runtimeMenu?.contains(target)) closeRuntimeMenu();
  if (target instanceof Node && !attachmentMenu.contains(target) && target !== attachmentButton) {
    closeAttachmentMenu();
  }
  if (target instanceof Node && !annotationAction?.contains(target)) hideAnnotationAction();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeRuntimeMenu();
    closeAttachmentMenu();
    hideAnnotationAction();
  }
});
window.addEventListener('resize', () => {
  closeRuntimeMenu();
  hideAnnotationAction();
});
messages.addEventListener('mouseup', (event) => {
  const target = event.target;
  if (!(target instanceof Element) || !target.closest('.final-message .message-content')) return;
  window.setTimeout(offerAnnotationForSelection, 0);
});
messages.addEventListener('scroll', hideAnnotationAction);
newTaskInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    startNewTask();
  }
});
stopButton.addEventListener('click', () => vscode.postMessage({ type: 'cancel' }));
backButton.addEventListener('click', () => { if (!running) setPage('home'); });
renameCurrentButton.addEventListener('click', () => {
  const session = asRecord(state.session);
  openRenameDialog(String(session.id || ''), String(session.title || ''));
});
settingsButton.addEventListener('click', () => vscode.postMessage({ type: 'openSettings' }));
attachmentButton.addEventListener('click', (event) => {
  event.stopPropagation();
  toggleAttachmentMenu();
});
// 这是 Extension Host -> Webview 的唯一入口。backendEvent 携带清理后的 Python
// 事件，attachment 消息则完成只能在 Host 中进行的编辑器读取。
window.addEventListener('message', (event: MessageEvent<unknown>) => {
  const envelope = asRecord(event.data);
  if (envelope.type === 'backendEvent') {
    handleBackendEvent(asRecord(envelope.event) as BackendEvent);
  } else if (envelope.type === 'attachmentResult') {
    attachmentPending = false;
    const attachment = asRecord(envelope.attachment) as unknown as EditorAttachment;
    if (attachment.id && attachment.content) {
      attachments = [...attachments.filter((item) => item.id !== attachment.id), attachment];
      renderAttachmentChips();
    }
    updateComposers();
  } else if (envelope.type === 'attachmentError') {
    attachmentPending = false;
    appendVisibleError(String(envelope.message || 'Could not attach editor context.'));
    updateComposers();
  } else if (envelope.type === 'attachmentCancelled') {
    attachmentPending = false;
    updateComposers();
  }
});

// 宣告就绪前先初始化本地控件。Provider 收到 `ready` 后会启动后端并返回权威状态。
updateComposers();
updateRuntimeControls();
vscode.postMessage({ type: 'ready' });
