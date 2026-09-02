# M0-3 Failure Class Contract

> Status: Frozen (M0-3)
> Update date: 2026-08-08

## Fixed Classification

`failure_class` may only be:

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

`kill_reason` remains a lower-level runtime artifact and is not used as a cross-batch aggregation key. The mapping is fixed by `agent_go.failure.classify_failure()`.

## Rules Matrix

| class | Capability failure denominator | Cost | resume allowed | Worktree retained |
|---|---:|---:|---:|---:|
| model_failure | Yes | Yes | Yes | Yes |
| verification_failure | Yes | Yes | Yes | Yes |
| timeout | Yes | Yes | Yes | Yes |
| budget_abort | No | Yes | Yes | Yes |
| infrastructure_failure | No | Yes | Yes | Yes |
| delivery_failure | No | Yes | Yes | Yes |
| user_cancelled | No | Yes | No | Yes |
| system_error | No | Yes | Yes | Yes |

Infrastructure failures, budget aborts, user cancellations, and system errors must not be counted toward the capability failure denominator; budget aborts must not be mapped to model or verification failures. Incurred costs remain in the cost audit, and whether they enter product cost metrics is governed separately by the valid-task rules.

## Aggregation and Timeout

`agent_go.metrics.aggregate_failure_classes()` always outputs the full eight-class counts (uncounted classes are 0), per-class costs, valid-task denominator, exclusion counts, and exclusion reasons. Both the Bench model-level and batch-level reports retain this summary, without merging `infrastructure_failure`, `budget_abort`, or `timeout` into `model_failure`.

- `timeout` is a capability failure in product metrics.
- Records with `timed_out=true` are also marked as right-censored observations on the cost baseline, without changing the product failure classification.
- `kill_reason=cleanup_race` is counted separately, indicating the subtask completed and the cleanup race, and is not counted as a timeout failure.

## Priority

Task-level explicit `failure_class` takes priority, followed by `delivery_failed`, then the subtask `kill_reason` mapping, and finally inference from verification and process results. The priority order during aggregation is: user cancellation, budget, timeout, infrastructure, system, delivery, verification, model.