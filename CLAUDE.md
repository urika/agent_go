# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

agent_go is a modular Python CLI tool (37 modules, ~17,300 lines) that wraps Claude Code with a structured Plan -> Decompose -> Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code. Supports concurrent execution, interrupt/resume, crash recovery, config-driven role-skill mapping, verification loop with auto-retry, role-aware and difficulty-based model routing, worktree preservation for failed tasks, multi-channel completion notification, remote branch push, and an MCP server/client layer (agent_go can be consumed as an MCP server and can itself consume external MCP tools inside subtasks).

No external Python dependencies — uses only stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`, `http.server`).

## Commands

```bash
# Install
pip install -e .

export AGENT_GO_API_KEY="sk-ant-..."

# Run a task (also: python3 -m agent_go run, python3 agent_go.py run)
agent_go run <repo-path> '<task>'

# Headless with concurrency and remote push
agent_go run <repo-path> '<task>' --yes --parallel 3 --remote origin

# With explicit skills and agent type
agent_go run <repo-path> '<task>' --skill security-review --agent-type reviewer

# With structured Task Spec (SDD input contract) — recommended for non-trivial tasks
agent_go spec template <repo-path> --output docs/tasks/task-xxx.md   # generate spec template
agent_go spec validate docs/tasks/task-xxx.md <repo-path>            # L1 admission gate
agent_go run <repo-path> --spec docs/tasks/task-xxx.md --yes         # run with spec
agent_go run <repo-path> --spec docs/tasks/task-xxx.md --force       # skip admission gate

# With custom config file
agent_go --config /path/to/config.json run <repo-path> '<task>'

# Resume an interrupted task
agent_go resume <task-id>

# Rebuild meta.json after a SIGKILL / abnormal interruption
agent_go recover <task-id>

# Monitor running tasks
agent_go status --watch

# Execution replay (timeline visualization)
agent_go replay <task-id>

# Checkpoint management (worktree file snapshots)
agent_go checkpoint list <task-id>
agent_go checkpoint restore <task-id> --name <sub-id> [--target <dir>]
agent_go checkpoint delete <task-id> --name <sub-id>

# Inspect preserved worktrees of failed/blocked subtasks
agent_go inspect <task-id>

# Aggregate result review (per-file diff summary + approve/reject)
agent_go review --task <task-id>
agent_go review --task <task-id> --deep

# MCP server (agent_go as an MCP server: JSON-RPC 2.0 over stdio, or HTTP/SSE)
agent_go mcp
agent_go mcp --http --host 127.0.0.1 --port 8090

# List / show / clean
agent_go list
agent_go show <task-id>
agent_go clean

# Dev: lint, type-check, and test
pip install pytest pytest-mock ruff mypy
ruff check agent_go/ --select=E,F,W --ignore=E501
mypy agent_go/ --ignore-missing-imports
pytest tests/ -q
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
        │     ├── mcp_client.start_all() → external MCP tools exposed as mcp__{server}__{tool}
        │     ├── spawns claude -p (or greywall wrapper)
        │     ├── loads skills + agent type per subtask
        │     ├── checkpoint snapshots taken for rollback (checkpoint.py)
        │     ├── verification loop: fix (stdout/stderr+diff injected) → re-verify,
        │     │   max_retries configurable (default 3), retry_timeout hard kill
        │     ├── git commit + tag ({task_id}/{sub_id} namespaced) — commit is the completion boundary
        │     └── worker metering (tokens/cost/difficulty → metering.jsonl)
        ├── push branches to remote (if --remote)
        ├── remove worktrees + delete tags + restore gc.auto
        │   └── preserves failed/blocked worktrees for human review (agent_go inspect)
        ├── notify_event()       → desktop/webhook/command channels
        └── final report + quality dashboard in cmd_pr
