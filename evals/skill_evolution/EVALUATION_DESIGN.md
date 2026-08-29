# Skill 自进化评测流程与用例设计

本文档说明 `evals/skill_evolution` 的评测目标、完整执行流程、四组评测用例及结果解释方式。
运行命令参见 [README.md](README.md)。

## 一、评测意义

Skill 自进化不能只检查“是否生成了一个 `SKILL.md`”。真正需要回答的是：Agent 能否从一次任务及后续反馈中提炼出可复用经验，并在没有见过的新任务中因为使用该 Skill 而表现得更好。

因此，本评测采用“诱导任务 + 独立迁移任务”的成对设计：

```text
诱导任务
  -> Agent 完成任务
  -> 隐藏 Verifier 检查结果
  -> 用户安全反馈
  -> Skill 自进化生成 Skill

独立迁移任务
  -> Baseline：不提供生成的 Skill
  -> Forced：强制加载生成的 Skill
  -> 比较正确性、约束遵循、报告质量和执行成本
```

这种设计主要评测以下能力：

1. **经验提取**：能否从任务过程和用户反馈中总结出可跨任务复用的方法，而不是复制当前任务答案。
2. **迁移效果**：生成的 Skill 能否改善同类但内容不同的新任务。
3. **行为正确性**：能否完成核心功能并保留相邻旧行为。
4. **约束遵循**：能否遵守只读、不得修改测试、限制依赖和指定输出位置等要求。
5. **偏好学习**：能否把用户明确要求的汇报结构迁移到后续任务。
6. **质量与安全性**：能否提供行为层验证、代码证据和来源引用，并避免无依据扩展。
7. **效率变化**：当正确性相同时，Skill 是否减少尝试次数、工具步骤或 Token 消耗。

评测不向 Agent 暴露隐藏断言、隐藏输入和期望输出。否则 Agent 可能只针对测试修补，无法衡量其独立调查、实现和迁移能力。

## 二、完整评测流程

### 1. 加载任务定义

每个任务由一个 `task.json` 描述，包含：

- 任务 ID 和任务类别；
- 希望学到的 Skill 目标；
- 诱导任务和迁移任务的公开请求；
- 工作区和隐藏 Verifier 路径；
- 检查项类别、权重、是否必需及可反馈给 Agent 的安全提示；
- 诱导任务完成或达到轮次上限后的终结反馈。

运行 `--all` 时，Runner 会发现 `tasks/<category>/<task>/task.json` 下的全部任务，并按路径顺序串行执行。

### 2. 创建隔离环境

原始任务只作为只读夹具使用。Runner 会把任务复制到系统临时目录后运行，阶段结束后再把最终工作区复制回输出目录保存：

```text
runs/skill-evolution-eval/<task-id>/
├── induction/
│   ├── workspace/
│   └── home/
├── transfer/
│   ├── baseline/
│   │   ├── workspace/
│   │   └── home/
│   └── forced/
│       ├── workspace/
│       └── home/
├── result.json
└── report.md
```

默认执行根目录为 `/tmp/codemate-skill-evolution-eval/<output-hash>/<task-id>`，可通过 `--work-dir` 修改。执行目录放在源码仓库之外，防止 Agent 通过父目录发现 `task.json`、隐藏 Verifier 或 CodeMate 自身源码。`runs/.../workspace` 是阶段结束后的持久化副本，不是 Agent 实际运行时的目录。

隔离规则如下：

- 每个阶段使用独立工作区和独立 `CODEMATE_HOME`；
- 复制时清除任务夹具中的 `.codemate`，不继承项目配置和项目级 Skill；
- `WorkspaceContext` 直接使用启动目录作为工作区根，不会向上继承 CodeMate 源码仓库的 Git 根目录；
- 不读取用户原有的 `~/.codemate`，因此不会继承用户级 Skill 和会话状态；
- MCP 服务器列表为空；
- 长期记忆 backend 为 `disabled`；
- benchmark 模式关闭相关记忆召回、候选记忆提取、Dream 和会话标题生成；
- Skill 自进化只在诱导阶段开启，Baseline 和 Forced 均关闭在线演化；
- 诱导阶段和 Baseline 启动时必须看不到任何 Skill；
- Forced 启动时只能看到本次诱导阶段生成的 Skill。

这些限制用于减少长期记忆、旧 Skill、MCP 和历史会话对结果的污染，使 Baseline 与 Forced 的主要变量只有“是否加载新 Skill”。

### 3. 执行诱导任务

诱导任务最多进行四轮任务修正：

