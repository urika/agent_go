# ADR-009: Bench 收敛优先于扩大全量矩阵

## 状态

Accepted

## 决策

在最近 Bench 失败原因、状态机和 Plan 质量尚未收敛前，不继续扩大“任务 × 模型 × repeat”矩阵。Bench 采用分层收敛流程：

```text
smoke
  -> Golden Tasks
  -> 代表性分层任务
  -> decision baseline
```

全量 decision/stress Bench 只用于阶段性决策和正式 baseline，不用于每次代码变更回归。

## 原因

最近失败数据同时混合了：

- 真实 verification/semantic failure。
- verification command 被安全策略拒绝。
- timeout 和 retry 消耗。
- blocked 级联。
- 历史 Claude 非零退出误失败。
- 历史 failure class 缺失和旧 Accepted Delivery 口径。

在这些原因未分离前，增加模型、任务和重复次数只会放大测量噪声，不能证明模型或 Plan 变好。

## 固定运行策略

| 阶段 | 任务 | 模型 | repeat | 目的 |
|---|---|---|---:|---|
| 日常 smoke | 7 个 smoke | 1 个低成本模型 | 1 | 快速回归 |
| Golden Tasks | 2 easy + 2 medium + 2 hard | 1 个模型 | 3 | 验证系统逻辑和可重复性 |
| 代表性实验 | smoke + 2 medium + 2 hard | 1-2 个模型 | 1-2 | 验证 Plan/Verifier 改动 |
| 正式 decision | 固定 decision 集合 | 固定模型池 | 3 | 阶段性模型/产品决策 |
| stress | high variance/hard | 少量模型 | 单独运行 | 单独报告，不进入普通平均 |

所有收敛运行固定：`source_batch`、schema、配置、timeout、retry、`bench-parallel=1`。不同 source batch 或 schema 不直接合并。

## 收敛门禁

进入下一阶段前必须满足：

- 新 record schema 通过率 100%。
- `failure_class` 完整率 100%。
- `blocked` 都有 root failure。
- verification command rejected 可单独统计且不计模型能力失败。
- 不存在无 delivery branch/PR 却 `accepted_delivery=true`。
- 同一任务重复运行的失败原因稳定可解释。
- Plan quality 埋点覆盖率 100%。
- 没有新的状态机逻辑错误。

## 禁止事项

- 不用历史 exploratory 结果和新 batch 混合排名。
- 不把 `pass_rate` 或旧 `$ / pass` 当产品主 KPI。
- 不因一次模型失败就扩大 retry、切换多个模型或增加全量任务。
- 不把 blocked 下游重复计为独立模型失败。
- 不把验证命令拒绝计为 verification 能力失败。

## 退出条件

只有当 Golden Tasks 达到稳定、可解释、无系统级状态错误后，才允许运行代表性实验；代表性实验确认 Plan/Verifier 改动有效后，才生成新的 decision baseline。

## 关联实现

- `agent_go/bench.py`
- `agent_go/metrics.py`
- `agent_go/failure.py`
- `agent_go/bench_schema.py`
- `agent_go/metric_report.py`
- `docs/m0-task-list.md`
