# SWE-bench Verified GPT-5.5 与 GPT-5.4 100 任务测试报告

## 1. SWE-bench Verified 是什么

SWE-bench 使用真实 GitHub issue、对应仓库和指定 base commit 评测 coding
agent。Agent 得到问题描述和处于历史状态的代码仓库，需要调查代码、生成补丁并进行
验证。之后，官方 harness 会在隔离的 Docker 环境中重新应用补丁和评测测试，判断
修改是否真正解决问题且没有破坏需要保留的行为。

一次任务的基本流程是：

```text
真实 GitHub issue
  -> 在官方 instance image 中准备 base commit
  -> Codemate 调查并修改 /testbed
  -> 导出 git patch
  -> 在干净评分环境中应用 agent patch
  -> 应用官方 test patch
  -> 运行 FAIL_TO_PASS 和 PASS_TO_PASS
  -> 生成 report.json 和汇总结果
```

SWE-bench Verified 是从原始 SWE-bench 中人工筛选和校验的 500 个任务。相比未经人工
复核的任务，它尽量排除了描述不清、无法复现或测试不可靠的问题，更适合衡量 Agent
在真实代码仓库中的问题理解、修改和回归保护能力。

### 1.1 官方测试如何判断通过

官方评测把测试分为两组：

- `FAIL_TO_PASS`：在 base commit 上失败，补丁后应该通过的目标测试；
- `PASS_TO_PASS`：在 base commit 上已经通过，补丁后仍应通过的回归测试。

一个任务只有在目标测试和回归测试都满足要求时才算 `resolved`。因此：

```text
只修复 issue 示例但仍有目标测试失败
  -> unresolved

目标测试全部通过但破坏旧行为
  -> unresolved

目标测试和回归测试全部通过
  -> resolved
```

本报告使用官方 gold patch 的修改文件范围辅助判断 Agent 是否找到了正确层次，但不把
gold patch 当作唯一合法实现。文件范围对比只能提供定位证据，最终正确性仍以官方测试
结果为准。

### 1.2 本次抽样

两次实验使用完全相同的分层抽样文件：

```text
/home/xidiannss/Experiments/yfy/agents/benchs/swebench/
  data/samples/verified_stratified_100_seed_20260730.jsonl
```

100 个任务覆盖 12 个仓库：

| 仓库 | 任务数 |
| --- | ---: |
| Django | 42 |
| SymPy | 14 |
| Sphinx | 9 |
| Matplotlib | 7 |
| scikit-learn | 7 |
| Astropy | 5 |
| Xarray | 5 |
| pytest | 4 |
| Pylint | 3 |
| Requests | 2 |
| Flask | 1 |
| Seaborn | 1 |

这是同一样本、同一 Agent bundle、不同模型的直接对比，不存在抽样任务不同造成的
分数偏差。

## 2. 实验设置

| 配置项 | GPT-5.5 实验 | GPT-5.4 实验 |
| --- | --- | --- |
| Run ID | `verified100_gpt55_seed20260730` | `verified100_gpt54_seed20260730` |
| 模型 | `openai:gpt-5.5` | `openai:gpt-5.4` |
| 任务数 | 100 | 100 |
| 数据集 | SWE-bench Verified 分层抽样 | 同左 |
| Agent 权限 | `full` | `full` |
| 运行环境 | 官方 instance image + Docker | 同左 |
| Bundle SHA256 | `868486f5...b806a` | 相同 |
| Review 子 Agent | 未包含 | 未包含 |

原始运行数据保存在：

```text
/home/xidiannss/Experiments/yfy/agents/benchs/swebench/runs/
  verified100_gpt55_seed20260730/
  verified100_gpt54_seed20260730/
```

每个任务均保留：

```text
instances/<instance-id>/
  prompt.txt
  patch.diff
  status.json
  agent/
    console.log
    trace.jsonl
    session.json
  evaluation/
    report.json
    run_instance.log
    test_output.txt
```

两个 bundle 完全相同，且 trace 中的 `review` 工具调用均为 0。因此本次结果是无独立
修改后审查的基线，不能用于评价后来加入的 review 子 Agent。

## 3. 总体结果

