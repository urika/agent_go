# agent_go 功能架构与流程设计

> 状态：As-Planned / 与当前 PRD v3.0 对齐
> 更新日期：2026-08-08
> 关联：[prd.md](../prd.md) · [roadmap.md](../roadmap.md) · [software-development-lifecycle.md](software-development-lifecycle.md)

## 1. 产品流程

```text
需求输入
  -> Spec Review
  -> Architecture Design / Review
  -> Plan Generation / Review
  -> Task Decompose
  -> DAG Execute
  -> Verify / Repair
  -> Spec + Architecture Compliance Review
  -> Delivery Ready
  -> PR / Merge
  -> Accepted Delivery
```

agent_go 当前核心实现覆盖 `Plan -> Decompose -> Execute -> Verify`；Architecture Review、Spec Compliance Review 和完整 Delivery 状态是当前 M0/M1 的补齐目标。

## 2. 阶段职责

| 阶段 | 输入 | 输出 | 主要角色 | Gate |
|---|---|---|---|---|
| Spec Review | 用户目标、约束、验收 | 合规 Task Spec | PM/Engineer | L1/L1.5 |
| Architecture | Spec、代码库、约束 | Architecture design | Architect | 人工确认 |
| Plan | Spec、Architecture | Plan JSON | Planner | 用户确认 |
| Decompose | Plan | DAG subtasks | Planner | 冲突检查 |
| Execute | Subtask、worktree | commit/result | Worker | commit |
| Verify | commit、验收命令 | verification result | Verifier/Repairer | verify pass |
| Compliance | Spec、Architecture、diff | evidence/report | Reviewer | review |
| Delivery | accepted commits | delivery branch/PR | Delivery | Accepted Delivery |

## 3. 角色边界

- `planner`：生成和拆分执行计划，不直接修改业务代码。
- `architect`：分析架构和技术方案，默认只读。
- `developer`：在隔离 worktree 中实现代码。
- `tester`：补充或执行测试。
- `reviewer`：审查 diff、Spec 和架构合规性，不作为 Worker 的隐式重试。
- `delivery`：汇总 commit、创建或更新 PR，不重新执行 Agent。

## 4. 状态流转

```text
DRAFT
  -> SPEC_REVIEW
  -> ARCHITECTURE_REVIEW
  -> PLAN_REVIEW       # 规划审查门（plan accepted → 待执行）
  -> EXECUTING
  -> VERIFYING
      -> FIXING -> VERIFYING
      -> BLOCKED
      -> VERIFICATION_FAILED
  -> DELIVERY_READY
  -> PR_CREATED

EXECUTING / VERIFYING
  --中断(SIGINT/SIGTERM)--> PAUSED   # 可恢复锚点（M0 修复：不再复用 PLAN_REVIEW）
  --resume--> EXECUTING
  -> ACCEPTED_DELIVERY
```

恢复相关状态：

```text
EXECUTING/VERIFYING
  -> INTERRUPTED
  -> RECOVERING
  -> EXECUTING
```

重要区分：

- `COMMITTED_UNVERIFIED`：代码已保存，但不能进入下游或交付。
- `VERIFICATION_FAILED`：验证未通过。
- `DELIVERY_FAILED`：代码和验证可能通过，但交付分支或 PR 失败。
- `ACCEPTED_DELIVERY`：代码、验证和交付均满足产品契约。

## 5. 失败回退策略

| 信号 | 默认动作 |
|---|---|
| 单次代码/测试错误 | repair retry |
| 连续无进展 | 标记 `no_progress`，停止无效 retry |
| 上游失败 | 下游 `BLOCKED` |
| 预算超限 | `budget_abort` 或受控 degrade |
| Spec 范围不足 | 记录 `spec_deviation`，请求人工确认 |
| 架构约束违反 | review 阻断 delivery |
| PR 创建失败 | `DELIVERY_FAILED`，允许独立重试 |
