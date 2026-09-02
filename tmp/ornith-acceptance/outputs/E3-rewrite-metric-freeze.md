# M0-6 指标公式冻结 发布说明

## 新增

- 新增「有效任务」判定规则：`valid_task` 未显式设为 `false` 且未被 `excluded`，`failure_class` 不含 `budget_abort`、`infrastructure_failure`、`user_cancelled`、`system_error`；`model_failure`、`verification_failure`、`timeout`、`delivery_failure` 保留在产品指标分母中。
- 新增「有效成本」：为有效任务 `total_cost_usd` 之和；被排除任务仍保留在审计和 failure class 分布中，但不进入产品 KPI 分母或有效成本。
- 新增主指标 `Accepted Delivery Rate` = `accepted_delivery_count / valid_task_count`，及 `Cost per Accepted Delivery` = `valid_cost / accepted_delivery_count`；无分母时返回 `null`，不能返回 `0`。
- 新增辅助指标：`First-pass Rate`、`Time to Accepted Delivery`、`Human Intervention Minutes`、`Timeout Rate`、`Retry Rate`、`Delivery Failure Rate`，均为 `[0, 1]` 小数（`Time to Accepted Delivery` 除外）。

## 变更

- 同一计算批次只聚合同一 `suite` 和 `source_batch`。
- 旧 `$ / pass` 降级为诊断指标 `pass_rate_diagnostic` 与 `dollar_per_pass_diagnostic`；部分子任务完成率不构成产品成功率，也不替代 `accepted_delivery`。

## 修复

- 实现入口为 `agent_go.metrics.compute_frozen_metrics()`，结果同时输出有效分母、排除原因和 failure class 分布，保证重复计算得到相同结果；诊断指标只能在相同 `suite`、相同 `source_batch` 内比较，不能作为产品主 KPI、交付成功率或跨批次排名依据。