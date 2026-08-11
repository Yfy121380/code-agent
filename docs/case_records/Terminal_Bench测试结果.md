# Terminal-Bench 2 GPT-5.5 全量测试报告

## 1. Terminal-Bench 是什么

Terminal-Bench 是面向终端智能体的综合能力评测。它不只要求 Agent 修改一个已有代码仓库，而是把 Agent 放入隔离的 Docker 环境中，提供一段自然语言任务说明，让 Agent 使用终端和文件工具完成真实操作，最后由独立 verifier 检查容器中的最终状态。

一次任务的基本流程是：

```text
自然语言任务说明
  -> 启动任务专用 Docker 环境
  -> 安装并运行 Agent
  -> Agent 在 /app 中调查、创建或修改文件并执行命令
  -> Agent 退出
  -> 独立 verifier 检查文件、程序行为、服务状态或数值结果
  -> 生成 reward 和运行记录
```

与只评测代码补丁的 benchmark 相比，Terminal-Bench 更强调端到端执行能力。Agent 不仅要理解需求和编写代码，还可能需要：

- 调查一个陌生环境，判断可用的语言、工具和依赖；
- 安装、编译和配置软件；
- 编写脚本、服务、配置文件或数据处理流程；
- 恢复损坏的数据、仓库或系统状态；
- 运行长期进程，并保证进程、端口或 socket 在任务结束后仍然可用；
- 完成科学计算、机器学习、密码分析、逆向分析或多媒体处理；
- 清理临时文件，并满足严格的最终目录结构；
- 在看不到 verifier 实现的情况下，独立验证最终行为。

### 1.1 本次使用的数据集

本次运行的是本地 `terminal-bench-2` 数据集，共 85 个任务。任务难度分布为：

| 难度 | 数量 |
| --- | ---: |
| Easy | 4 |
| Medium | 53 |
| Hard | 28 |

这些任务覆盖的主要领域如下：

| 任务领域 | 数量 | 代表任务 |
| --- | ---: | --- |
| 软件工程 | 25 | 构建 Cython 扩展、修复 OCaml GC、实现 gRPC KV Store、配置 Git Web Server |
| 系统管理、调试与文件操作 | 18 | QEMU 启动、SQLite 截断恢复、WAL 恢复、Git 泄露恢复、大规模文本编辑 |
| 科学计算、数据与机器学习 | 27 | 自适应拒绝采样、MCMC、Raman 拟合、PyTorch 并行、MTEB、投资组合优化 |
| 安全 | 8 | XSS 过滤、代码漏洞修复、密码恢复、7z 哈希破解、证书配置 |
| 数学 | 4 | 最大特征值、形式化证明、电路计算、约束调度 |
| 其他综合任务 | 3 | 游戏策略、个人助理、视频处理 |

因此，这 85 个任务并不是 85 个同质化的小型编程题。它们同时考查需求理解、终端操作、环境管理、领域知识、实现能力和最终验证能力。

### 1.2 评测结果应该如何理解

Terminal-Bench 通常使用 0/1 reward：

- `1.0`：最终状态通过 verifier；
- `0.0`：最终状态未通过 verifier；
- 无 reward：verifier 自身超时或评测流程没有得到有效结果。

需要注意，零分不一定代表 Agent 的方案错误。模型连接中断、依赖下载失败、外部 Git 仓库不可达和 verifier 超时，也会导致任务没有通过。因此，分析时必须区分：

```text
Agent 能力失败
模型或运行时中断
Verifier 基础设施失败
```

只报告一个未经归因的平均 reward，会把模型服务、网络和评测环境问题错误地归到 Agent 能力上。

## 2. 实验设置

本次实验配置如下：

| 配置项 | 设置 |
| --- | --- |
| 数据集 | Terminal-Bench 2，本地完整 85 任务 |
| Agent | Codemate 0.1.0 |
| 模型 | OpenAI-compatible GPT-5.5 |
| 并发任务数 | 2 |
| 单任务失败重试 | 最多 1 次 |
| 运行环境 | Harbor + Docker |
| Agent 权限 | full |
| Verifier 缓存 | 挂载共享 uv 缓存 |
| 总运行时间 | 约 9 小时 41 分钟 |

原始实验数据保存在：

```text
/home/xidiannss/Experiments/yfy/agents/benchs/TerminalBench/
  data/terminal-bench-2/tasks_index.jsonl
  jobs/tb2-codemate-gpt55-all-cached/result.json
  jobs/tb2-codemate-gpt55-all-cached/<trial-id>/
```

