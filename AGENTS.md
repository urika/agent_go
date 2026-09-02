# AGENTS.md

Guidance for AI coding agents working in this repository. This file keeps only conceptual content and pointers; detailed references live in `docs/design/`:

| Reference doc | Content |
|---|---|
| `docs/design/cli-commands.md` | Full CLI command reference (all subcommands + flags) |
| `docs/design/module-catalog.md` | Module catalog (brief) + appendix: detailed per-module purposes |
| `docs/design/runtime-design-decisions.md` | Key design decisions, full text (worktree/routing/cost/sandbox/humility layer) |
| `docs/design/code-review-checklist.md` | Code review checklist with examples (loop truncation etc.) |
| `docs/design/config-schema.md` | Config field reference (`~/.agent_go/config.json`) |

## Project Overview

agent_go is a modular Python CLI tool (69 modules, ~39,800 lines) that wraps Claude Code with a structured Plan → Decompose → Execute workflow. It calls external LLM APIs to generate execution plans, then runs each step as an isolated subtask in a git worktree with Claude Code. Supports concurrent execution, interrupt/resume/crash-recovery, config-driven role-skill mapping, verification loop with auto-retry, role-aware and difficulty-based model routing, worktree preservation for failed tasks, multi-channel notification, remote branch push, and an MCP server/client layer (agent_go can be consumed as an MCP server and can itself consume external MCP tools inside subtasks).

## Tech Stack & Build

- **Python ≥ 3.9**, setuptools build backend (`pyproject.toml`), console entry point `agent_go = agent_go.cli:main`. Alternative entries: `python3 -m agent_go`, `python3 agent_go.py`.
- **Runtime has zero external dependencies** — stdlib only (`urllib`, `subprocess`, `json`, `logging`, `pathlib`, `http.server`). Do not add runtime deps without explicit discussion.
- Dev/test deps (external): `pytest`, `pytest-mock`, `ruff`, `mypy`, `pyyaml`.

```bash
pip install -e .                 # editable install (build)
pip3 install pytest pytest-mock  # test deps
```

## Commands (quick start)

Full command reference: `docs/design/cli-commands.md`.

```bash
export AGENT_GO_API_KEY="sk-ant-..."

agent_go run <repo-path> '<task>'                                     # run a task
agent_go run <repo-path> '<task>' --yes --parallel 3 --remote origin  # headless + concurrency + push
agent_go run <repo-path> --spec docs/tasks/task-xxx.md --yes          # run with Task Spec (L1 admission gate; --force skips)
agent_go resume <task-id>                                             # resume interrupted task
agent_go recover <task-id>                                            # rebuild meta.json after SIGKILL
agent_go inspect <task-id>                                            # preserved worktrees of failed subtasks
agent_go review --task <task-id>                                      # aggregate result review (approve/reject)
agent_go eval bench --suite golden                                    # model benchmark
agent_go web --port 8091                                              # Web 操作台
```

Other command groups: `list`/`show`/`status`/`report`, `pr`/`merge`, `config` (local/cloud/status profiles), `models` (model registry), `spec` (template/validate), `skills`/`agents`, `kanban`, `problems`/`trust`/`attribution` (humility layer), `replay`/`checkpoint`/`plan-history`/`plan-diff`, `governance`/`deviation`/`decision`, `cache`, `migrate`, `ci`, `mcp`, `clean`, `router`.

