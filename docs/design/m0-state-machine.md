# M0-2 任务状态机

> 状态：冻结（M0-2）
> 更新日期：2026-08-08

## 统一状态

```text
DRAFT
SPEC_REVIEW
ARCHITECTURE_REVIEW
PLAN_REVIEW
PAUSED
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

语义区分（M0 状态机修复，2026-08-08）：
- **PLAN_REVIEW**：规划审查门（`ARCHITECTURE_REVIEW → plan accepted → PLAN_REVIEW → execution started → EXECUTING`）。仅表示"计划已获准、待执行"的短暂过渡。
- **PAUSED**：任务被中断/暂停（SIGINT/SIGTERM）时的可恢复锚点，`resume` 从 PAUSED 回 EXECUTING。**仅当中断时无确定性能力失败子任务**才标 PAUSED——若中断时已有 `failed` 子任务（retry 耗尽/验证未通过），终态优先标 `VERIFICATION_FAILED`（能力失败优先），因为 PAUSED 暗示"恢复后能继续"，但能力失败恢复后大概率仍失败。
- **BLOCKED**：纯约束/编排阻断（cost 熔断、metering 不可用、依赖环）——**无能力失败子任务**。若任务有 `failed` 子任务（含其级联的 blocked 下游），终态为 `VERIFICATION_FAILED`（能力失败优先），而非 BLOCKED。
- 此前运行时把中断暂停误写为 PLAN_REVIEW，已改为 PAUSED；BLOCKED 的判定优先级已修正（能力失败优先）；**PAUSED 同样遵循能力失败优先**——中断时若有 failed 子任务，标 VERIFICATION_FAILED 而非 PAUSED（2026-08-08 修复）。

## 迁移表

| 当前状态 | 事件 | 下一状态 |
|---|---|---|
| DRAFT | spec accepted | SPEC_REVIEW |
| SPEC_REVIEW | architecture accepted | ARCHITECTURE_REVIEW |
| ARCHITECTURE_REVIEW | plan accepted | PLAN_REVIEW |
| PLAN_REVIEW | execution started | EXECUTING |
| EXECUTING / VERIFYING | interrupted (SIGINT/SIGTERM), 无 failed 子任务 | PAUSED |
| EXECUTING / VERIFYING | interrupted (SIGINT/SIGTERM), 有 failed 子任务 | VERIFICATION_FAILED（能力失败优先）|
| PAUSED | resume | EXECUTING |
| EXECUTING | verification started | VERIFYING |
| VERIFYING | commit exists, verification pending | COMMITTED_UNVERIFIED |
| VERIFYING | all verification passed, delivery pending | DELIVERY_READY |
| DELIVERY_READY | delivery branch and PR/merge valid | ACCEPTED_DELIVERY |
| DELIVERY_READY | delivery gate failed | DELIVERY_FAILED |
| VERIFYING | verification failed | VERIFICATION_FAILED |
| EXECUTING / VERIFYING | 约束/编排阻断（cost/metering/依赖环，无 failed 子任务）| BLOCKED |
| EXECUTING / VERIFYING | 子任务能力失败（含级联 blocked）| VERIFICATION_FAILED |
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
| `paused` | `PAUSED` |
| `cancelled` | `CANCELLED` |

迁移后的原值写入 `legacy_status`，不再作为跨接口展示状态。
