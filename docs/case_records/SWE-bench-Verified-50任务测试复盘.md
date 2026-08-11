# SWE-bench Verified 50 任务测试复盘：从通过率到 Agent 失败归因

## 1. 测试概述

本次测试从 SWE-bench Verified 中分层抽取了 50 个真实开源软件问题，覆盖 Django、SymPy、Matplotlib、scikit-learn、Sphinx、Pylint 等不同规模和类型的 Python 项目。Codemate 使用 GPT-5.5，在官方 instance image 中修改代码，最终通过 SWE-bench 官方 harness 应用补丁并运行目标测试与回归测试。

测试结果如下：

| 指标 | 数量 |
| --- | ---: |
| 抽取任务总数 | 50 |
| 正常生成非空 patch | 42 |
| 官方评测通过 | 33 |
| 官方评测未通过 | 9 |
| Agent 基础设施错误 | 8 |

在 42 个完成有效 Agent 运行并生成 patch 的任务中：

```text
33 / 42 = 78.6%
```

如果暂时把 8 个基础设施错误也计入总任务分母，则当前结果下限为：

```text
33 / 50 = 66.0%
```

但这两组数据表达的含义不同：

- `33/42` 表示成功完成 Agent 推理和代码修改后的任务通过率。
- `33/50` 是在基础设施错误尚未补跑时的保守整体结果。
- 8 个 `agent_error` 均由模型 API 连接中断或 SSL 读取超时造成，不能直接归因于 Agent 的代码理解能力。

因此，本次能力归因主要分析 9 个已经生成 patch、但未通过官方测试的任务。

## 2. 总体发现

Codemate 已经能够完成大部分真实仓库问题，说明工具调用、仓库调查、代码编辑和基础验证链路整体有效。但失败任务暴露出一个比较稳定的模式：

> Agent 往往能够修复 issue 中最直接的正向示例，却不能稳定建立完整的行为边界，也不能保证验证过程独立于自己的实现假设。

主要问题可以概括为：

1. **行为契约不完整**：知道需要改变什么，但没有完整识别哪些旧行为、输入类型和用户配置必须保持不变。
2. **调查过早收敛**：找到一个看似合理的修改点后就开始实现，没有继续检查同一语义的其他入口和调用路径。
3. **验证存在确认偏误**：临时测试通常由实现者自己编写，容易只证明当前方案可以工作，而不是独立判断需求是否真正满足。
4. **相关失败测试没有成为硬阻塞**：个别任务中，Agent 遇到直接相关的失败测试后，选择解释或调整测试场景，而不是重新审查实现。
5. **缺少独立的修改后审查**：最终 diff、行为边界和测试结果仍由同一轮 Agent 自我确认，难以发现自己已经接受的错误假设。

下面通过两个代表性案例说明这些问题。

## 3. 案例一：scikit-learn 输出索引修复引入类型回归

### 3.1 任务目标

任务为：

```text
instance_id: scikit-learn__scikit-learn-25747
issue: FeatureUnion 在启用 pandas transform output 并聚合数据时失败
```

输入是带日期索引的 DataFrame。自定义 transformer 使用 `groupby()` 聚合数据后，输出行数和原始输入不同，并带有新的聚合索引。原实现试图把原始输入索引强制赋给聚合结果，因长度不一致而报错。

真正需要建立的行为边界是：

| 输出类型 | 正确索引行为 |
| --- | --- |
| 已经是 DataFrame | 保留 transformer 返回的索引 |
| ndarray 等无索引输出 | 可以使用原始输入索引构造 DataFrame |
| 聚合后行数变化 | 不能强制使用原始输入索引 |
| 普通等长 ndarray 输出 | 仍应正常包装，不能访问不存在的 `.index` 属性 |

### 3.2 Agent 的修改

Agent 增加了基于长度的判断：

```python
if index is not None and len(index) == len(data_to_wrap):
    data_to_wrap.index = index
else:
    index = None

if isinstance(data_to_wrap, pd.DataFrame):
    ...
```

这个修改让 issue 中的聚合示例能够运行：当聚合结果行数变化时，原始索引被丢弃，pandas 可以保留结果自身的索引。

但它存在两个问题。

第一，类型判断发生得太晚。对于普通 ndarray，只要长度与输入一致，就会执行：

```python
data_to_wrap.index = index
```

ndarray 没有 `.index` 属性，因此原本能够工作的旧行为出现回归。官方 `PASS_TO_PASS` 测试直接暴露了这个问题：

```text
test__wrap_in_pandas_container_dense
AttributeError: 'numpy.ndarray' object has no attribute 'index'
```

第二，即使输出已经是 DataFrame，只要行数与输入相同，Agent 仍会覆盖 transformer 自己返回的索引。官方目标测试要求保留已有 DataFrame 的索引，因此继续失败：

```text
test_set_output_pandas_keep_index
expected: ["s0", "s1"]
actual:   [0, 1]
```

### 3.3 正确设计

正确的判断依据不是“长度是否相等”，而是“输出是否已经拥有自己的 DataFrame 语义”：

```python
if isinstance(data_to_wrap, pd.DataFrame):
    if columns is not None:
        data_to_wrap.columns = columns
    return data_to_wrap

return pd.DataFrame(data_to_wrap, index=index, columns=columns)
```

