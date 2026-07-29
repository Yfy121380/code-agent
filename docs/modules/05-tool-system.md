# Tool System 模块笔记

## 1. 模块定位

Tool System 负责把模型生成的结构化工具调用转换成真实环境中的操作。它是 Codemate 从“会回答问题”变成“能完成 coding task”的核心执行层。

一个工具在系统里不是只有一个执行函数，而是一套完整契约：

- **schema**：告诉模型可以传哪些参数、哪些字段必填、枚举值有哪些。
- **description**：告诉模型什么时候用这个工具、返回什么格式、注意什么。
- **validator**：执行前做参数校验、路径解析、权限 gate 和风险判断。
- **handler**：真正执行工具动作。
- **summary**：把工具调用和结果压缩成终端可读展示。
- **trace/history**：把工具行为记录进运行日志和模型历史。

设计重点是：模型可以提出行动请求，但行动必须被工具系统结构化、校验、审批、记录后才能执行。

## 2. 工具注册方式

Codemate 的内置工具是显式注册的。每个工具都有固定 schema、风险标记和 runner。这样模型看到的是一个边界明确、可审计的动作集合。

当前内置工具分为几类：

- 文件调查工具：`list_files`、`read_file`、`grep`
- 网络工具：`web_search`、`web_extract`、`web_research`
- Shell 工具：`run_shell`
- 文件修改工具：`write_file`、`patch_file`
- 任务规划工具：`todo_write`
- Skill 工具：`skill_load`、`skill_unload`
- 子 agent 工具：`delegate`
- MCP 工具：动态发现并包装成 `mcp__server__tool`

MCP 工具不是写死在内置工具表中，而是启动时读取 settings 中的 MCP server 配置，连接 server，调用 `tools/list`，再把每个 MCP tool 包装成普通工具暴露给模型。

## 3. 参数校验总流程

所有工具执行前都会经过统一校验。整体流程可以概括为：

```text
模型返回 tool_call
  -> 检查工具是否存在
  -> schema 层限制字段结构
  -> validator 做手写参数校验
  -> 路径解析和权限 gate
  -> 必要时请求用户审批
  -> handler 执行真实动作
  -> 结果写入 trace / history / working memory
```

Schema 主要解决“参数结构是否合理”：必填字段、字段类型、枚举值、是否允许额外字段。

Validator 解决 schema 无法表达或不适合表达的规则，例如：

- 字符串不能为空。
- 数字范围限制。
- 文件必须存在或目录必须存在。
- `grep` 的上下文参数只能在 `content` 模式使用。
- `todo_write` 中最多一个 phase 为 `in_progress`。
- `patch_file` 的 `old_text` 必须在文件中恰好出现一次。
- `web_extract` 拒绝 localhost、私有 IP、loopback 等 URL。
- `run_shell` 需要分析命令类型、路径、通配符和危险操作。

校验失败一般返回 `rejected`，表示工具没有执行。工具执行中失败则返回 `error`，表示工具已经进入执行阶段但动作失败。

## 4. 路径和权限校验

文件工具和 shell 命令中的路径会统一解析成真实绝对路径：

- 支持绝对路径。
- 支持相对路径，相对路径按 workspace root 解析。
- 支持 `~` 展开。
- 解析 `../`。
- 解析符号链接，防止路径绕过。

路径解析后会标记位置：

- `workspace`：工作区内路径。
- `internal`：Codemate 内部目录，例如项目配置、项目状态、memory、skills。
- `outside_workspace`：工作区外路径。

之后进入权限 gate：

- 先看 deny，再看 allow。
- read 访问命中 read deny 直接拒绝。
- write 访问命中 write deny 直接拒绝。
- read allow 或 write allow 命中则直接放行。
- 未命中规则时，根据审批策略决定 allow、ask 或拒绝。

审批策略大致是：

