# Skill 自进化评测

该数据集用于衡量 Agent 能否根据诱导任务中的用户反馈生成可复用的 Skill，
并使用该 Skill 改善独立迁移任务的执行结果。

完整的评测意义、执行流程、用例设计和结果解释参见
[EVALUATION_DESIGN.md](EVALUATION_DESIGN.md)。

每组任务包含：

- 一个诱导任务工作区，最多进行四轮由反馈驱动的任务修正；
- 一个未在诱导阶段出现的迁移任务工作区，分别运行 Baseline 组和强制加载生成 Skill 的 Forced 组；
- 存放在 Agent 工作区之外的隐藏验证器；
- 确定性的功能、回归、指令遵循、用户偏好和质量检查。

隐藏验证器的具体证据只会写入评测工件，不会发送给 Agent。Agent 收到的反馈来自
`task.json` 中经过单独检查的 `agent_feedback`，不会包含隐藏输入、断言或期望输出。

## 运行单个任务

```bash
python -m evals.skill_evolution.runner \
  --task evals/skill_evolution/tasks/code_modification/runtime_validation/task.json \
  --provider openai \
  --model YOUR_MODEL \
  --output-dir runs/skill-evolution-eval
```

实际任务默认在 `/tmp/codemate-skill-evolution-eval` 下的隔离副本中执行，结束后再将
最终工作区复制到 `--output-dir`。可使用 `--work-dir` 指定其他外部执行目录。

## 运行全部任务

```bash
python -m evals.skill_evolution.runner \
  --all \
  --provider openai \
  --model YOUR_MODEL \
  --output-dir runs/skill-evolution-eval
```

运行器会为诱导组、Baseline 组和 Forced 组分别创建独立的工作区与
`CODEMATE_HOME`。在线 Skill 自进化只在诱导阶段开启；迁移阶段使用 benchmark
隔离配置，同时关闭在线自进化和长期记忆，避免跨任务状态污染结果。
`WorkspaceContext` 会直接把复制后的启动目录作为工作区根，不会把外层 CodeMate
源码仓库误认为任务根目录。

评测固定使用项目级 Skill。运行期间新 Skill 写入外部临时工作区的
`<workspace>/.codemate/skills`；诱导阶段完成后，该目录随工作区复制到
`runs/.../induction/workspace/.codemate/skills`，Forced 组也只从这里复制 Skill。
各阶段的 `home/skills` 只是隔离的用户级目录，必须保持为空；运行器不会读取或写入
真实的 `~/.codemate/skills`。启动时如发现任一 Skill 根目录或演化目标不符合上述约束，
评测会立即失败。

运行时还会关闭候选记忆提取、Dream、相关记忆召回、会话标题生成和 MCP，且不会继承
用户级或任务夹具中已有的 Skill。诱导组与 Baseline 组启动时必须看不到任何 Skill；
Forced 组只能看到本次诱导阶段新生成的 Skill，否则评测会直接失败。

“新生成 Skill”通过诱导开始前后的项目 Skill 快照差集确定。快照记录 Skill 名称和
`SKILL.md` 内容哈希：只有结束时新增的名称才算新 Skill，已有 Skill 的内容变化不会被
误判为新建。新增文件还必须登记在 Skill evolution 的 managed registry 中，确保它由
自进化链路创建，而不是模型直接写入 Skill 目录；不满足条件时评测会直接失败。
如果诱导阶段没有生成受管 Skill，该任务会记录为 `no_skill_generated` 并跳过迁移阶段。

输出目录中包含每个任务的 `result.json`、`report.md`，以及全部任务的汇总文件
`summary.json`。运行器会保留生成的工作区副本，便于调试和复现实验。使用同一输出目录
重新运行同一任务时，运行器会替换该任务以前的工件，防止旧会话或旧 Skill 影响新结果。
