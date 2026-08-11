/**
 * Python JSONL Bridge 消息在 TypeScript 侧共用的数据结构。
 *
 * 接口只能提供编译期约束，运行时收到的仍是不可信 JSON，因此读取字段前还要使用
 * isRecord() 和 isBridgeEvent() 做最小结构校验。
 */

/** Webview 首页展示的会话基础信息。 */
export interface SessionInfo {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** 随 ready、session 和 run 事件返回的完整 UI 状态快照。 */
export interface BridgeState {
  workspace_root: string;
  provider: string;
  model: string;
  available_providers: string[];
  available_models: string[];
  approval_policy: string;
  workflow_mode: string;
  session: SessionInfo;
  retry: {
    available: boolean;
    user_request: string;
  };
  change_sets: unknown[];
}

/**
 * Python Bridge 发出的所有事件共用的信封结构。
 *
 * commentary、tool、approval 和 command 的载荷不同，因此其余字段保持 unknown。
 * Webview 只在对应事件分支中进一步收窄类型。
 */
export interface BridgeEvent {
  type: string;
  request_id?: string;
  interaction_id?: string;
  state?: BridgeState;
  history?: DisplayHistoryItem[];
  [key: string]: unknown;
}

/** 用于重建可见聊天记录的、经过裁剪的历史消息投影。 */
export interface DisplayHistoryItem {
  id: string;
  role: string;
  kind: string;
  content: string;
  name?: string;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call_id?: string;
  metadata?: Record<string, unknown>;
  conversation_id: string;
  created_at: string;
}

/** 判断任意 JSON 值是否具备 Bridge 事件要求的最小结构。 */
export function isBridgeEvent(value: unknown): value is BridgeEvent {
  return isRecord(value) && typeof value.type === 'string';
}

/**
 * 将 unknown 收窄为普通键值对象。
 * 数组会被排除，因为协议中的具名字段必须是 JSON 对象。
 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