针对首次运行中没有形成有效能力结论的 15 个任务，后续使用同一个旧 bundle 进行了一轮补跑：

```text
jobs/tb2-codemate-gpt55-non-capability-rerun-old/result.json
```

补跑仍未启用 review 子 Agent，因此与首次运行的基线配置保持一致。

本次使用的是加入 review 子 Agent 之前打包的 Codemate。因此运行记录中：

```text
review 调用次数 = 0
```

这意味着本次结果可以作为“无独立修改后审查”的基线，但不能用来评价后来新增的 review 功能是否有效。

首次运行的 85 个任务合计产生：

| 运行指标 | 数量 |
| --- | ---: |
| 模型请求 | 1646 次 |
| 工具调用 | 1936 次 |
| Commentary 进度消息 | 1215 条 |
| Review 调用 | 0 次 |

在 70 个能够完成 Agent 执行并得到有效 verifier 结论的任务中，成功任务和能力失败任务的平均模型调用次数都约为 21 次。这个结果很重要：

> 失败并不是普遍由“调查步数太少”造成的。Agent 已经进行了相近数量的模型请求，但部分任务缺少严格的需求约束提取、证据判断和独立验证。

## 3. 总体结果

### 3.1 Harbor 原始统计

| 指标 | 数量 |
| --- | ---: |
| 总任务 | 85 |
| Reward 1.0 | 53 |
| Reward 0.0 | 29 |
| 无 reward 的 verifier 超时 | 3 |
| Harbor 原始平均 reward | 62.35% |

原始通过率为：

```text
53 / 85 = 62.35%
```

Harbor 将 3 个 verifier 超时任务排除在实际计分 trial 外，因此其实际计分分母为 82：

```text
53 / 82 = 64.63%
```

### 3.2 归因后的结果

对每个失败任务的 Agent 输出、trace、verifier 日志和测试结果进行检查后，可以把 85 个任务划分为：

| 结果类型 | 数量 | 是否代表 Agent 任务能力 |
| --- | ---: | --- |
| 通过 | 53 | 是 |
| Agent 能力失败 | 17 | 是 |
| 模型流中断 | 7 | 否，任务没有正常完成 |
| Verifier 网络或依赖失败 | 5 | 否，没有运行到有效断言 |
| Verifier 超时 | 3 | 否，没有得到有效结论 |
| 合计 | 85 |  |

剔除模型中断和 verifier 基础设施故障后，有效可评测任务为 70 个：

```text
53 / 70 = 75.71%
```

因此，本次结果应同时保留两个数字：

- **原始端到端通过率：62.35%**。它反映在当前模型服务、网络和 Harbor 环境下，完整运行一次能够交付成功结果的比例。
- **有效任务能力通过率：75.71%**。它排除了没有形成有效 Agent 结果或 verifier 结论的任务，更适合分析 Codemate 本身的任务完成能力。

不能只保留 `75.71%` 而隐藏原始结果，因为连接稳定性和评测工程同样是可用 Agent 系统的一部分；也不能只使用 `62.35%` 判断 Agent 推理能力，因为其中混入了 15 个不可归因于任务方案的失败。

### 3.3 首次运行未通过任务的类型统计

本次共有 32 个任务没有得到通过结果。按照主要失败来源统计：

| 失败类型 | 数量 | 占全部未通过任务 |
| --- | ---: | ---: |
| Agent 能力失败 | 17 | 53.13% |
| 模型流中断 | 7 | 21.88% |
| Verifier 网络或依赖失败 | 5 | 15.63% |
| Verifier 超时 | 3 | 9.38% |
| 合计 | 32 | 100.00% |

其中只有 17 个任务得到了完整 Agent 结果和有效 verifier 结论，可以用于分析 Agent 的任务能力；当时其余 15 个任务需要补跑。

### 3.4 15 个非能力失败任务的补跑结果

补跑的 15 个任务中，8 个通过，6 个得到 `reward=0`，1 个 verifier 超时：

| 补跑结果 | 数量 |
| --- | ---: |
| 通过 | 8 |
| Reward 0.0 | 6 |
| Verifier 超时 | 1 |
| 合计 | 15 |

补跑后通过的任务为：

- `modernize-scientific-stack`
- `multi-source-data-merger`
- `overfull-hbox`
- `regex-chess`
- `reshard-c4-data`
- `rstan-to-pystan`
- `schemelike-metacircular-eval`
- `sparql-university`

其余 7 个任务的最终归因为：

