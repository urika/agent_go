# agent_go Functional Architecture and Workflow Design

> Status: As-Planned / Aligned with current PRD v3.0
> Update date: 2026-08-08
> Related: [prd.md](../prd.md) · [roadmap.md](../roadmap.md) · [software-development-lifecycle.md](software-development-lifecycle.md)

## 1. Product Workflow

```text
Requirement input
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

agent_go's current core implementation covers `Plan -> Decompose -> Execute -> Verify`; Architecture Review, Spec Compliance Review, and complete Delivery status are the current M0/M1 backfill targets.

## 2. Phase Responsibilities

| Phase | Input | Output | Primary Role | Gate |
|---|---|---|---|---|
| Spec Review | User goal, constraints, acceptance | Compliant Task Spec | PM/Engineer | L1/L1.5 |
| Architecture | Spec, codebase, constraints | Architecture design | Architect | Human confirmation |
| Plan | Spec, Architecture | Plan JSON | Planner | User confirmation |
| Decompose | Plan | DAG subtasks | Planner | Conflict check |
| Execute | Subtask, worktree | commit/result | Worker | commit |
| Verify | commit, acceptance command | verification result | Verifier/Repairer | verify pass |
| Compliance | Spec, Architecture, diff | evidence/report | Reviewer | review |
| Delivery | accepted commits | delivery branch/PR | Delivery | Accepted Delivery |

## 3. Role Boundaries

- `planner`: Generates and decomposes the execution plan; does not directly modify business code.
- `architect`: Analyzes architecture and technical approaches; read-only by default.
- `developer`: Implements code in an isolated worktree.
- `tester`: Supplements or executes tests.
- `reviewer`: Reviews diff, Spec, and architecture compliance; does not serve as an implicit retry for the Worker.
- `delivery`: Aggregates commits, creates or updates PR, does not re-execute the Agent.

## 4. State Transitions

```text
cmd_run → EXECUTING
    ├─ All success → DELIVERY_READY → ACCEPTED_DELIVERY
    ├─ Capability failure → VERIFICATION_FAILED
    ├─ Constraint block → BLOCKED
    ├─ SIGINT/SIGTERM (no failed) → PAUSED → resume → EXECUTING
    └─ MCP cancel → CANCELLED
```
v2 simplified (2026-08-08): 8 states. See [m0-state-machine.md](m0-state-machine.md).

Important distinctions:

- `VERIFICATION_FAILED`: Verification did not pass (capability failure, counts toward model capability denominator).
- `BLOCKED`: Constraint block (plan quality / cost / metering / dependency cycle, does not count toward capability denominator).
- `DELIVERY_FAILED`: Code and verification may pass, but delivery branch or PR fails.
- `ACCEPTED_DELIVERY`: Code, verification, and delivery all satisfy the product contract.

## 5. Failure Rollback Strategy

| Signal | Default Action |
|---|---|
| Single code/test error | repair retry |
| Continuous no progress | Mark `no_progress`, stop ineffective retry |
| Upstream failure | Downstream `BLOCKED` |
| Budget exceeded | `budget_abort` or controlled degrade |
| Insufficient Spec scope | Record `spec_deviation`, request human confirmation |
| Architecture constraint violation | Review blocks delivery |
| PR creation failure | `DELIVERY_FAILED`, allow independent retry |