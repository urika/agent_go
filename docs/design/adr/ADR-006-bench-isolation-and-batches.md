# ADR-006: Bench 使用进程隔离和批次治理

## 状态

Accepted

## 决策

Bench 通过 subprocess 调用 CLI，不直接 import 核心 pipeline；每条记录必须包含 schema、suite、source batch 和任务版本。

## 原因

- 防止评估过程污染主进程状态。
- 防止测试间共享 Git/config/metering 状态。
- 防止不同采集器和任务版本被错误混比。

## 约束

- 22 个 canonical 任务保留，但按 suite 选择运行。
- smoke/core/decision/stress 分开报告。
- `$ / pass` 只作为同 suite、同批次诊断指标。
- 产品主指标使用 Cost per Accepted Delivery。

## 实现

`bench.py`、`bench_schema.py`、`batch_governance.py`、`eval_suite/task_catalog.json`。