| 任务 | 最终归因 | 说明 |
| --- | --- | --- |
| `mteb-retrieve` | Agent 能力失败 | 返回了 HumanEval 论文，而非目标 MTEB 论文；检索语义或 BGE 查询用法不正确 |
| `torch-pipeline-parallelism` | Agent 能力失败 | forward 大部分通过，但 backward 激活梯度不一致 |
| `build-cython-ext` | 模型流中断 | 未完成的进度文本被 runtime 当成 final，任务提前结束 |
| `video-processing` | Codemate 运行时集成问题 | Agent 虚拟环境被放到 `PATH` 最前，遮蔽了任务镜像中已安装 `cv2`/`numpy`/`toml` 的 Python |
| `portfolio-optimization` | Verifier 网络失败 | 下载 `setuptools` 等依赖时超时，未进入有效功能断言 |
| `sam-cell-seg` | Verifier 网络失败 | `uv` 和 MobileSAM 依赖下载出现 SSL EOF |
| `torch-tensor-parallelism` | Verifier 超时 | 下载大型 PyTorch/CUDA 依赖时达到 1800 秒超时，未进入功能断言 |

### 3.5 补跑后的合并结果

将首次运行与补跑结果合并后，85 个任务的最终归因为：

| 最终结果 | 数量 | 是否形成有效能力结论 |
| --- | ---: | --- |
| 通过 | 61 | 是 |
| Agent 能力失败 | 19 | 是 |
| 模型流中断 | 1 | 否 |
| Codemate 运行时集成问题 | 1 | 否 |
| Verifier 网络或依赖失败 | 2 | 否 |
| Verifier 超时 | 1 | 否 |
| 合计 | 85 |  |

因此需要区分三种统计口径：

- **首次运行原始通过率：** `53/85 = 62.35%`。这是严格的单次端到端结果。
- **补跑后累计完成率：** `61/85 = 71.76%`。该数字包含第二次机会，不是 `pass@1`。
- **有效任务能力通过率：** 排除 5 个仍未形成有效能力结论的任务后，`61/80 = 76.25%`。

这 5 个被排除的任务是 `build-cython-ext`、`video-processing`、`portfolio-optimization`、`sam-cell-seg` 和 `torch-tensor-parallelism`。其中前两项仍然暴露了 Codemate 自身的可靠性问题，因此只能在分析模型任务能力时排除，不能从 Agent 系统端到端质量中忽略。

## 4. 已体现出的能力

首次运行的 53 个通过任务，以及补跑后累计通过的 61 个任务表明，Codemate 已经能够在较广泛的真实终端任务中完成完整闭环，而不仅是生成代码片段。

### 4.1 构建与环境配置

Agent 成功完成了 CompCert、POV-Ray、PMARS 等构建任务，也完成了 QEMU Alpine SSH、PyPI Server、Nginx 请求日志和 OpenSSL 自签名证书等环境配置任务。这说明：

- Shell 工具链能够支撑安装、构建和配置流程；
- Agent 能够根据陌生环境选择命令并处理部分依赖问题；
- 最终产物和服务状态在多数任务中能够通过外部 verifier。

### 4.2 软件工程与系统修复

Agent 通过了 Git 泄露恢复、多分支 Git 操作、SQLite 截断恢复、OCaml GC 修复、内存堆崩溃调查和代码漏洞修复等任务。这些任务要求的不只是写文件，还包括理解已有状态、执行恢复操作并保留正确结果。

### 4.3 数据、算法与科学计算

通过任务还包括自适应拒绝采样、最大特征值、约束调度、MIPS 解释器、Stan MCMC、数据集 token 统计、金融文档处理和推理批处理调度器。这说明 Agent 对数值计算、数据处理和算法实现具备一定泛化能力。

### 4.4 调查与验证链路整体可用

运行记录中，Agent 通常能够形成：

```text
调查环境
  -> 识别约束
  -> 创建或修改产物
  -> 执行局部验证
  -> 汇报结果
```

现有的 commentary、工具调用、trace 和 Harbor 产物也足以支持失败后的逐任务复盘。这是后续继续改进提示词、review 和运行时错误处理的基础。

## 5. 首次运行的非能力失败及补跑结论

### 5.1 模型流中断：首次 7 个，补跑后剩余 1 个

以下任务没有正常完成：

- `build-cython-ext`
- `regex-chess`
- `reshard-c4-data`
- `rstan-to-pystan`
- `schemelike-metacircular-eval`
- `sparql-university`
- `video-processing`

这些任务的共同特征是：