### 3.1 原始通过率

| 指标 | GPT-5.5 | GPT-5.4 |
| --- | ---: | ---: |
| 总任务 | 100 | 100 |
| 生成非空补丁 | 100 | 98 |
| 官方通过 | 77 | 70 |
| 官方未通过的非空补丁 | 23 | 28 |
| 空补丁 | 0 | 2 |
| Harness 错误 | 0 | 0 |
| 原始通过率 | **77.00%** | **70.00%** |

GPT-5.5 比 GPT-5.4 多通过 7 个任务，提升 7 个百分点。

GPT-5.4 的官方 harness 只收到 98 个非空补丁，因此还有一个需要保留的统计口径：

```text
GPT-5.4 原始端到端通过率：70 / 100 = 70.00%
GPT-5.4 非空补丁通过率：70 / 98 = 71.43%
```

`71.43%` 只描述已经形成补丁的任务，不能替代 `70.00%`。两个空补丁同样是 Agent
系统未能交付结果，应计入端到端分母。

### 3.2 模型调用和工具使用

| 运行指标 | GPT-5.5 | GPT-5.4 |
| --- | ---: | ---: |
| 模型请求 | 2062 | 2105 |
| 平均模型请求/任务 | 20.62 | 21.05 |
| 工具调用 | 2677 | 3343 |
| Commentary | 1444 | 1301 |
| Review 调用 | 0 | 0 |
| 平均 Agent 执行时间 | 300.98 秒 | 307.11 秒 |

成功与失败任务的模型调用次数为：

| 结果 | GPT-5.5 平均/中位数 | GPT-5.4 平均/中位数 |
| --- | ---: | ---: |
| 通过 | 19.81 / 19 | 20.11 / 18 |
| 未通过 | 23.35 / 23 | 23.23 / 19.5 |

两组中，失败任务使用的模型调用都不比成功任务少。GPT-5.4 还执行了比 GPT-5.5 多
约 `24.9%` 的工具调用，但通过率更低。这说明当前失败的主要原因不是步数不足或读取
文件太少，而是：

- 调查后仍然选错修改层；
- 没有把分散证据整理成完整行为契约；
- 修改范围变宽，但没有带来更准确的实现；
- 验证没有覆盖真正需要保留的相邻行为。

### 3.3 两个模型的结果一致性

| 结果关系 | 任务数 |
| --- | ---: |
| 两个模型都通过 | 67 |
| 两个模型都失败 | 20 |
| 仅 GPT-5.5 通过 | 10 |
| 仅 GPT-5.4 通过 | 3 |
| 合计 | 100 |

两个模型在 87 个任务上得到相同结论，在 13 个任务上不同。GPT-5.5 的净优势来自：

```text
10 个 GPT-5.5 独有通过
- 3 个 GPT-5.4 独有通过
= 7 个净增通过
```

这也说明模型升级并不是严格单调改进。GPT-5.5 整体更强，但仍有 3 个 GPT-5.4
能够解决、GPT-5.5 未解决的任务。

## 4. 分仓库结果

| 仓库 | 任务数 | GPT-5.5 | GPT-5.4 |
| --- | ---: | ---: | ---: |
| Astropy | 5 | 4/5，80.0% | 3/5，60.0% |
| Django | 42 | 39/42，92.9% | 35/42，83.3% |
| Matplotlib | 7 | 5/7，71.4% | 3/7，42.9% |
| Seaborn | 1 | 0/1，0.0% | 0/1，0.0% |
| Flask | 1 | 1/1，100.0% | 1/1，100.0% |
| Requests | 2 | 0/2，0.0% | 1/2，50.0% |
| Xarray | 5 | 4/5，80.0% | 4/5，80.0% |
| Pylint | 3 | 1/3，33.3% | 1/3，33.3% |
| pytest | 4 | 3/4，75.0% | 3/4，75.0% |
| scikit-learn | 7 | 5/7，71.4% | 5/7，71.4% |
| Sphinx | 9 | 7/9，77.8% | 7/9，77.8% |
| SymPy | 14 | 8/14，57.1% | 7/14，50.0% |

