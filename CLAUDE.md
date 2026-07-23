# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

agent_go is a modular Python CLI tool (18 source files, ~4950 lines) that wraps Claude Code with a structured Plan -> Decompose -> Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code. Supports concurrent execution, interrupt/resume, config-driven role-skill mapping, and remote branch push.

No external Python dependencies — uses only stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`).

## Commands

```bash
export AGENT_GO_API_KEY="sk-ant-..."

# Run a task
python3 agent_go.py run <repo-path> '<task>'

# Headless with concurrency and remote push
python3 agent_go.py run <repo-path> '<task>' --yes --parallel 3 --remote origin

# With explicit skills and agent type
python3 agent_go.py run <repo-path> '<task>' --skill security-review --agent-type reviewer

# Resume an interrupted task
python3 agent_go.py resume <task-id>

# Monitor running tasks
python3 agent_go.py status --watch

# List / show / clean
python3 agent_go.py list
python3 agent_go.py show <task-id>
python3 agent_go.py clean

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
  │     └── call_api()         → unified Anthropic/OpenAI/DeepSeek/custom
  ├── confirm_plan()           → Y/S/D/E/R/N interactive (--yes skips)
  ├── plan_to_subtasks()       → injects agent_prompt + applies role-skill rules
  ├── confirm_subtasks()       → Y/N/E/A/D interactive
  └── _run_pipeline()
        ├── disable gc.auto    → concurrency safety
        ├── topological waves  → ThreadPoolExecutor with --parallel N
        ├── run_subtask()
        │     ├── git worktree add -b agent_go/{task_id}/{sub_id}
        │     ├── git merge upstream tag → artifact passing
        │     ├── writes TASK.md (path-rewritten for isolation)
        │     ├── spawns claude -p (or greywall wrapper)
        │     ├── loads skills + agent type per subtask
        │     ├── git commit + tag ({task_id}/{sub_id} namespaced)
        │     └── verification + auto-retry on failure
        ├── push branches to remote (if --remote)
        ├── remove worktrees + delete tags + restore gc.auto
        └── final report
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI commands: run, resume, list, show, status, pr, config, clean, cache |
| `api.py` | LLM API: generate_plan, call_api, decompose_fallback, plan cache |
| `ui.py` | Interactive prompts: confirm_plan, confirm_subtasks, plan_to_subtasks |
| `executor.py` | Core subtask runner: worktree create, skill load, claude spawn, verify |
| `pipeline.py` | Wave scheduler, concurrency, worktree/tag cleanup, remote push, SIGINT |
| `subtask.py` | Claude -p headless runner, git merge upstream |
| `git_utils.py` | Project analysis, worktree create/remove/prune, gc.auto control |
| `skills.py` | Skill loading, discovery, rendering (YAML frontmatter + Markdown) |
| `agents.py` | Agent type system: developer/architect/reviewer/tester |
| `role_skill_map.py` | Config-driven rule matching: keywords, file patterns, agent type |
| `config.py` | Config loading, logging, API key resolution |
| `console.py` | Unified output layer: quiet/verbose modes, table/data formatting |
| `utils.py` | Commit formatting, slugify, shell safety, version detection, doc reading |
| `metrics.py` | Data collection: timing, change stats, token counts, merge results |
| `eval.py` | Quality/perf/cost/reliability/ux evaluation and reporting |
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
pytest tests/           # 639 tests (~14s)
pytest tests/ -q        # Quiet mode
pytest tests/ -k "not integration"  # Unit tests only
pytest tests/ -k "TestFormatCommit" -v  # Run specific test class
```

## File Organization

```
agent_go/           # 18 package modules (~5000 lines)
tests/              # 33 test files, 639 tests
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
