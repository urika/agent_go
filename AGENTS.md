# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

agent_go is a modular Python CLI tool (36 modules, ~16,500 lines) that wraps Claude Code with a structured Plan -> Decompose -> Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code. Supports concurrent execution, interrupt/resume/crash-recovery, config-driven role-skill mapping, verification loop with auto-retry, role-aware and difficulty-based model routing, worktree preservation for failed tasks, multi-channel notification, remote branch push, and an MCP server/client layer (agent_go can be consumed as an MCP server and can itself consume external MCP tools inside subtasks).

No external Python dependencies — uses only stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`).

## Commands

```bash
# Install
pip install -e .

export AGENT_GO_API_KEY="sk-ant-..."

# Run a task
agent_go run <repo-path> '<task>'

# Headless with concurrency and remote push
agent_go run <repo-path> '<task>' --yes --parallel 3 --remote origin

# With explicit skills and agent type
agent_go run <repo-path> '<task>' --skill security-review --agent-type reviewer

# With verification loop and worktree preservation
agent_go run <repo-path> '<task>' --max-retries 5 --preserve-worktrees

# Auto git init for non-git target dir (local-only, no remote)
agent_go run <repo-path> '<task>' --auto-init

# Export subtask artifacts (worktree/__artifacts__/) to a directory before cleanup
agent_go run <repo-path> '<task>' --artifact-dir ~/Desktop/reports

# With custom config file
agent_go --config /path/to/config.json run <repo-path> '<task>'

# Resume an interrupted task
agent_go resume <task-id>

# Rebuild meta.json after a SIGKILL / abnormal interruption
agent_go recover <task-id>
agent_go recover <task-id> --dry-run    # scan only, don't update meta.json

# Inspect preserved worktrees after failed task
agent_go inspect <task-id>
agent_go inspect <task-id> --all    # show all subtasks
agent_go inspect <task-id> --json   # machine-readable

# Aggregate result review (M7): per-file diff summary + approve/reject
agent_go review --task <task-id>
agent_go review --task <task-id> --deep          # independent-model per-subtask analysis
agent_go review --task <task-id> --approve       # or --reject / --changes-requested

# Plan version history
agent_go plan-history <task-id>
agent_go plan-diff <task-id> --v1 1 --v2 2

# Execution replay (timeline visualization)
agent_go replay <task-id>
agent_go replay <task-id> --json

# Checkpoint management (worktree file snapshots)
agent_go checkpoint list <task-id>
agent_go checkpoint restore <task-id> --name <sub-id> [--target <dir>]
agent_go checkpoint delete <task-id> --name <sub-id>

# MCP server (JSON-RPC 2.0 over stdio, or HTTP/SSE)
agent_go mcp
agent_go mcp --http --host 127.0.0.1 --port 8090   # HTTP transport: POST /mcp + GET /mcp (SSE) + GET /health
AGENT_GO_MCP_HTTP_TOKEN=xxx agent_go mcp --http   # 启用 Bearer token 鉴权

# Monitor running tasks
agent_go status --watch

# Model benchmark / cross-judgment / evaluation / gate
agent_go eval bench --tasks eval_suite/ --candidate-models M1,M2 --repeat 3
agent_go eval models --results eval_suite/results.jsonl
agent_go eval judge --results eval_suite/results.jsonl --judge-models M1,M2
agent_go eval judge calibrate --llm-scores ... --human-scores ...
agent_go eval gate --baseline 0.05
agent_go eval gate --check-regression --update-baseline

# PR / CI workflow generation / cache / router / skills / agents / config
agent_go pr <task-id> --push
agent_go ci --dry-run
agent_go cache stats
agent_go router show
agent_go skills list
agent_go skills show <name>
agent_go agents
agent_go config