GPT-5.5 的主要增益来自 Django、Matplotlib、Astropy 和 SymPy。Requests 的结果
相反，但样本只有 2 个，不能据此判断 GPT-5.4 在 Requests 上整体更强。

从失败集中度看，两组共同较弱的部分是：

- SymPy 的底层表达式、矩阵和多项式语义；
- Pylint、Sphinx 等跨分析阶段或跨渲染阶段的修改；
- Matplotlib 的共享状态、初始化和清理行为；
- 需要精确保留容器、索引或 MRO 语义的共享工具函数。

## 5. 官方失败类型

### 5.1 GPT-5.5 的 23 个失败

| 主要结果 | 数量 | 含义 |
| --- | ---: | --- |
| 仅目标测试失败 | 20 | 补丁没有完整实现目标行为 |
| 目标测试和回归测试都失败 | 2 | 目标未解决，同时引入回归 |
| 仅回归测试失败 | 1 | 目标已解决，但破坏旧行为 |
| 空补丁 | 0 | 所有任务都形成了代码修改 |

其中：

- `psf__requests-1766` 和 `psf__requests-2317` 同时存在目标失败和回归；
- `matplotlib__matplotlib-20826` 已通过目标测试，但有 2 个回归测试失败。

### 5.2 GPT-5.4 的 30 个端到端失败

| 主要结果 | 数量 | 含义 |
| --- | ---: | --- |
| 仅目标测试失败 | 24 | 补丁没有完整实现目标行为 |
| 仅回归测试失败 | 4 | 目标已解决，但破坏旧行为 |
| 目标和回归同时失败 | 0 | 无 |
| 空补丁 | 2 | Agent 正常退出，但没有交付修改 |

4 个回归型失败为：

- `django__django-14170`
- `matplotlib__matplotlib-20826`
- `matplotlib__matplotlib-25960`
- `psf__requests-2317`

2 个空补丁为：

- `django__django-14999`
- `sphinx-doc__sphinx-9602`

### 5.3 GPT-5.4 空补丁不是代码能力失败

这两个任务都完成了有效调查，并已经定位到合理的修改方向，但在第一次编辑前运行：

```text
git status --short
```

结果发现：

```text
?? .codemate/
```

Codemate 自己在 `/testbed` 创建了 `.codemate/`，模型却把它解释为用户产生的意外
工作区改动，并按安全规则停止：

```text
I hit an unexpected workspace change: .codemate/
I'm stopping here before making edits.
```

benchmark 是非交互运行，没有用户可以选择“忽略并继续”，因此任务以正常退出、空
补丁结束。

这属于 Agent 运行流程缺陷：

```text
runtime 创建项目状态目录
  -> git status 将其显示为未跟踪
  -> 模型把自身产物误判为外部修改
  -> 非交互任务无法继续
```

修复方向不是修改代码推理提示词，而是让 benchmark 模式把 `.codemate/` 放在仓库外、
加入排除规则，或在工作树检查中明确标记为 Codemate 自身运行产物。

## 6. 补丁范围分析

将失败补丁的最终修改文件与官方 gold patch 的修改文件进行对比，得到：

| 文件范围关系 | GPT-5.5 | GPT-5.4 |
| --- | ---: | ---: |
| 修改文件与 gold 完全一致 | 12 | 12 |
| 与 gold 部分重合 | 7 | 12 |
| 与 gold 完全不重合 | 4 | 4 |
| 无补丁 | 0 | 2 |

这个结果说明失败并不只是“没找到文件”：

- GPT-5.5 有 19/23 个失败至少修改了 gold 涉及的文件；
- GPT-5.4 有 24/30 个失败至少修改了 gold 涉及的文件；
- 很多失败发生在正确文件内部，但条件、API 或行为边界仍然不正确。

两组共同出现的 4 个完全不重合案例尤其有代表性：

| 任务 | Agent 修改层 | Gold 修改层 |
| --- | --- | --- |
| `matplotlib__matplotlib-20826` | `axes/_base.py`、`axes/_subplots.py` | `axis.py` |
| `sympy__sympy-17630` | `blockmatrix.py` | `matexpr.py` |
| `sympy__sympy-20428` | `densearith.py` 或 `densetools.py` | `expressiondomain.py` |
| `sympy__sympy-21379` | `hyperbolic.py` | `core/mod.py` |

