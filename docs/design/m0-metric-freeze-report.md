# M0-9 Metric Freeze 报告

> 状态：冻结实现（M0-9）
> 更新日期：2026-08-08

生成命令：

```bash
agent_go eval metric-freeze \
  --results eval_suite/baselines/<source_batch>/results.jsonl \
  --source-batch <source_batch> \
  --suite decision \
  --report-output eval_suite/baselines/<source_batch>/summary.json
```

报告固定包含：

- `metric_freeze_version`、`bench_schema_version`
- 不可变 `source_batch`
- `task_catalog_hash`、`config_hash`
- `suite`、`models`、`repeat`、任务数和 record 数
- elapsed/runtime 时间范围
- 普通任务与 stress 任务的 failure class 分布
- Accepted Delivery Rate、PR 创建率、Delivery Failure Rate、Cost per Accepted Delivery
- 有效任务分母、排除数量和排除原因

报告拒绝空批次、混合 `source_batch`、混合 suite、缺少 catalog 或 schema 无效的结果。`stress` 或 `high_variance` 任务单独放入 `stress_metrics`，不进入普通 `metrics`。
