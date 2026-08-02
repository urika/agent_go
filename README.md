# agent_go

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-1569%20passed-green)](tests/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Stdlib Only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)]()

**Plan Mode orchestration tool** — wraps Claude Code with a structured `Plan -> Decompose -> Execute` workflow. LLM generates an execution plan, each step runs as an isolated subtask in its own git worktree with Claude Code. Subtasks execute concurrently with topological wave scheduling.

## Why agent_go?

Give Claude Code a complex task — refactoring auth, upgrading dependencies, adding a feature — and it can drift. agent_go breaks the work into **2–5 independently executable subtasks**, each with its own isolated worktree, verification command, agent role, and skill injection. Results flow downstream via git merge.

- **Structured execution** — LLM generates the plan, not ad-hoc decisions
- **Isolated worktrees** — shared `.git` object db, each subtask on its own branch
- **Concurrent execution** — topological wave scheduling with `--parallel N`
- **Smart role/skill routing** — config-driven rules match subtasks to Agent types and Skills
- **Interrupt & resume** — SIGINT pauses, `agent_go resume` picks up where you left off
- **Remote push** — push worktree branches to a remote for CI/CD integration
- **Zero dependencies** — pure Python stdlib
- **Plan cache** — SHA256 cache key + 24h TTL reduces API costs
- **Interrupt & crash recovery** — SIGINT pauses with `resume`; SIGKILL/abnormal exits rebuild `meta.json` via `recover`
- **Result review** — `review --task <id>` aggregates per-file diffs with approve/reject decisions; `--deep` adds independent-model analysis
- **MCP server & client** — expose agent_go as an MCP server (stdio or HTTP/SSE, 6 tools); subtasks can consume tools from external MCP servers (`mcp__{server}__{tool}`)
- **Artifact export** — subtasks write deliverables to `__artifacts__/` in their worktree; with `--artifact-dir`, files are exported to your directory before worktree cleanup
- **Evaluation** — `eval quality/perf/cost/reliability/ux` built-in analytics
- **Release gate** — `eval gate --baseline 0.05` enforces $/pass rate budget (北极星指标); CI step fails on regression
- **Model benchmark** — `eval bench --models M1,M2` compares N models on standard task suite (sequential `--parallel 1`, dynamic timeout, per-subtask `binary_pass`/`semantic_pass`/`plan_step_count`); `eval baseline` runs `claude -p` bare-line control; `eval models` outputs decision matrix ($/pass unified, K8, lint/test regression); records `timed_out`/`judge_model`/`planner_model`/`source_batch` per run (S10-P1/P2); `eval judge` runs cross-model judgment with self-bias quantification
- **Cross-judgment** — `eval judge --judge-models M1,M2` runs N-model mutual review with self-bias prevention; `eval judge calibrate` for human calibration

## Quick Start

```bash
git clone https://github.com/urika/agent_go.git
cd agent_go
pip install -e .

export AGENT_GO_API_KEY="sk-ant-..."

# Run a task
agent_go run ~/my-project "重构认证模块，从 JWT 迁移到 OAuth2"

# Headless with concurrency and remote push
agent_go run ~/my-project "升级所有依赖" --yes --parallel 3 --remote origin

# Export subtask artifacts (worktree/__artifacts__/) to a directory before cleanup
agent_go run ~/my-project "生成 Q3 季度汇报 PPT" --yes --artifact-dir ~/Desktop/reports

# With explicit skills
agent_go run ~/my-project "安全审查" --skill security-review --docs "README.md,docs/spec.md"

# With custom config file
agent_go --config /path/to/config.json run ~/my-project "<task>"
```

## Commands

