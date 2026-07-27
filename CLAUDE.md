# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

agent_go is a modular Python CLI tool (28 modules, ~10,300 lines) that wraps Claude Code with a structured Plan -> Decompose -> Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code. Supports concurrent execution, interrupt/resume, config-driven role-skill mapping, verification loop with auto-retry, role-aware and difficulty-based model routing, worktree preservation for failed tasks, multi-channel completion notification, and remote branch push.

No external Python dependencies — uses only stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`).

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

# With custom config file
agent_go --config /path/to/config.json run <repo-path> '<task>'

# Resume an interrupted task
agent_go resume <task-id>

# Monitor running tasks
agent_go status --watch

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
        │     ├── S4: difficulty → worker_models → claude --model
        │     ├── spawns claude -p (or greywall wrapper)
        │     ├── loads skills + agent type per subtask
        │     ├── verification loop: fix (stdout/stderr+diff injected) → re-verify,
        │     │   max_retries configurable (default 3), retry_timeout hard kill
        │     ├── git commit + tag ({task_id}/{sub_id} namespaced)
        │     └── worker metering (tokens/cost/difficulty → metering.jsonl)
        ├── push branches to remote (if --remote)
        ├── remove worktrees + delete tags + restore gc.auto
        │   └── preserves failed/blocked worktrees for human review (agent_go inspect)
        ├── notify_event()       → desktop/webhook/command channels (M1)
        └── final report + quality dashboard in cmd_pr (M3)
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI commands: run, resume, list, show, status, pr, config, clean, cache, inspect, review, router, eval, ci, plan-history, plan-diff |
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
| `evaluator.py` | LLM semantic evaluation for verification loop |
| `notify.py` | Multi-channel event notification: desktop/webhook/command, IM adapters |
| `goal_injector.py` | /goal Stop Hook injection: .claude/settings.json + verify-goal.sh |
| `metrics.py` | Data collection: timing, change stats, estimate_cost, aggregate_metering |
| `eval.py` | Quality/perf/cost (per-role)/reliability/ux evaluation, eval gate ($/pass baseline + regression) |
| `planning.py` | Planning helpers: estimate_task_duration (M4) |
| `pricing.py` | Model price table (22 models), MODEL_TIER, provider defaults |
| `bench.py` | Model benchmark orchestrator: eval bench over eval_suite tasks |
| `cross_judge.py` | Cross-model judgment matrix (self-bias prevention) + human calibration |
| `agent_loop.py` | Autonomous agent loop (--agent-loop): tool-use ReAct loop |
| `tool_executor.py` | Tool registry for agent loop: bash safety rules, file ops |
| `tui.py` | Curses-based status dashboard (live task monitoring) |
| `workflow_gen.py` | GitHub Actions CI workflow auto-generation |

## Key Design Decisions

- **Worktree isolation**: `git worktree add -b agent_go/{task_id}/{sub_id}` creates branch-specific worktrees sharing the repo's object database. Tags are namespaced as `{task_id}/{sub_id}` to avoid cross-task collisions.
- **Artifact passing**: Upstream subtask tags are directly `git merge`d into downstream worktrees — no temp remotes needed since all worktrees share the same object db.
- **Concurrency safety**: `git gc.auto` is disabled before concurrent execution and restored after pipeline completion.
- **Config-driven role routing**: `~/.agent_go/role_skill_map.json` maps keyword/file-pattern/agent-type conditions to required and recommended skills. Rules are injected into the Plan prompt and applied as post-LLM fallback.
- **Plan prompt**: Injects installed Skill inventory table + role-skill rule summary so LLM knows available Skills before generating steps. `agent_type` and `skills` fields required in output.
- **Three-tier fallback**: External API -> local model (localhost:8000) -> rule-based decomposition.
- **Config**: `~/.agent_go/config.json` (auto-created). Shallow-merged with `DEFAULT_CONFIG`.
- **API key**: `AGENT_GO_API_KEY` env var > `config.json` `api_key`.
- **Logging**: Dual-format — INFO human-readable + DEBUG JSON events.
- **Output abstraction**: `Console` class (quiet/verbose modes) is injected at CLI entry and shared via module-level default. All user-facing output goes through it — no bare `print()` calls.
- **Sandbox**: Prefers `greywall`, falls back to native `claude`.
- **CI**: `.github/workflows/test.yml` runs pytest + ruff (E,F,W) + mypy on push/PR to main. Config in `pyproject.toml`.

## Testing

```bash
pytest tests/           # 1130 tests (~17s)
pytest tests/ -q        # Quiet mode
pytest tests/ -k "not integration"  # Unit tests only
pytest tests/ -k "TestFormatCommit" -v  # Run specific test class
```

## File Organization

```
agent_go/           # 28 package modules (~10,300 lines)
tests/              # 46 test files, 1130 tests
eval_suite/         # Standard task suite for eval bench (8 tasks + fixtures)
docs/
├── README.md       # 文档索引
├── architecture.md # 核心架构、关键设计决策、数据流
├── prd.md          # 产品定位、功能优先级、NFR KPI
├── spec.md         # 所有模块接口速查（浓缩版）
├── ISSUES.md       # 已知 bug 和改进项
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