- 输出在 commentary 或工作过程的中间突然结束；
- 最后一次模型请求没有完整 token usage；
- 部分输出出现重复片段，例如同一句开头连续出现两次；
- runtime 最终仍将不完整输出当作正常 final 结束。

问题不仅是上游连接不稳定，也包括当前 OpenAI-compatible 流式响应处理不够严格。运行时在没有收到 `response.completed` 时，会使用已经收到的增量文本构造 fallback 结果。远端连接如果在流中途正常 EOF，部分 commentary 就可能被错误地当作最终回答，任务不会进入重试。

首次运行时，这 7 个任务应该归为“模型服务中断 + runtime 完整性判断缺失”，而不是 Agent 已经执行了错误方案。

补跑后，`regex-chess`、`reshard-c4-data`、`rstan-to-pystan`、`schemelike-metacircular-eval` 和 `sparql-university` 已通过；`build-cython-ext` 仍因同类中断失败。`video-processing` 则进一步定位为 Codemate 虚拟环境污染任务 `PATH` 的集成问题，不再归入模型流中断。

#### 改进方式

流式请求必须满足以下完成条件：

```text
收到 response.completed
  -> 接受本轮结果

连接结束但没有 response.completed
  -> 抛出可重试的 stream interrupted 错误
  -> 不将增量文本合成为 final
```

同时需要保证重试不会重复写入 commentary、tool call 或 history，避免一次中断污染后续上下文。

### 5.2 Verifier 网络或依赖失败：首次 5 个，补跑后剩余 2 个

以下任务虽然得到 `reward=0`，但 verifier 日志显示失败发生在依赖获取阶段，没有执行到真正的功能断言：

| 任务 | 基础设施问题 |
| --- | --- |
| `multi-source-data-merger` | 下载或解压 `pyarrow` 时网络超时 |
| `portfolio-optimization` | 下载 `setuptools`、`numpy` 等依赖时超时 |
| `sam-cell-seg` | 拉取 MobileSAM Git 仓库时出现 SSL EOF |
| `torch-pipeline-parallelism` | 大型 CUDA/PyTorch 依赖下载超时 |
| `torch-tensor-parallelism` | 大型 CUDA/PyTorch 依赖下载超时 |

补跑后，`multi-source-data-merger` 已通过；`torch-pipeline-parallelism` 进入了有效断言并确认为能力失败；`torch-tensor-parallelism` 则在依赖下载阶段超时。最终仍属于 verifier 网络或依赖失败的任务是 `portfolio-optimization` 和 `sam-cell-seg`。

### 5.3 Verifier 超时：首次 3 个，补跑后剩余 1 个

| 任务 | 超时阶段 |
| --- | --- |
| `modernize-scientific-stack` | 安装 pandas、matplotlib、scipy 等科学计算依赖 |
| `mteb-retrieve` | 安装 MTEB、Torch 和 CUDA 相关大型依赖 |
| `overfull-hbox` | apt 更新或软件包安装 |

补跑后，`modernize-scientific-stack` 和 `overfull-hbox` 已通过；`mteb-retrieve` 进入了有效断言并确认为能力失败。另一个首次归入网络失败的 `torch-tensor-parallelism` 在补跑时成为唯一剩余的 verifier 超时任务。

共享 uv 缓存只能复用已经成功下载的内容，不能解决首次下载速度、GitHub 连接和 apt 镜像速度问题。

## 6. Agent 能力失败归因

首次运行确认了 17 个有效能力失败，补跑又确认了 `mteb-retrieve` 和 `torch-pipeline-parallelism` 两项，合计 19 项。下面先保留首次 17 项的详细归因，再单独记录补跑新增的两项。

### 6.1 能力失败类型统计

按照每个任务最主要的根因统计：

| 能力失败类型 | 数量 | 占 17 个能力失败任务 | 涉及任务 |
| --- | ---: | ---: | --- |
| 使用近似、猜测或替代方案，缺少直接证据 | 8 | 47.06% | `db-wal-recovery`、`extract-elf`、`extract-moves-from-video`、`filter-js-from-html`、`gcode-to-text`、`gpt2-codegolf`、`protein-assembly`、`raman-fitting` |
| 精确约束识别或独立验证不完整 | 7 | 41.18% | `cancel-async-tasks`、`dna-assembly`、`dna-insert`、`model-extraction-relu-logits`、`polyglot-c-py`、`polyglot-rust-c`、`tune-mjcf` |
| 最终状态和进程生命周期检查不足 | 1 | 5.88% | `install-windows-3-11` |
| 时间和资源管理不足 | 1 | 5.88% | `caffe-cifar-10` |
| 合计 | 17 | 100.00% |  |

