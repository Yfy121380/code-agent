# 权限审批策略与沙箱系统笔记

## 1. 模块定位

权限审批策略与沙箱系统负责控制 agent 的行动边界。Coding agent 需要读取文件、修改文件、执行 shell、访问网络和调用外部工具；如果没有统一权限模型，模型一次错误调用就可能破坏工作区、覆盖用户文件或读取敏感信息。

这套系统分成两层：

```text
执行前：权限审批
执行时：沙箱约束
```

执行前的权限审批回答的是：**这次工具调用是否允许执行，是否需要问用户？**

执行时的沙箱回答的是：**即使这次 shell 命令被允许执行，它运行时是否还能越界访问或修改不该碰的路径？**

设计目标不是让模型永远不犯错，而是在模型犯错时，runtime 能把风险挡在执行边界外。

## 2. 审批策略总览

Codemate 当前支持四种 approval policy：

```text
ask
auto
read_only
full
```

它们最大的区别体现在路径权限和高风险工具上。

### ask

`ask` 是默认策略。

特点：

- 命中 read allow / write allow 的路径可以直接执行。
- 命中 read deny / write deny 的路径直接拒绝。
- 没命中 allow 也没命中 deny 的 read/write 路径需要询问用户。
- MCP 默认询问。
- web 工具默认放行。
- unknown/dangerous shell 默认询问。

`ask` 适合日常开发：低风险、明确允许的路径可以执行；模糊或越出默认范围的操作交给用户确认。

### auto

`auto` 比 `ask` 更宽松，主要用于减少常规开发中的打断。

和 `ask` 的核心区别：

- **读操作更宽松**：只要没有命中 read deny，即使不在 read allow 中，也可以自动放行。
- **工作区写入更宽松**：workspace 和 internal 位置的写入可以自动放行。
- workspace 外写入仍然需要询问，除非已经在 write allow 中。
- unknown/dangerous shell 仍然需要询问。
- MCP 仍然需要询问。
- web 工具默认放行。

所以 auto 不是“全部自动通过”。它只是认为常规读操作和工作区内普通写操作足够常见，可以减少审批噪音。

这里要注意一个实现细节：auto 下工作区写入的自动放行是在 gate 判断阶段完成的，效果上相当于“本次 workspace 写路径被允许”，但不一定把整个 workspace 持久写进 settings 的 `write.allow`。

### read_only

`read_only` 用于只读调查、复盘和子 agent 调研。

特点：

- 允许文件读取类工具：`list_files`、`read_file`、`grep`。
- 允许 web 工具：`web_search`、`web_extract`、`web_research`。
- 允许 session 内状态工具：`todo_write`、`skill_load`、`skill_unload`。
- 允许只读 shell 命令，例如 `ls`、`cat`、`rg`、`git log`、`python -m py_compile`。
- 拒绝文件写入类工具：`write_file`、`patch_file`。
- 拒绝修改类 shell。
- 拒绝 MCP。

`read_only` 的重点是“不能修改本地文件或外部系统”。它不等于完全不能调用工具，因为调查任务仍然需要读文件、搜索和记录 todo。

### full

`full` 是完全放权模式，主要用于测试场景。

特点：

- 普通 read/write gate 中，没有命中 deny 的路径自动放行。
- unknown/dangerous shell 自动放行。
- MCP 自动放行。
- web 工具自动放行。
- shell 不进入 bwrap 沙箱，直接按当前进程环境执行。

`full` 的定位是完全信任场景，主要用于本地测试。它不会提供沙箱兜底，因此不适合作为默认运行模式。少数极度危险的 shell 命令仍会在审批前被硬拒绝，例如重启、关机、提权、杀进程、磁盘格式化和挂载类命令。

## 3. 文件访问权限规则

文件访问权限分成四类规则：

```text
read.allow
read.deny
write.allow
write.deny
```

判断原则是：

```text
deny > allow > approval policy
```

