# agent_go

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-163%20passed-green)](tests/)
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
- **Evaluation** — `eval quality/perf/cost/reliability/ux` built-in analytics

## Quick Start

```bash
git clone https://github.com/urika/agent_go.git
cd agent_go

export AGENT_GO_API_KEY="sk-ant-..."

# Run a task
python3 agent_go.py run ~/my-project "重构认证模块，从 JWT 迁移到 OAuth2"

# Headless with concurrency and remote push
python3 agent_go.py run ~/my-project "升级所有依赖" --yes --parallel 3 --remote origin

# With explicit skills
python3 agent_go.py run ~/my-project "安全审查" --skill security-review --docs "README.md,docs/spec.md"
```

## Commands

| Command | Description |
|---------|-------------|
| `run <repo> '<task>'` | Plan, decompose, execute |
| `resume <task-id>` | Resume a paused/interrupted task |
| `list` | List all historical tasks |
| `show <task-id>` | Show task details with agent roles and skill hits |
| `status` | Live status monitoring (`--watch` for auto-refresh) |
| `pr <task-id>` | Generate and create PR (requires `gh` CLI) |
| `config` | View current configuration |
| `clean` | Remove all task data |
| `skills` | List available Skills |
| `agents` | List available Agent types |
| `ci` | Generate GitHub Actions workflow |
| `review` | Code review with Claude |
| `cache` | Plan cache management |
| `eval` | Quality/performance/cost evaluation |

### Options

| Flag | Description |
|------|-------------|
| `--yes, -y` | Skip all confirmations, run headless |
| `--headless` | Subtasks use `claude -p` (non-interactive) |
| `--parallel N` | Max concurrent subtasks (default 1) |
| `--docs <paths>` | Mount reference documents (comma-separated) |
| `--issue <N>` | Link GitHub issue (injected into commits) |
| `--skill <names>` | Load Skills by name (comma-separated) |
| `--agent-type <type>` | Set default Agent type for all subtasks |
| `--remote <url>` | Push worktree branches to remote |
| `--no-cache` | Skip Plan cache lookup |

## Architecture

```
agent_go/
├── __init__.py          # Package exports (v2.0.0)
├── cli.py               # cmd_run, cmd_resume, cmd_status — CLI entry points
├── config.py            # Config loading, API key resolution, logging
├── api.py               # call_api, generate_plan, decompose_fallback
├── ui.py                # confirm_plan, confirm_subtasks, plan_to_subtasks
├── git_utils.py         # analyze_project, worktree create/remove/prune
├── subtask.py           # _git_merge_upstream, _run_headless
├── executor.py          # run_subtask — core subtask runner
├── pipeline.py          # _run_pipeline — wave scheduler + cleanup
├── utils.py             # _format_commit, _slugify, shell safety
├── skills.py            # Skill loading, discovery, rendering
├── agents.py            # Agent type definitions
├── role_skill_map.py    # Config-driven role->skill matching rules
├── metrics.py           # Data collection (timing/change_stats/token)
├── eval.py              # Quality/perf/cost/reliability/ux analysis
├── tui.py               # Curses status dashboard
├── workflow_gen.py      # CI workflow auto-generation
agent_go.py               # Entry-point wrapper
tests/                    # 163 tests across 13 test files
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

### Role-Skill Mapping

`~/.agent_go/role_skill_map.json` defines rules for matching subtasks to Agent types and Skills. Supports keyword matching, file pattern matching, and agent type matching. Required skills are always injected; recommended skills fill in when LLM doesn't specify.

## Testing

```bash
pip3 install pytest pytest-mock

pytest tests/              # 163 tests (~4s)
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