这里按照主要根因进行互斥分类，便于统计。实际任务可能同时涉及多个问题，例如 `tune-mjcf` 既存在性能验证不足，也存在环境限制；表中只记录对最终失败影响最大的原因。

从 Terminal-Bench 元数据定义的技术领域看，17 个能力失败任务分布为：

| 技术领域 | 失败数量 | 占 17 个能力失败任务 |
| --- | ---: | ---: |
| 科学计算 | 5 | 29.41% |
| 软件工程 | 4 | 23.53% |
| 文件操作与数据恢复 | 4 | 23.53% |
| 机器学习 | 1 | 5.88% |
| 安全 | 1 | 5.88% |
| 系统管理 | 1 | 5.88% |
| 数学 | 1 | 5.88% |
| 合计 | 17 | 100.00% |

科学计算、软件工程和文件操作合计 13 个，占能力失败的 `76.47%`。不过不能直接据此判断 Agent 在这些领域的准确率更低，因为还需要同时考虑每个领域在 85 个总任务中的任务数量。这里的统计只表示失败任务集中在哪些领域，不是分领域通过率。

### 6.2 约束识别不完整，验证没有覆盖真实边界

这类任务中，Agent 通常完成了主要功能，但遗漏了一个严格约束、相邻场景或最终状态要求。

#### 案例一：`cancel-async-tasks`

Verifier 通过 5/6。真正失败的场景是：

```text
任务数量大于最大并发数
  + 进程收到 SIGINT
  + 仍在队列中、尚未启动的任务也必须被正确清理
```

Agent 自己构造的测试使用了直接取消 coroutine 的方式，没有复现真实进程信号和排队任务的组合。因此测试通过只能证明“直接取消已启动任务”可用，不能证明需求中的 SIGINT 清理行为正确。

这个案例说明验证脚本不能只覆盖实现者最容易构造的正向路径，而应该从任务说明中提取事件来源、并发边界和生命周期要求。

#### 案例二：DNA 引物任务

`dna-assembly` 和 `dna-insert` 都生成了看似合理的引物，并进行了局部计算，但最终未满足 verifier 使用的精确条件：

- `dna-assembly` 中 SNAP 引物对的真实 Tm 差为 7.0，超过要求的 5；
- `dna-insert` 中反向引物的真实 Tm 为 55.86，低于要求的 58。

Agent 使用了不同工具或近似算法，并把近似结果当作目标工具的等价结论。这反映出：

> 当任务明确指定计算工具、参数或阈值时，替代实现只能用于探索，不能直接作为最终合格证据。

#### 其他表现

- `polyglot-c-py` 和 `polyglot-rust-c` 的功能基本正确，但留下了额外的 `cmain` 或 `main` 文件，不满足最终目录只保留指定文件的要求；
- `tune-mjcf` 通过了 3/4 项，但运行时间比例为 68.53%，高于要求的 60% 上限；
- `model-extraction-relu-logits` 的单次自测通过，但随机重新运行后失败，说明验证缺少固定随机种子和多次重复。

### 6.3 用近似、猜测或替代方案代替任务要求

这是本次最值得警惕的一类失败。Agent 能够生成一个“看上去像答案”的结果，但缺少足够证据证明它与任务要求等价。

| 任务 | 主要问题 |
| --- | --- |
| `db-wal-recovery` | 恢复出了 11 条记录，但关键记录 `id=1` 仍为 100，而不是 WAL 中更新后的 150；只检查了数量和结构，没有检查语义 |
| `extract-elf` | 假设 PIE 基址为 `0x400000` 并跳过可写段，最终输出格式正确但与参考结果匹配率为 0% |
| `extract-moves-from-video` | 使用 speedrun.com 路线资料代替对目标视频的真实分析，相似度只有 29.3% |
| `filter-js-from-html` | 使用正则表达式处理对抗性 HTML，既漏掉 XSS，又错误修改 5/12 个安全 HTML |
| `gcode-to-text` | 通过视觉猜测得到普通英文句子，但真实内容是 `flag{gc0d3_iz_ch4LLenGiNg}` |
| `gpt2-codegolf` | 认为任务不可行后提交了一个主动退出 1 的诊断 stub |
| `protein-assembly` | 没有识别出目标抗体对应 FLAG 序列 `DYKDDDDK`，选择了错误蛋白 |
| `raman-fitting` | 使用启发式局部拟合，G 峰和 2D 峰结果偏离预期 |