也就是说：

- 只要命中 read deny，就拒绝读取。
- 只要命中 write deny，就拒绝写入。
- 没命中 deny，再看 allow。
- 还没命中，再根据 `ask/auto/read_only/full` 决定 allow、ask 或 reject。

## 4. 默认权限规则

系统会内置一组默认规则。

默认 read allow：

- workspace root。
- 项目 `.codemate` 配置目录。
- 绑定到当前项目的状态目录，例如 sessions 和 memory 所在项目状态根目录。

默认 write allow：

- 项目 `.codemate` 配置目录。
- 绑定到当前项目的状态目录。

默认 read deny 包括敏感目录和文件，例如：

```text
~/.ssh
~/.gnupg
~/.aws
~/.azure
~/.gcloud
~/.kube
~/.docker
~/.config/gh
~/.password-store
~/.netrc
~/.npmrc
~/.pypirc
~/.git-credentials
```

默认 write deny 包括 shell 和 git 配置文件，例如：

```text
~/.bashrc
~/.zshrc
~/.profile
~/.bash_profile
~/.zprofile
~/.zshenv
~/.gitconfig
```

这些默认规则的目标是保护最常见的密钥、凭据、云配置、集群配置和 shell 启动配置。

## 5. settings.json 权限配置

用户可以在 settings 中配置权限规则。

用户级：

```text
~/.codemate/settings.json
```

项目级：

```text
<workspace>/.codemate/settings.json
```

配置结构：

```json
{
  "permissions": {
    "read": {
      "allow": [],
      "deny": []
    },
    "write": {
      "allow": [],
      "deny": []
    }
  }
}
```

用户级 settings 先加载，项目级 settings 后加载。权限规则不是简单覆盖，而是追加聚合。这样用户可以配置全局规则，项目可以补充本项目需要的规则。

## 6. 权限规则如何加载和聚合

权限规则来源有四类：

```text
默认规则
用户级 settings
项目级 settings
session 临时权限
```

加载时会做几件事。

### 路径规范化

所有规则路径都会转成真实绝对路径：

```text
原始路径
  -> 展开 ~
  -> 相对路径按 workspace root 解析
  -> resolve(strict=False)
  -> 得到真实绝对路径
```

这样可以避免同一条规则因为当前 shell 目录不同而含义变化，也能处理 `../` 和符号链接。

### 隐含规则

聚合时有两个重要隐含规则：

- `write.allow` 自动加入 `read.allow`。
- `read.deny` 自动加入 `write.deny`。

原因很直接：

- 一个目录允许写，通常也必须允许读，否则很多写操作无法判断当前状态。
- 一个目录禁止读，更不应该允许写。

### 父子目录合并

同一类规则中，如果父目录已经覆盖子目录，就删除子目录规则。

例如：

```text
allow /tmp
allow /tmp/a/b
```

会压缩成：

```text
allow /tmp
```

这样规则更简洁，判断时也不用反复匹配大量冗余子路径。

### 冲突处理

冲突处理遵循：

```text
deny 优先
```

如果某个路径同时被 allow 和 deny 覆盖，最终按 deny 处理。

比如：

```text
read.allow = /home/user
read.deny  = /home/user/.ssh
```

访问 `/home/user/.ssh/id_rsa` 时仍然拒绝。

## 7. 临时权限如何更新

当某个工具调用需要审批时，UI 会给出选项：

```text
Allow once
Allow read for /some/dir this session
Deny
```

或者：

```text
Allow once
Allow write for /some/dir this session
Deny
```

`Allow once` 只允许当前工具调用执行一次，不修改会话权限。

`Allow read/write for ... this session` 会把具体目录加入当前 session 的 `temporary_permissions`，并立即重新构建聚合权限规则。临时权限会保存到 session 中，所以恢复会话后权限行为保持一致。

临时权限只存目录：

