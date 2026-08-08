# M0-3 Failure Class 契约

> 状态：冻结（M0-3）
> 更新日期：2026-08-08

## 固定分类

`failure_class` 只能是：

```text
model_failure
verification_failure
timeout
budget_abort
infrastructure_failure
delivery_failure
user_cancelled
system_error
```

`kill_reason` 保留为低层运行时证据，不作为跨批次聚合键。映射由
`agent_go.failure.classify_failure()` 固定执行。

## 规则矩阵

| class | 能力失败分母 | 成本 | 允许 resume | 保留 worktree |
|---|---:|---:|---:|---:|
| model_failure | 是 | 是 | 是 | 是 |
| verification_failure | 是 | 是 | 是 | 是 |
| timeout | 是 | 是 | 是 | 是 |
| budget_abort | 否 | 是 | 是 | 是 |
| infrastructure_failure | 否 | 是 | 是 | 是 |
| delivery_failure | 否 | 是 | 是 | 是 |
| user_cancelled | 否 | 是 | 否 | 是 |
| system_error | 否 | 是 | 是 | 是 |

基础设施失败、预算中止、用户取消和系统错误不得计入模型能力失败分母；预算中止不得映射为模型或验证失败。已产生的成本仍保留在成本审计中，是否进入产品成本指标由有效任务规则另行控制。

## 聚合与 Timeout

`agent_go.metrics.aggregate_failure_classes()` 始终输出八类完整计数（没有记录的类别为 0）、各类成本、有效任务分母、排除数量和排除原因。Bench 模型级与批次级报告都保留这份摘要，不把 `infrastructure_failure`、`budget_abort` 或 `timeout` 合并成 `model_failure`。

- 产品指标中 `timeout` 是能力失败。
- `timed_out=true` 的记录同时标记为成本基线的右删失观测，不改变产品失败分类。
- `kill_reason=cleanup_race` 单独计数，表示子任务已完成且收尾竞态，不计为 timeout 失败。

## 优先级

任务级显式 `failure_class` 优先，其次是 `delivery_failed`、子任务 `kill_reason` 映射，最后才根据验证和进程结果推断。聚合时优先级为：用户取消、预算、超时、基础设施、系统、交付、验证、模型。
