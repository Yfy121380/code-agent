可以，当前过程笔记可以总结为这套设计。

**定位**

过程笔记只记录运行中出现的异常工具调用，不记录普通事实、不记录文件摘要、不参与普通 memory 召回。它的作用是给下一轮模型一个短期执行提醒：哪些调用刚出过问题、哪些文件需要复查、哪些调用不要原样重试。

**记录内容**

最多保留 6 条错误笔记，每条笔记记录：

```json
{
  "kind": "error | partial_success | rejected",
  "tool": "工具名",
  "affected_paths": ["相关文件"],
  "note": "给模型的注意事项",
  "created_turn": 轮次",
  "updated_turn": 轮次,
  "status": "open"
}
```

不同原因的note设计不同，其中：

- `error`：记录报错信息，并提示“这样会出错，先检查失败原因再重试”
- `partial_success`：记录哪些文件发生变化，并提示“先检查这些文件，再继续使用或重试”
- `rejected`：记录这个工具调用被拒绝过，并提示“这个工具 + 参数被拒绝过，尽量不要原样调用”

**使用方式**

过程笔记不放进 `episodic_notes`，也不走 `retrieval_candidates()`。

它应该单独渲染进 prompt，例如：

```text
Process notes are short-term execution reminders. Use them to avoid repeated failed actions, but verify the current workspace state before making assumptions.
Process notes:
- error: run_shell failed with exit_code 1.
  Note: This command failed before; inspect the error before retrying.
- partial_success: patch_file changed sample.txt before failing.
  Note: Read or inspect affected files before continuing.
- rejected: read_file with the same arguments was rejected as repeated.
  Note: Avoid calling the same tool with the same arguments again.
```

**写入规则**

工具调用后，如果状态异常，就写入或更新过程笔记：

```text
tool_status == error           -> 写 error 笔记
tool_status == partial_success -> 写 partial_success 笔记
tool_status == rejected        -> 写 rejected 笔记
```

同类笔记可以合并更新，而不是重复追加。例如同一个 `tool + reason + affected_paths` 再次出现，就更新 `updated_turn` 和计数。

**清除规则**

所有过程笔记都有三轮 `ask()` 的 TTL，超过三轮自动过期。

额外清除策略：

```text
error:
- 同一个工具后续 ok 后清除

partial_success:
- affected_paths 被 read_file / diff 检查后清除
- 或相关文件后续成功写入并验证后清除

rejected:
- 同一个工具后续 ok 后清除
- repeated_call 类型：只要后续采取了不同动作就清除
```

**保留优先级**

最多 6 条，超过上限时优先删除：

```text
已 resolved/expired > 最旧 rejected > 最旧 error > 最旧 partial_success
```

因为 `partial_success` 可能意味着 workspace 已经发生副作用，风险最高，应该最后删除。

**一句话版**

过程笔记是一组短期、最多 6 条、三轮过期的异常执行提醒；它只记录 `error`、`partial_success`、`rejected`，单独进入 prompt，不参与普通记忆召回，并在后续成功执行、复查文件或采取不同动作后清除。