```

If the process is killed (SIGKILL) mid-run, `agent_go recover <task-id>` rebuilds `meta.json` from worktree state: commit+verify-pass → completed, commit+verify-fail → failed, no commit+orphan changes → reset (resume reruns it), no commit+no changes → no_changes (resume reruns it). It never commits orphan changes itself — commit staying the sole completion boundary is load-bearing for resume correctness.

## Key Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI commands: run, resume, list, show, status, pr, config, spec, clean, cache, inspect, review, router, eval, ci, recover, replay, checkpoint, mcp, plan-history, plan-diff |
| `api.py` | LLM API: generate_plan, call_api, decompose_fallback, plan cache |
| `ui.py` | Interactive prompts: confirm_plan, confirm_subtasks, plan_to_subtasks |
| `executor.py` | Core subtask runner: worktree create, skill load, claude spawn, verify |
| `pipeline.py` | Wave scheduler, concurrency, worktree/tag cleanup, remote push, SIGINT |
| `subtask.py` | Claude -p headless runner, git merge upstream |
| `git_utils.py` | Project analysis, worktree create/remove/prune, gc.auto control |
| `skills.py` | Skill loading, discovery, rendering (YAML frontmatter + Markdown) |
| `agents.py` | Agent type system: developer/architect/reviewer/tester |
| `role_skill_map.py` | Config-driven rule matching: keywords, file patterns, agent type |
| `config.py` | Config loading, logging, API key resolution, meter_event |
| `console.py` | Unified output layer: quiet/verbose modes, table/data formatting |
| `utils.py` | Commit formatting, slugify, shell safety, version detection, doc reading |
| `router.py` | Role-aware model routing: planner/worker/reviewer, fallback + circuit breaker |
| `evaluator.py` | LLM semantic evaluation + failure summary for verification loop |
| `notify.py` | Multi-channel event notification: desktop/webhook/command, IM adapters |
| `goal_injector.py` | /goal Stop Hook injection: .claude/settings.json + verify-goal.sh |
| `metrics.py` | Data collection: timing, change stats, estimate_cost, aggregate_metering |
| `eval.py` | Quality/perf/cost (per-role)/reliability/ux evaluation, eval gate ($/pass baseline + regression) |
| `planning.py` | Planning helpers: estimate_task_duration |
| `pricing.py` | Model price table (48 models), MODEL_TIER, provider defaults |
| `bench.py` | Model benchmark orchestrator: eval bench over eval_suite tasks |
| `cross_judge.py` | Cross-model judgment matrix (self-bias prevention) + human calibration |
| `agent_loop.py` | Autonomous agent loop (--agent-loop): tool-use ReAct loop |
| `tool_executor.py` | Tool registry for agent loop: bash safety rules, file ops |
| `tui.py` | Curses-based status dashboard (live task monitoring) |
| `workflow_gen.py` | GitHub Actions CI workflow auto-generation |
| `replay.py` | Execution replay timeline: loads meta/metering/results, ASCII/JSON visualization |
| `checkpoint.py` | Worktree file snapshot manager: take/restore/delete, subtask rollback |
| `mcp_server.py` | agent_go exposed as an MCP server: JSON-RPC 2.0 over stdio, tools + lifecycle event stream |
| `mcp_http.py` | MCP server HTTP/SSE transport: POST /mcp (JSON-RPC), GET /mcp (SSE notifications), GET /health |
| `mcp_client.py` | MCP consumption layer: subtasks call external MCP server tools, namespaced `mcp__{server}__{tool}` |
| `recover.py` | Rebuilds meta.json from worktree state after SIGKILL/abnormal interruption |
| `assessment.py` | False-positive evaluation data layer: AssessmentEvent model, persistence, aggregation (pure data module, no core-module imports) |
| `artifacts.py` | Artifact export (S9-B): collect worktree/__artifacts__/ into --artifact-dir before cleanup |
| `spec.py` | Task Spec parsing + L1 admission gate (S11-P0): 7-section Markdown spec → structured constraints injected into Plan prompt; deterministic pre-flight checks (required sections / file path validity / verification whitelist / length floor) |
| `lint.py` | AST-based static checks: detects suspicious for-loop body truncation (see review pattern below) |

## Key Design Decisions

- **Worktree isolation**: `git worktree add -b agent_go/{task_id}/{sub_id}` creates branch-specific worktrees sharing the repo's object database. Tags are namespaced as `{task_id}/{sub_id}` to avoid cross-task collisions.
- **Artifact passing**: Upstream subtask tags are directly `git merge`d into downstream worktrees — no temp remotes needed since all worktrees share the same object db.
- **Concurrency safety**: `git gc.auto` is disabled before concurrent execution and restored after pipeline completion.
- **Config-driven role routing**: `~/.agent_go/role_skill_map.json` maps keyword/file-pattern/agent-type conditions to required and recommended skills. Rules are injected into the Plan prompt and applied as post-LLM fallback.
- **Plan prompt**: Injects installed Skill inventory table + role-skill rule summary so LLM knows available Skills before generating steps. `agent_type` and `skills` fields required in output.
- **Three-tier fallback**: External API -> local model (localhost:8000) -> rule-based decomposition.
- **Verification loop**: Failed subtasks auto-retry with full failure context (stdout/stderr/git diff) injected into the fix prompt. Configurable max retries (`--max-retries`); worktree preserved for manual inspection on final failure.
- **Difficulty routing**: Planner tags subtasks with `difficulty`; `worker_models` config maps difficulty to a model name passed via `claude --model`; `worker_backends` maps model names to API base URLs (per-subtask `ANTHROPIC_BASE_URL` injection); difficulty and actual model are recorded in metering.
- **Planner API isolation**: `planner_api` config block overrides `plan_api` for plan generation only — independent model/provider for planning vs. execution.
- **Crash recovery**: commit is the sole completion boundary. `agent_go recover` never commits orphan changes on your behalf — it only classifies worktree state so `resume` knows what to rerun.
- **MCP dual role**: agent_go is both an MCP server (`mcp_server.py` / `mcp_http.py`, exposing its own tools to external clients) and an MCP consumer (`mcp_client.py`, letting subtasks call tools from external MCP servers). External tools are namespaced `mcp__{server}__{tool}` to avoid colliding with native tools (Read/Write/Edit/Bash). Consumer failures are isolated per-server (try/except) and degrade to a warning rather than blocking the pipeline.
- **Config**: `~/.agent_go/config.json` (auto-created). Shallow-merged with `DEFAULT_CONFIG`.
- **API key**: `AGENT_GO_API_KEY` env var > `config.json` `api_key`. Template vars (`${VAR_NAME}`) resolved from environment.
- **Local model cost tracking**: `local_models` list marks model names routed to local backends — metering cost is zeroed for matched models.
- **Logging**: Dual-format — INFO human-readable + DEBUG JSON events.
- **Output abstraction**: `Console` class (quiet/verbose modes) is injected at CLI entry and shared via module-level default. All user-facing output goes through it — no bare `print()` calls.
- **Sandbox**: Prefers `greywall`, falls back to native `claude`.
- **CI**: `.github/workflows/test.yml` runs pytest + ruff (E,F,W) + mypy on push/PR to main. Config in `pyproject.toml`.

## Code Review Checklist

When reviewing Python code in this repo, check for these high-risk patterns first (also enforced by `lint.py`'s AST check):

**Loop body truncation (critical)** — after a `for x in collection:` loop, code that references the loop variable `x` to do state mutation is running *outside* the loop, acting only on the last element:

```python
# ❌ BUG: mutations after the loop — only processes the last item
for st in wave:
    fut = executor.submit(run_subtask, ...)
    futures[fut] = st