| Command | Description |
|---------|-------------|
| `run <repo> '<task>'` | Plan, decompose, execute |
| `resume <task-id>` | Resume a paused/interrupted task |
| `recover <task-id>` | Rebuild `meta.json` from worktree state after SIGKILL/abnormal interruption |
| `list` | List all historical tasks |
| `show <task-id>` | Show task details with agent roles and skill hits |
| `status` | Live status monitoring (`--watch` for auto-refresh, `--no-tui` text mode) |
| `pr <task-id>` | Generate and create PR (requires `gh` CLI; `--push` to auto-create) |
| `review --task <id>` | **Result review (M7)** — aggregate per-file diff summary + approve/reject/changes-requested; `--deep` for independent-model analysis |
| `review <repo>` | Code review with Claude on a repo or PR |
| `inspect <task-id>` | Inspect preserved worktrees of failed/blocked subtasks (`--json` available) |
| `replay <task-id>` | Execution timeline replay (`--json` for machine-readable) |
| `checkpoint list/restore/delete` | Worktree file snapshot manager for subtask rollback |
| `plan-history <id>` / `plan-diff <id>` | Plan version history and diff between versions |
| `mcp` | Start MCP server (stdio, or `--http` for HTTP/SSE with Bearer token auth) |
| `router <show/enable/disable/set-role>` | Role-aware model routing configuration |
| `config` | View current configuration |
| `clean` | Remove all task data |
| `skills list/show` | List Skills / show a Skill's full SKILL.md |
| `agents` | List available Agent types |
| `ci` | Generate GitHub Actions workflow |
| `cache` | Plan cache management (`list`/`clean`/`clear`/`stats`) |
| `eval` | Quality/performance/cost evaluation |
| `eval gate` | **Release gate** — fail CI if $/pass rate exceeds baseline (北极星指标) |
| `eval gate --check-regression` | **Regression gate** — fail if $/pass rate regressed >10% vs stored baseline (PRD "不劣化") |
| `eval gate --update-baseline` | Reset stored baseline to current rate (use after model upgrades) |
| `eval bench` | **Model benchmark** — compare N models on standard task suite, output decision matrix; `--source-batch` records batch identity; sequential `--parallel 1` + dynamic timeout + code quality (lint/tests) collection (S10-P2) |
| `eval baseline` | **Bare-line control** — `claude -p` runs without harness, quantifies harness ROI (S10-P2) |
| `eval models` | **Productivity report** — per-model pass_rate / $/pass / K8 / lint/test regression / recommendation |
| `eval judge` | **Cross-judgment** — N-model mutual review with self-bias prevention + self-bias quantification |
| `eval judge calibrate` | **Human calibration** — compare LLM vs human scores, detect unreliable judges |
| `web` | **Read-only Web observability** — task list / subtask detail / logs / metering / timeline at `127.0.0.1:8091` (`--host`/`--port`/`--token` optional) |

### Options

| Flag | Description |
|------|-------------|
| `--yes, -y` | Skip all confirmations, run headless |
| `--headless` | Subtasks use `claude -p` (non-interactive) |
| `--quiet, -q` / `--verbose` | Suppress non-error output / show debug output |
| `--parallel N` | Max concurrent subtasks (default 1) |
| `--max-retries N` | Max verification-fix retries per subtask (default 3) |
| `--preserve-worktrees` / `--no-preserve` | Keep failed/blocked worktrees for manual inspection |
| `--no-verify-block` | Verification failure does NOT block downstream subtasks (default: block) |
| `--goal` / `--goal-hook` | Inject /goal + Stop Hook into subtask worktrees |
| `--semantic-eval` / `--no-semantic-eval` | Toggle LLM semantic evaluation in verification |
| `--agent-loop` | Hybrid strategy: simple tasks via direct API, complex tasks via `claude -p` |
| `--interactive` | Start TUI dashboard monitoring subtask execution |
| `--step-confirm` | Pause to confirm before each wave |
| `--auto-init` | Auto `git init` + first commit for non-git target dirs |
| `--artifact-dir <dir>` | Export artifacts (subtask `__artifacts__/` files) to this directory |
| `--docs <paths>` | Mount reference documents (comma-separated) |
| `--issue <N>` | Link GitHub issue (injected into commits) |
| `--skill <names>` | Load Skills by name (comma-separated) |
| `--agent-type <type>` | Set default Agent type for all subtasks |
| `--remote <url>` | Push worktree branches to remote |
| `--no-cache` | Skip Plan cache lookup |

## Architecture

