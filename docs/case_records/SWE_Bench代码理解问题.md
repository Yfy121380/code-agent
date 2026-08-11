# 从局部修复到行为契约：一次 Coding Agent 问题定位与改进实践

## 1. 案例概述

这次问题表面上是 Codemate 没有正确完成一个 SWE-bench Verified 任务，实际暴露的是 coding agent 更普遍的缺陷：

> Agent 能够定位报错位置并写出一个局部有效的补丁，但没有建立完整的行为边界，因此修复了用户描述的现象，却破坏了没有被明确写进问题描述的相邻旧行为。

问题并不在工具数量、上下文长度或代码修改能力，而在修改前的代码理解过程不够稳定。Agent 把 issue 中的示例当成了完整规格，只回答了“哪里需要删一行”，没有充分回答：

- 真正需要改变的行为是什么？
- 哪些相邻行为必须保持不变？
- 仓库中的实现、调用方和测试提供了哪些证据？
- 修改应该发生在哪一层，才能覆盖所有相关路径？
- 验证是否同时覆盖了新行为和旧行为？

解决方案也不是简单要求模型“多读几个文件”，而是把代码修改流程从“发现症状后尽快修改”调整为“先建立行为契约，再选择正确的修改层次，最后进行双向验证”。

本次改进后，Codemate 在相同任务上得到了正确补丁，并通过 SWE-bench Verified 的全部目标测试与回归测试：

```text
FAIL_TO_PASS: 1/1
PASS_TO_PASS: 5/5
resolved: true
```

## 2. 具体问题：Requests 自动发送 Content-Length

### 2.1 任务描述

案例来自 SWE-bench Verified：

```text
instance_id: psf__requests-1142
repository: psf/requests
```

Issue 的核心描述是：

```text
requests.get is ALWAYS sending content length

requests.get 总是给请求添加 Content-Length header。
GET 请求不应该自动添加这个 header，或者至少应该允许不发送它。
例如，amazon.com 会对包含 Content-Length 的 GET 请求返回 503。
```

这个描述明确指出了一个现象：

```text
GET + no body
    不应该自动发送 Content-Length: 0
```

但它没有完整描述其他 HTTP 方法、带 body 请求、显式 header 和认证流程应该如何处理。这些内容必须从代码库本身继续调查，不能仅靠 issue 文本推断。

### 2.2 原始实现

问题位于 `PreparedRequest.prepare_content_length()`。原始代码会在进入函数时无条件设置：

```python
self.headers["Content-Length"] = "0"
```

随后，如果 body 存在，再根据 body 类型计算实际长度。

这意味着无 body 的 GET 请求也会得到：

```text
Content-Length: 0
```

从局部看，删除这行代码似乎就能解决 issue；但从整个请求准备流程看，这行代码还承担着另一个旧行为：

```text
POST / PUT / PATCH / DELETE + no body
    仍然自动发送 Content-Length: 0
```

因此，正确的行为不是“所有无 body 请求都不设置 Content-Length”，而是根据请求方法区分。

### 2.3 完整行为契约

结合实现、调用路径和现有行为，可以得到本次修改应满足的行为矩阵：

| 场景 | 预期行为 |
| --- | --- |
| GET，无 body | 不自动添加 `Content-Length` |
| HEAD，无 body | 不自动添加 `Content-Length` |
| POST/PUT/PATCH/DELETE，无 body | 保留 `Content-Length: 0` |
| 任意方法，有普通 body | 计算并设置实际长度 |
| 任意方法，有类文件 body | 保留 seek/tell 长度计算逻辑 |
| 用户显式设置 `Content-Length` | 保留用户设置 |
| 认证流程重新计算长度 | 不应让无 body 的 GET 重新得到长度 0 |

这里的关键不是记住 HTTP 方法列表，而是认识到一次行为修改通常包含两部分：

```text
change:   本次需要改变的行为
preserve: 修改后必须继续成立的相邻行为
```

## 3. 第一次修复为什么失败

### 3.1 错误补丁

Codemate 第一次生成的补丁只删除了无条件赋值：

```diff
 def prepare_content_length(self, body):
-    self.headers["Content-Length"] = "0"
     if hasattr(body, "seek") and hasattr(body, "tell"):
         ...
```

这个补丁确实修复了 issue 中直接描述的场景：

```text
GET + no body -> 不再自动设置 Content-Length
```

但它同时改变了所有无 body 请求：

```text
POST + no body   -> Content-Length 消失
PUT + no body    -> Content-Length 消失
PATCH + no body  -> Content-Length 消失
DELETE + no body -> Content-Length 消失
```

因此它是一个局部有效、整体错误的修复。

