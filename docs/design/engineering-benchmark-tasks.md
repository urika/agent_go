# 工程级 Benchmark 任务设计

## Overview

6 realistic multi-file tasks across 2 fixture projects (task-mgr, data-pipeline),
covering 6 production engineering patterns. Each is designed to require 3-6 subtasks.

## Tasks

| # | Pattern | Fixture | Task | Exp. Subtasks | Est. Time | Est. Files |
|---|---------|---------|------|---------------|-----------|------------|
| 13 | Security Hardening | task-mgr | Input validation, shell injection, path traversal, XSS sanitization | 4 | 10min | 5 |
| 14 | Performance Optimization | task-mgr | LRU cache, batch writes, debounce, count-only list | 4 | 10min | 4 |
| 15 | Race Condition Fix | task-mgr | Thread lock, atomic write, JSON retry, concurrent tests | 4 | 10min | 3 |
| 16 | Refactoring | data-pipeline | Stage post-validation, pipeline integration, error collection | 4 | 10min | 3 |
| 17 | Feature Add | data-pipeline | Conditional branching, fan-out, dry-run, BranchRouterStage | 5 | 15min | 4 |
| 18 | Test Infrastructure | data-pipeline | Integration test suite, fixtures, CLI end-to-end tests | 4 | 10min | 6 |

## Engineering Patterns Covered

### 1. Security Hardening (Task 13)
Multi-layer defense: input validation (CLI layer) → path traversal (storage layer) → 
XSS sanitization (model layer) → verification (test layer).

**Why this exercises the agent:** 4 distinct attack vectors across 4 files + 1 new test file.
Each vector requires a different fix pattern. Verification must prove each vulnerability is closed.

### 2. Performance Optimization (Task 14)
Multiple optimization strategies: LRU cache (memory), batch+debounce (I/O), 
indexed lookup (algorithmic), thin endpoint (API design).

**Why this exercises the agent:** Trade-off decisions (cache size, debounce window),
cross-cutting concern (cache invalidation across save/load), benchmark verification.

### 3. Race Condition Fix (Task 15)
Distributed concurrency patterns: thread lock, atomic file write (crash safety),
retry with backoff (read consistency).

**Why this exercises the agent:** Non-deterministic bugs are hardest to fix.
Verification requires concurrent execution tests. Three complementary strategies
for one root cause.

### 4. Stage Validation Refactor (Task 16)
Cross-cutting architectural change: add validate() to ABC → integrate into Pipeline →
error collection → tests. Spans 3 source files across 2 packages.

**Why this exercises the agent:** Requires understanding ABC contract enforcement,
error aggregation pattern, and pipeline lifecycle ordering. 
Typical "thin vertical slice" refactor.

### 5. Conditional Branching (Task 17)
New architectural primitive: branching + fan-out + dry-run + new stage.
Complex interdependence between Pipeline and Stage classes.

**Why this exercises the agent:** 5 subtasks with clear dependencies.
Must understand Pipeline's execution model before modifying it.
Fan-out requires ThreadPoolExecutor which needs tests for concurrent correctness.

### 6. Integration Test Suite (Task 18)
Infrastructure task: fixtures → test cases → conftest → idempotency.
No production code changes — pure test infrastructure.

**Why this exercises the agent:** Test design patterns (fixtures, cleanup, idempotency).
CLI integration testing requires understanding argparse dispatch.
No feature code to modify — pure quality-of-life improvement.

## Expected Decomposition Pattern

For task-mgr tasks (13-15), the planner should produce ~4 subtasks:
```
Wave 1: Model layer changes (models.py + tests)
Wave 2: Storage layer changes (storage.py + tests)
Wave 3: CLI layer changes (cli.py + tests)
Wave 4: Integration verification
```

For data-pipeline tasks (16-18), the planner should produce ~4-5 subtasks:
```
Wave 1: Core ABC changes (stage.py)
Wave 2: Pipeline orchestration (pipeline.py)
Wave 3: Stage implementations (stages/)
Wave 4: Test files
Wave 5: Fixtures + integration tests (task 18 only)
```
