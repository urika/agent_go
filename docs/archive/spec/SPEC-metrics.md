# metrics 模块规格说明

## 概述

`metrics` 是 Plan Mode 执行流水线的结构化指标采集模块，为每个子任务（subtask）在独立 git worktree 中的执行过程产出标准化指标 dict。它不读写文件、不维护状态，是纯函数式的数据采集层：耗时采集、git 变更统计、上游 merge 结果、API token 用量四类指标各由一个函数负责。产出的指标由 `executor.py` 汇总进子任务结果字典（`timing` / `change_stats` / `merge_results` 字段），供上层评估与日志使用。模块仅依赖 Python stdlib。

## 公共接口

模块通过 `__all__`（`agent_go/metrics.py:5-8`）导出 4 个公共函数，无私有函数。

### `collect_timing(worktree_create_ms, merge_upstream_ms, claude_execute_ms, verification_ms, git_commit_ms) -> dict[str, float]`

- 位置：`agent_go/metrics.py:10-18`
- 参数：5 个 `float`，分别为 worktree 创建、上游 merge、Claude 执行、验证、git 提交五个阶段的耗时（毫秒）。
- 返回：同名字段的 dict，每个值经 `round()` 取整为整数（注意返回类型标注为 `dict[str, float]`，但实际是 `int`）。
- 副作用：无。

### `collect_change_stats(worktree_path: Path) -> dict[str, Any]`

- 位置：`agent_go/metrics.py:21-59`
- 参数：`worktree_path` — 子任务 worktree 的路径。
- 返回：变更统计 dict，字段：
  - `files_changed: int` — 去重后的变更文件总数（tracked 变更 + untracked 新文件）
  - `insertions: int` / `deletions: int` — 来自 `git diff --numstat HEAD` 的累计行数；二进制文件（numstat 显示 `-`）计 0
  - `new_files: int` — `git status --porcelain` 中以 `??` 开头的未跟踪文件数
  - `modified_files: int` — `files_changed - new_files`
  - `actual_files: list[str]` — 去重后的文件路径列表（numstat 第 3 列 + porcelain 中 `??` 行的文件名）
- 副作用：在 `worktree_path` 下执行两个只读 git 命令（`git diff --numstat HEAD`、`git status --porcelain`）。

### `collect_merge_result(upstream_id: str, success: bool, conflict_files: Optional[list[str]] = None) -> dict[str, Any]`

- 位置：`agent_go/metrics.py:62-66`
- 参数：`upstream_id` 上游子任务 ID；`success` merge 是否成功；`conflict_files` 冲突文件列表（可选）。
- 返回：`{"upstream": upstream_id, "status": "success" | "conflict"}`；仅当 `conflict_files` 为 truthy 时附加 `conflict_files` 字段（空列表不会写入）。
- 副作用：无。

### `extract_usage(api_response: dict[str, Any], provider: str, model: str) -> dict[str, Any]`

- 位置：`agent_go/metrics.py:69-76`
- 参数：`api_response` API 响应 dict（读取其 `usage.input_tokens` / `usage.output_tokens`）；`provider`、`model` 原样透传。
- 返回：`{"prompt_tokens": int, "completion_tokens": int, "model": str, "provider": str}`；缺失字段默认 0。
- 副作用：无。
- 注意：当前**未被任何生产代码调用**（仅测试与设计文档引用），属于预置接口，见「维护注意事项」。

## 关键逻辑与流程

### `collect_change_stats` 的两阶段统计（核心逻辑）

1. **tracked 变更统计**（`agent_go/metrics.py:27-38`）：运行 `git diff --numstat HEAD`，逐行按 `\t` 切分，要求至少 3 列；第 1、2 列累加为 insertions/deletions（值为 `"-"` 即二进制文件时跳过累加），第 3 列加入 `actual_files`。
2. **untracked 新文件统计**（`agent_go/metrics.py:40-50`）：运行 `git status --porcelain`，仅识别以 `??` 开头的行计入 `new_files`；文件名取 `line[3:]`，若不在 `actual_files` 中则追加（去重）。
3. **汇总**（`agent_go/metrics.py:52-59`）：`files_changed = len(actual_files)`，`modified_files = files_changed - new_files`。

调用方语义（`agent_go/executor.py:246-250`）：executor 仅在 `has_changes` 为真时调用本函数，否则直接构造全零 dict —— 即「无变更」路径绕过本模块。

### 调用链中的位置

- `executor.py:409-410`：对每个上游子任务 merge 后，依据 `.MERGE_CONFLICT` 标记文件是否存在调用 `collect_merge_result`；冲突文件列表来自标记文件内容按行拆分。
- `executor.py:484-485`：子任务执行收尾时用各阶段计时调用 `collect_timing`（`claude_execute_ms` 由秒换算为毫秒并取整）。
- 结果组装：`executor.py:493-495` 将 `timing` / `change_stats` / `merge_results` 放入子任务结果 dict。