- `ask`：不在 allow 中的读写通常需要询问。
- `auto`：读默认放行；工作区和内部目录写入可自动放行；其他写入需要询问。
- `read_only`：按工具元数据只允许读类或会话内状态工具，例如文件读取/搜索、web、todo、skill 和只读 shell；拒绝文件写入、MCP 和其他非只读工具。
- `full`：路径没有被 deny 时自动放行。

当需要 ask 时，审批界面可以选择只允许一次，也可以把某个具体目录作为本 session 临时 allow。临时 allow 会更新 session 中的权限规则，并重新聚合后用于后续工具校验和 sandbox 构造。

## 5. 文件调查工具

### list_files

`list_files` 用于列出目录的直接子项，不递归。它适合快速了解项目结构、定位候选文件。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `path` | string | 否 | `.` | 必须是目录路径 |

输出格式：

```text
[D] codemate/tools
[F] README.md  12 lines
[F] uv.lock  large file
[F] image.png  image file
```

限制和校验：

- `path` 会解析为真实绝对路径。
- 目标必须是目录。
- 默认忽略 `.codemate` 等内部/忽略目录，但 memory 和 skills 目录可以显式访问。
- 最多展示前 200 个直接子项。
- 小于等于 10MB 的 UTF-8 文本文件会显示准确行数，帮助模型判断是否适合全文读取。
- 大于 10MB 的文本文件显示 `large file`，不会为了统计行数扫描整个文件。
- 支持的图片文件会显示 `image file`，目前识别 PNG、JPEG、WebP 和 GIF。
- 二进制文件通过前 8192 bytes 中是否包含空字节或 UTF-8 解码是否失败做简单判断，并显示 `binary file`。
- 读路径需要通过 read 权限 gate。

### read_file

`read_file` 用于读取本地文件。对于 UTF-8 文本文件，它按行返回内容；对于支持的图片文件，它返回图片元信息，并把图片内容作为模型可识别的图片输入传递给模型。修改已有文件前必须先读目标文件，避免基于未知内容写入。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `path` | string | 是 | 无 | 必须是文件路径 |
| `start` | integer | 否 | `1` | 1-based 起始行；`read_all=false` 时生效 |
| `end` | integer | 否 | `200` | 1-based 结束行，包含该行；必须 `end >= start` |
| `read_all` | boolean | 否 | `false` | 为 `true` 时读取全文，忽略 `start/end` |

文本读取规则：

- `read_all=true` 时读取整个文件。
- `read_all=true` 时 `start/end` 被忽略。
- 所有工具结果都会经过统一大小限制；如果读取结果超过全局工具结果上限，会保留开头和结尾并提示已截断。
- 因此大文件仍建议先通过 `list_files` 查看行数，再使用 `start/end` 分段读取。

文本输出格式：

```text
# README.md
   1: hello
   2: world
```

图片读取规则：

- 支持 PNG、JPEG、WebP 和 GIF。
- 图片读取不使用行号；`start`、`end` 和 `read_all` 会被忽略。
- 读取时会校验图片格式、原始大小和解码后的像素数量，避免把过大的图片直接塞进模型上下文。
- 如果图片尺寸或体积过大，会在传给模型前进行缩放或压缩。
- history 和 session 文件只保存短文本元信息和图片缓存引用，不保存很长的 base64 内容。


限制和校验：

- `path` 必须存在且是文件。
- `read_all` 必须是 boolean。
- 文本文件在非全文模式下要求 `start >= 1` 且 `end >= start`。
- 图片文件必须是真实有效的支持格式，不能只靠扩展名伪装。
- 工具成功结果最终最多保留 30000 字符，超出时会加截断提示。
- 读路径需要通过 read 权限 gate。
- 工具结果会进入 history；最近读过的文件和短摘要会沉淀进 working memory。

### grep