# List / show / clean
agent_go list
agent_go show <task-id>
agent_go clean
```

## Architecture

```
cmd_run()
  ├── analyze_project()        → git ls-files or find
  ├── get_git_info()           → remote, branch, commit
  ├── get_resource_map()       → directories, config files
  ├── generate_plan()          → calls LLM API, returns structured JSON
  │     ├── injects skill inventory + role-skill rule summary into prompt
  │     ├── call_api()         → unified Anthropic/OpenAI/DeepSeek/custom
  │     ├── router.py          → optional role-aware routing (planner/worker/reviewer)
  │     └── metering.jsonl     → per-call role/tokens/cost/latency (planner + worker)
  ├── confirm_plan()           → Y/S/D/E/R/N interactive (--yes skips)
  ├── plan_to_subtasks()       → injects agent_prompt + applies role-skill rules + difficulty
  ├── confirm_subtasks()       → Y/N/E/A/D interactive
  ├── estimate_task_duration() → M4 time estimate (historical median × topo waves)
  └── _run_pipeline()
        ├── disable gc.auto    → concurrency safety
        ├── topological waves  → ThreadPoolExecutor with --parallel N
        │   └── upstream failed/blocked → downstream marked blocked & skipped
        ├── run_subtask()
        │     ├── git worktree add -b agent_go/{task_id}/{sub_id}
        │     ├── git merge upstream tag → artifact passing
        │     ├── writes TASK.md (path-rewritten; optional /goal via --goal)
        │     ├── optional Stop Hook injection (--goal-hook)
        │     ├── difficulty → worker_models → claude --model
        │     ├── checkpoint snapshot taken for rollback (checkpoint.py)
        │     ├── spawns claude -p (or greywall wrapper)
        │     ├── loads skills + agent type per subtask
        │     ├── git commit + tag ({task_id}/{sub_id} namespaced)
        │     └── verification + auto-retry loop (configurable max_retries)
        ├── push branches to remote (if --remote)
        ├── remove worktrees + delete tags + restore gc.auto
        │   └── preserves failed/blocked worktrees for human review
        ├── final report
        │   └── lists preserved worktree paths + failure reasons
        └── inspect command → list preserved worktrees, paths, git branches