```text
执行当前请求
  -> 保存 Final、Trace 和运行指标
  -> 运行隐藏 Verifier
  -> 所有必需硬检查通过？
       是：结束任务修正
       否：生成安全反馈，进入下一轮
```

隐藏 Verifier 会检查工作区最终状态、Agent 最终回答和必要的 Trace 信息。检查项分为：

- `functional`：目标功能是否正确；
- `regression`：原有或相邻行为是否保持；
- `instruction`：是否遵守用户明确约束；
- `safety`：是否满足安全边界；
- `preference`：是否遵循用户偏好的输出方式；
- `quality`：验证、证据和报告质量是否达标。

每项检查是否参与 `completed` 判定由其 `required` 字段决定。通常功能、回归、指令和
安全检查是必需项；为了让诱导任务形成真实的“结果—反馈—修正”链路，代码修改和代码
调研的诱导阶段还把待学习的报告偏好或代码证据要求设为必需项。迁移阶段再将报告偏好
恢复为非必需评分项，用于观察 Skill 是否能在没有反馈的情况下迁移该要求。

如果当前轮未完成，Runner 最多选择三个失败项生成反馈，优先级为：

```text
必需项优先
-> functional
-> regression
-> safety
-> instruction
-> preference
-> quality
```

反馈只使用 `task.json` 中提前审核过的 `agent_feedback`，不会包含 Verifier 收集到的私有证据。例如，Agent 可以得知“参数边界仍不完整”，但不会看到隐藏测试使用了哪些具体输入。

任务修正结束后，Runner 还会发送一次 `pass_feedback` 或 `fail_feedback`。Skill 自进化采用延迟反馈窗口：一次回答先形成 Pending Window，下一条用户反馈到来后才变为 Ready Window。因此，这条终结反馈用于让最后一轮完整任务过程进入候选提取链路。Runner 随后等待 Skill 自进化后台任务结束。

### 4. 识别本次生成的 Skill

Runner 不会简单地把 `.codemate/skills` 中的所有目录都视为新 Skill，而是同时验证：

1. 诱导开始前的 Skill 快照中没有该名称；
2. 诱导结束后的快照中出现了该名称；
3. Skill 已登记在 Skill evolution 的 managed registry；
4. registry 中记录的受管文件仍存在，且内容哈希一致。

快照保存 Skill 名称和 `SKILL.md` 内容哈希。已有 Skill 被修改不会被算作新建；模型绕过自进化链路直接写入 Skill 目录也不会被接受，评测会直接报错。

如果诱导阶段没有生成任何符合上述条件的 Skill，任务结果记为 `no_skill_generated`，并跳过 Baseline 和 Forced。没有 Skill 时继续运行两组迁移任务无法形成有效对照，只会增加模型调用成本和随机差异。

### 5. 执行迁移对照

迁移任务与诱导任务属于同一能力类别，但使用不同的代码、接口或调研主题。

Runner 从同一份迁移夹具分别创建两个全新副本：

#### Baseline

- 不复制任何新 Skill；
- Agent 启动时不可见任何 Skill；
- Skill 自进化关闭；
- 独立完成一次迁移任务。

#### Forced

- 只复制诱导阶段生成且通过 ownership 校验的 Skill；
- 在任务开始前强制完整加载这些 Skill；
- Skill 自进化关闭，避免在迁移过程中继续学习；
- 在其他配置相同的情况下完成同一迁移任务。

这里采用 Forced 而不是自然检索，是为了直接测量“Skill 内容本身是否有帮助”。如果同时评测自然检索，结果会混合 Skill 质量和检索命中率，难以定位问题来源。

### 6. 计算分数和结果分类

每个检查项具有独立权重：

```text
总分 = 已通过检查项权重之和 / 全部检查项权重之和 * 100
```

同时按类别计算 `category_scores`，方便区分功能、回归、约束、偏好和质量方面的变化。

Baseline 与 Forced 的最终关系分为：

- `improved`：Forced 总分高于 Baseline，且没有必需行为回归；
- `more_efficient`：两者质量相同，Forced 的尝试次数和工具步骤不更差，并且步骤、尝试或 Token 至少一项改善；
- `neutral`：质量和效率没有可确认的改善；
- `harmful`：Forced 总分下降、必需检查从通过变为失败，或 Baseline 已完成而 Forced 未完成。
- `no_skill_generated`：诱导过程没有沉淀出可用于迁移测试的受管 Skill，因此未执行迁移对照。

正确性优先于效率。即使 Forced 使用更少 Token，只要引入功能回归，也会被标记为 `harmful`。

### 7. 保存评测工件