### 3.2 验证为何没有发现错误

第一次运行的验证脚本输出了：

```text
GET no body: <absent>
HEAD no body: <absent>
POST no body: <absent>
PUT no body: <absent>
POST body: 3
GET explicit header: 9
```

Agent 随后把这些结果全部判断为正确。这说明问题不只是“少测了几个分支”，而是验证预期本身来自刚写完的实现：

1. Agent 先删除了默认 `Content-Length: 0`。
2. 验证脚本观察到 POST/PUT 也不再有该 header。
3. Agent 没有从旧实现、相关约定或测试中独立推导预期。
4. 最终用“程序确实表现成我刚改的样子”证明“修改是正确的”。

这是一种典型的自证式验证：

> 验证只确认实现做了什么，却没有独立确认实现应该做什么。

### 3.3 Agent 设计上的根因

第一次失败反映了几项相互关联的问题。

#### 把问题示例当成完整规格

Issue 只提到了 GET。Agent 因此只关注“删除 GET 的长度头”，没有主动寻找其他方法是否依赖同一段共享逻辑。

#### 定位到文件后过早收敛

找到 `prepare_content_length()` 后，局部修复非常直观。模型很容易把“已经找到可修改的一行”误认为“已经理解了问题”。

#### 阅读代码不等于建立约束

第一次运行也读取了相关文件，但没有把证据整理成明确的 `change/preserve` 边界。读过调用方和测试并不能自动保证模型在修改时仍会使用这些信息。

#### 只要求最小补丁，容易产生浅层补丁

“保持修改最小”本身是正确原则，但必须以解决根因和保持行为契约为前提。否则模型会优先选择行数最少的补丁，而不是语义范围最准确的补丁。

#### 验证缺少独立预期

验证覆盖了一些相关场景，却没有先根据仓库证据写清每个场景的预期，最终把回归行为当成了成功结果。

## 4. 问题的本质

这次问题可以概括为三个层次。

### 4.1 代码层：共享逻辑中的条件边界

`prepare_content_length()` 是多个 HTTP 方法共用的逻辑。修改共享逻辑时，不能只验证触发 issue 的单一入口，还要检查同一函数服务的其他调用场景。

### 4.2 Agent 层：从症状修复转向契约修复

Agent 原本采用的隐式流程接近：

```text
定位症状 -> 找到代码 -> 写最小补丁 -> 验证新现象
```

改进后的流程是：

```text
判断变更风险
    -> 调查目标实现和相关路径
    -> 建立 change/preserve 行为契约
    -> 根据仓库证据选择修改层次
    -> 写最小且完整的补丁
    -> 验证新行为和保留行为
    -> 检查最终 diff 和相关路径
```

### 4.3 验证层：新行为与回归行为必须同时验证

Bug fix 的验证至少要回答两个问题：

```text
目标问题是否已经消失？
原来正确的相关行为是否仍然成立？
```

只有第一个问题通过，不能证明补丁正确。

## 5. 如何解决

### 5.1 按变更风险决定调查深度

新的工作流不要求所有任务都进行大范围调查。机械修改可以保持轻量，但以下任务需要扩大调查范围：

- Bug 修复。
- 公共 API 行为修改。
- 共享代码修改。
- 条件分支较多的逻辑。
- 兼容性、默认值或错误处理修改。
- 用户请求存在歧义的任务。

这样可以避免两种极端：

- 简单修改也进行无意义的大规模搜索。
- 高风险修改只看一个局部文件就直接动手。

### 5.2 修改前建立行为契约

对于非机械修改，Agent 在第一次编辑前需要通过 commentary 明确说明：

- 可能的根因或目标行为。
- 支持判断的关键仓库证据。
- 必须保留的相邻行为。
- 准备在哪一层修改以及原因。

这不是为了增加过程文字，而是让调查结果在编辑前形成可检查的中间产物。以本案例为例，正确的编辑前结论应接近：

```text
问题来自 prepare_content_length 对空 body 无条件写入长度 0。
Adapter 只透传 PreparedRequest.headers，auth 也会重新调用长度计算，
因此修复点应放在 prepare_content_length 本身。

需要改变：GET/HEAD 无 body 时不自动设置 Content-Length。
需要保留：非 GET/HEAD 的空 body 仍设置 0；有 body 时仍计算长度；
显式 header 和 auth 路径不能被破坏。
```

如果这段结论无法写清楚，就说明还没有足够信息安全地修改代码。

### 5.3 将示例视为证据，而不是完整规格

新的规则要求 Agent 对 issue、错误信息和用户示例保持正确态度：

