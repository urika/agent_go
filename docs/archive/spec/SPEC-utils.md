# utils 模块规格说明

## 概述

`agent_go/utils.py` 是 agent_go 的通用工具模块，集中存放与具体业务流程无关、被多个上层模块共享的辅助函数。其核心职责有三块：(1) 参考文档读取（带路径穿越防护与截断）；(2) 验证命令的安全白名单校验（防止 LLM 生成的验证命令注入 shell）；(3) Conventional Commits 提交消息生成、线程安全文件追加、工具版本检测等杂项。模块被 `cli.py`、`ui.py`、`executor.py` 导入，是整个 Plan -> Decompose -> Execute 流程中执行安全与提交规范的基础设施。模块通过 `__all__ = ["read_reference_docs", "SAFE_VERIFICATION_PREFIXES"]`（`utils.py:5`）声明公共接口，其余函数为下划线前缀的内部实现，但多处被其他模块和测试直接使用。

## 公共接口

### `read_reference_docs(doc_paths: list[str], repo: Path, logger: logging.Logger) -> str`（`utils.py:7`）
- 参数：`doc_paths` 为相对 repo 的文档路径列表；`repo` 为仓库根目录；`logger` 用于记录读取/拒绝事件。
- 返回：拼接后的文档内容字符串，每个文件以 `===== 文件名 =====` 包裹；无有效内容时返回 `""`。
- 行为：逐路径 `resolve()` 后校验必须在 `repo` 内（防路径穿越）；文件读取限长 15000 字符（超出截断并标注原长度），目录则 `rglob("*.md")` 递归读取所有 Markdown，每个限长 8000 字符；读取失败仅 warning，不中断。
- 副作用：仅日志输出，无文件写入。

### `SAFE_VERIFICATION_PREFIXES: list[str]`（`utils.py:135`）
- 由 `_build_safe_prefixes()` 从 `_CMD_ARG_RULES` 动态生成的排序去重前缀列表（如 `go test`、`pytest`、`git diff`），用于保持向后兼容的粗粒度白名单匹配。

### 内部函数（下划线前缀，但被跨模块/测试使用）

| 函数 | 位置 | 职责 |
|---|---|---|
| `_is_safe_verification_command(command: str)` | `utils.py:145` | 四阶段校验验证命令安全性，返回 `(is_safe, reason)` 二元组（注解已于 2026-07-23 修正为 `-> tuple[bool, str]`）。被 `executor.py` 调用。 |
| `_log_rejected_command(command, reason, logger, task_id="", sub_id="")` | `utils.py:260` | 记录被拒绝的验证命令：logger warning + `config.log_event` 结构化事件 + 追加写 `~/.agent_go/verification_audit.jsonl` 审计文件。被 `executor.py` 调用。 |
| `_safe_append_to_file(filepath, text, logger, max_retries=10) -> None` | `utils.py:292` | 基于锁文件（`<file>.lock`）的线程安全追加写入，指数退避（`0.1 * (attempt+1)` 秒）最多重试 10 次，拿不到锁则 warning 后直接写。 |
| `_slugify(text: str, max_len=30) -> str` | `utils.py:312` | 标题转分支名短标识：非 `[a-zA-Z0-9一-鿿]` 字符替换为 `-`，截断至 `max_len`。**当前包内无调用方，仅测试使用。** |
| `_detect_commit_prefix(title: str) -> str` | `utils.py:317` | 按中英文关键词映射 Conventional Commits 类型：`feat`/`fix`/`refactor`/`docs`/`test`/`chore`，无匹配回退 `chore`。 |
| `_detect_commit_scope(title: str) -> str` | `utils.py:341` | 提取 scope：优先标题中 `\((\w+)\)` 显式声明，其次匹配常见模块名关键词（auth/api/ui/db/config/test/doc/cli/server/client/middleware/schema），无匹配返回 `""`。**当前包内无调用方，仅测试使用。** |
| `_format_commit(title, issue_ref="", sub_id="", scope="") -> str` | `utils.py:355` | 生成提交消息：`{prefix}({scope}): {title}`，可追加 `Refs: #{issue_ref}`，固定追加 `agent_go: {sub_id}` trailer。被 `executor.py:255` 调用（不传 scope）。 |
| `_detect_tool_versions(logger) -> dict[str, str]` | `utils.py:367` | 对 `claude`、`greywall` 执行 `--version`（timeout=10s），返回 `{tool: version}` dict；未安装/失败仅 debug 日志。被 `cli.py` 调用。 |
| `_build_safe_prefixes()` | `utils.py:121` | 从 `_CMD_ARG_RULES` 生成前缀列表，模块加载时执行一次。 |