这些不是简单的文件搜索遗漏，而是典型的“在异常暴露处修补，而没有继续追到语义失效
的底层抽象”。

## 7. 共同失败任务反映的问题

20 个任务在两个模型上都失败，说明它们更可能反映 Agent 工作流中的稳定弱点，而不只是
一次模型采样波动。

### 7.1 在症状层打补丁，没有找到根因层

#### 案例一：`sympy__sympy-20428`

问题表现为 `clear_denoms()` 返回一个打印为零、内部却不是语义零的 `Poly`。两个模型
分别在 `densearith.py` 和 `densetools.py` 中增加 strip 或清理逻辑，试图修补某个产生
坏多项式的操作。

官方修复位于表达式域对象的布尔/零值语义层。真正问题不是某一次乘法或
`clear_denoms()` 忘了整理列表，而是底层 `ExpressionDomain.Expression` 对语义零的
判断错误。只在一个输出操作上 strip，无法覆盖其他生成同类对象的路径，因此目标测试
继续失败。

#### 案例二：`sympy__sympy-21379`

issue 在包含 `Piecewise` 的双曲函数表达式执行 `subs()` 时暴露
`PolynomialError`。两个模型都在 `hyperbolic.py` 中捕获或规避异常，因为错误表面上
通过 `sinh/cosh/tanh` 触发。

官方修复位于通用 `Mod.eval` 路径。双曲函数只是构造出触发条件的上层表达式，实际异常
来自更底层的模运算化简。两个补丁只覆盖了可见示例，没有修复通用异常源。

#### 案例三：`sympy__sympy-17630`

两个模型都在 `BlockMatrix._blockmul()` 附近把标量零转换为 `ZeroMatrix`。官方修复
位于通用矩阵表达式的 `MatAdd` 后处理，使矩阵加法中的零在整个表达式系统中保持正确
矩阵类型。

Agent 看到了 `BlockMatrix` 的报错位置，却没有继续追踪“为什么矩阵加法产生标量零”。
因此补丁针对一个调用方，而不是产生错误值的共享抽象。

### 7.2 找到相关文件，但没有覆盖跨模块行为契约

#### `pydata__xarray-6992`

两个模型都只修改了 `xarray/core/dataset.py`。官方修复同时涉及 Dataset 和 indexes
层。该问题来自 index 重构后 `_coord_names` 与索引对象更新不同步，只修 Dataset 的
一个赋值点无法覆盖索引创建、替换和传播路径，最终 12 个目标测试全部失败。

#### `pylint-dev__pylint-4551`

任务要求 Pyreverse 正确展示类型注解。实际输出跨越 AST inspector、diagram model、
writer 和辅助格式化逻辑。两个模型主要集中在 inspector，虽然能够收集部分类型信息，
但没有把信息稳定传递到最终图输出，10 个目标测试仍然失败。

#### `sphinx-doc__sphinx-9461`

classmethod property 的 autodoc 行为同时涉及对象检查、autodoc documenter 和 Python
domain option。两个模型只覆盖部分路径，导致修复在某些入口有效，在完整文档生成链路
中仍然失败。

这类任务说明：

> 找到一个包含目标关键词的文件，不等于已经找到了完整的数据流。

### 7.3 使用了看似合理但不等价的判断条件

#### `scikit-learn__scikit-learn-25747`

FeatureUnion 在 pandas 输出模式下必须保留 transformer 已经返回的 DataFrame 索引。
两个模型都使用“输出长度是否与输入索引长度相等”决定是否覆盖索引。

这个条件能修复 issue 中“聚合后行数变化”的示例，却不能覆盖“行数不变但 transformer
主动返回新索引”的情况。正确边界是输出是否已经是带自身语义的 DataFrame，而不是
长度是否相等。

#### `pytest-dev__pytest-10356`

任务涉及类继承层次中的 marks。两个模型都调整了 MRO 遍历或 mark 展开逻辑，但没有
准确区分：

- 从继承层次读取 mark；
- 在当前节点存储 mark；
- 当前类与基类 mark 的顺序；
- 是否重复继承。