## 依赖关系

- **内部模块**：无 import；本模块是被依赖方。唯一调用方是 `agent_go/executor.py`（`executor.py:10` 导入前三个函数）。
- **stdlib**：`subprocess`（执行 git 命令）、`pathlib.Path`（类型标注）、`typing.Any/Optional`。
- **外部 CLI**：`git`（仅 `collect_change_stats` 使用，命令为 `git diff --numstat HEAD` 与 `git status --porcelain`，均只读、无 `check=True`）。
- **环境变量 / 文件系统路径**：无直接依赖；git 命令的工作目录由调用方传入的 `worktree_path` 决定。

## 数据结构与持久化

无持久化。模块不写任何文件，产出均为内存中的 dict，结构见「公共接口」各函数返回值说明。指标的最终落盘由调用方（executor 的结果 dict 及其上游）负责。

## 错误处理与边界情况

- **git 命令失败不防御**：`collect_change_stats` 中的两次 `subprocess.run` 均未设 `check=True`，也未包裹 try/except。git 不存在（`FileNotFoundError`）会直接向上抛出（测试 `test_subprocess_failure` 已锁定该行为）；git 返回非零时静默忽略，按空输出统计。
- **二进制文件**：numstat 输出 `-` 时 insertions/deletions 计 0，但文件仍计入 `actual_files`。
- **已暂存新文件不识别**：porcelain 中 `A ` 开头的行不视为新文件（只有 `??` 才算）。若文件已 `git add` 但未提交，它也不出现在 `git diff --numstat HEAD` 的 diff 输出之外时可能漏统计——不过调用点在 `git add -A` 之前执行（`executor.py:246` 先于 `executor.py:256` 的 add），实际执行流程中不会触发。
- **`collect_merge_result` 空列表**：`conflict_files=[]` 是 falsy，不会写入结果 dict，与 `None` 行为一致。
- **`extract_usage` 缺失字段**：`usage` 整体缺失或部分字段缺失时默认 0，不抛异常。

## 测试覆盖

对应测试文件：`tests/test_metrics.py`（209 行，4 个测试类，宣称"全覆盖"）。

- `TestCollectTiming`：字段完整性、零值、`round()` 取整（含 `.499`/`.501` 边界）、返回 dict 形状。
- `TestCollectChangeStats`：mock `subprocess.run`，覆盖有变更、无变更、仅新文件（`??`）、二进制 numstat（`-`）、git 不存在时 `FileNotFoundError` 传播。
- `TestCollectMergeResult`：成功/失败、带与不带冲突文件、空冲突列表不写入。
- `TestExtractUsage`：正常响应、`usage` 缺失、部分字段缺失默认值。

测试未覆盖：`actual_files` 去重路径（同一文件同时出现在 numstat 与 porcelain 的情况）、porcelain 中带引号的文件名。

## 维护注意事项

- **`extract_usage` 是死代码**：已列入 `__all__` 并有测试，但生产代码中无任何调用。`docs/design/PRD-项目评估体系设计.md`（A4 项）规划它用于解析 `api.py` 的 `call_api` 响应并在 plan 阶段记录，属于未落地的预置接口；删除或接线前先确认该规划状态。
- **类型标注不准确**：`collect_timing` 标注返回 `dict[str, float]`，`round(float)` 实际返回 `int`；`dict[str, Any]` 更准确。
- **无防御的 subprocess**：两处 `subprocess.run` 不检查 `returncode`，git 异常输出（如非仓库目录）会被静默当作"无变更"。测试已把 `FileNotFoundError` 传播固化为预期行为，若未来要加容错需同步改测试。
- **解析脆弱点**：
  - numstat 按 `\t` 切分，含特殊字符的文件名依赖 git 的转义规则，未做反转义处理；
  - porcelain 取 `line[3:]` 且未处理带引号路径（如含空格/中文文件名 git 默认加引号）；
  - `modified_files = files_changed - new_files` 依赖「numstat 与 porcelain 文件集不重叠」的隐含假设。
- **与 executor 的隐式耦合**：`collect_change_stats` 必须在 `git add -A` 之前调用才能正确识别 `??` 新文件；executor 在 `has_changes=False` 时手工构造全零 dict，与本模块的零值输出结构必须保持字段一致（两处字段列表是硬编码重复，改字段需同步）。
- **耗时口径**：`collect_timing` 只做取整，不做单位换算；`claude_execute_ms` 的秒→毫秒换算发生在调用方（`executor.py:484`），其余四个值由调用方以毫秒传入，口径一致性靠调用方自觉。
