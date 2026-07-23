# pipeline 模块规格说明

## 概述

`agent_go/pipeline.py` 是 agent_go 的执行调度核心，负责把用户确认后的子任务列表（`confirmed`）按依赖关系做拓扑波次（wave）调度，调用 `executor.run_subtask` 在独立 git worktree 中逐个/并发执行。它还负责执行前的 `gc.auto` 禁用、SIGINT/SIGTERM 中断处理（暂停并可恢复）、执行后的远程分支推送、worktree/tag 清理，以及最终结果写回 `meta.json` 和控制台报告输出。模块无 `__all__` 导出项（`agent_go/pipeline.py:13`），唯一入口是内部函数 `_run_pipeline`，由 `cli.py` 的 `cmd_run`（`agent_go/cli.py:297`）和 `cmd_resume`（`agent_go/cli.py:385`）调用。

## 公共接口

模块无公开接口（`__all__ = []`）。唯一的对外函数是内部函数：

### `_run_pipeline(...)` — 内部

签名（`agent_go/pipeline.py:15-16`）：

```python
def _run_pipeline(confirmed: list[dict[str, Any]], repo: Path, task_dir: Path,
                  logger: logging.Logger, config: dict[str, Any], headless: bool,
                  parallel: int, issue_ref: str, meta: dict[str, Any],
                  worktree_map: Optional[dict[str, Path]] = None,
                  results_map: Optional[dict[str, dict[str, Any]]] = None,
                  completed_ids: Optional[set] = None,
                  remote_url: str = "") -> None
```

参数：

- `confirmed`：已确认的子任务列表，每项至少含 `id`、`title`、`description`、`depends_on`（list，可缺省）。
- `repo`：目标仓库路径（含 `.git` 时才执行 git 相关操作）。
- `task_dir`：任务目录，子任务产物写入 `task_dir/<sub_id>/`，`meta.json` 写入 `task_dir/`。
- `logger`：执行日志 logger（写入 `task_dir/execution.log`，由调用方配置）。
- `config`：配置 dict。**当前函数体内未使用**（保留参数）。
- `headless`：透传给 `run_subtask`，控制 headless 沙箱模式。
- `parallel`：最大并发数；`> 1` 时波次内用 `ThreadPoolExecutor` 并发，否则串行。
- `issue_ref`：issue 引用，透传给 `run_subtask`。
- `meta`：任务元数据 dict，至少含 `task_id`、`status`；函数会原地修改并写回 `meta.json`。
- `worktree_map` / `results_map` / `completed_ids`：恢复模式传入的已有状态；缺省初始化为空。
- `remote_url`：非空时执行后把每个子任务分支推送到该远程。

返回值：无（`None`）。副作用：

- 调用 `run_subtask` 执行每个子任务（产生 git worktree、分支、tag，并运行 Claude Code 子进程）。
- 每个子任务完成后写 `task_dir/<sub_id>/result.json`。
- 结束（或全部已完成）时写 `task_dir/meta.json`。
- 修改仓库 git 配置 `gc.auto`（执行前置 0，结束后恢复）。
- 可选执行 `git push`；清理 worktree（`git worktree remove --force` + `prune`）和 tag（`git tag -d`）。
- 中断时调用 `sys.exit(0)` 退出进程。
- 通过 `get_default_console()` 输出最终报告。

## 关键逻辑与流程