结果是局部示例可行，但官方目标语义仍然失败。

#### `astropy__astropy-13033`

两个模型都定位到 required column 校验，却围绕“缺失一列”增加特殊分支，没有统一
格式化完整 required columns 和实际 columns。问题要求的是正确表达整体列约束，而
不是只替换一个错误场景的文字。

### 7.4 目标修复后破坏旧行为

#### `matplotlib__matplotlib-20826`

两个模型都让目标共享轴测试通过，但破坏了 2 个极坐标相关回归测试。它们在 Axes 或
Subplot 层重新应用 tick label 状态，而官方修复保留 Axis 的 tick kwargs，只重置真正
应该清理的 grid 状态。

这说明 Agent 已经理解“clear 后要恢复共享轴可见性”，但修复范围过宽，改变了其他轴
类型依赖的初始化行为。

#### `psf__requests-2317`

任务涉及 bytes HTTP method 的规范化。两个模型都同时修改了 `models.py` 和
`sessions.py`，把转换扩散到多个阶段。

- GPT-5.5：8 个目标测试和 33 个回归测试失败；
- GPT-5.4：目标测试通过，但 2 个回归测试失败。

官方修改集中在 session 请求边界。重复或过早转换让内部方法类型和旧调用约定发生变化，
说明公共输入规范化应该放在一个明确边界，而不是在多个对象层重复处理。

## 8. 两个模型的差异

### 8.1 GPT-5.5 独有通过的 10 个任务

```text
astropy__astropy-14182
django__django-12663
django__django-13023
django__django-14170
django__django-14559
django__django-14999
django__django-15916
matplotlib__matplotlib-25960
matplotlib__matplotlib-26208
sympy__sympy-15976
```

其中几个差异很有代表性：

- `django__django-13023`：GPT-5.4 只处理 `TypeError`，遗漏同一路径的
  `ValueError`；GPT-5.5 覆盖了实际输入边界。
- `django__django-14559`：`bulk_update()` 需要返回更新行数，并在空对象列表时返回
  `0`。GPT-5.4 累加了正常批次，却保留空列表的裸 `return`，因此只失败一个目标测试。
- `django__django-14170`：GPT-5.4 通过禁用现有优化修复目标行为，引入 9 个回归；
  GPT-5.5 保留了原有查询语义。
- `django__django-15916`：GPT-5.4 只调整 factory 属性传递，遗漏直接声明在
  `ModelForm.Meta` 中的 callback；GPT-5.5 同时处理了元类读取路径。
- `django__django-14999`：GPT-5.4 已定位根因但被 `.codemate/` 停止，GPT-5.5
  正常形成补丁。

GPT-5.5 的优势主要体现在条件边界和跨入口一致性，而不是更长的调查过程。

### 8.2 GPT-5.4 独有通过的 3 个任务

```text
django__django-14140
django__django-15695
psf__requests-1766
```

- `django__django-14140`：GPT-5.5 只修复部分 Q 对象 children 表示，仍有 2 个目标
  测试失败；GPT-5.4 覆盖了完整序列化形态。
- `django__django-15695`：GPT-5.5 在反向迁移中执行了索引重命名，但该 unnamed
  index 路径按契约应为 no-op，最终多执行 2 条 SQL；GPT-5.4 的 no-op 判断通过测试。
- `psf__requests-1766`：GPT-5.5 对 Digest Auth qop/nonce 路径的调整同时产生目标和
  回归失败，GPT-5.4 保持了更准确的旧分支语义。

这三个案例说明不能只依靠模型版本替代验证。更强模型也会在某次任务中选择更复杂但错误
的实现。

### 8.3 GPT-5.4 的修改边界更松

| 补丁指标 | GPT-5.5 | GPT-5.4 |
| --- | ---: | ---: |
| 平均 patch 字符数 | 1290.8 | 1663.2 |
| patch 字符数中位数 | 910.5 | 1189.0 |
| 平均修改文件数 | 1.10 | 1.35 |
| 最终包含测试文件修改的任务 | 0 | 22 |