`grep` 用于按正则搜索文件或目录，适合定位符号、配置项、错误信息和影响范围。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `pattern` | string | 是 | 无 | 正则表达式，不能为空 |
| `path` | string | 否 | `.` | 文件或目录路径，必须存在 |
| `mode` | string | 否 | `content` | `files_with_matches` / `count` / `content` |
| `before` | integer | 否 | `0` | content 模式中匹配前上下文行数，0-50 |
| `after` | integer | 否 | `0` | content 模式中匹配后上下文行数，0-50 |
| `context` | integer | 否 | `0` | 对称上下文行数，0-50 |

三种模式：

- `files_with_matches`：只返回包含匹配的文件路径。
- `count`：返回 `total_matches` 和各文件匹配次数。
- `content`：返回匹配行、文件路径和行号，可带上下文。

限制和校验：

- `pattern` 不能为空。
- `path` 必须存在，且是文件或目录。
- `mode` 必须是三种枚举之一。
- `before/after/context` 必须非负且不超过 50。
- `before/after/context` 只能在 `content` 模式使用。
- `before/after` 会分别覆盖 `context` 对应方向。
- 读路径需要通过 read 权限 gate。
- 优先使用系统 `rg`，没有 `rg` 时使用 Python fallback。

## 6. 网络工具

网络工具底层使用 Tavily API，不直接让 agent 自己抓网页。Web 内容被视为不可信证据，而不是指令。

### web_search

`web_search` 用于查找当前外部信息、官方文档、新闻、产品或政策更新，或者当模型不知道具体 URL 时先搜索。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | 无 | 不能为空，最长 1000 字符 |
| `max_results` | integer | 否 | `5` | 1-20 |
| `search_depth` | string | 否 | `basic` | `basic` / `advanced` / `fast` / `ultra-fast` |
| `topic` | string | 否 | `general` | `general` / `news` / `finance` |
| `time_range` | string | 否 | 无 | `day` / `week` / `month` / `year` |
| `start_date` | string | 否 | 无 | `YYYY-MM-DD` |
| `end_date` | string | 否 | 无 | `YYYY-MM-DD` |
| `include_domains` | string[] | 否 | `[]` | 最多 20 项，元素不能为空 |
| `exclude_domains` | string[] | 否 | `[]` | 最多 20 项，元素不能为空 |

限制和校验：

- `time_range` 不能和 `start_date/end_date` 混用。
- 日期必须是 `YYYY-MM-DD`。
- web 工具默认按只读信息获取处理，自动放行。
- 不应用于本地 workspace 文件调查，本地文件使用 `list_files/read_file/grep`。

### web_extract

`web_extract` 用于读取指定 URL 的正文内容。通常在 `web_search` 选出来源后使用。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `urls` | string[] | 是 | 无 | 1-20 个 HTTP/HTTPS URL |
| `extract_depth` | string | 否 | `basic` | `basic` / `advanced` |
| `format` | string | 否 | `markdown` | `markdown` / `text` |
| `query` | string | 否 | 无 | 最长 1000 字符 |
| `chunks_per_source` | integer | 否 | `3` | 1-5 |
| `timeout` | integer | 否 | `30` | 5-120 秒 |

限制和校验：

- URL 必须是 HTTP 或 HTTPS。
- URL 必须包含 hostname。
- 拒绝 localhost、`.localhost`、私有 IP、loopback、link-local、multicast、reserved IP。
- web 工具默认自动放行。

### web_research

`web_research` 用于更宽泛、多来源、需要综合的调研任务。它不适合简单事实查询。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `input` | string | 是 | 无 | 不能为空，最长 4000 字符 |
| `model` | string | 否 | `auto` | `mini` / `pro` / `auto` |
| `include_domains` | string[] | 否 | `[]` | 最多 20 项，元素不能为空 |
| `exclude_domains` | string[] | 否 | `[]` | 最多 20 项，元素不能为空 |
| `output_length` | string | 否 | `standard` | `short` / `standard` / `long` |

内部固定使用 numbered citation format。`mini` 最多等待约 300 秒，其他模式最多等待约 900 秒。

