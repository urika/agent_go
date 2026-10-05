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
- [ADR-011 Pipeline 本地模型自动限流（云端并行、本地串行）](ADR-011-local-model-serialize.md)
- [ADR-012 Spec-to-Test 验收测试管线（Accepted，默认关 opt-in）](ADR-012-spec-to-test-pipeline.md)
- [ADR-013 jev 离线复核排序试点的外发边界与本地数据面（Accepted，2026-10-05）](ADR-013-jev-offline-triage-egress.md)
- [ADR-014 规则集执行面与全局规则数据面（Proposed，P1 前须 Accepted）](ADR-014-rule-set-execution-plane.md)
- [ADR-015 Problem ↔ GitHub Issue 联动（Accepted，默认关的对外写入面）](ADR-015-problem-issue-linkage.md)