任务提示允许临时修改测试，但明确要求最终恢复。GPT-5.4 有 22 个最终补丁保留测试文件
改动，其中 5 个任务最终失败。官方 harness 在评分前会恢复或应用自己的测试补丁，因此
这些测试改动不一定直接造成分数下降，但它们表明 Agent 没有可靠执行最终状态检查：

```text
临时增加验证
  -> 测试结束
  -> 未恢复测试文件
  -> 直接导出最终 patch
```

这既污染交付补丁，也扩大了评审范围。相同 bundle 下 GPT-5.5 没有出现这一问题，
说明提示词能够被执行，但 GPT-5.4 的指令遵循稳定性更弱。

## 9. 33 个未统一通过的任务

### 9.1 两个模型共同失败：20 个

| 任务 | 主要问题 |
| --- | --- |
| `astropy__astropy-13033` | 报错逻辑只覆盖局部缺列场景，没有统一表达完整列约束 |
| `django__django-12308` | JSONField 展示使用了错误的表单准备层，而不是字段 prep API |
| `matplotlib__matplotlib-20676` | 修改 handle 几何/transform，未修复初始化边界来源 |
| `matplotlib__matplotlib-20826` | 目标通过但引入极坐标回归，修改层过宽 |
| `mwaskom__seaborn-3187` | 没有在 formatter 层正确关闭 offset/scientific 行为 |
| `psf__requests-2317` | bytes method 规范化扩散到多个层次并产生回归 |
| `pydata__xarray-6992` | 只修 Dataset，遗漏 indexes 层的数据流 |
| `pylint-dev__pylint-4551` | 只收集部分类型信息，未覆盖最终 diagram/writer 输出 |
| `pylint-dev__pylint-4604` | AST type comment 的 Attribute/Name 处理不完整 |
| `pytest-dev__pytest-10356` | MRO mark 的读取、存储和顺序语义不准确 |
| `scikit-learn__scikit-learn-14983` | 自行拼接 Repr，未复用已有统一表示逻辑 |
| `scikit-learn__scikit-learn-25747` | 用长度判断代替 DataFrame 容器语义 |
| `sphinx-doc__sphinx-9461` | autodoc、inspect 和 domain 之间的契约不完整 |
| `sphinx-doc__sphinx-9602` | GPT-5.5 parser 修复仍不完整；GPT-5.4 被运行目录阻塞 |
| `sympy__sympy-13974` | TensorProduct 的 Pow 分派和化简规则不完整 |
| `sympy__sympy-17630` | 在 BlockMatrix 调用方修补，未修矩阵表达式零值根因 |
| `sympy__sympy-18211` | 针对 Equality/solveset 分支处理，未覆盖通用失败回退 |
| `sympy__sympy-20428` | 在多项式操作处 strip，未修表达式域语义零判断 |
| `sympy__sympy-20438` | 只增加局部集合比较，未覆盖通用 relational/set fallback |
| `sympy__sympy-21379` | 在双曲函数处捕获，未修 Mod 化简的底层异常源 |

### 9.2 仅 GPT-5.4 失败：10 个

这 10 个任务对应 GPT-5.5 的净优势，主要包括：

- 条件分支遗漏：`django-13023`、`django-14559`；
- 跨入口不一致：`django-15916`；
- 修复目标但引入回归：`django-14170`、`matplotlib-25960`；
- 修改点或行为判断错误：`astropy-14182`、`django-12663`、
  `matplotlib-26208`、`sympy-15976`；
- 运行流程空补丁：`django-14999`。

### 9.3 仅 GPT-5.5 失败：3 个

```text
django__django-14140
django__django-15695
psf__requests-1766
```

这三项说明 GPT-5.5 的总体提升不能消除单任务不稳定性。后续 A/B 测试应保留逐任务结果，
而不能只比较总分。

## 10. Codemate 暴露出的核心问题

### 10.1 “读了很多代码”没有转化为行为契约

GPT-5.4 的工具调用更多，失败任务的模型调用也更多，但最终仍经常停留在局部症状。说明
Agent 缺少的不是观察数量，而是把观察整理成以下结构的稳定过程：