这类失败的共同问题不是代码不会写，而是证据标准过低：

```text
输出格式正确
不等于
输出语义正确
```

当缺少直接证据时，Agent 有时会选择一个容易实现的替代答案，并在最终回复中把它描述成已完成。这会比明确报告阻塞更危险。

### 6.4 最终状态和生命周期检查不足

`install-windows-3-11` 通过了 3/4 项，但 verifier 检查时 QEMU monitor socket 不存在。Agent 在工作过程中认为持久进程已经启动，却没有在退出前重新确认：

- 目标进程是否仍然存活；
- socket 或端口是否仍然存在；
- 进程是否因为 shell 生命周期、超时或父进程退出而终止；
- verifier 将通过什么稳定接口访问服务。

这说明“命令启动成功”不能等价于“最终状态持续成立”。对服务、虚拟机和后台任务，需要在最终回答前增加一次独立的存活性检查。

### 6.5 时间和资源管理不足

`caffe-cifar-10` 在 2400 秒后发生 Agent timeout。Agent 已经进行了 57 次模型请求和 66 次工具调用，但结束时仍在下载 CIFAR 数据，缺少模型文件和 `training_output`，verifier 只通过 3/6。

这个案例说明取消主 Agent 的固定步数上限并不代表可以取消任务预算。对于下载、训练和编译类任务，Agent 需要管理的是墙钟时间和阶段预算：

```text
环境调查
  -> 依赖准备
  -> 核心实现
  -> 运行产物
  -> 验证和收尾
```

如果前两个阶段持续消耗大部分时间，应尽早切换镜像、缓存、较小数据、恢复下载或其他可行方案，并为最终执行和验证保留时间。

### 6.6 补跑新确认的两个能力失败

#### `mteb-retrieve`

Verifier 两项检查只通过一项。Agent 返回了：

```text
HumanEval: Benchmarking Python code generation via functional examples
```

期望结果是：

```text
MTEB: Massive Text Embedding Benchmark
```

Agent 加载了指定 BGE 模型和 revision，但直接对查询做了普通归一化编码，未稳定复现 BGE 检索查询的指令语义或 MTEB 评测用法。这属于“工具和模型看似正确，但缺少对实际检索协议的确认”。

#### `torch-pipeline-parallelism`

Verifier 显示 forward 路径大部分通过，但 backward 在 `lm_head.bwd` 上出现激活梯度不一致，最大差异为：

```text
0.03571479767560959
```

world size 1 和 2 都失败。Agent 运行环境中缺少 Torch，最后主要依赖 `py_compile` 验证，没有覆盖端到端的反向传播数值一致性。这个案例说明，并行计算代码的语法正确和 forward 通过，都不能代替 backward 数值验证。

## 7. 19 个能力失败任务明细

| 任务 | Verifier 结果 | 主要失败原因 |
| --- | --- | --- |
| `caffe-cifar-10` | 3/6，Agent timeout | 下载和训练阶段耗时失控，最终产物不完整 |
| `cancel-async-tasks` | 5/6 | 未覆盖 SIGINT、排队任务和并发上限组合 |
| `db-wal-recovery` | 5/7 | 恢复结构正确，但关键 WAL 更新值错误 |
| `dna-assembly` | 0/1 | 引物 Tm 差超过约束 |
| `dna-insert` | 0/1 | 反向引物 Tm 低于约束，替代计算工具不等价 |
| `extract-elf` | 1/2 | 使用未经证实的地址与段假设，参考匹配率 0% |
| `extract-moves-from-video` | 1/2 | 使用外部路线资料替代目标视频分析 |
| `filter-js-from-html` | 0/2 | 用正则处理 HTML，安全性和保真性均失败 |
| `gcode-to-text` | 1/2 | 视觉猜测错误，没有可靠解析或验证 |
| `gpt2-codegolf` | 0/1 | 提交故意失败的诊断 stub，没有交付解法 |
| `install-windows-3-11` | 3/4 | QEMU monitor socket 在验证时不存在 |
| `model-extraction-relu-logits` | 0/1 | 随机方法只验证一次，结果不可复现 |
| `polyglot-c-py` | 0/1 | 功能正确但留下额外文件 |
| `polyglot-rust-c` | 0/1 | 功能正确但留下额外文件 |
| `protein-assembly` | 0/1 | 目标抗体和蛋白识别错误 |
| `raman-fitting` | 1/3 | 启发式拟合落入错误峰值或局部最优 |
| `tune-mjcf` | 3/4 | 正确性基本满足，但实际性能不达标 |
| `mteb-retrieve` | 1/2 | 检索到 HumanEval 而非 MTEB，BGE 查询语义或调用方式不正确 |
| `torch-pipeline-parallelism` | forward 大部分通过，backward 失败 | 未验证反向传播的梯度一致性 |