已经是 DataFrame 时保留其索引；只有无容器语义的输出才使用外部索引进行包装。

### 3.4 暴露出的 Agent 问题

Agent 的临时验证覆盖了 issue 中的聚合成功路径，也运行了一个自己选择的相邻测试，但没有运行足以覆盖普通 ndarray 的相关测试集合。

这说明当前 Agent 比较擅长回答：

```text
这个示例现在是否能运行？
```

但没有稳定回答：

```text
这个底层共享函数还服务哪些输出类型？
修改是否保持了原有类型分支的契约？
```

## 4. 案例二：Sphinx Enum 修复中的验证确认偏误

### 4.1 任务目标

任务为：

```text
instance_id: sphinx-doc__sphinx-9281
issue: Python Enum 默认值在函数签名中被渲染为难看的 repr
```

原输出类似：

```text
<MyEnum.ValueA: 10>
```

期望输出为：

```text
MyEnum.ValueA
```

### 4.2 Agent 的修改

Agent 在 `object_description()` 中增加了 Enum 分支：

```python
if isinstance(object, enum.Enum):
    return "%s.%s" % (object.__class__.__qualname__, object.name)
```

对于定义在模块顶层的 Enum，这个实现能够得到预期结果。但 `__qualname__` 会包含类的完整限定作用域。如果 Enum 定义在测试函数内部，结果会变成：

```text
test_object_description_enum.<locals>.MyEnum.FOO
```

官方测试期望的仍然是：

```text
MyEnum.FOO
```

正确实现应使用：

```python
object.__class__.__name__
```

而不是 `__qualname__`。

### 4.3 验证过程中发生了什么

这个案例最有价值的地方不是最后选错了一个 Python 属性，而是 Agent 的临时测试实际上已经准确暴露了问题。

Agent 最初把 Enum 定义在测试函数内部，测试失败并显示 `<locals>`。但它没有据此重新审查 `__qualname__` 是否符合需求，而是判断：

```text
实际 issue 中的 Enum 位于模块顶层，因此是临时测试的期望写错了。
```

随后，Agent 把临时 Enum 移到模块顶层，测试通过，并以此确认实现正确。官方隐藏测试保留了局部 Enum 场景，因此最终失败。

### 4.4 暴露出的 Agent 问题

这是典型的验证确认偏误：

```text
实现产生异常结果
    ↓
测试准确暴露边界问题
    ↓
Agent 调整测试场景以符合实现
    ↓
验证通过，但真实缺陷仍然存在
```

临时测试由同一个 Agent 根据自己的实现假设编写和解释时，并不天然具有独立性。测试失败必须优先触发需求和实现复查，而不是直接判断测试场景不合理。

## 5. 其他失败任务反映的问题

其余任务进一步印证了上述结论：

- `django__django-14534`：为了保留旧 ID 行为增加 fallback，但 issue 明确要求直接使用 `attrs.id`；无 ID 时本应返回 `None`。
- `django__django-15916`：只修复了 `modelform_factory()` 的 callback 继承，没有处理直接在 `ModelForm.Meta` 中声明 callback 的核心元类路径。
- `matplotlib__matplotlib-26466`：复制了 annotation 的 `xy`，但遗漏了同一测试覆盖的 `OffsetFrom._ref_coord`。
- `mwaskom__seaborn-3069`：无条件反转 nominal Y 轴，连用户显式设置的 Y limits 也被反转。
- `pylint-dev__pylint-8898`：把括号内所有逗号都视为正则内部字符，导致本应报错的非法 CSV 不再报错；Agent 还把直接相关的失败测试解释为旧行为。
- `sympy__sympy-13878`：数值或化简后等价的公式不一定满足符号系统要求的规范结构；同时 `Rational(1, l)` 对符号参数存在潜在问题。
- `pylint-dev__pylint-4604`：官方测试补丁导入了基线中不存在的 `IS_PYPY`，测试在收集阶段失败。Agent 的直接复现和 functional test 实际通过，这一项更接近 benchmark 测试耦合问题，不能简单归为实现错误。

## 6. 对 Agent 设计的启示

本次测试说明，仅继续增加“深入理解代码”“检查相邻行为”之类的提示词，收益可能有限。当前提示词已经表达了这些要求，但模型不一定会稳定执行。

更可靠的方案是把关键步骤落实为 Runtime 工作流：

1. 修改前生成简短的行为清单，包括目标行为、保留行为、输入类型、相关入口和验证项。
2. 相关测试失败时阻止正常结束，除非能够证明该失败在修改前已经存在且与任务无关。
3. 临时测试同时覆盖正例、负例和至少一个相邻旧行为。
4. 成本合理时运行整个相关测试类或测试文件，而不是只运行自己挑选的少量测试。
5. 在最终回答前增加独立的只读 patch review，专门检查遗漏路径、错误假设和行为回归。
6. 对模型 API 请求增加重试，避免网络问题把有效 benchmark 样本变成基础设施错误。

这次评测的核心结论不是 Codemate 只能解决 33 个任务，而是：

> Codemate 已经具备解决真实代码问题的基础能力，但要进一步提升稳定性，关键不再是增加更多工具，而是让行为建模、独立审查和验证失败处理成为不可跳过的流程。