## 7. Shell 工具

### run_shell

`run_shell` 用于执行 shell 命令，适合测试、语法检查、git 状态、构建命令和少量必须用 shell 完成的操作。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `command` | string | 是 | 无 | 命令不能为空 |
| `timeout` | integer | 否 | `20` | 1-120 秒 |

校验流程：

1. 命令不能为空。
2. timeout 必须在 1-120 秒。
3. Shell safety 分析命令主体、风险类型、路径、重定向和通配符。
4. 根据命令类型映射为 read/write/unknown/dangerous 访问。
5. 对识别出的路径做真实路径解析。
6. 进入权限 gate，必要时审批。
7. 如果开启 sandbox，则在 bwrap 沙箱中执行。

命令风险大致分为：

- `read`：低风险读取类命令，例如 `ls`、`cat`、`rg`、`git status`、`git log`、`python -m py_compile`。
- `risky`：普通修改或测试类命令，例如 `mkdir`、`touch`、`cp`、`mv`、`pytest`、`git add`、`git commit`。
- `unknown`：无法确定风险的命令，默认 ask。
- `dangerous`：高风险命令，例如 `python`、`rm`、`chmod`、`sudo`、`git reset`、`git clean`、`git push`。

特殊规则：

- 风险写操作出现通配符会被拒绝。
- 部分危险命令的危险目标会被直接拒绝。
- `unknown` 和 `dangerous` 在 `full` 之外默认需要 ask。
- `read_only` 模式下允许只读 shell，拒绝修改类 shell。
- sandbox 开启时，文件系统会再提供第二层防线：多数路径只读，read deny 被隐藏，write 只允许 allow 目录。

## 8. 文件修改工具

### write_file

`write_file` 用于创建、覆盖或追加 UTF-8 文本文件。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `path` | string | 是 | 无 | 文件路径，不能是目录 |
| `content` | string | 是 | 无 | 写入或追加的文本内容 |
| `mode` | string | 否 | `overwrite` | `overwrite` / `append` |

行为：

- `overwrite`：创建新文件或替换整个文件内容。
- `append`：向文件末尾追加内容，文件不存在则创建。
- 自动创建父目录。

限制和校验：

- `path` 不能指向目录。
- 必须提供 `content`。
- `mode` 必须是 `overwrite` 或 `append`。
- 写路径需要通过 write 权限 gate。
- 对已有文件做 overwrite 前，prompt 规则要求先读目标文件。

### patch_file

`patch_file` 用于对已有文件做精确局部替换。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `path` | string | 是 | 无 | 必须是已有文件 |
| `old_text` | string | 是 | 无 | 必须非空，且在文件中恰好出现一次 |
| `new_text` | string | 是 | 无 | 替换文本 |

限制和校验：

- 目标必须是文件。
- `old_text` 不能为空。
- 必须提供 `new_text`。
- `old_text` 在当前文件中必须恰好出现一次；0 次或多次都会拒绝。
- 写路径需要通过 write 权限 gate。

这种设计比按行号 patch 更稳，因为它要求模型基于当前文件内容做精确替换，避免行号偏移或误改重复片段。

## 9. Todo 工具

### todo_write

`todo_write` 用于创建或更新当前 session 的 active todo plan。它只修改 session 状态，不修改工作区文件。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `todos` | array | 是 | 无 | 完整替换当前 todo plan |

每个 phase：

| 字段 | 类型 | 必填 | 范围/规则 |
| --- | --- | --- | --- |
| `phase` | string | 是 | 非空，高层阶段目标 |
| `status` | string | 是 | `pending` / `in_progress` / `completed` |
| `tasks` | array | 是 | 可以为空 |

每个 task：

| 字段 | 类型 | 必填 | 范围/规则 |
| --- | --- | --- | --- |
| `description` | string | 是 | 非空，具体可执行任务 |
| `status` | string | 是 | `pending` / `in_progress` / `completed` |