- 如果审批对象是文件，临时 allow 目录是文件的父目录。
- 如果审批对象是目录，临时 allow 目录就是该目录。
- 如果一次工具调用涉及多个不同目录，则不会提供单个目录的快捷 allow 选项。

临时权限不会写回 settings.json。它只服务当前会话，避免一次临时审批永久改变全局配置。

## 8. 路径判断流程

工具传入路径后，会先转成真实绝对路径：

```text
raw path
  -> Path(raw).expanduser()
  -> 绝对路径直接使用
  -> 相对路径拼到 workspace root
  -> resolve()
```

然后标记位置：

```text
workspace
internal
outside_workspace
```

含义：

- `workspace`：当前项目工作区内。
- `internal`：Codemate 的项目配置或项目状态目录。
- `outside_workspace`：工作区外。

这个位置不会直接决定 allow/deny，而是进入权限 gate 时参与审批策略判断。例如 auto 下 workspace/internal 写入可自动放行，outside_workspace 写入仍然需要询问。

## 9. 工具如何使用权限规则

文件工具和 shell 的路径会走同一套 read/write gate。

文件读取类：

- `list_files`
- `read_file`
- `grep`

使用 read gate。

文件写入类：

- `write_file`
- `patch_file`

使用 write gate。

Shell 则先做风险分类：

- read shell 走 read gate。
- risky shell 走 write gate。
- unknown shell 和 dangerous shell默认ask。

MCP 不走路径规则，因为 MCP 能力由外部 server 定义，默认单独按 MCP gate 处理。

Web 工具也不走路径规则，因为它不修改本地文件，默认按只读外部信息获取处理。

## 10. Bash 风险分类

`run_shell` 是最复杂的工具，因为 shell 命令可以把真实行为藏在参数、重定向、脚本、动态展开或子进程中。

Codemate 将 shell 命令分为四类：

```text
read
risky
unknown
dangerous
```

风险等级按顺序递增：

```text
read < risky < unknown < dangerous
```

如果一条命令由多个片段组成，会取最高风险等级。

### read 命令

read 命令被认为是低风险读取类命令。

包括：

```text
pwd
ls
cat
head
tail
nl
wc
rg
grep
find
git status
git diff
git log
git show
python -m py_compile
python3 -m py_compile
```

这些命令通常用于查看文件、搜索、查看 git 状态或做语法检查。

### risky 命令

risky 是普通修改或常规开发命令。

包括：

```text
mkdir
touch
cp
mv
echo
tee
git add
git commit
pytest
uv run pytest
uv run python -m pytest
```

这些命令可能创建文件、移动文件、写入输出、更新 git 暂存区或产生测试缓存，因此不能按纯读取处理。

### unknown 命令

unknown 表示系统无法可靠判断命令风险。

例如：

```text
npm run build
make test
custom-script
curl ...
wget ...
pip install ...
```

unknown 默认需要询问，full 下才自动通过。网络类 shell 命令目前没有单独建模，无法识别时就作为 unknown 处理。

### dangerous 命令

dangerous 是高风险命令。

包括：

```text
python
python3
uv run python
rm
chmod
chown
kill
pkill
reboot
shutdown
sudo
su
dd
mkfs
mount
umount
git reset
git clean
git push
```

这些命令可能执行任意代码、删除文件、改权限、杀进程、修改磁盘、改变 git 历史或推送远端，因此默认需要询问。普通 dangerous 命令在 full 下可以自动通过，例如删除工作区内某个明确文件；但系统级高危命令仍然会被硬拒绝。

硬拒绝命令包括：

```text
reboot
shutdown
sudo
su
kill
pkill
dd
mkfs
mount
umount
```

## 11. Bash 如何识别命令主体

Shell 分析会先用 `shlex` 拆分命令，并按这些操作符切分片段：

```text
;
|
&&
||
```

每个片段提取一个 command subject。

常见识别规则：

