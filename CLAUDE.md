# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

agent_go is a modular Python CLI tool (17 modules, ~4330 lines) that wraps Claude Code with a structured Plan -> Decompose -> Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code. Supports concurrent execution, interrupt/resume, config-driven role-skill mapping, and remote branch push.

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

## Key Modules (17 modules, ~4330 lines)

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI commands: run, resume, list, show, status, pr, config, clean |
| `api.py` | LLM API: generate_plan, call_api, decompose_fallback |
| `ui.py` | Interactive prompts: confirm_plan, confirm_subtasks, plan_to_subtasks |
| `executor.py` | Core subtask runner: worktree create, skill load, claude spawn, verify |
| `pipeline.py` | Wave scheduler, concurrency, worktree/tag cleanup, remote push |
| `subtask.py` | Claude -p headless runner, git merge upstream |
| `git_utils.py` | Project analysis, worktree create/remove/prune, gc.auto control |
| `skills.py` | Skill loading, discovery, rendering (YAML frontmatter + Markdown) |
| `agents.py` | Agent type system: developer/architect/reviewer/tester |
| `role_skill_map.py` | Config-driven rule matching: keywords, file patterns, agent type |
| `config.py` | Config loading, logging, API key resolution |
| `utils.py` | Commit formatting, slugify, shell safety, version detection |

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
- **Sandbox**: Prefers `greywall`, falls back to native `claude`.

## Testing

```bash
pytest tests/           # 163 tests (~4s)
pytest tests/ -q        # Quiet mode
pytest tests/ -k "not integration"  # Unit tests only
pytest tests/ -k "TestFormatCommit" -v  # Run specific test class
```

## File Organization

```
agent_go/           # 17 Python modules (~4330 lines)
tests/              # 15 test files, 163 tests
docs/design/        # Design docs, requirements, product roadmap
docs/archive/       # Historical code review records
```