## 关键逻辑与流程

### 验证命令安全校验（`_is_safe_verification_command`，`utils.py:145-257`）
四阶段流水线，任一阶段失败即返回 `(False, reason)`：
1. **shlex 解析**（`utils.py:160-167`）：`shlex.split` 失败（如引号不闭合）直接拒绝。
2. **shell 注入扫描**（`utils.py:169-180`）：对原始命令串匹配 6 个预编译正则（`utils.py:138-143`）：命令链 `[;&]|&&|\|\|`、命令替换 `$( )`/反引号/`${`、`curl|wget ... | sh`、危险 `rm -r /~`、输出重定向（排除 `2>&1`/`1>&2`）、输入重定向。
3. **命令 + 子命令查找**（`utils.py:182-239`）：在 `_CMD_ARG_RULES`（`utils.py:44-118`）中查 binary；支持两级 alias（顶层如 `python`，子命令级如 `python -m pytest` → `pytest` 规则）；子命令按 key 长度降序做最长前缀匹配（支持多词子命令如 `-m pytest`）；未匹配到具体子命令时回退到空子命令 `""` 规则，无默认规则则拒绝。
4. **逐 token 校验**（`utils.py:241-256`）：剩余 token 中，遇 `--` 后全部按 positional 校验；以 `-` 开头的按 `flags` 正则、其余按 `positionals` 正则匹配，不匹配即拒绝并报告 token 位置。

### 拒绝命令审计（`_log_rejected_command`，`utils.py:260-289`）
双通道记录：调用 `config.log_event(logger, "verification_rejected", {...})`（ImportError 时静默跳过），并追加 JSON 行到 `~/.agent_go/verification_audit.jsonl`（含 timestamp/前 200 字符命令/reason/task_id/sub_id）；审计写入异常被吞掉，不阻塞主流程。

### 提交消息生成（`_format_commit`，`utils.py:355-365`）
`_detect_commit_prefix` 按优先级依次匹配：feat（实现/新增/add/implement…）→ fix（修复/fix/bug…）→ refactor（重构/优化/refactor…）→ docs（文档/docs…）→ test（测试/test…）→ chore（配置/依赖/chore…），最终回退 `chore`。中文关键词直接子串匹配，英文关键词对 `title.lower()` 子串匹配（非词边界，可能误命中，见维护注意事项）。

## 依赖关系

- **stdlib**：`subprocess`、`json`、`re`、`time`、`shlex`、`logging`、`pathlib.Path`、`datetime`。
- **内部模块**：`agent_go.config.log_event`（`config.py:115`）——在 `_log_rejected_command` 内延迟 import，签名 `log_event(logger, event, data)`，向 logger 写一条 JSON debug 日志。
- **外部 CLI**：`claude --version`、`greywall --version`（`_detect_tool_versions`）。
- **文件系统**：`~/.agent_go/verification_audit.jsonl`（审计日志，自动 `mkdir -p`）。
- **被调用方**：`cli.py`（`read_reference_docs`、`_detect_tool_versions`）、`ui.py`（`read_reference_docs`）、`executor.py`（`_format_commit`、`_is_safe_verification_command`、`_log_rejected_command`）。
- 无环境变量依赖，无第三方包。

## 数据结构与持久化

- `_CMD_ARG_RULES`（`utils.py:44-118`）：三层 dict `{binary: {subcommand: {"flags": 正则, "positionals": 正则}}}`；值为 `str` 时表示 alias 指向另一规则集。覆盖 go/pytest/npm/npx/yarn/pnpm/cargo/make/mvn/gradle/jest/vitest/mocha/ruff/mypy/black/isort/shellcheck/shfmt/gh/git/deno/phpunit/phpstan/phpcs/rspec/rubocop 等约 27 个工具。
- 持久化文件：`~/.agent_go/verification_audit.jsonl`，JSON Lines 格式，每行字段 `timestamp`（ISO 格式）、`command`（≤200 字符）、`reason`、`task_id`、`sub_id`。
- 锁文件：`_safe_append_to_file` 使用 `<目标文件>.lock` 作为临时锁，写完即删。

