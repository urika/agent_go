# subtask 模块规格说明

## 概述

`agent_go/subtask.py` 是 Plan Mode 编排流水线中的"子任务执行原语"模块，位于 executor 层之下。它提供两个底层能力：(1) 无头模式（headless）启动 Claude Code CLI 执行单个子任务，带 stream-json 实时事件解析、交互检测、空闲超时终止和有限重试；(2) 将上游子任务产出的 git tag 合并进当前子任务的 worktree。模块本身不做调度（波次并发、依赖拓扑由 `executor.py` 负责），只负责单个子任务的进程生命周期与 git 合并操作。

模块声明 `__all__ = []`（`agent_go/subtask.py:8`），即设计上不对外导出任何符号；实际由 `executor.py` 通过显式私有导入使用（`executor.py:7`）。

## 公共接口

模块无公开接口（`__all__` 为空）。以下为模块级符号及承担关键职责的内部函数：

### 常量

- `EXIT_CODE_INTERACTION = 130`（`agent_go/subtask.py:11`）— claude 进程退出码 130（SIGINT），在本模块语义中等价于"检测到交互请求"。用于 `_run_headless` 判断是否需要重试。

### 函数

- `_git_merge_upstream(src_worktree: Path, dst_worktree: Path, tag: str, logger: logging.Logger, headless: bool = False) -> None`（内部，`agent_go/subtask.py:13`）
  - 参数：`src_worktree` 上游 worktree 路径（仅语义性参数，实际 merge 在 `dst_worktree` 内执行）；`dst_worktree` 目标 worktree；`tag` 上游产物 tag；`headless` 冲突处理策略开关。
  - 副作用：在 `dst_worktree` 中执行 `git merge <tag> --no-commit`；成功则 `git commit --no-edit -m "merge upstream <tag>"`；冲突时写入 `dst_worktree/.MERGE_CONFLICT` 文件，并按 `headless` 决定保留冲突现场或 `git merge --abort`。
  - 返回 `None`，所有失败仅记录日志，不抛异常。
  - 调用方：`executor.py:401`。

- `_run_headless(task_md: str, worktree: Path, env: dict[str, str], logger: logging.Logger, sub_id: str, active_pids: Optional[set] = None, active_pids_lock: Optional[threading.Lock] = None, allowed_tools: Optional[list] = None) -> subprocess.CompletedProcess`（内部，`agent_go/subtask.py:53`）
  - 参数：`task_md` 子任务 prompt 全文；`worktree` 子进程工作目录；`env` 子进程环境变量（完整替换，非增量）；`sub_id` 日志前缀标识；`active_pids`/`active_pids_lock` 由 executor 传入用于进程组跟踪（中断时统一 kill）；`allowed_tools` Claude Code 工具白名单，非空时追加 `--allowedTools a,b,c`，`None`/空列表表示不限制。
  - 返回：`subprocess.CompletedProcess`，`args` 为空列表，`returncode` 为最后一次尝试的退出码，`stdout` 为全部尝试的事件摘要行（带时间戳，含 `--- attempt=N exit_code=M ---` 分隔行），`stderr` 恒为空字符串。注意这不是真实子进程 stdout 的透传，而是解析后的摘要。
  - 调用方：`executor.py:201`（正常执行）、`executor.py:305`（fix 修复执行）。

## 关键逻辑与流程

### 1. `_git_merge_upstream` 合并流程（`agent_go/subtask.py:13-51`）

1. 在 `dst_worktree` 执行 `git merge <tag> --no-commit`（行 20-22）。worktree 间共享对象库，tag 直接可见，无需 fetch。
2. merge 成功（returncode == 0）：执行 `git commit --no-edit` 固化合并（行 24-26）；commit 失败只记 warning，不视为错误（行 27-28）。
3. merge 失败（冲突）：用 `git diff --name-only --diff-filter=U` 收集冲突文件列表（行 31-34），写入 `dst_worktree/.MERGE_CONFLICT`（行 41-42）；无冲突文件输出时内容为"未知冲突"。
4. 冲突分流（行 44-51）：`headless=True` 时保留工作区冲突标记（`<<<<<<<`），交由 Claude Code 现场自动解决；否则 `git merge --abort`，留给用户手动处理。

### 2. `_run_headless` 执行流程（`agent_go/subtask.py:53-273`）