```
agent_go/
├── __init__.py          # Package exports
├── cli.py               # CLI entry points + all subcommands
├── config.py            # Config loading, API key resolution, logging
├── api.py               # call_api, generate_plan, decompose_fallback
├── ui.py                # confirm_plan, confirm_subtasks, plan_to_subtasks
├── planning.py          # Planning helpers (estimate_task_duration)
├── git_utils.py         # analyze_project, worktree create/remove/prune
├── subtask.py           # _git_merge_upstream, _run_headless, worker metering
├── executor.py          # run_subtask — core subtask runner + verification loop
├── pipeline.py          # _run_pipeline — wave scheduler + cleanup + SIGINT
├── utils.py             # _format_commit, _slugify, shell safety
├── skills.py            # Skill loading, discovery, rendering
├── agents.py            # Agent type definitions
├── role_skill_map.py    # Config-driven role->skill matching rules
├── router.py            # Role-aware model routing (planner/worker/reviewer)
├── pricing.py           # Model price table + MODEL_TIER
├── metrics.py           # Data collection (timing/change_stats/token/metering)
├── evaluator.py         # Verification evaluation + failure summary
├── agent_loop.py        # Autonomous agent loop (--agent-loop)
├── tool_executor.py     # Tool registry for agent loop
├── notify.py            # Multi-channel notifications (desktop/webhook/command)
├── goal_injector.py     # /goal + Stop Hook injection
├── console.py           # Console output abstraction
├── tui.py               # Curses status dashboard
├── workflow_gen.py      # CI workflow auto-generation
├── replay.py            # Execution timeline replay
├── checkpoint.py        # Worktree file snapshot manager
├── recover.py           # Rebuild meta.json from worktree state
├── mcp_server.py        # MCP server (stdio): tools + resources + prompts
├── mcp_http.py          # MCP server HTTP/SSE transport
├── mcp_client.py        # MCP consumption layer (external MCP tools in subtasks)
├── eval.py              # Quality/perf/cost/reliability/ux analysis + gate
├── bench.py             # Model benchmark orchestrator (eval bench)
├── cross_judge.py       # Cross-model judgment matrix + human calibration
├── assessment.py        # False-positive evaluation data layer
├── artifacts.py         # Artifact export (S9-B: __artifacts__/ -> --artifact-dir)
├── web_server.py        # Web observability platform: tasks/overview/cost/models/config/storage (agent_go web)
└── lint.py              # AST-based static checks
agent_go.py               # Entry-point wrapper
tests/                    # 1569 tests across 61 test files
eval_suite/               # Standard task suite for eval bench (22 tasks + 4 fixtures)
```

## Configuration

Config at `~/.agent_go/config.json` (auto-created). See [`config.example.json`](config.example.json).

### API Providers

| Provider | Default Model |
|----------|--------------|
| `anthropic` | `claude-sonnet-4-20250514` |
| `openai` | `gpt-4o` |
| `deepseek` | `deepseek-chat` |
| `custom` | (any OpenAI-compatible endpoint) |

### Key Settings

| Key | Default | Description |
|-----|---------|-------------|
| `behavior.auto_confirm_plan` | `false` | Skip plan confirmation |
| `behavior.auto_confirm_subtasks` | `false` | Skip subtask confirmation |
| `behavior.max_plan_iterations` | `5` | Max plan regeneration |
| `skills.auto_discover` | `false` | Auto-match skills by keywords |
| `agents.default` | `developer` | Default Agent type |
| `worker_models.hard` | `""` | S4: hard 难度子任务路由的模型（空 = CLI 默认） |

### Role-Skill Mapping

`~/.agent_go/role_skill_map.json` defines rules for matching subtasks to Agent types and Skills. Supports keyword matching, file pattern matching, and agent type matching. Required skills are always injected; recommended skills fill in when LLM doesn't specify.

## Testing

```bash
pip3 install pytest pytest-mock

pytest tests/              # 1569 tests (~60s)
pytest tests/ -q           # Quiet mode
pytest tests/ -k "not integration"  # Unit tests only
```

## Requirements

- Python 3.9+
- [Claude Code](https://claude.ai/code) CLI (`claude`)
- Optional: [Greywall](https://github.com/anthropics/greywall) for sandboxed execution
- API key for Plan generation

## License

MIT License — see [LICENSE](LICENSE).
