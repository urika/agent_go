# 系统 ADR

本目录记录影响系统边界、数据契约、可靠性和产品验收的关键技术决策。

## ADR 清单

- [ADR-001 Worktree 隔离](ADR-001-worktree-isolation.md)
- [ADR-002 三层完成边界](ADR-002-completion-boundaries.md)
- [ADR-003 DAG 与 Artifact 传递](ADR-003-dag-artifact-transfer.md)
- [ADR-004 Recover 不自动提交孤儿改动](ADR-004-recover-no-orphan-commit.md)
- [ADR-005 分层成本控制](ADR-005-cost-control-layers.md)
- [ADR-006 Bench 进程隔离与批次治理](ADR-006-bench-isolation-and-batches.md)
- [ADR-007 Accepted Delivery](ADR-007-accepted-delivery.md)
- [ADR-008 数据驱动 timeout 设置模型（实测 P95 × 余量）](ADR-008-timeout-setting-model.md)
- [ADR-009 Bench 收敛优先于扩大全量矩阵](ADR-009-bench-convergence.md)
- [ADR-010 轨迹平台化三层切分（代理层不做 LLM 会话管理）](ADR-010-trajectory-layering.md)
