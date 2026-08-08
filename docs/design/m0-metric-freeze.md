# M0-6 指标公式冻结

> 状态：冻结（M0-6）
> 更新日期：2026-08-08

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
