# M0-2 任务状态机

> 状态：冻结（M0-2）
> 更新日期：2026-08-08

## 统一状态

```text
DRAFT
SPEC_REVIEW
ARCHITECTURE_REVIEW
PLAN_REVIEW
EXECUTING
VERIFYING
COMMITTED_UNVERIFIED
DELIVERY_READY
ACCEPTED_DELIVERY
VERIFICATION_FAILED
DELIVERY_FAILED
BLOCKED
CANCELLED
```

任务级状态位于 `meta.json.status`，新任务同时写入 `status_schema_version: 1`。
`results[].status` 仍是子任务执行结果，不属于本状态机。

## 迁移表

| 当前状态 | 事件 | 下一状态 |
|---|---|---|
| DRAFT | spec accepted | SPEC_REVIEW |
| SPEC_REVIEW | architecture accepted | ARCHITECTURE_REVIEW |
| ARCHITECTURE_REVIEW | plan accepted | PLAN_REVIEW |
| PLAN_REVIEW | execution started | EXECUTING |
| EXECUTING | verification started | VERIFYING |
| VERIFYING | commit exists, verification pending | COMMITTED_UNVERIFIED |
| VERIFYING | all verification passed, delivery pending | DELIVERY_READY |
| DELIVERY_READY | delivery branch and PR/merge valid | ACCEPTED_DELIVERY |
| DELIVERY_READY | delivery gate failed | DELIVERY_FAILED |
| VERIFYING | verification failed | VERIFICATION_FAILED |
| EXECUTING / VERIFYING | upstream failure | BLOCKED |
| any non-terminal state | user cancellation | CANCELLED |
| interrupted legacy task | resume/recover | EXECUTING or COMMITTED_UNVERIFIED |

`ACCEPTED_DELIVERY`、`VERIFICATION_FAILED`、`DELIVERY_FAILED`、`BLOCKED`、`CANCELLED` 是终态；`resume` 只能从可恢复状态重新进入 `EXECUTING`，不能把终态静默改成成功。

## 旧状态迁移

旧 `status` 仍可被读取。显式迁移时通过 `agent_go.status.migrate_meta_status()` 归一化；未迁移的历史任务保留原展示值，避免接口兼容回归：

| 旧值 | 新值 |
|---|---|
| `running`, `interrupted`, `stale_aborted` | `EXECUTING` |
| `completed` | `DELIVERY_READY` |
| `failed` | `VERIFICATION_FAILED` |
| `paused` | `PLAN_REVIEW` |
| `cancelled` | `CANCELLED` |

迁移后的原值写入 `legacy_status`，不再作为跨接口展示状态。