```text
示例说明至少有一个行为需要改变，
但不一定枚举了全部边界和兼容性要求。
```

完整意图还需要结合：

- 目标实现。
- 调用方。
- 相邻条件分支。
- 现有测试。
- 文档和命名。
- 仓库中的同类实现。

### 5.4 在正确层次做最小且完整的修改

本案例中，如果只在 GET 便利函数入口做特殊处理，可能遗漏：

- 直接构造 `Request("GET", ...)` 的路径。
- 认证流程重新计算长度的路径。
- 其他调用 `prepare_content_length()` 的入口。

调查发现 adapter 只负责透传 prepared headers，而 auth 可能再次调用长度计算。因此正确修改层是 `prepare_content_length()`，但需要让它依据 `self.method` 区分空 body 行为。

最终补丁为：

```python
def prepare_content_length(self, body):
    if body is not None:
        if hasattr(body, "seek") and hasattr(body, "tell"):
            body.seek(0, 2)
            self.headers["Content-Length"] = str(body.tell())
            body.seek(0, 0)
        else:
            self.headers["Content-Length"] = str(len(body))
    elif self.method not in ("GET", "HEAD"):
        self.headers["Content-Length"] = "0"
```

这个补丁仍然很小，但它不是以“少改一行”为目标，而是以“准确覆盖行为契约”为目标。

### 5.5 使用独立预期进行双向验证

新的验证规则要求：

- 从用户请求和仓库证据推导预期，不能只参考刚写完的实现。
- 同时验证要改变的行为和要保留的行为。
- 运行时行为修改不能只使用语法检查。
- 修改后检查 diff 和相关路径，防止意外改变默认值、备用分支和兼容行为。

本案例最终验证了：

```text
GET / HEAD + no body:
    Content-Length 不存在

POST / PUT / PATCH / DELETE + no body:
    Content-Length == "0"

GET + body "abc":
    Content-Length == "3"

GET + explicit Content-Length:
    保留显式值

GET + basic auth:
    auth 后不会重新添加 Content-Length: 0
```

## 6. 改进后的实际执行过程

第二次运行仍使用 `gpt-5.5`，但采用了改进后的工作流规则。

### 6.1 调查范围发生变化

Agent 不再只读取报错函数，而是依次检查：

- 请求对象和 body 准备流程。
- GET 等便利 API 如何传递参数。
- Session 如何准备请求。
- Adapter 最终如何发送 headers。
- Auth 是否会重新计算长度。
- 现有测试中的相邻行为。

这次调查得到两个第一次没有稳定保留下来的关键结论：

1. Adapter 不会自动纠正该 header，问题应在请求准备阶段解决。
2. Auth 会再次调用长度计算，因此只修 `prepare_body` 可能遗漏认证路径。

### 6.2 编辑前明确了保留行为

在第一次修改前，Agent 明确写出：

```text
保留“有 body 时一定计算长度”和“POST/PUT/PATCH 等可带 body 方法
空 body 时发送 0”的行为，只让 GET/HEAD 在 body 为 None 且用户
未显式设置时不自动生成 Content-Length。
```

这段 commentary 是本次行为改进的关键证据。它表明 Agent 在写补丁前已经建立了行为边界，而不是修改后再解释补丁。

### 6.3 验证环境问题的处理

该任务使用的是历史版本 Requests，而本机只有 Python 3.13。直接导入会遇到与本次修改无关的兼容问题：

- 标准库 `cgi` 已被移除。
- `collections.MutableMapping` 等接口已经迁移到 `collections.abc`。

Agent 没有修改仓库来迎合本机环境，也没有污染用户 Python 环境，而是在单次验证进程中加入最小兼容 shim，只用于执行 `PreparedRequest` 行为检查。

这使验证仍然停留在运行时行为层，而不是因为环境问题退化为单纯的 `py_compile`。

## 7. 解决后的结果

### 7.1 软件行为

修改后的 Requests 行为符合完整契约：

| 场景 | 修改前 | 错误补丁 | 正确补丁 |
| --- | --- | --- | --- |
| GET，无 body | `Content-Length: 0` | 无 header | 无 header |
| HEAD，无 body | `Content-Length: 0` | 无 header | 无 header |
| POST，无 body | `Content-Length: 0` | 无 header，回归 | `Content-Length: 0` |
| PUT/PATCH/DELETE，无 body | `Content-Length: 0` | 无 header，回归 | `Content-Length: 0` |
| 有 body | 正确计算长度 | 正确计算长度 | 正确计算长度 |
| 显式 header | 保留 | 保留 | 保留 |
| Auth 后重新计算 | 可能给 GET 加 0 | 不加，但同时丢失其他空 body 行为 | 按方法保持正确语义 |