## 8. Codemate 暴露出的核心问题

### 8.1 模型调用多，不代表形成了完整行为契约

成功任务和能力失败任务的平均模型调用次数几乎相同。单纯增加最大步数、鼓励多读文件或让 Agent 调用更多工具，不会自动解决问题。

真正缺失的是一个稳定闭环：

```text
任务约束
  -> 对应证据
  -> 实现选择
  -> 对应验证
  -> 最终状态复查
```

失败任务中常见的情况是：调查获得了大量信息，但没有把每个硬性要求映射到验证证据。

### 8.2 自己编写的验证容易带有确认偏误

Agent 经常按照自己的实现方式编写测试，因此测试会自然地证明自己的假设。例如：

- 用 coroutine 直接取消代替真实 SIGINT；
- 用 primer3-py 的结果代替任务指定的 `oligotm`；
- 只运行一次随机算法；
- 检查文件存在，却不检查是否存在额外文件；
- 检查进程启动日志，却不在最终时刻检查进程存活。

验证需要从原始需求派生，而不是从当前实现反推。

### 8.3 对缺失证据的处理不够保守

在视频、G-code、ELF、蛋白和 GPT-2 等任务中，Agent 在证据不足时仍然给出了确定答案。一个可靠的 coding agent 应该区分：

- 已通过直接验证的结论；
- 有合理依据但仍需验证的假设；
- 当前环境下无法证明的内容。

无法证明时应继续寻找其他证据，或者明确说明阻塞，而不是将近似结果包装为完成结果。

### 8.4 缺少独立的最终审查

本次基线没有调用 review 子 Agent。实现者和验证者是同一模型上下文，容易共享同一错误假设。额外文件、随机稳定性、后台进程状态、精确阈值和替代工具等问题，都适合由独立 review 阶段检查。

### 8.5 流式响应完整性属于运行时正确性

首次运行的 7 个模型中断任务说明：模型适配层不能只负责解析格式，还必须保证一次响应在协议上完整。否则上游偶发断流会被伪装成 Agent 主动结束，既不会重试，也很难从 Harbor 的 reward 中直接识别。

## 9. 改进方案

### 9.1 第一优先级：修复流式请求完成判定

这是最明确的运行时 bug，应该优先于提示词调整：

1. 只有收到 `response.completed` 才接受完整响应。
2. 流结束但没有完成事件时，抛出可重试异常。
3. 不允许使用残缺的 commentary 构造 final。
4. 重试时保证 history 和 UI 输出幂等，避免重复消息。
5. 在 trace 中记录断流位置、已接收 item 和重试次数。

补跑已经证明其中 5 个任务可以在模型响应完整时通过。修复后应优先重跑仍因同类问题失败的 `build-cython-ext`。

### 9.2 建立“要求到证据”的任务账本

对于复杂任务，Agent 在实施前应形成轻量的内部检查表：

```text
requirement:
  任务要求或必须保留的行为

evidence:
  哪个文件、命令输出、文档或实验支持当前判断

implementation:
  通过什么改动满足该要求

validation:
  用什么独立方式证明要求已满足
```

这不是要求输出冗长计划，而是防止某个明确约束在几十轮工具调用后被遗忘。最终回答前必须检查每项 requirement 是否有对应 validation。

### 9.3 加强最终验证门

最终退出前增加统一检查：

- 重新读取原始任务中的输出文件、名称、格式和数量要求；
- 列出最终工作区，检查临时文件、编译产物和额外脚本；
- 对后台服务检查 PID、端口、socket 和实际请求；
- 对数值阈值使用实际结果计算，而不是根据趋势推断；
- 对随机算法固定种子并进行多次独立运行；
- 对任务指定的工具和参数，使用同一工具验证；
- 对性能要求运行真实 benchmark，并保留测量结果。

### 9.4 使用领域匹配的方法

Agent 需要更明确地识别“通用文本处理无法可靠解决的结构化问题”：

- HTML/XSS 应使用解析器、allowlist 和浏览器行为验证，而不是只靠正则；
- ELF 和数据库恢复需要依据格式规范和真实元数据，不能硬编码常见地址；
- 数值拟合和优化任务需要检查残差、边界和多初值稳定性；
- 序列设计必须使用任务指定的热力学模型和参数；
- 图像、视频和音频任务不能用外部文字描述替代原始媒体证据。