```text
目标行为是什么
哪些旧行为必须保留
证据来自哪些实现、调用方和测试
错误值在哪一层首次产生
修改应该放在哪个共享边界
每项行为由什么验证覆盖
```

### 10.2 容易把异常暴露位置当作根因位置

四个共同 disjoint-file 失败都具有相同模式：

```text
上层模块抛出异常
  -> Agent 在上层增加转换、catch 或特殊分支
  -> 实际错误来自底层通用对象语义
  -> issue 示例可能变化，但官方行为仍失败
```

对于共享表达式、索引、格式化、AST 和状态清理逻辑，Agent 需要追踪错误值的产生位置，
而不是只修改堆栈最后出现的业务模块。

### 10.3 跨模块数据流调查不完整

Xarray、Pylint 和 Sphinx 的失败说明，读取同类文件不等于沿数据流调查。一个值可能经过：

```text
解析/收集
  -> 中间表示
  -> 状态传播
  -> 最终渲染或执行
```

只修其中一层时，局部检查可以通过，完整链路仍然失败。

### 10.4 验证仍偏向当前实现

失败补丁通常已经运行测试或最小行为脚本，但验证容易选择当前实现最容易通过的场景：

- 只验证 issue 中给出的一个输入；
- 用长度相等代替验证已有索引语义；
- 只看目标测试，不覆盖同一共享函数的相邻类型；
- 遇到部分相关测试失败后，没有重新审查修改层；
- 临时测试完成后没有统一检查并恢复。

验证必须从行为契约派生，而不是从补丁结构反推。

### 10.5 缺少独立的最终审查

本次两组运行都没有 review 子 Agent。实现、测试选择、失败解释和最终 diff 判断都由同一
上下文完成，容易共享同一个错误假设。

尤其适合独立 review 发现的问题包括：

- 是否只修了 issue 示例；
- 修改文件是否位于真正产生错误值的层；
- 目标函数是否还有其他调用方或输入类型；
- 是否改变默认值、MRO、索引、格式化或兼容分支；
- 测试文件和临时产物是否已恢复；
- 相关测试失败是否被错误解释为无关。

## 11. 改进方案

### 11.1 把修改前行为清单变成 Runtime 阶段

提示词已经要求深入调查，但仅靠自然语言不能保证稳定执行。对非机械代码修改，可在首次
编辑前要求形成一个轻量、机器可检查的工作状态：

```text
intent:
  本次需要改变的行为

evidence:
  相关实现、调用方、测试和同类代码中的关键证据

preserve:
  修改后必须保持不变的相邻行为

layer:
  错误值在哪里产生，准备在哪一层修改

validation:
  目标行为和保留行为分别如何验证
```

它不需要直接展示给用户，也不应变成冗长计划；作用是阻止 Agent 在没有建立行为边界时
直接编辑。

### 11.2 增加“根因层检查”

在选择修改文件后，至少进行一次反向检查：

```text
当前文件是在产生错误值，还是只在消费错误值？
同类消费者是否也可能遇到该值？
是否存在更底层的标准化、状态传播或语义判断入口？
```

如果补丁主要由 catch、类型转换、特殊 case 或结果清理组成，更应该检查是否只是
workaround。

### 11.3 让验证覆盖 change 和 preserve

每个非机械修复至少应有两类验证：

- `change`：issue 指向的新行为；
- `preserve`：共享路径上最重要的旧行为。

对于条件分支和公共 API，还应覆盖：

- 空值和非空值；
- 默认配置和显式配置；
- 目标类型和相邻类型；
- 正向和反向操作；
- 当前入口和另一个共享调用入口。

不要求盲目运行整个仓库测试，但应优先运行包含目标测试和相邻行为的完整测试类或文件。

### 11.4 将相关测试失败设为结束阻塞

如果目标测试或直接相关测试仍然失败，Agent 不应正常宣称完成。允许结束的情况只有：

1. 能证明失败在 base commit 上已经存在且与改动无关；
2. 测试环境本身无法运行，并明确记录未验证内容；
3. 用户明确接受不完整结果。

否则应回到行为契约和修改层重新调查。

### 11.5 强制执行最终工作区清理

在导出 patch 前由 runtime 而不是模型自觉完成：