- `git status`、`git diff`、`git log`、`git show` 识别为 `git <subcommand>`。
- `python -m py_compile` 识别为语法检查 read 命令。
- 普通 `python` 识别为 dangerous。
- `uv run pytest` 识别为 risky。
- `uv run python -m pytest` 识别为 risky。
- `uv run python` 识别为 dangerous。
- `npm <subcommand>` 识别成 `npm <subcommand>`，如果没有在已知表中则进入 unknown。

如果多个片段风险不同，最终使用最高风险。

## 12. Bash 如何提取路径

路径提取是保守规则，不执行命令，只根据命令主体和参数形态找“可能是路径”的参数。

### 普通路径命令

这些命令会从非 option 参数中提取路径：

```text
ls
cat
head
tail
nl
wc
mkdir
touch
find
```

例如：

```text
cat src/main.py
```

提取：

```text
src/main.py
```

`find` 如果没有显式路径，默认提取：

```text
.
```

### rg / grep

`rg` 和 `grep` 会把第一个非 option 参数视为 pattern，后面的非 option 参数视为路径。

例如：

```text
rg "TODO" codemate tests
```

提取：

```text
codemate
tests
```

如果没有显式路径，默认提取：

```text
.
```

### cp / mv

`cp` 和 `mv` 会把非 option 参数都作为路径，包括源路径和目标路径。

例如：

```text
cp a.txt b.txt
```

提取：

```text
a.txt
b.txt
```

### git 命令

`git add` 会提取非 option 参数作为路径。

`git diff` 和 `git show` 只有在出现 `--` 后，才提取 `--` 之后的路径。否则不提取路径，因为这些命令可能查看 commit 或对象。

### python

`python -m py_compile file.py` 会提取要检查的 Python 文件路径。

普通 `python script.py` 或 `uv run python script.py` 属于 dangerous，会提取脚本路径，但权限上按 dangerous 处理。

### tee 和重定向

`tee` 会提取输出目标路径。

Shell 重定向会单独识别：

```text
echo hello > out.txt
echo hello >> out.txt
```

提取：

```text
out.txt
```

只要出现重定向，命令风险至少提升到 risky。

## 13. Bash 自动拒绝规则

有些情况不进入审批，直接拒绝。

### 空命令

命令为空直接拒绝。

### shell 解析失败

`shlex` 无法解析命令时拒绝。

### 动态 shell 展开

如果命令包含：

```text
执行一段命令，并把命令标准输出捕获为字符串，替换到原命令位置。
`...`
$(...)
启动子shell执行命令
bash -c "XXX"
sh -c "XXX"
```

会标记为 dynamic shell expansion，并至少提升到 risky。当前实现不会因为动态展开本身直接拒绝，但会提高审批风险。

### 危险命令危险目标

对于部分危险命令，如果目标是极危险路径，会直接拒绝。

例如：

```text
rm /
rm ~
rm ~/...
rm /*...
```

会被拒绝，因为这种操作即使询问也过于危险。

### 风险写操作通配符

如果 risky 或 dangerous 命令路径中出现通配符：

```text
*
?
[
]
```

会直接拒绝。

原因是通配符会让实际修改范围不可控，审批时用户看到的路径不等于最终被修改的所有路径。

## 14. Bash 与审批策略如何结合

Shell 分析完成后，会得到：

```text
kind
subjects
paths
reasons
has_glob
has_redirection
has_dynamic_expansion
blocked
```

如果 `blocked=true`，直接拒绝。

否则：

- `read` 命令使用 read gate。
- `risky` 命令使用 write gate。
- `unknown` 命令使用 unknown gate。
- `dangerous` 命令使用 dangerous gate。

unknown/dangerous 的特点是：

- `full` 下 allow。
- 其他策略下 ask。

read/risky 则继续结合路径规则和 approval policy 判断。也就是说，普通文件读写和 read/risky shell 更依赖 read/write allow/deny；unknown/dangerous shell 更依赖用户确认和 sandbox 兜底。