### 9.5 引入独立 review 子 Agent

新的 review 阶段应重点检查：

- 是否遗漏任务中的硬约束；
- 每个结论是否有直接证据；
- 是否使用了未经证明的替代工具或近似方案；
- 验证是否独立于实现假设；
- 随机、性能和生命周期要求是否真正测试；
- 最终目录是否有额外产物；
- Agent 是否在无法证明时仍声称完成。

本次测试中 review 调用为 0，因此需要使用新 bundle 对同一失败子集进行 A/B 测试，才能判断 review 的实际收益。优先重跑 19 个已确认的能力失败任务，比直接重新跑全部 85 个任务更能定位改进效果。

### 9.6 增加墙钟时间和阶段预算

不恢复主 Agent 的固定步数上限，但为高成本操作增加：

- 下载和安装的单阶段时间预算；
- 失败后的最大重试次数；
- 大型依赖的缓存、镜像或断点续传策略；
- 为最终运行和验证保留的时间；
- 超时前的降级或阻塞报告。

这样可以避免 `caffe-cifar-10` 一类任务在准备阶段耗尽全部时间。

### 9.7 改善 benchmark 基础设施

为了让后续结果更稳定，应：

- 预热大型 Python、CUDA、apt 和 Git 依赖；
- 为 verifier 配置稳定代理、镜像和更合理的下载超时；
- 区分 `agent_error`、`model_interrupted`、`verifier_error` 和 `capability_fail`；
- 自动汇总每类错误及其分母；
- 对不可评测任务自动生成补跑清单；
- 保存 Agent console、trace、verifier 输出和任务元数据。

## 10. 后续验证计划

推荐按以下顺序继续：

1. 修复流式响应完整性判断，重跑仍中断的 `build-cython-ext`。
2. 修复 Harbor 适配器对任务 `PATH` 的污染，重跑 `video-processing`。
3. 在网络和缓存稳定后，补跑 `portfolio-optimization`、`sam-cell-seg` 和 `torch-tensor-parallelism`。
4. 使用带 review 功能的新 bundle 重跑 19 个能力失败任务。
5. 对比无 review 与有 review 的通过数、平均模型调用次数、墙钟时间和主要失败类型。
6. 如果失败类型明显减少，再运行完整 85 任务确认没有损害原本通过的任务。

重点不应只是追求更高的单次分数，而是验证每个改动解决了哪一类失败：

| 改动 | 主要观察指标 |
| --- | --- |
| 流式完整性修复 | `build-cython-ext` 能否在断流后正常重试并完成 |
| Harbor `PATH` 隔离 | `video-processing` 能否直接使用任务镜像已安装的 Python 依赖 |
| 要求到证据账本 | 精确约束、文件清理和相邻场景失败是否减少 |
| Review 子 Agent | 19 个能力失败任务中能额外发现并修正多少问题 |
| 强化最终验证 | 随机性、性能、后台服务和目录状态失败是否减少 |
| 缓存与网络优化 | 剩余 3 个 verifier 基础设施失败是否得到有效结论 |

## 11. 结论

本次 Terminal-Bench 2 全量实验的首次运行原始通过率为 `53/85 = 62.35%`。对首次未形成有效能力结论的 15 个任务补跑后，累计有 61 个任务通过，即 `61/85 = 71.76%`；但该数字包含第二次机会，不能作为 `pass@1`。

补跑后仍有 5 个任务没有形成有效的任务能力结论：1 个模型流中断、1 个 Codemate 运行时集成问题、2 个 verifier 网络失败和 1 个 verifier 超时。排除这 5 个任务后，有效任务能力通过率为 `61/80 = 76.25%`。这个数字只用于分析可评测任务上的完成能力，不能取代原始端到端通过率。

结果说明 Codemate 已经能够完成相当一部分跨领域终端任务，工具调用、环境调查、代码实现和基本验证链路具备实际可用性。但失败分析也表明，当前系统的主要瓶颈已经不再是“能否调用工具或写出代码”，而是：

> 能否把任务中的每个硬约束转化为可靠证据，并在退出前独立确认最终状态确实满足要求。

下一阶段最有价值的工作不是继续增加工具数量或简单放宽步数，而是修复流式完成判定、建立要求到证据的验证闭环、强化最终状态检查，并通过 review 子 Agent 对错误假设进行独立审查。