每个任务保存：

- `result.json`：完整检查结果、私有证据、运行指标和生成 Skill；
- `report.md`：适合人工阅读的诱导与迁移对比，不展示私有证据；
- 各阶段工作区副本：用于检查 Agent 最终改动；
- 各阶段独立 `CODEMATE_HOME`：包含 Session、Trace、Transcript 和 Skill evolution 记录。

全部任务还会生成 `summary.json`。使用同一个输出目录重新运行同一任务时，会先删除该任务旧工件，避免旧 Skill、Session 或文件改动污染新结果。

## 三、评测用例设计

### 用例总览

| 类别 | 诱导任务 | 迁移任务 | 主要迁移能力 |
| --- | --- | --- | --- |
| 代码修改 | 修复批处理大小的跨层解析 | 修复消息超时的跨层解析 | 区分缺省值和非法 falsy 值、保护副作用分支、运行时验证 |
| 代码调研 | 调查文档预览服务 | 调查事件发布服务 | 排除旧入口，追踪装配、条件分支、配置流和错误转换 |
| 从零构建 | 实现笔记服务 | 实现库存服务 | 从公开契约覆盖正常行为、失败语义和状态隔离 |
| 资料调研 | 构建可复现性报告 | 生产可观测性报告 | 事实提取、结论与来源对应、限制无依据扩展 |

### 1. 代码修改：运行时边界与行为验证

任务 ID：`code-modification-runtime-validation`

#### 诱导任务

Agent 需要调查 `bulk_apply` 的公共入口、默认值解析和批处理执行器，修复显式
`batch_size=0` 被 `or` 回退为默认值的问题，同时保持 `None`、合法值和 `dry_run`
行为不变。

隐藏检查覆盖：

- `0`、负数、布尔值、浮点数和字符串按契约抛出 `TypeError` 或 `ValueError`；
- 无效输入在 writer 收到任何 batch 前失败；
- 显式合法批大小和 `None` 默认值仍生成正确批次及统计；
- `dry_run` 处理相同输入但不调用 writer；
- `batch_public_tests.py` 内容保持不变；
- 最终回答使用“修改内容、验证结果、剩余风险”三个标题；
- 最终回答说明执行过测试或运行时行为验证。

#### 迁移任务

Agent 需要在另一套消息分发代码中修复 `timeout_ms=0` 被默认值吞掉的问题。

隐藏检查覆盖：

- `0`、负数、布尔值、浮点数和字符串必须在 transport 调用前被拒绝；
- 显式超时和 `None` 默认超时仍正确传给 transport；
- `validate_only` 保留计划结果但不能发送消息；
- 公开测试不得修改；
- 继续使用诱导阶段要求的汇报结构和行为验证说明。

#### 评测重点

该用例检查 Skill 是否学到跨模块运行时选项修改的一般方法：先从公共入口追踪默认值
解析和副作用边界，区分 `None` 与其他 falsy 值，再同时验证显式值、默认值和无副作用
分支，而不是只记住某个函数的参数名称。

### 2. 代码调研：调用链和错误路径

任务 ID：`code-research-call-chain`

#### 诱导任务

Agent 需要在带同名 legacy 入口的文档服务中确认 `preview_document` 的真实路径，解释
请求上下文和依赖装配、缓存命中/未命中、租户策略、配置及渲染参数如何流动，并追踪
缺失文档与底层超时的错误转换。

隐藏检查覆盖：

- 覆盖 API、Context、Container、Controller、Service、Repository、Gateway 和 Renderer；
- 区分缓存命中与未命中路径，说明 table、tenant、locale、include-deleted 和脱敏策略；
- 说明 `DocumentMissing -> HttpError(404)`；
- 说明 `TimeoutError -> StorageUnavailable -> HttpError(503)`；
- 所有源码保持原样；
- 为六个真实路径模块分别提供有效的 `相对路径:行号` 代码引用；
- 使用“入口与装配、核心调用与分支、错误传播”组织报告。

#### 迁移任务

Agent 需要在另一套带 legacy 入口的事件服务中调查 `publish_event`，追踪 Context、
Container、Controller、Publisher、Router、Handler、Receipt 和 Outbox 的关系。

隐藏检查覆盖：

- 区分首次发布与重复 request ID 的幂等分支，并说明 tenant 和 outbox stream 的传递；
- 区分 `UnknownTopic -> 404`、`InvalidPayload -> 422` 和
  `ConnectionError -> OutboxUnavailable -> 503`；
- 使用文件哈希确认全部源码未修改；
- 为六个真实路径模块提供有效代码引用；
- 保持已学习的报告结构。

