# M0-6 指标公式冻结

> 状态：冻结（M0-6）；2026-10-05 增补"诊断字段口径登记"（**不改主指标公式**）
> 更新日期：2026-08-08（诊断字段口径登记：2026-10-05）

## 任务集合

同一计算批次只聚合同一 `suite` 和 `source_batch`。有效任务满足：

- `valid_task` 未显式设为 `false`，且未被 `excluded`。
- `failure_class` 不属于 `budget_abort`、`infrastructure_failure`、`user_cancelled`、`system_error`。
- `model_failure`、`verification_failure`、`timeout` 和 `delivery_failure` 保留在产品指标分母中。

有效成本是有效任务的 `total_cost_usd` 之和。被排除任务仍保留在审计和 failure class 分布中，但不进入产品 KPI 分母或有效成本。

## 主指标

```text
Accepted Delivery Rate
= accepted_delivery_count / valid_task_count
```

```text
Cost per Accepted Delivery
= valid_cost / accepted_delivery_count
```

无分母时返回 `null`，不能返回 0。

## 辅助指标

所有 Rate 返回 `[0, 1]` 的小数：

- `First-pass Rate` = `binary_pass=true AND total_retries=0` 的有效任务数 / 有效任务数。
- `Time to Accepted Delivery` = Accepted Delivery 任务 `elapsed_sec` 的算术平均值。
- `Human Intervention Minutes` = 有效任务 `human_intervention_minutes` 之和，缺失按 0。
- `Timeout Rate` = `failure_class=timeout` 的有效任务数 / 有效任务数。
- `Retry Rate` = `total_retries>0` 的有效任务数 / 有效任务数。
- `Delivery Failure Rate` = `failure_class=delivery_failure` 的有效任务数 / 有效任务数。

部分子任务完成率不构成产品成功率，也不替代 `accepted_delivery`。

## 诊断指标

旧 `$ / pass` 降级为诊断指标：

```text
pass_rate_diagnostic = sum(pass_rate) / valid_task_count
dollar_per_pass_diagnostic = valid_cost / sum(pass_rate)
```

它只能在相同 `suite`、相同 `source_batch` 内比较，不能作为产品主 KPI、交付成功率或跨批次排名依据。

实现入口为 `agent_go.metrics.compute_frozen_metrics()`，结果中同时输出有效分母、排除原因和 failure class 分布，保证重复计算得到相同结果。

## 诊断字段口径登记：`plan_acceptance_coverage`（2026-10-05）

`plan_acceptance_coverage`（`agent_go/planning.py` `validate_plan_quality`，经 `cli.py`/`bench.py` 透传）**不进主指标分母**，但其口径在 2026-10-05 发生过变更，按冻结纪律在此登记：

| 记录形态 | 口径 |
|---|---|
| **无** `plan_coverage_basis` 键（旧批） | 仅在计划含 spec REQ/AC ID 时给出 ID 级验收覆盖率，否则 `null`；`null`＝"未产出"，**不等于 0** |
| `plan_coverage_basis="acceptance_ids"` | ID 级覆盖率（与旧口径一致） |
| `plan_coverage_basis="structural"` | 结构性验收覆盖：带验证命令的子任务里"未锚定到核心文件、或验证非 suite 级"的占比（S-1 前瞻可测面） |
| `plan_coverage_basis=null` | 无任何带验证命令的子任务 ⇒ 覆盖率 `null` |

使用规则：

- **跨记录比较前先按 `plan_coverage_basis` 是否存在分段**——变更跨批生效，一批 `results_*.jsonl` 可能同时含两种口径，混算即错。生效边界以**记录是否携带该键**机械判定，不按日期假设。
- 该字段与 `plan_requirement_coverage` 同属规划质量**诊断**面：不得单独用作发布门（发布门仍为 `eval gate`），也不得进入跨批排名。
- 治理状态：口径变更已登记，**待 P-1 追认**（见 [three-project-architecture-review-20260819.md](three-project-architecture-review-20260819.md) §4）。
