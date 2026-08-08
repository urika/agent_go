# M0-11 最小端到端验证

M0-11 的门禁由 `tests/test_m0_e2e.py` 覆盖：

- 单任务元数据必须产生 Accepted Delivery 决策字段。
- 缺少 commit 或验证失败不能 Accepted Delivery。
- PR 交付失败必须是 delivery failure，不得伪装成 model failure。
- infrastructure failure 不进入能力失败分母。
- `--suite smoke` 只调度 catalog 中的 smoke 任务。
- 每条结果必须通过 Bench Schema，并包含 suite、source_batch、schema version。
