# M0-4 Bench Schema

> 状态：冻结（M0-4）
> 更新日期：2026-08-08

## 版本

当前固定 `bench_schema_version = 1`。不同版本的 JSONL 结果不得直接合并；必须先迁移到同一版本。

## 必填字段

| 字段 | 类型 | 空值语义 |
|---|---|---|
| `task_id` | string | 不允许缺失 |
| `task_version` | string | M0-5 完成前允许 `unversioned` |
| `suite` | string | 全量任务使用 `canonical` |
| `source_batch` | string | 空字符串表示调用方未提供批次，不得跨批次合并 |
| `model` | string | 裸跑基线也必须填写 |
| `planner_model` | string | 基线无 Planner 时为空字符串 |
| `judge_model` | string | 未启用 Judge 时为空字符串 |
| `repeat` | positive integer | 从 1 开始 |
| `difficulty` | `easy` / `medium` / `hard` | 缺失时使用 `medium` |
| `failure_class` | fixed string or null | 成功且无失败时为 `null` |
| `accepted_delivery` | boolean | 不允许缺失 |
| `delivery_branch_created` | boolean | 不允许缺失 |
| `pr_created` | boolean | 不允许缺失 |
| `spec_compliance` | boolean or null | 未评估为 `null` |
| `architecture_compliance` | boolean or null | 未评估为 `null` |
| `total_cost_usd` | non-negative number | 无成本记录为 `0` |
| `elapsed_sec` | non-negative number | 必须存在，即使任务超时 |

`failure_class` 的允许值来自 M0-3 的八类固定集合。`pass_rate`、`binary_pass`、`kill_reason` 等为可选诊断字段，不能替代上述必填字段。

## 校验

```bash
agent_go eval validate-schema --results eval_suite/results.jsonl
```

代码入口为 `agent_go.bench_schema.validate_record()` 和
`agent_go.bench_schema.validate_results_file()`。Bench 和 baseline 在写入 JSONL 前都会校验；缺字段、类型错误、枚举错误或版本错误的 record 被拒绝。
