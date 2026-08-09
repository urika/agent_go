# M0-2 任务状态机

> 状态：冻结（M0-2），v2 精简（2026-08-08）
> 对应代码：`agent_go/status.py`

## 状态枚举（8 个）

```text
EXECUTING          ← 运行中 / 待 resume / recover 发现未完成
PAUSED             ← 被中断/暂停（SIGINT/SIGTERM），可 resume 恢复

DELIVERY_READY     ← 全部子任务完成，交付待执行
ACCEPTED_DELIVERY  ← 交付门通过（终态）
DELIVERY_FAILED    ← 交付门失败（终态）
VERIFICATION_FAILED← 能力失败（终态）
BLOCKED            ← 约束阻断（终态）：plan 质量 / cost / metering / 依赖环
CANCELLED          ← 用户取消（终态）
```

`results[].status` 仍是子任务执行结果，不属于本任务级状态机。

## 状态语义

### 非终态

| 状态 | 含义 | 可转入终态 | resume 合法 |
|---|---|---|---|
| `EXECUTING` | pipeline 运行中，或 recover 发现需 resume 的工作 | 全部 | 是 |
| `PAUSED` | 信号中断，无能力失败子任务。`failed_ids` 为空 | VERIFICATION_FAILED / BLOCKED / DELIVERY_READY / CANCELLED | 是 |
| `DELIVERY_READY` | 子任务全部完成，交付尚未执行 | ACCEPTED_DELIVERY / DELIVERY_FAILED | 否 |

### 终态（5 个，不再发生状态转移）

| 终态 | 触发条件 | 子任务失败特征 |
|---|---|---|
| `ACCEPTED_DELIVERY` | 交付门通过 | 无 failed，无 blocked |
| `VERIFICATION_FAILED` | 存在子任务能力失败 | `results[]` 中有 `status="failed"` |
| `BLOCKED` | 约束阻断，无能力失败 | 无 `failed`，仅有 `blocked`（cost/metering/plan quality/依赖环） |
| `DELIVERY_FAILED` | 交付门失败 | 无 failed（子任务成功但交付动作失败） |
| `CANCELLED` | 用户主动取消（MCP cancel） | 无关 |

**核心区分**：`VERIFICATION_FAILED` vs `BLOCKED` — 前者是模型执行能力不足（计入能力失败分母），后者是工程/成本约束阻断（不计入模型能力失败分母）。

## 迁移表

| 当前 | 事件 | 下一 |
|---|---|---|
| — | cmd_run | EXECUTING |
| EXECUTING | 全部成功 | DELIVERY_READY |
| EXECUTING | 能力失败 | VERIFICATION_FAILED |
| EXECUTING | 约束阻断 | BLOCKED |
| EXECUTING | SIGINT/SIGTERM, 无 failed | PAUSED |
| EXECUTING | SIGINT/SIGTERM, 有 failed | VERIFICATION_FAILED |
| PAUSED | resume | EXECUTING |
| PAUSED | resume 后能力失败 | VERIFICATION_FAILED |
| DELIVERY_READY | 交付门通过 | ACCEPTED_DELIVERY |
| DELIVERY_READY | 交付门失败 | DELIVERY_FAILED |
| any | MCP cancel | CANCELLED |
| any | recover | EXECUTING（发现未完成时）/ 终态保持不变（全部 completed 时） |

### recover 语义（任务级）

recover 扫描 worktree 状态后：
- 存在 dirty / reset_failed / no_changes / 未完成子任务 → `EXECUTING`（需 resume）
- 全部 `completed` 但验证未确认 → `EXECUTING`（需 resume 重验证）
- 有 `failed` → `VERIFICATION_FAILED`
- 全部 `completed` + 验证通过（M1-3，2026-08-09）：
  - meta 已有 `ACCEPTED_DELIVERY` / `DELIVERY_FAILED` → **保留原交付状态**（不降级）
  - 已有 `pr_url` / `explicit_merge_commit` → `ACCEPTED_DELIVERY`
  - 否则 → `DELIVERY_READY`（交付待执行，不再降级为 EXECUTING）

recover **不会**写入 `COMMITTED_UNVERIFIED` 作为任务级状态；子任务级 `committed_unverified` 仅保留在 `results[].status` 中。recover 写回 meta 时保留全部 delivery 字段（`delivery_branch` / `pr_url` / `pr_head` / `pr_base` / `explicit_merge_commit`）。

## 旧状态迁移

旧 `status` 仍可被读取。`migrate_meta_status()` 归一化，未迁移的历史任务保留原展示值：

| 旧值 | 新值 | 原因 |
|---|---|---|
| `running`, `interrupted`, `stale_aborted` | `EXECUTING` | 可 resume |
| `paused` | `PAUSED` | 可 resume |
| `completed` | `DELIVERY_READY` | 子任务完成 |
| `failed` | `VERIFICATION_FAILED` | 能力失败 |
| `draft`, `spec_review`, `architecture_review`, `verifying`, `committed_unverified` | `EXECUTING` | 已移除；降级为可 resume |
| `plan_review` | `BLOCKED` | 已合并入约束阻断 |

## v2 精简记录（2026-08-08）

从 v1（14 状态）精简至 v2（8 状态）。

**移除（4 个，从未写入）**：
- `DRAFT` — 仅 recover 内部变量
- `SPEC_REVIEW` / `ARCHITECTURE_REVIEW` — 纯文档假设
- `VERIFYING` — 验证是 executor 内部同步调用

**合并（2 个，一跳过渡态）**：
- `PLAN_REVIEW` → `BLOCKED` — plan quality 不足是约束阻断
- `COMMITTED_UNVERIFIED` → 降级为 `results[].status` 子任务标注；任务级 recover 后直接标 `EXECUTING`，resume 重新验证
