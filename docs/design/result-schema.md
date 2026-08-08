# result.json Schema 参考文档

> 状态：As-Built（对应 `executor.py` `run_subtask` 返回值 + `pipeline.py` synthetic 路径）
> 更新日期：2026-08-08
> 关联：[ADR-002](adr/ADR-002-completion-boundaries.md) 完成边界、[ADR-005](adr/ADR-005-cost-control-layers.md) 成本控制、[data-architecture-and-flow.md](data-architecture-and-flow.md) §10 数据契约

本文档定义每个子任务结果文件 `~/.agent_go/task-<id>/sub-<n>/result.json` 的完整字段。结果同时镜像到 `meta.json` 的 `results[]` 数组。

---

## 持久化时机

- **执行路径**：`run_subtask` 返回后，`pipeline._record_subtask_result` 原子写入 `task_dir/<sub_id>/result.json`，然后镜像到 `meta.json results[]`（replace by `subtask_id` 或 append）
- **Synthetic 路径**：子任务未到达 `run_subtask`（被 blocked / skipped），pipeline 写入精简结果

---

## 字段总表

### 总是存在的字段

| 字段 | 类型 | 空值语义 | 来源 | 说明 |
|---|---|---|---|---|
| `subtask_id` | str | — | executor / synthetic | 子任务 ID |
| `status` | enum | — | executor / synthetic | `completed` / `no_changes` / `failed` / `blocked` / `degraded` |
| `exit_code` | int | — | executor / synthetic | 子进程返回码（synthetic 为 `-1`） |
| `summary` | str | — | executor / synthetic | 人类可读摘要 |
| `failure_reason` | str | `""` (成功时) | executor / synthetic | 失败原因 |
| `worktree` | str | `""` (synthetic) | executor | worktree 路径 |
| `sandbox_type` | str | — | executor | 沙箱类型（`headless` / `greywall`） |
| `verify_ok` | bool | — | executor | 验证是否通过 |
| `duration_sec` | float | `0.0` (synthetic) | executor | 执行时长（秒） |
| `commit_hash` | str | `""` (无提交) | executor | Git commit hash |
| `failure_class` | str | — | pipeline `classify_failure` | 失败分类（setdefault） |

### 仅执行路径存在的字段

| 字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `retry_count` | int | executor | 验证重试次数 |
| `timing` | dict | executor `collect_timing` | 计时明细（claude_execute_ms / verification_ms / git_commit_ms / total_ms） |
| `change_stats` | dict | executor | 变更统计（files_changed / insertions / deletions） |
| `merge_results` | list | executor | 上游 tag merge 结果 |
| `verification_results` | list | executor | 每次验证的详细结果 |
| `verification_confidence` | dict | executor | 验证置信度 |
| `kill_reason` | str\|null | executor | 终止原因（见 kill_reason 归因表） |
| `degraded` | bool | executor | 是否经历了模型降级 |
| `agent_type_source` | str | executor | agent 类型来源（`default` / `plan` / `config`） |
| `skills_unresolved` | list | executor | 未解析到的 Skill 名称 |

### 仅 Synthetic 路径存在的字段

| 字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `blocked_by` | list | pipeline | 阻断来源（如 `["cost_control"]` / 上游子任务 ID 列表） |

---

## status 枚举值

| status | 含义 | 触发条件 |
|---|---|---|
| `completed` | 子任务成功完成 | verify_ok=True AND git_ok=True AND exit_code=0 |
| `no_changes` | Claude 未产生变更 | worktree 干净，无 commit |
| `failed` | 子任务失败 | verify_ok=False OR git_ok=False OR exit_code≠0 |
| `blocked` | 被上游失败或预算阻断 | 上游 failed 或 L3/reservation 超限 |
| `degraded` | 成本降级后完成 | budget_mode=degrade 触发降级后成功 |

---

## kill_reason 归因