## 15. 沙箱的作用

静态 shell 分析不可能完全可靠。

例如：

```text
python test.py
```

它可能只是打印信息，也可能在脚本里写 `/home/user/test.txt`、删除文件、访问网络。仅靠字符串分析无法完全知道真实行为。

沙箱的作用是：**即使命令通过了审批，执行时也只能在允许的文件系统边界内产生影响。**

也就是说：

- 权限审批决定“是否可以尝试执行”。
- 沙箱决定“执行时最多能访问和修改哪些地方”。

## 16. 沙箱启用条件

沙箱配置在 settings 中：

```json
{
  "sandbox": {
    "enabled": true
  }
}
```

默认启用。

非 `full` 模式执行 shell 前会做 bwrap preflight：

- 检查是否安装 `bwrap`。
- 检查当前系统能否启动 bubblewrap。
- 如果系统不支持非特权 user namespace 或 bwrap 无法启动，shell 工具会返回执行错误，而不是直接裸跑命令。

`full` 模式会跳过沙箱和 preflight，直接执行 shell 命令。

## 17. 沙箱如何工作

沙箱使用 bubblewrap 构造命令。

基础参数：

```text
bwrap
  --unshare-all
  --share-net
  --die-with-parent
  --ro-bind / /
  --dev /dev
  --proc /proc
  --tmpfs /tmp
```

含义：

- `--unshare-all`：隔离 namespace，包括 mount、pid 等。
- `--share-net`：网络不隔离，默认可访问网络。
- `--die-with-parent`：父进程退出时沙箱进程也退出。
- `--ro-bind / /`：把宿主根文件系统以只读方式挂进沙箱。
- `--dev /dev`：创建可用的 `/dev`。
- `--proc /proc`：创建沙箱内进程视角的 `/proc`。
- `--tmpfs /tmp`：给沙箱一个临时空 `/tmp`。

然后根据权限规则继续添加挂载。

## 18. read deny 如何应用到沙箱

read deny 会被遮蔽。

如果 read deny 是目录：

```text
--tmpfs <denied-dir>
```

也就是用空 tmpfs 覆盖该目录，让沙箱内看不到真实内容。

如果 read deny 是文件：

```text
--ro-bind /dev/null <denied-file>
```

也就是用 `/dev/null` 覆盖该文件。

这样即使命令试图读取 `.ssh`、`.aws`、`.netrc` 等路径，也只能看到空目录或空文件。

## 19. write allow 如何应用到沙箱

根文件系统默认是只读的：

```text
--ro-bind / /
```

因此命令默认不能写任何地方。

沙箱会把允许写的目录重新 bind 成可写：

```text
--bind <allowed-dir> <allowed-dir>
```

写目录来源包括：

- 聚合后的 `write.allow` 目录。
- 本次工具 gate 中审批通过的写目标目录。

也就是说，如果用户选择 `Allow once` 执行某个写 shell，沙箱也会把这次审批对应的写目录临时加入本次命令的可写挂载；否则命令虽然通过审批，但实际写入会被只读根文件系统挡住。

如果某个 write allow 位于 read deny 下，deny 优先，沙箱不会重新开放它。

## 20. 沙箱运行目录和 HOME

沙箱执行时还会设置：

```text
--chdir <workspace-root>
--setenv HOME <real-home>
/bin/sh -lc <command>
```

含义：

- 命令工作目录是 workspace root。
- `HOME` 环境变量仍指向真实 home 路径。
- 但真实 home 文件系统默认是只读，且敏感 read deny 路径被遮蔽。

这样路径语义尽量接近真实系统，减少模型因为沙箱内路径变化而误判；同时通过只读根、deny 遮蔽和 write allow bind 控制实际权限。

## 21. 审批和沙箱的关系

审批和沙箱不是重复设计，而是互补。

审批层：

- 看工具参数。
- 看路径规则。
- 看 shell 风险分类。
- 决定 allow/ask/deny。
- 让用户可以临时授权。