状态校验：

- 最多一个 phase 是 `in_progress`。
- 同一个 phase 内最多一个 task 是 `in_progress`。
- `pending` phase 不能包含 completed 或 in_progress task。
- `completed` phase 不能包含 pending 或 in_progress task。
- task 为 `in_progress` 时，所属 phase 必须是 `in_progress`。
- `todo_write` 每次都是完整替换，所以更新时必须带上所有仍然相关的 phase 和 task。

执行行为：

- 如果传入空 todos，则清空当前 todo。
- 如果所有 phase 都 completed，也会清空 todo，避免已完成计划长期留在 working memory。
- 否则写入 session，并在 working memory 中显示为 `current_todos`。

## 10. Skill 工具

### skill_load

`skill_load` 用于把一个可用 skill 加载进 working memory。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `name` | string | 是 | 无 | 必须是 Available skills 中存在的 skill |

限制和校验：

- skill name 会规范化。
- 不能加载已经 active 的 skill。
- skill 必须存在。
- `SKILL.md` frontmatter 中的 name 必须和目录名一致。
- active skill 数量有上限。
- 加载后记录 trace。

加载后，working memory 会展示 skill root 和完整指令。Skill 中提到的 `scripts/`、`references/`、`examples/`、`templates/` 都按这个 root 解析。

### skill_unload

`skill_unload` 用于卸载不再相关的 active skill。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `name` | string | 是 | 无 | 必须是 active skill |
| `reason` | string | 否 | `""` | 简短卸载原因 |

限制和校验：

- 只能卸载已经 active 的 skill。
- 卸载后从 working memory 移除。
- 卸载行为记录 trace。

设计上不要求每完成一个请求就卸载 skill。更合理的规则是：当用户切换到无关任务，或者 skill 明显不再适用时再卸载。

## 11. Delegate 工具

### delegate

`delegate` 用于把 1-3 个边界清晰的只读调查任务交给子 agent 并发执行。它适合多分支调查，不适合简单读取或直接修改。

参数：

| 参数 | 类型 | 必填 | 默认值 | 范围/规则 |
| --- | --- | --- | --- | --- |
| `tasks` | array | 是 | 无 | 1-3 个调查任务 |
| `max_steps` | integer | 否 | `20` | 1-40 |

每个 task：

| 字段 | 类型 | 必填 | 范围/规则 |
| --- | --- | --- | --- |
| `task` | string | 是 | 非空调查问题 |
| `focus` | string | 否 | 可选范围提示，如目录、文件、模块、URL |

子 agent 限制：

- 使用独立 session 和 run 目录，不污染父 history。
- 继承父 session 的临时读权限。
- 子 agent 的审批策略固定为 `read_only`。
- 只允许使用 `list_files`、`read_file`、`grep`、`web_search`、`web_extract`、`todo_write`。
- 不允许修改文件。
- 不负责最终决策和最终回答。

主 agent 应把 delegate 返回结果当作调查证据。如果后续要编辑某个文件，主 agent 仍需要自己读取目标文件。

## 12. MCP 工具

MCP 工具来自外部 MCP server，不在内置工具表里写死。

加载流程：

```text
settings.json
  -> mcp.servers
  -> connect server
  -> tools/list
  -> MCP tool schema
  -> wrapper name: mcp__server__tool
  -> 合并进工具注册表
```

支持的连接方式：

- `stdio`
- `http` / `streamable_http`
- `sse`

每个 MCP 工具会保留 server 提供的 description 和 input schema。调用时，Codemate 根据 wrapper name 找到原始 MCP tool，再通过已连接的 MCP session 调用。

权限策略：

- MCP 是动态外部工具，默认需要 ask。
- `auto` 下也不会自动放行 MCP。
- `full` 下自动放行。
- `read_only` 下 MCP 工具被拒绝。