recover <task-id>   → rebuild meta.json from worktree state after SIGKILL
resume <task-id>    → reruns uncompleted subtasks from meta.json state
```

If the process is killed (SIGKILL) mid-run, `agent_go recover <task-id>` rebuilds `meta.json` from worktree state: commit+verify-pass → completed, commit+verify-fail → failed, no commit+orphan changes → reset (resume reruns it), no commit+no changes → no_changes. It never commits orphan changes itself — commit stays the sole completion boundary for resume correctness.

## Key Modules (36 modules, ~16,500 lines)

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI commands: run, resume, recover, list, show, status, pr, config, clean, inspect, review, router, cache, eval, ci, skills, agents, plan-history, plan-diff, replay, checkpoint, mcp |
| `api.py` | LLM API: generate_plan, call_api, decompose_fallback, plan cache |
| `ui.py` | Interactive prompts: confirm_plan, confirm_subtasks, plan_to_subtasks |
| `executor.py` | Core subtask runner: worktree create, skill load, claude spawn, verify loop |
| `pipeline.py` | Wave scheduler, concurrency, worktree preservation/cleanup, remote push, SIGINT |
| `subtask.py` | claude -p headless runner, git merge upstream, worker metering, difficulty env |
| `notify.py` | Multi-channel event notification: desktop/webhook/command, IM adapters |
| `goal_injector.py` | /goal Stop Hook injection: .claude/settings.json + verify-goal.sh |
| `git_utils.py` | Project analysis, worktree create/remove/prune, gc.auto control |
| `skills.py` | Skill loading, discovery, rendering (YAML frontmatter + Markdown) |
| `agents.py` | Agent type system: developer/architect/reviewer/tester |
| `role_skill_map.py` | Config-driven rule matching: keywords, file patterns, agent type |
| `router.py` | Role-aware model routing: planner/worker/reviewer, fallback + circuit breaker |
| `evaluator.py` | LLM semantic evaluation + failure summary for verification loop |
| `metrics.py` | Data collection: timing/change stats, estimate_cost, aggregate_metering |
| `config.py` | Config loading, logging, API key resolution, meter_event |
| `utils.py` | Commit formatting, slugify, shell safety, version detection |
| `eval.py` | Quality/perf/cost (per-role)/reliability/UX analysis + eval gate ($/pass baseline + regression) |
| `planning.py` | Planning helpers: estimate_task_duration |
| `pricing.py` | Model price table (48 models), MODEL_TIER, provider defaults |
| `replay.py` | Execution replay timeline: load meta/metering/results, ASCII/JSON visualization |
| `checkpoint.py` | Worktree file snapshot manager: take/restore/delete |
| `recover.py` | Rebuild meta.json from worktree state after SIGKILL/abnormal interruption |
| `mcp_server.py` | MCP server over stdio: 6 tools (run/resume/inspect/review/list/cancel) + resources + prompts |
| `mcp_http.py` | MCP server HTTP/SSE transport: POST /mcp + GET /mcp (SSE) + GET /health, Bearer auth |
| `mcp_client.py` | MCP consumption layer: subtasks call external MCP tools, namespaced `mcp__{server}__{tool}` |
| `bench.py` | Model benchmark orchestrator: eval bench over eval_suite tasks |
| `cross_judge.py` | Cross-model judgment matrix (self-bias prevention) + human calibration |
| `assessment.py` | False-positive evaluation data layer: AssessmentEvent model, persistence, aggregation |
| `artifacts.py` | Artifact export (S9-B): collect worktree/__artifacts__/ into --artifact-dir before cleanup |
| `agent_loop.py` | Autonomous agent loop (--agent-loop): tool-use ReAct loop |
| `tool_executor.py` | Tool registry for agent loop: bash safety rules, file ops |
| `console.py` | Console output abstraction: quiet/verbose modes, lazy default binding, tables |
| `tui.py` | Curses status dashboard |
| `workflow_gen.py` | GitHub Actions workflow generation (ci command) |
| `lint.py` | AST-based static checks: suspicious for-loop body truncation |

## Key Design Decisions

- **Worktree isolation**: `git worktree add -b agent_go/{task_id}/{sub_id}` creates branch-specific worktrees sharing the repo's object database. Tags are namespaced as `{task_id}/{sub_id}` to avoid cross-task collisions.
- **Artifact passing**: Upstream subtask tags are directly `git merge`d into downstream worktrees — no temp remotes needed since all worktrees share the same object db.
- **Concurrency safety**: `git gc.auto` is disabled before concurrent execution and restored after pipeline completion.
- **Config-driven role routing**: `~/.agent_go/role_skill_map.json` maps keyword/file-pattern/agent-type conditions to required and recommended skills. Rules are injected into the Plan prompt and applied as post-LLM fallback.
- **Plan prompt**: Injects installed Skill inventory table + role-skill rule summary so LLM knows available Skills before generating steps. `agent_type` and `skills` fields required in output.
- **Three-tier fallback**: External API -> local model (localhost:8000) -> rule-based decomposition.
- **Verification loop**: Failed subtasks auto-retry with full failure context (stdout/stderr/git diff) injected into fix prompt. Configurable max retries (`--max-retries`). Worktree preserved for manual inspection on final failure.
- **Worktree preservation**: Failed/blocked subtask worktrees are preserved after pipeline completion. `agent_go inspect <task-id>` lists paths and branch names for manual review.
- **Result review (M7)**: `agent_go review --task <task-id>` aggregates per-file diff summaries across subtasks with approve/reject/changes-requested decisions; `--deep` runs independent-model per-subtask analysis.
- **Difficulty routing**: Planner tags subtasks with `difficulty`; `worker_models` config maps difficulty to a model name passed via `claude --model`; `worker_backends` maps model names to API base URLs (per-subtask `ANTHROPIC_BASE_URL` injection, overrides `worker_base_url`); difficulty and actual model recorded in metering.
- **Planner API isolation**: `planner_api` config block overrides `plan_api` for plan generation only — supports independent model/provider for planning vs execution.
- **Crash recovery**: commit is the sole completion boundary. `agent_go recover` never commits orphan changes on your behalf — it only classifies worktree state so `resume` knows what to rerun.
- **MCP dual role**: agent_go is both an MCP server (`mcp_server.py` / `mcp_http.py`, exposing run/resume/inspect/review/list/cancel tools + resources + prompts) and an MCP consumer (`mcp_client.py`, letting subtasks call tools from external MCP servers, namespaced `mcp__{server}__{tool}`). Consumer failures are isolated per-server and degrade to a warning rather than blocking the pipeline.
- **Config**: `~/.agent_go/config.json` (auto-created). Shallow-merged with `DEFAULT_CONFIG`.
- **API key**: `AGENT_GO_API_KEY` env var > `config.json` `api_key`. Template vars (`${VAR_NAME}`) resolved from environment.
- **Local model cost tracking**: `local_models` list marks model names routed to local backends — metering cost is zeroed for matched models.
- **Logging**: Dual-format — INFO human-readable + DEBUG JSON events.
- **Output abstraction**: `Console` class (quiet/verbose modes) is injected at CLI entry and shared via module-level default. All user-facing output goes through it — no bare `print()` calls.
- **Sandbox**: Prefers `greywall`, falls back to native `claude`.
- **CI**: `.github/workflows/test.yml` runs pytest + ruff (E,F,W) + mypy on push/PR to main. Config in `pyproject.toml`.

## Testing

```bash
pytest tests/           # 1464 tests (~35s)
pytest tests/ -q        # Quiet mode
pytest tests/ -k "not integration"  # Unit tests only
pytest tests/ -k "TestFormatCommit" -v  # Run specific test class
```

## Code Review Checklist

When reviewing Python code, check for these high-risk patterns:

### Loop body truncation (critical)

**Anti-pattern**: After a `for x in collection:` loop, code references `x` (the loop variable) to do state mutations — this means the mutations are outside the loop, acting only on the last element.

```python
# ❌ BUG: mutations after the loop — only processes last item
for st in wave:
    fut = executor.submit(run_subtask, ...)
    futures[fut] = st
