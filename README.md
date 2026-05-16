# agent_go

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-107%20passed-green)](tests/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Stdlib Only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)]()

**Plan Mode orchestration tool** that wraps Claude Code with a structured `Plan → Decompose → Execute` workflow. AI generates an execution plan, then each step runs as an isolated subtask in its own git worktree with Claude Code.

## Why agent_go?

Give Claude Code a single complex task — refactoring auth, upgrading dependencies, adding a feature — and it can get lost in the weeds. agent_go breaks the work into **2–5 independently executable subtasks**, each with its own isolated worktree, verification command, and agent prompt. The results flow downstream via git merge, so later subtasks build on earlier ones.

- **Structured execution** — LLM generates the plan, not ad-hoc decisions
- **Isolated worktrees** — each subtask gets its own sandbox; no cross-contamination
- **Concurrent execution** — topological wave scheduling with `--parallel N`
- **Interrupt & resume** — Ctrl+C to pause, `agent_go resume` to pick up where you left off
- **Zero dependencies** — pure Python stdlib: `urllib`, `subprocess`, `json`, `pathlib`

## Quick Start

```bash
# Install (no deps, no build — just clone)
git clone https://github.com/urika/agent_go.git
cd agent_go

# Set your API key
export AGENT_GO_API_KEY="sk-ant-..."

# Run a task
python3 agent_go.py run ~/my-project "重构认证模块，从 JWT 迁移到 OAuth2"

# Run headless with concurrency
python3 agent_go.py run ~/my-project "升级所有依赖" --yes --parallel 3

# Run with reference documents
python3 agent_go.py run ~/my-project "添加用户邀请功能" --docs "README.md,docs/spec.md"
```

## Workflow

```
agent_go run <repo> "<task>"
   │
   ├── analyze_project()       → git ls-files, find key dirs
   ├── generate_plan()         → LLM API returns structured JSON with steps
   ├── confirm_plan()          → Y/S/D/E/R/N interactive loop (skippable with --yes)
   ├── plan_to_subtasks()      → inject agent_prompt + shared resources into each step
   ├── confirm_subtasks()      → review final subtask list
   │
   └── for each wave:
         ├── run_subtask()      → git clone/merge → write TASK.md → spawn Claude Code
         ├── verify             → run verification command, auto-retry on failure
         └── merge upstream     → git merge results into downstream subtasks
```

## Commands

| Command | Description |
|---------|-------------|
| `run <repo> '<task>'` | Main entry: plan, decompose, execute |
| `resume <task-id>` | Resume a paused/interrupted task |
| `list` | List all historical tasks |
| `show <task-id>` | Show task details (plan, results, timings) |
| `status` | Live status of running tasks |
| `pr <task-id>` | Generate PR description (requires `gh` CLI) |
| `config` | View current configuration |
| `clean` | Remove all task data |

### Options

| Flag | Description |
|------|-------------|
| `--yes, -y` | Skip all confirmations, run headless |
| `--headless` | Subtasks use `claude -p` (non-interactive) |
| `--parallel N` | Max concurrent subtasks (default 1, recommended 3) |
| `--docs <paths>` | Mount reference documents (comma-separated, directories recursive) |
| `--issue <N>` | Link GitHub issue (injected into commits and TASK.md) |

## Architecture

```
agent_go/
├── __init__.py       # Package exports
├── cli.py            # cmd_run, cmd_resume, main — CLI entry points
├── config.py         # Config loading, API key resolution, logging
├── api.py            # call_api, generate_plan, decompose_fallback
├── ui.py             # confirm_plan, confirm_subtasks, print_plan — interactive prompts
├── git_utils.py      # analyze_project, get_git_info, get_resource_map
├── subtask.py        # _git_merge_upstream, _run_headless — Claude Code interaction
├── executor.py       # run_subtask — the core subtask runner
├── pipeline.py       # _run_pipeline — topological wave scheduler
└── utils.py          # _format_commit, _slugify, shell safety, version detection
agent_go.py            # Thin entry-point wrapper
```

## Configuration

Config lives at `~/.agent_go/config.json` (auto-created on first run). See [`config.example.json`](config.example.json).

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
| `behavior.auto_verify_subtask` | `false` | Skip verification prompt |
| `behavior.max_plan_iterations` | `5` | Max plan regeneration loops |
| `headless.idle_timeout` | `300` | Kill Claude after N seconds idle |
| `headless.max_retries` | `2` | Max retries on timeout |

### Fallback Chain

When the Plan API is unavailable, agent_go falls back through three tiers:

1. **Primary API** (Anthropic/OpenAI/DeepSeek) → structured JSON plan
2. **Local model** (`http://localhost:8000`) → LLM-based decomposition
3. **Rule-based** (`DECOMPOSE_RULES`) → keyword-pattern matching

## Testing

```bash
pip3 install pytest pytest-mock

# All 107 tests (~3.5s)
pytest tests/

# Unit tests only (<1s)
pytest tests/ -k "not integration"

# Integration tests (~2s, all external calls mocked)
pytest tests/test_integration.py -v
```

## Design

- **No external dependencies** — pure Python 3.9+ stdlib
- **Shell safety** — verification commands validated against whitelist + injection pattern detection before `shell=True` fallback
- **Thread-safe** — file-based locking for shared context under concurrent execution
- **API key security** — config file `chmod 600`, env var takes priority over file
- **Interrupt-safe** — SIGINT/SIGTERM handlers save state for later `resume`
- **Path safety** — regex boundary matching prevents worktree path injection; `--docs` paths validated within repo
- **Conventional Commits** — auto-detected from subtask titles (supports Chinese + English)

## Requirements

- Python 3.9+
- [Claude Code](https://claude.ai/code) CLI (`claude`)
- Optional: [Greywall](https://github.com/anthropics/greywall) for sandboxed execution
- API key for Plan generation (Anthropic/OpenAI/DeepSeek)

## License

MIT