这样设计是因为 MCP 工具能力由外部 server 定义，可能访问文件、网络、数据库或其他系统，不能简单按内置低风险工具处理。

## 13. 工具结果和状态

工具结果会被分类记录：

- `ok`：执行成功。
- `rejected`：执行前被拒绝，例如未知工具、参数错误、权限 deny、重复调用拦截、审批拒绝。
- `error`：工具进入执行阶段后失败，例如文件过长、命令退出非零、API 请求失败。
- `partial_error` / `error`：delegate 多任务中可能有部分子任务失败。

这些状态会影响后续工作记忆：

- 成功的 `read_file` 会更新 recent files 和 file summaries。
- 成功的 `write_file` / `patch_file` 会更新 recent files，并让旧文件摘要失效。
- 工具错误会记录 process notes，提醒模型避免重复错误调用。
- 成功工具调用会清理部分已经解决的 process notes。

工具结果也会进入 history，作为下一轮模型观察。旧的观察类工具结果可能被 microcompact 清理，但错误结果通常会保留，因为它们对后续决策有价值。

## 14. 终端展示

终端不会总是完整展示所有工具结果。设计原则是：

- 文件、搜索、web 这类大结果只展示摘要。
- `run_shell` 的 stdout/stderr 只展示前后少量行，避免终端被刷屏。
- `todo_write` 展示当前 plan。
- `delegate` 展示每个子任务的简要结果和 child run 信息。
- 详细文本内容仍进入 history 和 trace，便于模型继续使用和后续复盘；图片结果只记录元信息和缓存引用，实际图片内容在请求模型时再读取。

这样做是为了让用户能看清 agent 在做什么，而不是被大段工具参数和输出淹没。

## 15. 设计难点

### 参数 schema 不等于完整安全

Schema 只能限制字段形状，不能表达所有安全规则。比如 shell 命令风险、路径是否越权、URL 是否指向内网、patch 是否唯一命中，都需要 validator 手写校验。

### Shell 工具不能只靠命令白名单

Shell 可能通过重定向、脚本、动态执行、Python、通配符等方式产生副作用。因此需要命令风险分类、路径识别、权限 gate 和 sandbox 多层防护。

### 文件修改必须避免不确定性

`write_file` 和 `patch_file` 都可能覆盖用户修改。解决方式是：prompt 要求改前读取，`patch_file` 要求 `old_text` 精确且唯一，写路径必须通过权限 gate。

### MCP 工具能力不可预测

MCP server 是外部动态能力，工具描述和参数来自 server。解决方式是：动态包装成普通工具，但权限上默认 ask，不把它当作内置低风险工具。

### 工具结果不能无限进入上下文

读文件、搜索、web 和 shell 输出都可能很长。解决方式是：UI 展示摘要，history 里对旧观察结果做 microcompact，working memory 只沉淀少量高价值状态。

## 16. 面试复述版本

Codemate 的工具系统不是让模型直接执行任意动作，而是把每个动作封装成有 schema、description、validator、handler、summary 和 trace 的结构化工具。模型只能发起工具调用，真正执行前必须经过参数校验、路径解析、权限 gate 和必要审批。

内置工具覆盖文件读取、搜索、修改、shell、todo、skill、web 和 delegate。文件工具和 shell 共用路径权限体系，路径会解析为真实绝对路径，再根据 read/write allow/deny 和审批策略决定 allow、ask 或 deny。Shell 额外做命令风险分类，并在开启 sandbox 时进入 bwrap 沙箱执行。

工具设计的重点是边界清晰：`read_file` 负责读取，`grep` 负责搜索，`patch_file` 负责精确替换，`write_file` 负责创建/覆盖/追加，`todo_write` 维护任务计划，`delegate` 做只读调查，web 工具获取外部证据，MCP 工具作为动态外部能力默认需要审批。这样既能让 agent 完成真实 coding 工作，又能把风险控制在可审计、可拦截、可复盘的范围内。