for fut in as_completed(futures):    # ← same level as for st above
    st = futures[fut]
    try:
        result = fut.result()
    except Exception:
        ...
with meta_lock:                      # ← OUTSIDE the as_completed loop!
    results_map[st["id"]] = result   # ← only saves last st
completed_ids.add(st["id"])          # ← only adds last st

# ✅ CORRECT: all per-item processing inside the loop
for st in wave:
    fut = executor.submit(run_subtask, ...)
    futures[fut] = st
for fut in as_completed(futures):
    st = futures[fut]
    try:
        result = fut.result()
    except Exception:
        ...
    with meta_lock:                  # ← INSIDE the loop
        results_map[st["id"]] = result
    completed_ids.add(st["id"])
```

**Detection rule**: When a `for` loop processes a collection and the lines immediately after the loop reference the loop variable (`st`, `item`, `fut`, etc.), verify the indentation: these should likely be inside the loop.

### Other review patterns

- **Thread safety**: Shared mutable state (`results_map`, `completed_ids`, counters) modified across threads must be protected by a lock.
- **Mock side_effect exhaustion**: When mocking a function with `side_effect=[...]`, ensure the number of expected calls doesn't exceed the list length — otherwise `StopIteration` crashes the test.
- **Subprocess pipe deadlock**: Always use `capture_output=True` or `stdin=PIPE` with `communicate()`, never `wait()` on a pipe that fills the buffer.
- **Temp directory leak**: Tests that create directories/files should use `tmp_path` fixture (auto-cleanup) or clean up in a `finally` block.

## File Organization

```
	agent_go/           # 36 Python modules (~16,500 lines)
	tests/              # 60 test files, 1464 tests
eval_suite/         # Standard task suite for eval bench (22 tasks + 4 fixtures)
docs/design/        # Design docs, requirements, product roadmap
docs/archive/       # Historical code review records
```