### 7.2 正式评测

SWE-bench Verified harness 的结果为：

```text
patch_successfully_applied: true
resolved: true

FAIL_TO_PASS:
    test_no_content_length: passed

PASS_TO_PASS:
    5 passed

PASS_TO_FAIL:
    0
```

这说明：

- 目标失败测试已经被修复。
- 评测选取的已有正确行为没有发生回归。
- 补丁可以正常应用。
- 任务被正式判定为解决。

### 7.3 Agent 行为

与第一次运行相比，第二次运行体现出以下变化：

| 第一次运行 | 改进后运行 |
| --- | --- |
| 找到报错函数后快速收敛 | 按风险扩大到调用方、测试、auth 和 adapter |
| 把 GET 示例当成全部需求 | 把示例当证据，并继续寻找完整行为边界 |
| 只说明要修什么 | 同时说明要修什么和不能破坏什么 |
| 追求删除一行的最小补丁 | 在正确层次实现语义范围最小的补丁 |
| 根据新实现解释测试结果 | 根据仓库证据预先定义验证预期 |
| POST/PUT 回归未被识别 | 主动验证非 GET/HEAD 空 body 行为 |
| 局部行为通过但正式评测失败 | 目标测试与回归测试全部通过 |

## 8. 代价与局限

改进后的运行并非没有成本。

第一次运行约为 352 秒，第二次运行约为 609 秒，调查和验证时间增加约 73%。增加的时间主要来自：

- 阅读更多相关路径。
- 在编辑前整理行为契约。
- 处理历史依赖与当前 Python 的兼容问题。
- 验证更多相邻行为。

这个成本对于公共 API 和共享逻辑的 Bug 修复是合理的，但不应该无条件施加到机械修改上。因此规则强调按风险调整调查深度，而不是要求每次修改都遍历整个仓库。

同时，一次成功案例只能说明新流程具有明显正向信号，不能证明它对所有任务都稳定有效。模型本身具有随机性，后续仍应使用更多同类任务进行对照：

- 公共 API 条件分支任务。
- 默认值和兼容性任务。
- 跨调用路径的共享逻辑任务。
- 错误类型和异常路径任务。

如果后续仍频繁出现“读过相关代码但修改时忘记约束”的问题，可以进一步把行为契约从 prompt 约束升级为 runtime 中的结构化任务状态，例如保存：

```json
{
  "intent": "需要改变的行为",
  "evidence": ["支持判断的仓库证据"],
  "preserve": ["必须保持不变的相邻行为"],
  "approach": "修改层次与原因",
  "validation": ["新行为验证", "旧行为回归验证"]
}
```

当前阶段先通过更多任务验证 prompt 改造是否足够，再决定是否增加这类运行时机制。

## 9. 可复用的方法

这次案例形成了一套可以用于普通开发、Bug 修复和 benchmark 任务的方法。

### 修改前

1. 判断任务是机械修改还是行为修改。
2. 行为修改需要定位目标实现，并检查足够的调用方、测试、文档和同类实现。
3. 明确要改变的行为。
4. 明确必须保留的相邻行为。
5. 确认证据来自仓库，而不是只来自问题描述或模型猜测。
6. 选择能够覆盖所有相关入口的正确修改层。

### 修改时

1. 优先复用项目已有抽象、命名、异常和条件处理模式。
2. 保持补丁范围小，但不能为了少改代码而只处理一个示例。
3. 不修改无关代码，不通过修改测试掩盖实现问题。

### 修改后

1. 检查最终 diff。
2. 验证目标行为已经改变。
3. 验证关键相邻行为仍然成立。
4. 检查默认值、备用分支、错误路径和兼容行为。
5. 运行聚焦测试；运行时行为不能只靠语法检查。
6. 如果环境阻塞验证，使用隔离且最小的替代方案，并明确未验证范围。

## 10. 总结

这次问题最重要的结论不是“需要让模型多读代码”，而是：

> Coding agent 在修改代码前需要建立可验证的行为契约，在修改后需要分别证明目标行为已经修复、相邻旧行为没有被破坏。

原来的 Codemate 已经具备定位文件、读取代码、修改源码和运行验证的工具能力，但工具能力不会自动转化为整体代码理解。真正缺少的是一套稳定的认知流程，把分散的调查结果组织成：

```text
问题本质
仓库证据
目标行为
保留行为
修改层次
双向验证
```

当这套流程被明确写进工作规则后，Agent 不再满足于删除最显眼的一行，而是能够识别共享逻辑中的条件边界，在正确层次实现小而完整的修复，并用正式回归结果证明修改成立。