沙箱层：

- 只作用于 `run_shell`。
- 不关心模型为什么要执行这个命令。
- 不重新理解 shell 语义。
- 用文件系统挂载规则限制实际读写。

典型例子：

```text
python test.py
```

审批层会把它当 dangerous，询问用户。

如果用户允许执行，但 `test.py` 内部试图写未被允许的路径，沙箱会因为根文件系统只读而阻止写入。

这正是沙箱存在的价值：它防的是静态分析看不到的运行时行为。

## 22. 设计难点

### 难点一：审批策略不能只按工具名判断

同一个工具的风险取决于参数。`run_shell` 可以是 `git log`，也可以是 `rm -rf`；`read_file` 可以读工作区文件，也可以读敏感路径。

解决方式是：工具名只提供初步风险，真正决策要结合参数、路径、权限规则和 shell 分析。

### 难点二：路径必须统一规范化

如果不处理 `~`、相对路径、`../` 和符号链接，模型可以无意或有意绕过权限规则。

解决方式是：所有规则路径和工具路径都转成真实绝对路径，再做 allow/deny 匹配。

### 难点三：Bash 无法完全静态分析

Shell 可以执行脚本、动态展开、调用 Python、重定向、通过子进程写文件。

解决方式是：静态层只做保守分类和路径提取，无法确定的归为 unknown 或 dangerous；执行层用 bwrap 沙箱兜底。

### 难点四：规则冲突必须简单

如果 allow/deny、默认/用户/项目/临时规则互相冲突时逻辑太复杂，很难解释也很难测试。

解决方式是：统一采用 deny 优先；write allow 自动 read allow；read deny 自动 write deny；同类规则父目录覆盖子目录。

### 难点五：临时权限需要可恢复

如果用户在一次会话中允许了 workspace 外路径，恢复 session 后权限行为不一致，会导致 agent 再次执行时突然失败。

解决方式是：临时权限写入 session，而不是只存在进程内存里。

## 23. 面试复述版本

Codemate 的权限系统分成审批和沙箱两层。审批层在工具执行前判断这次调用是 allow、ask 还是 deny；沙箱层只作用于 shell，在命令执行时用文件系统挂载限制真实读写范围。

审批策略有 ask、auto、read_only 和 full。ask 是默认模式，未命中 allow 的读写需要询问；auto 对读更宽松，只要不命中 read deny 就自动放行，并且工作区和内部目录写入自动放行；read_only 只允许读类、web、todo、skill 和只读 shell；full 是完全信任模式，会自动通过审批，并且 shell 不进入沙箱。MCP 因为是外部动态工具，ask/auto 下默认询问，read_only 下拒绝，full 下放行。

文件权限通过 read/write allow/deny 管理。规则来自默认配置、用户 settings、项目 settings 和 session 临时权限。所有路径都会展开 `~`、按 workspace 解析相对路径并 resolve 成真实绝对路径。规则合并时 deny 优先，write allow 自动加入 read allow，read deny 自动加入 write deny，同类父子目录会压缩成父目录。

Bash 会先做风险分类：read、risky、unknown、dangerous。它用 shlex 拆命令，识别 git/python/uv/npm 等命令主体，从常见路径命令、grep/rg、cp/mv、git、python py_compile、tee 和重定向中提取路径。风险写命令带通配符会直接拒绝，危险命令指向 `/`、`~`、`~/...`、`/*...` 等目标也会拒绝。

Shell 通过审批后，如果 sandbox 开启且当前不是 full 模式，会用 bwrap 运行：根文件系统只读挂载，read deny 用空目录或 `/dev/null` 遮蔽，write allow 和本次审批通过的写目录重新 bind 为可写，网络默认保留。这样即使命令内部通过脚本或 Python 尝试越界写入，也会被沙箱的文件系统权限挡住。full 模式用于完全信任的测试场景，会跳过这层沙箱。