1. **初始化**（`pipeline.py:18-27`）：补默认状态容器，取 `task_id`，创建 `meta_lock`（保护 results/meta 写入）与 `active_pids` + `active_pids_lock`（跟踪子进程 PID，供中断时 SIGKILL）。统计已有 `degraded`/`no_changes` 数量到 `degraded_count`（**该计数后续未被使用**）。
2. **禁用 gc.auto**（`pipeline.py:30-36`）：若 `repo/.git` 存在，调 `_set_gc_auto(repo, "0")` 并记录原值，避免并发 worktree 操作共享对象库时的 gc 竞态。
3. **注册信号处理器**（`pipeline.py:39-54`）：`_on_interrupt` 闭包仅设置 `_interrupted` 事件并对 `active_pids` 中 PID 发 `SIGKILL`（async-signal-safe，不做 I/O）；同时替换 SIGINT 和 SIGTERM 处理器，保存原处理器以便恢复。
4. **跳过已完成**（`pipeline.py:57-62`）：过滤 `completed_ids`；若全部完成，直接置 `meta["status"]="completed"`、写 `meta.json`，恢复信号处理器与 gc.auto 后返回（2026-07-23 前此路径不恢复，见 docs/ISSUES.md ISSUE-4）。
5. **拓扑波次循环**（`pipeline.py:68-132`）：
   - 每轮从 `remaining` 中挑出所有 `depends_on` 均已完成的子任务组成 wave；若 wave 为空说明依赖循环或依赖无法满足，记 error 并 `break`（`pipeline.py:71-73`，**注意：break 后仍走正常清理与完成报告流程，meta 可能被标为 completed**）。
   - `actual_workers = min(parallel, len(wave))`（`parallel > 1` 时），否则为 1。
   - 串行路径（`pipeline.py:78-91`）：逐个调用 `run_subtask`，传入 `upstream`（依赖子任务的 worktree 路径映射 `{dep_id: worktree_map[dep_id]}`）；在 `meta_lock` 下更新 `worktree_map`（固定记为 `task_dir/<id>/work`）、`results_map`，并单独写 `result.json`。
   - 并发路径（`pipeline.py:92-117`）：`ThreadPoolExecutor` 提交整个 wave，`as_completed` 收集结果；future 抛异常时构造 `status="failed"`、`exit_code=-1` 的兜底 result（`pipeline.py:103-107`），其余处理同串行。
   - 中断检测（`pipeline.py:120-129`）：wave 结束后若 `_interrupted` 已置位，写 `meta.json`（`status="paused"`）、恢复信号处理器与 gc.auto，然后 `sys.exit(0)`。
6. **远程推送**（`pipeline.py:138-159`）：`remote_url` 非空且有 `.git` 时，对每个子任务先用 `git branch --list agent_go/<task_id>/<sub_id>` 检查分支存在（worktree 创建失败走 clone 降级时分支可能不存在），存在则 `git push <remote_url> <branch>:<branch>`；失败仅 warning 计数，不中断。
7. **清理**（`pipeline.py:162-196`）：有 `.git` 时，对每个子任务若 `task_dir/<id>/work` 存在则 `_worktree_remove`，随后 `_worktree_prune`；再逐个 `git tag -d <task_id>/<sub_id>` 删除 executor 打的 tag；最后恢复 gc.auto 原值。所有清理失败仅记日志，不抛异常。
8. **收尾**（`pipeline.py:199-216`）：汇总 `meta["results"]`（按 `confirmed` 顺序），任一 result 为 `failed` 则 `meta["status"]="failed"` 否则 `"completed"`，写 `meta.json`；控制台打印带状态图标（✅/⏭️/❌/⏳）的最终报告和产物路径。

## 依赖关系

内部模块（`pipeline.py:7-9`）：

- `console.get_default_console()` — 获取模块级 Console 实例，用于打印完成信息/最终报告。
- `executor.run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees, headless, issue_ref, active_pids, active_pids_lock)` — 执行单个子任务（`agent_go/executor.py:376`）；并发路径以位置参数提交，返回 result dict（字段见下节）。
- `git_utils._set_gc_auto(repo, value) -> (original_value, ok, err)`（`agent_go/git_utils.py:76`）。
- `git_utils._worktree_remove(repo, worktree_path) -> (ok, err)`（`agent_go/git_utils.py:52`）。
- `git_utils._worktree_prune(repo) -> (ok, err)`（`agent_go/git_utils.py:65`）。

外部依赖：

- CLI 命令：`git`（config gc.auto、branch --list、push、worktree remove/prune、tag -d，经 `subprocess.run` 调用）；Claude Code 子进程由 executor 间接启动。
- 环境变量：无直接读取。
- 文件系统：`repo/.git`（能力探测）；`task_dir/meta.json`、`task_dir/<sub_id>/result.json`、`task_dir/<sub_id>/work`（worktree 路径）、`task_dir/execution.log`（仅打印路径，写入由调用方/executor 负责）。
- 信号：SIGINT、SIGTERM；`os.kill(pid, SIGKILL)`。

## 数据结构与持久化

子任务 dict（`confirmed` 元素）：`id`、`title`、`description`、`depends_on: list[str]`。

result dict（`run_subtask` 返回值，落盘为 `result.json`）：`subtask_id`、`status`（`completed` / `no_changes` / `failed` / `degraded`）、`exit_code`、`summary`、`worktree`、`sandbox_type`、`verify_ok`、`duration_sec`。并发异常兜底构造见 `pipeline.py:104-106`。

`meta` dict（落盘为 `task_dir/meta.json`，`indent=2, ensure_ascii=False`）：至少含 `task_id`、`status`（`running` → `completed` / `failed` / `paused`）；收尾时追加 `results` 数组。