Dirty working tree at run start: headless/`--yes` ABORTS (fail-safe) unless `--baseline` (commit dirty changes as baseline first) or `--allow-dirty` (subtasks build from HEAD, won't see dirty changes).

## Runtime Architecture

```
cmd_run()
  ├── analyze_project() / get_git_info() / get_resource_map()
  ├── spec.py                  → optional Task Spec admission gate
  ├── generate_plan()          → LLM API; injects skill inventory + role-skill rules;
  │                              router.py role-aware routing; metering.jsonl per call
  ├── confirm_plan() / plan_to_subtasks() / confirm_subtasks()  (--yes skips)
  └── _run_pipeline()
        ├── disable gc.auto (concurrency safety)
        ├── topological waves → ThreadPoolExecutor (--parallel N)
        │   └── upstream failed/blocked → downstream blocked & skipped
        ├── run_subtask()
        │     ├── git worktree add -b agent_go/{task_id}/{sub_id}
        │     ├── git merge upstream tag → artifact passing
        │     ├── writes TASK.md; difficulty → worker_models → claude --model
        │     ├── spawns claude -p (interactive path may wrap greywall)
        │     ├── git commit + tag ({task_id}/{sub_id} namespaced)
        │     └── verification + auto-retry loop (--max-retries)
        ├── push branches (if --remote)
        └── cleanup worktrees/tags; failed/blocked worktrees preserved for review

recover → rebuild meta.json from worktree state after SIGKILL
resume  → rerun uncompleted subtasks from meta.json
```

**Completion boundary**: commit is the sole completion boundary. `recover` only classifies worktree state (commit+verify-pass → completed; commit+verify-fail → failed; no commit+orphan changes → reset; no commit+no changes → no_changes) — it never commits orphan changes itself.

Module layout (full catalog with per-module responsibilities: `docs/design/module-catalog.md`): `cli.py` (entry/dispatch), `api.py` (LLM plan), `pipeline.py` (DAG waves/concurrency), `executor.py` + `subtask.py` (per-subtask worktree/claude/verify), `router.py`/`models_registry.py`/`pricing.py` (model routing & cost), `evaluator.py`/`verify_chain.py` (verification), `eval.py`/`bench.py`/`metrics.py` (measurement), `mcp_server.py`/`mcp_http.py`/`mcp_client.py` (MCP dual role), `web_server.py`/`task_runner.py`/`tui.py` (UIs), `problems.py`/`knowledge.py`/`deviation.py` (cross-task memory), `config.py`/`console.py`/`utils.py` (shared infra).

## Key Design Decisions (速览)

Full text: `docs/design/runtime-design-decisions.md`.

- **Worktree isolation + artifact passing**: namespaced branches/tags `{task_id}/{sub_id}` share the repo object db; upstream tags are `git merge`d directly into downstream worktrees.
- **Three-tier plan fallback**: external API → local model (localhost:8000) → rule-based decomposition.
- **Verification loop**: failed subtasks auto-retry with full failure context; semantic-eval diff base is the subtask's pre-work HEAD after upstream merge (ISSUE-51).
- **Difficulty routing**: planner-tagged `difficulty` → `worker_models` → `claude --model`; `worker_base_url` is the single worker entry point (fine-grained routing lives proxy-side); `worker_backends` DEPRECATED.
- **MCP dual role**: agent_go is both MCP server and consumer (`mcp__{server}__{tool}` namespaced); consumer failures degrade to warnings.
- **Cost**: metering recomputes `cost_usd` from the actual model (claude route names may resolve to cheaper backends); 3-layer cost control all default OFF (enable only after `eval cost-baseline` calibration).
- **Output abstraction**: all user-facing output via `Console` class — no bare `print()`.
- **Sandbox**: interactive path prefers `greywall --watch --` (观察期全放行全记录); headless always runs native `claude -p`.
- **谦逊层 (H1-H4)**: orthogonal observation of blind spots (`meta.json blind_spots`), layer attribution, cross-task Problem memory (`~/.agent_go/problems.jsonl`); trust metrics (#49) gate phase-D autonomy.

## Code Style

- Ruff: `target-version = "py39"`, `line-length = 120`, lint rules `E,F,W` with `E501` ignored (formatter manages length). CI runs `ruff check agent_go/ --select=E,F,W --ignore=E501`.
- MyPy: `python_version = 3.9`, `ignore_missing_imports = true`, `warn_return_any = false` (JSON/config merging inherently returns `Any`).
- All user-facing output goes through the injected `Console` class (`console.py`) — never bare `print()`.
- Commit messages are formatted by `utils._format_commit` (prefix/scope detection); don't hand-roll commit messages in new code.
- Match the surrounding file's conventions; module header comments and docstrings are largely Chinese — follow the file you're editing.
- Module changes have doc-sync rules (`docs/design/module-catalog.md` §模块变更规则): new core module → update the catalog; public interface change → sync `docs/spec.md`; state/data-contract/boundary change → add/update an ADR (`docs/design/adr/`).

## Testing

```bash
pytest tests/                       # 2863 tests (115 files); --tb=short via pyproject addopts
pytest tests/ -k "not integration"  # unit tests only
pytest tests/test_format_commit.py::TestFormatCommitChinese::test_feat_add
```

- Integration tests mock all external deps (`generate_plan`, `run_subtask`, `_run_headless`, `subprocess.run`) — no real LLM/Claude/Git needed.
- Shared fixtures (`logger`, `temp_dir`, `sample_plan`, `minimal_plan`) live in `tests/conftest.py`.
- Custom marker `@pytest.mark.flaky` is registered (annotation only, no rerun mechanism).
- CI (`.github/workflows/test.yml`, push/PR to main): `pytest tests/ -q` → `agent_go eval gate --baseline 0.05` ($/pass release gate; passes automatically with no data) → ruff → mypy, on Python 3.9.

## Security Considerations

- **API keys**: `AGENT_GO_API_KEY` env var takes precedence over `config.json` `api_key`; `${VAR_NAME}` template vars resolve from the environment. The model registry (`models_registry.py`) stores only `key_ref` (env var name or secret-file reference) — never plaintext keys.
- **Verification command filter**: planner-generated verification commands pass `_is_safe_verification_command` (`utils.py`) — safe-prefix allowlist plus rejection logging; don't bypass it when adding execution paths.
- **Web console**: `agent_go web` binds `127.0.0.1` by default; use `--admin-token`/`--viewer-token` for multi-role access (admin full / viewer read-only). MCP HTTP transport uses Bearer token auth.
- **Secrets on disk**: task data, metering, and configs live under `~/.agent_go/` — treat as user-private; never commit contents into this repo.

## Code Review Checklist (速览)

Full checklist with examples: `docs/design/code-review-checklist.md`.

- **Loop body truncation (critical)**: after a `for x in ...:` loop, lines referencing the loop variable at the outer level only process the last item — per-item processing belongs inside the loop. (`lint.py` has an AST check for this pattern.)
- **Thread safety**: shared mutable state across threads (pipeline uses `ThreadPoolExecutor`) must be lock-protected.
- **Mock `side_effect` exhaustion**: expected calls must not exceed the list length (`StopIteration`).
- **Subprocess pipe deadlock**: use `capture_output=True` or `communicate()`, never `wait()` on a filling pipe.
- **Codegen/config drift**: `config.example.json` must stay in sync with `DEFAULT_CONFIG` (`config.py`); pricing additions update `pricing.py`; llama-defender contract changes are drift-checked by `tools/check_llama_contracts.py`.

## File Organization

```
agent_go/           # 69 Python modules (~39,800 lines); catalog: docs/design/module-catalog.md
tests/              # 115 test files, 2863 tests; fixtures in conftest.py
eval_suite/         # eval bench suite: golden_tasks/ m3_tasks/ phaseD_tasks/ fixtures/ baselines/, results_*.jsonl
docs/design/        # Design docs + reference docs split out of AGENTS.md (see top table); adr/ for ADRs
docs/archive/       # Historical code review records
tools/              # Dev scripts (bench split/recompute, contract drift checks, markdown link check, spec smoke)
agent_go.py         # Root-level convenience entry (python3 agent_go.py run ...)
config.example.json # Example config — keep in sync with DEFAULT_CONFIG
```