1. 组装命令（行 76-85）：`claude -p <prompt> --permission-mode bypassPermissions --no-session-persistence --output-format stream-json --verbose --include-partial-messages`，有 `allowed_tools` 时追加 `--allowedTools`。
2. `subprocess.Popen` 启动后将 pid 注册进 `active_pids`（有锁则持锁，行 88-92），供外部中断时统一清理。
3. 启动两个 daemon 线程分别逐行读 stdout/stderr，全部经 `parse_and_log` 处理（行 190-201）：
   - stderr 行：记录并做交互检测——匹配 `INTERACTION_PATTERNS`（行 64-69，含 "waiting for input"、"[y/n]"、"是否继续"、"请确认" 等 14 条中英文正则，大小写不敏感）即置 `waiting=True`（行 107-114）。
   - stdout 行：按 stream-json 解析事件（行 116-188）。`stream_event`（content_block_start/delta/stop）跟踪当前工具名与工具输入增量；`assistant` 记录 text/tool_use 块；`result` 记录 subtype；`user` 记录 tool_result 预览。非 JSON 行按纯文本记录。每条事件刷新 `last_ts`（行 104）。文本类输出降为 DEBUG，工具调用开始/结果为 INFO。
4. 主线程每 2s 轮询监控循环（行 203-213）：`idle > IDLE_TIMEOUT`（600s 无任何事件）则 `proc.kill()` 强制终止；`idle > HEARTBEAT`（60s）按心跳间隔打印"等待中"日志。
5. 进程退出后 join 两个读线程，`proc.wait()`，从 `active_pids` 摘除 pid（行 215-222）。
6. 重试控制（行 241-260），`MAX_ATTEMPTS = 2`（行 230）：
   - 第 2 次尝试时 prompt 追加 `RETRY_SUFFIX` 催促指令（行 225-229，"不要询问任何问题，不要等待确认"）。
   - 交互判定 = stderr 正则命中 或 退出码 == 130（`EXIT_CODE_INTERACTION`，行 253）。
   - 退出码 0 → 成功，停止；非交互原因的失败（如 API 错误）→ 不重试，直接结束（行 256-260）。
7. 全程通过 `log_event` 记录 `subtask_headless_start` / `subtask_headless_retry` / `subtask_headless_complete` 三个结构化事件（行 235、246、262-267）。

## 依赖关系

### 内部模块

- `from .config import log_event`（`agent_go/subtask.py:6`）— 结构化事件日志，签名为 `log_event(logger: logging.Logger, event: str, data: dict[str, Any]) -> None`（`config.py:115`），实现是把带 ISO 时间戳的 JSON 写到 `logger.debug`。

### 外部依赖

- CLI 命令：
  - `git` — `merge`、`commit --no-edit`、`diff --name-only --diff-filter=U`、`merge --abort`，均以 `dst_worktree` 为 cwd。
  - `claude`（Claude Code CLI）— `claude -p` 无头模式，依赖其 stream-json 输出协议（`stream_event`/`assistant`/`result`/`user` 事件类型）和 `--permission-mode bypassPermissions` 等参数。
- 环境变量：`env` 参数由调用方（executor）完整构造传入，本模块不读取任何环境变量。
- 文件系统：
  - 读：无（prompt 由调用方传入）。
  - 写：`dst_worktree/.MERGE_CONFLICT`（冲突信息，UTF-8）。
  - cwd：所有子进程在指定的 worktree 目录下运行。

## 数据结构与持久化

- 无 dataclass / 自定义数据结构；状态通过局部可变单元素列表（`last_ts`、`waiting`、`current_tool`、`tool_input`）在闭包与监控线程间共享，规避 GIL 下的显式锁需求。
- `active_pids: set[int]` 由调用方传入、本模块增删元素，是跨模块（executor 中断处理）共享的进程跟踪集合。
- 持久化仅一处：`dst_worktree/.MERGE_CONFLICT` 标记文件（`agent_go/subtask.py:41-42`），内容为冲突文件清单或"未知冲突"，由后续流程（如 Claude Code 现场解决或用户）消费；不读回、不清理。
- 返回值 `CompletedProcess.stdout` 为拼接的事件摘要字符串（非真实 stdout），每行格式 `[HH:MM:SS] <截断至200字符的内容>`，尝试之间以 `--- attempt=N exit_code=M ---` 分隔。

## 错误处理与边界情况