- 检查是否修改测试文件；
- 检查新增测试、临时脚本和构建产物；
- 检查 `.codemate/` 等 Agent 自身目录；
- 展示最终 source diff；
- benchmark 模式下按规则自动恢复临时测试文件。

这能解决 GPT-5.4 的测试污染和 `.codemate/` 空补丁问题。

### 11.6 使用 review 子 Agent 做独立验证

新的 review 阶段应该拿到：

- 原始任务；
- 修改后的 source diff；
- 关键调查证据；
- 已运行测试及结果；
- 最终工作区状态。

Review 重点回答：

```text
补丁是否位于正确层？
是否漏掉相关入口、类型或反向路径？
是否只覆盖了 issue 示例？
是否存在回归风险或未处理失败测试？
最终补丁是否包含无关文件？
```

为了验证 review 的真实收益，应使用同一 100 任务、同一模型做 A/B 对比，而不是把带
review 的新结果直接与本次不同模型基线混合。

### 11.7 保留逐任务对比，而不只看总分

GPT-5.5 比 GPT-5.4 高 7 个百分点，但仍丢失 3 个 GPT-5.4 能通过的任务。后续报告应
持续输出：

- 两者共同通过；
- 两者共同失败；
- 新版本新增通过；
- 新版本新增失败；
- 失败类型是否从 target failure 转为 regression；
- 平均模型调用、工具调用和耗时变化。

这样才能判断改进是否真正提高稳定性，而不是在不同任务之间交换成功和失败。

## 12. 后续验证计划

推荐按以下顺序继续：

1. 修复 `.codemate/` 自身目录误报，补跑 GPT-5.4 的 2 个空补丁任务。
2. 使用带 review 的新 bundle 重跑 20 个共同失败任务，观察根因层和跨模块失败是否减少。
3. 重跑 13 个模型结果不一致的任务，评估单次采样波动。
4. 再对完整 100 任务进行无 review/有 review A/B 测试。
5. 对比通过率之外的指标：回归任务数、空补丁数、测试文件污染数、模型请求和执行耗时。

最有价值的观测指标是：

| 改动 | 主要指标 |
| --- | --- |
| `.codemate/` 路径修复 | 空补丁是否从 2 降到 0 |
| 修改前行为清单 | 正确文件但错误条件的失败是否减少 |
| 根因层检查 | 4 个 disjoint-file 共同失败能否解决 |
| change/preserve 验证 | regression-only 和 target+regression 是否减少 |
| Runtime 最终清理 | 最终测试文件修改是否降到 0 |
| Review 子 Agent | 20 个共同失败中新增解决多少，成本增加多少 |

## 13. 结论

在同一批 100 个 SWE-bench Verified 任务、同一个无 review Codemate bundle 上：

```text
GPT-5.5：77 / 100 = 77.00%
GPT-5.4：70 / 100 = 70.00%
```

GPT-5.5 整体更强，尤其在条件边界、跨入口一致性和最终补丁清洁度上表现更稳定。
GPT-5.4 使用了更多工具、生成了更大的补丁，却没有转化成更高正确率，并出现 22 个
包含测试修改的最终补丁和 2 个由 `.codemate/` 引起的空补丁。

20 个共同失败任务说明，当前 Codemate 的主要瓶颈已经不是“能否找到相关代码并进行
修改”，而是：

> 能否把调查结果整理成完整行为契约，追踪到真正产生错误值的抽象层，并用独立于当前
> 实现的验证同时证明目标行为和相邻旧行为。

下一阶段最值得做的不是继续增加工具数量或最大步数，而是把行为清单、根因层检查、相关
测试失败阻塞、最终工作区清理和独立 review 变成稳定的 Runtime 流程，并用同一任务集
进行严格 A/B 验证。

| 模型 | Terminal-Bench 2.0 | SWE-bench Verified |
|---|---:|---|
| GPT-5.4 | 75.1% ✅官方 | 无官方数据，参考 GPT-5 初代 74.9% |
| GPT-5.5 | 82.7% ✅官方 | 无官方数据，第三方约 82.6% |
| GPT-5.6 Sol | 91.9% ⚠️第三方 | 无可靠数据 |