git 命名约定（与 executor 隐式耦合）：分支 `agent_go/<task_id>/<sub_id>`，tag `<task_id>/<sub_id>`，worktree 目录 `task_dir/<sub_id>/work`。

## 错误处理与边界情况

- 子任务级失败不中断管线：result 标 `failed`，最终 meta 状态置 `failed`；并发路径中 future 抛异常被捕获并转为 failed result。
- 依赖循环/不可满足：记 error 后 `break`，剩余子任务在报告中显示"未执行"（⏳），但 meta 仍可能被写为 `completed`（若无 failed）。
- 中断（SIGINT/SIGTERM）：信号处理器只做置标志 + SIGKILL 子进程；主循环在 wave 边界检测后写 `paused` 状态并 `sys.exit(0)`，可用 `agent_go resume <task_id>` 恢复。**注意中断发生在 wave 边界，正在运行的子任务被强杀，其结果不会落盘。**
- 推送/清理/tag 删除/gc 恢复失败：均仅记 warning/debug，不影响主流程与退出码。
- 恢复模式：`completed_ids` 非空时跳过已完成子任务；全部完成时提前返回并直接标 `completed`。

## 测试覆盖

- `tests/test_pipeline.py`（7 个用例，mock `run_subtask` / `_set_gc_auto` / `_worktree_remove` / `_worktree_prune` / `subprocess.run`）：串行顺序、并行执行、依赖顺序与 upstream 传递、gc.auto 禁用/恢复、恢复时跳过已完成、信号处理器不写 I/O 不 exit（仅置标志 + kill）、管线后 worktree 清理调用。
- `tests/test_integration.py`：多处直接调用 `_run_pipeline` 做集成验证（含并发与串行对比、resume 流程），另有 `agent_go.cli._run_pipeline` 的 patch 点。
- 未覆盖：远程推送分支、`degraded` 状态统计、依赖循环 break 路径、中断时 `sys.exit(0)` 主循环路径。

## 维护注意事项

- `config` 参数当前未被函数体使用（`pipeline.py:15`），属保留参数，改签名需同步 `cli.py:297`、`cli.py:385` 及测试 patch 点。
- `degraded_count`（`pipeline.py:26,86,112`）统计后从未消费，疑似遗留代码或半成品告警逻辑。
- 已修复（2026-07-23，docs/ISSUES.md ISSUE-4）：提前返回路径（`pipeline.py:58-62`）曾不恢复 gc.auto 与信号处理器，导致仓库 config 残留 `gc.auto=0`；现已补齐恢复逻辑。`try/finally` 统一清理的重构仍留作改进。
- 已修复（2026-07-23，docs/ISSUES.md ISSUE-7）：依赖循环 break 后未执行子任务曾导致 meta 误标 `completed`；现 wave 为空时将剩余子任务标记 `failed` 写入 `results_map`。
- 依赖循环时 `break` 后仍走清理与"全部完成"报告，meta 可能错误地标为 `completed`，且控制台打印 🎉 全部完成（实际未执行完的显示"未执行"）。
- `worktree_map[st["id"]]` 无条件记为 `task_dir/<id>/work`（`pipeline.py:83,109`），并未使用 result 中的 `worktree` 字段；若 executor 走了 clone 降级（无 worktree），此处路径与实际不符（清理时有 `exists()` 兜底，但 upstream 传递可能给下游错误路径）。
- 清理与推送依赖 git 命名约定（分支 `agent_go/<task_id>/<sub_id>`、tag `<task_id>/<sub_id>`），与 `executor.py` 中的命名是隐式字符串耦合，改一处必须同步另一处。
- `signal.signal` 只能在主线程调用，`_run_pipeline` 不可放入工作线程执行。
- 中断时 SIGKILL 由 `active_pids` 集合驱动，依赖 executor 正确登记/注销 PID；该集合与锁由本模块创建并以位置参数传入 `run_subtask`，参数顺序耦合（串行用关键字、并发用位置参数，见 `pipeline.py:81` vs `pipeline.py:97`）。
- 改进建议：消费或删除 `degraded_count`；提前返回路径补 gc/信号恢复；依赖循环时 meta 标 `failed`；`worktree_map` 改用 result["worktree"] 真实值；把巨型函数按"调度 / 推送 / 清理 / 报告"拆分（`docs/CODE_REVIEW.md` 已记录此建议，圈复杂度 13）。