#### 评测重点

该用例检查 Agent 是否能先确认真实入口并排除同名旧实现，再主动扩展到依赖装配、
上下文与配置传递、缓存或幂等分支、数据层和多级异常转换，最后用跨模块代码位置支撑
结论，而不是按文件名或第一个局部实现猜测调用链。

### 3. 从零构建：公开契约与状态隔离

任务 ID：`project-build-service-contract`

#### 诱导任务

Agent 需要根据 README 从零实现标准库版本的 `NotesService`，包括创建、查询、列出和删除笔记。

隐藏检查覆盖：

- 正常创建、列表顺序、删除和默认正文行为；
- 空 ID、空标题、重复 ID 和缺失 ID 的失败语义；
- `get()` 和 `list()` 返回快照，调用方修改返回字典不能污染内部状态；
- README 保持不变，源码只使用允许的标准库模块；
- 最终回答说明可执行验证；
- 使用“实现、验证、限制”三个标题。

#### 迁移任务

Agent 需要根据另一份 README 实现 `InventoryService`，包括增加库存、查询和预留库存。

隐藏检查覆盖：

- 正常累加、查询和扣减库存；
- 空 SKU、非正整数、布尔值和浮点数的输入校验；
- 未知 SKU 抛出 `KeyError`；
- 超量预留抛出 `ValueError` 且不能改变库存；
- 不同实例之间不能共享库存状态；
- README、标准库约束、验证说明和报告结构保持正确。

#### 评测重点

该用例检查 Skill 能否帮助 Agent 从公开契约系统推导接口、边界、失败行为和状态所有权，而不是只实现最短的正常路径。

### 4. 受控来源调研：证据对应与事实边界

任务 ID：`web-research-evidence-report`

该用例使用本地冻结的 `sources/` 资料包，而不访问真实互联网。这样可以固定来源内容，避免网页更新、网络失败或搜索排序变化影响实验复现。它评测的是来源阅读和证据组织能力，不是搜索引擎召回能力。

#### 诱导任务

Agent 需要根据三个来源文件编写构建可复现性报告，内容包括：

- 使用精确依赖锁；
- 在干净、隔离的环境中构建；
- 为发布产物保存并验证加密哈希。

隐藏检查覆盖：

- 三项关键建议均出现，并分别与 `[S1]`、`[S2]`、`[S3]` 正确对应；
- 报告写入 `report.md`，来源文件保持不变；
- 不凭空加入 Docker、GitHub Actions、Sigstore 或 SLSA 等来源未支持的具体技术；
- 报告使用“结论、证据、限制”组织。

#### 迁移任务

Agent 需要根据另一组来源编写生产可观测性报告，内容包括：

- 带请求关联标识的结构化日志；
- 请求率、错误率和延迟指标；
- 跨服务传播 Trace Context。

隐藏检查同时验证 `[S1]`、`[S2]`、`[S3]` 的对应关系，并禁止无依据加入 Prometheus、Grafana、Jaeger 或 OpenTelemetry 等具体技术名称。

#### 评测重点

该用例检查 Skill 能否让 Agent 区分“来源事实、基于事实的建议和资料边界”，确保关键结论可追溯，同时避免使用常识补写来源中不存在的具体事实。

## 四、如何解读当前结果

四组用例共同覆盖代码修改、代码理解、项目构建和资料调研，但它们仍属于小规模、受控评测。结果应按以下方式理解：

- `improved` 表示当前 Skill 在该迁移任务上带来了可测量的质量提升，不代表对所有同类任务都有效；
- `neutral` 可能表示 Skill 没有帮助，也可能表示 Baseline 已经满分，当前用例无法继续区分；
- `harmful` 需要优先检查 Skill 是否过度约束、错误迁移诱导任务细节，或干扰了 Agent 原有判断；
- 没有生成 Skill 时，Forced 与 Baseline 实际相同，通常只能得到 `neutral`；
- 当前 Verifier 以确定性检查为主，没有使用 LLM Judge，因此可复现性较高，但对表达质量和语义完整性的判断能力有限；
- 当前 Forced 设计不评测自然 Skill 检索质量；如果后续要评测检索，应单独增加 Natural 组，避免与 Skill 内容质量混为一个变量；
- 四个任务对不足以形成统计结论。正式比较模型或版本时，应增加同类迁移任务、固定模型参数并进行多次重复运行。

因此，这套数据集更适合用于验证 Skill 自进化链路是否成立、发现明显的正向迁移或负迁移，以及指导后续扩充评测集，而不应单独作为完整能力排名。