| kill_reason | 含义 | 归类 | 计入模型能力分母 |
|---|---|---|---|
| `cleanup_race` | 清理竞态 | 正常 | 否（标 completed） |
| `stuck` | Claude 卡住 | 能力 | **是** |
| `hard_timeout` | 超时 | 能力 | **是** |
| `over_budget_l2` | L2 熔断 | 成本 | 否 |
| `over_budget_l3` | L3 熔断 / reservation 不足 | 成本 | 否 |
| `metering_unavailable` | metering 写入失败 | 基础设施 | 否 |
| `system_error` | 内部异常 | 基础设施 | 否 |
| `infra` | API 故障 / 网络 | 基础设施 | 否 |
| `None` | 未触发 kill | — | — |

---

## Synthetic 结果变体

### 上游失败阻断

```json
{
  "subtask_id": "sub-3",
  "status": "blocked",
  "exit_code": -1,
  "summary": "上游子任务失败",
  "blocked_by": ["sub-1"],
  "failure_reason": "上游 sub-1 failed",
  "worktree": "",
  "sandbox_type": "headless",
  "verify_ok": false,
  "duration_sec": 0,
  "failure_class": "upstream_failure"
}
```

### L3 预算熔断

```json
{
  "subtask_id": "sub-5",
  "status": "blocked",
  "exit_code": -1,
  "summary": "L3 预算熔断",
  "blocked_by": ["cost_control"],
  "failure_reason": "任务累计成本超过预算上限",
  "kill_reason": "over_budget_l3",
  "worktree": "",
  "sandbox_type": "headless",
  "verify_ok": false,
  "duration_sec": 0,
  "failure_class": "budget_abort"
}
```

### 预算 reservation 不足

```json
{
  "subtask_id": "sub-6",
  "status": "blocked",
  "exit_code": -1,
  "summary": "预算 reservation 不足，未启动子任务",
  "blocked_by": ["cost_control"],
  "failure_reason": "并发启动前预算不足",
  "kill_reason": "over_budget_l3",
  "worktree": "",
  "sandbox_type": "headless",
  "verify_ok": false,
  "duration_sec": 0,
  "failure_class": "budget_abort"
}
```

### 依赖循环

```json
{
  "subtask_id": "sub-7",
  "status": "failed",
  "exit_code": -1,
  "summary": "依赖循环或无法满足的依赖，未执行",
  "failure_reason": "依赖循环",
  "worktree": "",
  "sandbox_type": "headless",
  "verify_ok": false,
  "duration_sec": 0,
  "failure_class": "dependency_error"
}
```

### metering 不可用

```json
{
  "subtask_id": "sub-8",
  "status": "blocked",
  "exit_code": -1,
  "summary": "metering 不可用，成本控制无法运行",
  "blocked_by": ["cost_control"],
  "kill_reason": "metering_unavailable",
  "worktree": "",
  "sandbox_type": "headless",
  "verify_ok": false,
  "duration_sec": 0,
  "failure_class": "infrastructure_failure"
}
```

---

## .preserved 标记

当 worktree 被保留（failed / blocked 子任务）时，worktree 目录下写入 `.preserved` 文件：

```json
{
  "subtask_id": "sub-2",
  "status": "failed",
  "failure_reason": "verification failed after 3 retries",
  "branch": "agent_go/task-abc123/sub-2",
  "degraded": false,
  "kill_reason": "stuck"
}
```

> **注意**：`branch` 字段仅出现在 `.preserved` 中，不在 `result.json` 主体中（`data-architecture-and-flow.md` 文档中列出的 `branch` 字段实际仅在 `.preserved` 中存在）。

---

## 已知不一致

### `claude_cost` 字段（死读）

`pipeline.py:850` 和 `:873` 中有 `r.get("claude_cost", 0)` 的聚合逻辑，但**没有任何代码向 result dict 写入 `claude_cost` 字段**。实际成本数据在 `metering.jsonl` 中，不在 `result.json` 中。该聚合始终返回 0，是已知的死读代码。

### 文档 vs 实际字段集差异

`architecture.md` 和 `data-architecture-and-flow.md` 文档中列出的 `results[]` 核心字段（`subtask_id, status, verify_ok, retry_count, commit_hash, branch, worktree, failure_reason, kill_reason, failure_class, verification_results, verification_state`）是**文档子集**，实际代码写入的字段更丰富（见上方完整字段表）。
