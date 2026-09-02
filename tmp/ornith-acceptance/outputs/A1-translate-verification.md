# agent_go Verification Design

> Status: Current M2 reliability baseline
> Last updated: 2026-08-08

## 1. Verification Levels

| Level | Goal | Result |
|---|---|---|
| Shell | Commands, tests, exit codes | `verification_results` |
| Code Quality | lint/type/test regression | `lint_errors/tests_broken` |
| Semantic | Semantic residuals not covered by Shell | `semantic_pass` |
| Spec Compliance | Whether requirements acceptance is satisfied | `spec_compliance` |
| Architecture Compliance | Whether architecture constraints are violated | `architecture_compliance` |
| Delivery | Whether an obtainable deliverable is produced | `accepted_delivery` |

Hard constraint: Semantic, Spec, or Architecture review cannot bypass the necessary Shell verification.

## 2. Retry Status

Each retry should record:

- attempt.
- failed commands.
- stdout/stderr tail.
- diff/stat summary.
- `diff_stat_hash`.
- `failure_pattern`.
- `effective_strategy`.
- `no_progress`.
- `failure_analysis` (if Reflexion is enabled).

## 3. No Progress Control

Default strategy:

```text
No substantial diff change across two consecutive retries
  -> mark no_progress
  -> stop the current retry loop
  -> failure_class=verification_failure
  -> optional manual/local re-planning
```

Stopping on no progress is a loss-control mechanism and does not mean the task is necessarily unrepairable.

## 4. Controlled Reflexion

- Trigger only after the retry threshold is reached.
- Prefer a different provider when using an independent evaluator.
- Generate only failure analysis and next-step strategy suggestions.
- Do not directly modify task state.
- Do not bypass Shell verification.
- Constrained by token, count, and task budget.
- Fall back to plain repair when the evaluator fails.

## 5. Verification Evidence

Each acceptance criterion should be associated with at least one piece of evidence:

- Test commands.
- lint/type commands.
- File/symbol checks.
- diff fragments.
- Spec Compliance Review result.
- Architecture Compliance Review result.