## 错误处理与边界情况

- **路径穿越**：`read_reference_docs` 用 `resolve()` + `startswith` 前缀校验拒绝越界路径（`utils.py:12`），仅 warning。
- **读取失败**：文档不存在/读取异常均降级为 warning，不抛异常；`errors="replace"` 容忍非 UTF-8 内容。
- **校验函数返回值**：`_is_safe_verification_command` 返回 `(is_safe, reason)` 二元组，调用方必须按元组解包（`executor.py` 如此）。
- **锁文件兜底**：`_safe_append_to_file` 重试 10 次（总等待约 5.5 秒）仍拿不到锁时 warning 后直接写；写失败时 `finally` 仍清理锁文件（`missing_ok=True`）。
- **审计失败静默**：审计文件写入的任何异常被 `except Exception: pass` 吞掉。
- **版本检测**：`FileNotFoundError`（工具未安装）与超时/其他异常均只记 debug，返回 dict 中缺失对应 key。
- **alias 链断裂**：`_is_safe_verification_command` 对 alias 目标不存在、alias 目标无默认规则等情形均有明确的拒绝 reason（`utils.py:196`、213、216）。

## 测试覆盖

无 `tests/test_utils.py`，但有多个按功能拆分的直接测试文件：

- `tests/test_read_reference_docs.py` — 文档读取（路径防护、截断等）。
- `tests/test_is_safe_verification_command.py` 与 `tests/test_safe_verification_command.py` — 安全校验四阶段逻辑（白名单、注入特征、alias 等）。
- `tests/test_format_commit.py` — `_format_commit` / `_detect_commit_prefix` / `_detect_commit_scope`。
- `tests/test_slugify.py` — `_slugify`。
- `tests/test_safe_append_to_file.py` — 锁文件并发追加。
- `tests/test_integration.py:27` 额外引用 `_detect_commit_prefix`。

未直接覆盖：`_log_rejected_command`（审计文件写入）、`_detect_tool_versions`、`_detect_commit_scope` 在 `_format_commit` 之外的独立路径（其仅被测试直接调用）。

## 维护注意事项

- **已修复（2026-07-23，docs/ISSUES.md ISSUE-5）**：`_is_safe_verification_command` 注解曾为 `-> bool`，与实际返回 `tuple[bool, str]` 不符；现已修正注解。修改签名仍须同步 `executor.py` 调用方。
- **`_slugify` / `_detect_commit_scope` 包内无调用方**：`executor.py:255` 调 `_format_commit` 时不传 `scope`，即 scope 检测逻辑在生产路径上实际未启用；`_slugify` 仅测试在用。删除或改动前先确认是否有计划启用。
- **英文关键词子串匹配误命中风险**：`_detect_commit_prefix` 用 `kw in title_lower`（如 "add" 会命中 "address"，"test" 命中 "latest"）；`_detect_commit_scope` 已用词边界正则规避同类问题，prefix 检测未跟进。
- **白名单维护成本**：`_CMD_ARG_RULES` 中正则全部硬编码，新增工具需同时更新规则与对应测试；`_SHELL_*` 注入正则与 flag 正则存在重叠防护（defense-in-depth），调整时注意不要单方面削弱。
- **锁文件机制非强保证**：`_safe_append_to_file` 拿不到锁时降级为直接写，高并发下仍可能交错；进程崩溃会遗留 `.lock` 文件（下次写入靠重试超时兜底，无 stale-lock 清理）。
- **已修复（2026-07-23，docs/ISSUES.md ISSUE-8）**：路径校验曾用 `str.startswith` 前缀匹配，兄弟前缀目录（如 `/repo-evil`）可绕过；现改用 `path.is_relative_to(repo_root)` 按路径段比较。
- **隐式耦合**：`__init__.py` 的注释（`__init__.py:8-10`）以注释形式声明了从 utils 导入的"约定接口"，重命名内部函数时需同步注释与测试。
