# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

agent_go is a single-file Python orchestration tool that wraps Claude Code with a structured Plan → Decompose → Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code.

No external Python dependencies — uses only stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`).

## Commands

```bash
# Run a task (requires AGENT_GO_API_KEY env var for Plan Mode)
export AGENT_GO_API_KEY="sk-ant-..."
python3 agent_go.py run <repo-path> '<task-description>'

# Skip all confirmations, run headless
python3 agent_go.py run <repo-path> '<task>' --yes

# Run with reference documents pre-loaded
python3 agent_go.py run <repo-path> '<task>' --docs "README.md,docs/spec.md"

# List historical tasks
python3 agent_go.py list

# Show task details
python3 agent_go.py show <task-id>

# View current config
python3 agent_go.py config

# Force interactive mode (overrides auto_confirm_plan/auto_confirm_subtasks)
AGENT_GO_INTERACTIVE=1 python3 agent_go.py run <repo-path> '<task>'
```

## Testing

See [TESTING.md](TESTING.md) for full test documentation.

```bash
# Run all 91 tests (unit + integration)
pytest tests/           # ~3.5s

# Unit tests only (pure functions, no mocking)
pytest tests/ -k "not integration"

# Integration tests (all external calls mocked)
pytest tests/test_integration.py -v
```

No build step needed — this is a pure Python stdlib project.

## Architecture

```
cmd_run()
  ├── analyze_project()        → git ls-files or find
  ├── get_git_info()           → remote, branch, commit
  ├── get_resource_map()       → directories, config files
  ├── generate_plan()          → calls external LLM API, returns structured JSON
  │     └── call_api()         → unified Anthropic/OpenAI/DeepSeek/custom interface
  ├── confirm_plan()           → Y/S/D/E/R/N interactive loop
  ├── plan_to_subtasks()       → injects agent_prompt + shared_resources into each subtask
  ├── confirm_subtasks()       → Y/N/E/A/D interactive loop
  └── for each subtask:
        run_subtask()
          ├── git clone / shutil.copytree → isolated worktree
          ├── writes TASK.md with agent prompt
          ├── spawns claude (with greywall if available)
          └── captures git diff --stat
```

## Key Design Decisions

- **Config file**: `~/.agent_go/config.json` (auto-created on first run). Merged with `DEFAULT_CONFIG` for forward compatibility with new fields.
- **API key resolution**: `AGENT_GO_API_KEY` env var takes priority over `config.json` `api_key` field.
- **Provider abstraction**: `call_api()` adapts request/response format per provider (Anthropic uses `x-api-key` header and `content[0].text`; OpenAI/DeepSeek/custom use `Authorization: Bearer` and `choices[0].message.content`).
- **Plan JSON contract**: API must return `{overview, steps[{id, title, description, files, verification, risks, agent_prompt}], dependencies, estimated_effort, shared_resources}`. The `agent_prompt` field is critical — it's the complete instruction given to Claude Code for that subtask.
- **Three-tier fallback**: External API → local model (localhost:8000) → rule-based decomposition (`DECOMPOSE_RULES` keyword matching).
- **Default confirm mode**: `config.behavior.auto_confirm_plan` and `auto_confirm_subtasks` enable non-interactive mode. Override with `AGENT_GO_INTERACTIVE=1`.
- **Reference docs**: Injected into the Plan API prompt. Single file max 15000 chars, directory `.md` files max 8000 chars each. Plan iteration capped at `max_plan_iterations` (default 5).
- **Logging**: Dual-format — INFO level human-readable lines + DEBUG level JSON events (key: `api_call`, `plan_generate`, `plan_complete`, `subtask_start`, `subtask_complete`, `user_plan_choice`, `user_verify`, etc.).
- **Sandbox**: Prefers `greywall` wrapper, falls back to native `claude` if not installed.

## Configuration Reference

Config file: `~/.agent_go/config.json` (auto-created on first run). See `config.example.json` for a clean template. Env vars override config.

### plan_api

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | `anthropic` | `anthropic` / `openai` / `deepseek` / `custom` |
| `base_url` | (per provider) | API endpoint |
| `api_key` | `""` | Leave empty; set `AGENT_GO_API_KEY` env var instead |
| `model` | `claude-sonnet-4-20250514` | DeepSeek: `deepseek-chat`, OpenAI: `gpt-4o` |
| `max_tokens` | `4096` | Response token limit |
| `temperature` | `0.2` | Lower = more deterministic |

### behavior

| Key | Default | Description |
|-----|---------|-------------|
| `auto_confirm_plan` | `false` | Auto-confirm plan (skip Y/S/D/E/R/N prompt) |
| `auto_confirm_subtasks` | `false` | Auto-confirm subtask list |
| `show_agent_prompt` | `true` | Show agent prompt preview |
| `show_resource_map` | `true` | Show shared resource map |
| `max_plan_iterations` | `5` | Max plan regeneration iterations |

### headless

| Key | Default | Description |
|-----|---------|-------------|
| `idle_timeout` | `300` | Kill claude after N seconds of no output |
| `max_retries` | `2` | Max retries on timeout |
| `heartbeat_interval` | `30` | Log heartbeat when idle for N seconds |