for fut in as_completed(futures):    # ← same level as for st above
    st = futures[fut]
    result = fut.result()
with meta_lock:                      # ← OUTSIDE the as_completed loop!
    results_map[st["id"]] = result   # ← only saves the last st

# ✅ CORRECT: all per-item processing inside the loop
for fut in as_completed(futures):
    st = futures[fut]
    result = fut.result()
    with meta_lock:                  # ← INSIDE the loop
        results_map[st["id"]] = result
```

Other patterns worth a second look: thread safety on shared mutable state (`results_map`, counters — must be lock-protected), `Mock(side_effect=[...])` exhaustion (list shorter than actual call count raises `StopIteration`), subprocess pipe deadlocks (use `capture_output=True`/`communicate()`, never `wait()` on a pipe that can fill), and temp-directory leaks in tests (use `tmp_path`, not manual cleanup).

## Testing

```bash
pytest tests/           # 1569 tests (~60s)
pytest tests/ -q        # Quiet mode
pytest tests/ -k "not integration"  # Unit tests only
pytest tests/ -k "TestFormatCommit" -v  # Run specific test class
```

## File Organization

```
agent_go/           # 36 package modules (~16,500 lines)
tests/              # 61 test files, 1569 tests
eval_suite/         # Standard task suite for eval bench (tasks + fixtures)
docs/
├── README.md       # 文档索引
├── architecture.md # 核心架构、关键设计决策、数据流
├── prd.md          # 产品定位、功能优先级、NFR KPI
├── spec.md         # 所有模块接口速查（浓缩版）
├── ISSUES.md       # 已知 bug 和改进项
├── roadmap.md      # 迭代排期（对齐 prd.md KPI）
├── design/         # 功能扩展和架构改进的设计方案
├── in/             # 进行中的设计草案
└── archive/        # 历史文档（旧 PRD、旧 spec、设计审查，不再维护）
pyproject.toml
.github/workflows/  # CI: pytest + ruff + mypy
```

## Documentation

一人项目，文档从简。核心维护 [docs/](docs/) 下 4 个文件 + [CLAUDE.md](CLAUDE.md)。

| 改了什么 | 更新哪个文档 |
|----------|-------------|
| 公共函数签名 | [spec.md](docs/spec.md) |
| 架构/设计决策 | [architecture.md](docs/architecture.md) |
| 产品方向/KPI | [prd.md](docs/prd.md) |
| CLI/命令/约定 | 本文件 (CLAUDE.md) |