- 整体策略：不向上抛异常。git 命令失败只记日志；claude 子进程失败通过返回码反馈给调用方。
- 交互检测双通道：stderr 正则命中，或退出码 == 130。两者任一成立即触发重试（最多 2 次），重试时注入催促后缀压制交互倾向。
- 非交互失败（returncode 非 0 且非 130，如 API 超时/限流）：不重试，直接返回最终退出码。
- 空闲超时：600s 无任何 stdout/stderr 事件 → `proc.kill()`（SIGKILL）。kill 后的退出码（-9）不属于交互，因此不会重试——长任务若在思考阶段超时会直接以失败告终，这是已知边界。
- 中断处理：进程运行期间 pid 保留在 `active_pids` 中，外部（executor/CLI 的 SIGINT 处理）可据此 kill 整个进程组；本模块自身只保证正常路径下摘除 pid，异常路径（如解析线程异常）无 try/finally 兜底。
- 读线程为 daemon 线程，主进程退出时不阻塞；EOF（`readline` 返回 `''`）后线程自然结束。
- stream-json 解析对未知事件类型静默忽略（行 186-188），单行 JSON 解析失败按纯文本处理（行 119-123），协议演进的容忍度较好。
- `git commit` 失败（如 merge 后无变更）仅 warning 记录，合并视为成功。

## 测试覆盖

测试文件：`tests/test_subtask.py`（370 行），通过 mock `subprocess.run`/`subprocess.Popen` 覆盖：

- `TestGitMergeUpstream`：merge 成功路径（merge + commit 均被调用）、冲突时非 headless 写 `.MERGE_CONFLICT` 且 abort、headless 保留冲突不 abort、`diff-filter=U` 空输出时写"未知冲突"、commit 失败不抛异常。
- `TestRunHeadless`：正常执行（命令含 `claude`、env 透传、returncode 0）、退出码 130 + stderr 交互文本触发重试（Popen 调 2 次）、空闲超时触发 `proc.kill()`（mock `time.time` 模拟 idle > 600s）、非交互失败不重试、重试 prompt 含 `RETRY_SUFFIX`（"系统指令"）、`active_pids` 注册与清理、`MAX_ATTEMPTS` 上限为 2、模块级常量 `EXIT_CODE_INTERACTION` 存在。

未覆盖点：`allowed_tools` 参数拼接、stream-json 各事件类型的解析细节（`parse_and_log` 内部分支）、心跳日志、`.MERGE_CONFLICT` 之外的日志事件内容断言。

## 维护注意事项

- 硬编码值集中：`IDLE_TIMEOUT = 600`、`HEARTBEAT = 60`、`MAX_ATTEMPTS = 2`（行 71-72、230）、日志截断长度 200 字符、`INTERACTION_PATTERNS` 14 条正则、`RETRY_SUFFIX` 中文催促文案。调整交互检测词表时需与 Claude Code 实际输出对照，误命中会导致无意义重试，漏判则交互卡死直到 600s 超时。
- `MAX_ATTEMPTS = 2` 实际是"1 次正常 + 1 次重试"，命名易误读；且重试仅在检测到交互时发生，对 API 抖动类失败无任何重试能力。
- `env` 参数是完整替换子进程环境（行 86），调用方必须自行从 `os.environ` 派生，本模块不做合并——改动 env 构造逻辑需同步检查 executor。
- 与 executor 的隐式耦合：`_run_headless` 返回的 `CompletedProcess.stdout` 是摘要而非真实输出，executor 若改为按内容判定任务成败需注意这一点；`active_pids` 集合的并发语义（多波次子任务并发时同一集合 + 锁）由调用方保证。
- `_git_merge_upstream` 的 `src_worktree` 参数未被使用（merge 只靠 tag 名在共享对象库中解析），属遗留签名，调用方传值仍需给出。
- `.MERGE_CONFLICT` 文件写入后无人清理，同一 worktree 多次冲突会覆盖；消费方需自行判断文件时效。
- 交互检测依赖 stderr 文本，但命令行带了 `--permission-mode bypassPermissions`，理论上 Claude Code 不应请求权限——保留检测是防御性的，若上游 CLI 行为变化（不再输出这些提示），重试机制会静默失效。
- 监控循环 `time.sleep(2)` 轮询 + daemon 线程读流：事件时间戳 `last_ts` 由读线程更新（行 104），多线程写同一列表元素依赖 CPython GIL 的原子性，属实用但脆弱的模式。
