# agent_go CLI 命令参考

> 来源：AGENTS.md 拆分（2026-09-02）。完整命令清单从 AGENTS.md 迁出，AGENTS.md 只保留快速上手子集。

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

# With verification loop and worktree preservation
agent_go run <repo-path> '<task>' --max-retries 5 --preserve-worktrees

# With structured Task Spec (SDD input contract) — recommended for non-trivial tasks
agent_go spec template <repo-path> --output docs/tasks/task-xxx.md   # generate spec template
agent_go spec validate docs/tasks/task-xxx.md <repo-path>            # L1 admission gate
agent_go run <repo-path> --spec docs/tasks/task-xxx.md --yes         # run with spec
agent_go run <repo-path> --spec docs/tasks/task-xxx.md --force       # skip admission gate

# Auto git init for non-git target dir (local-only, no remote)
agent_go run <repo-path> '<task>' --auto-init

# Dirty working tree at run start: headless/--yes ABORTS (fail-safe) unless you pass one of:
#   --baseline    commit uncommitted changes as the baseline first (subtasks see correct HEAD)
#   --allow-dirty explicitly accept the risk (subtasks build from HEAD, won't see dirty changes)
agent_go run <repo-path> '<task>' --yes --baseline

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

# SDD governance / delivery (M1.2/M2.5): traceability matrix, deviations, manual merge
agent_go governance [--json]                      # spec/architecture compliance traceability
agent_go deviation [task-id] [--json]             # spec/architecture/acceptance deviation records
agent_go merge <task-id> [--push] [--remote origin]   # merge delivery branch into target
agent_go migrate failure-metadata [--apply]       # migrate historical task metadata

# Global Problem records (M5/B4+H3): cross-task failure memory — 「越用越聪明」查询入口
agent_go problems                               # 列出全部 Problem（按出现次数降序，含休眠/解法摘要）
agent_go problems --aggregate                   # 聚合分析（状态分布/复发数/top 失败模式）
agent_go problems --only <problem-id>           # 单个详情（生命周期/根因/历史解法/复发重开）
agent_go problems --json                        # 机器可读
agent_go trust                                  # #49 信任指标（阶段 D 放行门）：审查后修改率/交付后返工率/复发可见率/盲区命中率
agent_go trust --json                           # 机器可读；--all 含 bench 任务（默认真实任务口径）
agent_go trust --window N                       # 观察窗口：最近 N 个任务（D-0 口径，默认 30；0=不限）

# MCP server (JSON-RPC 2.0 over stdio, or HTTP/SSE)
agent_go mcp
agent_go mcp --http --host 127.0.0.1 --port 8090   # HTTP transport: POST /mcp + GET /mcp (SSE) + GET /health
AGENT_GO_MCP_HTTP_TOKEN=xxx agent_go mcp --http   # 启用 Bearer token 鉴权

# Monitor running tasks
agent_go status --watch

# Model benchmark / cross-judgment / evaluation / gate
agent_go eval bench --tasks eval_suite/ --candidate-models M1,M2 --repeat 3   # 启动前探测实际后端+校验定价（S12）
agent_go eval bench --suite golden                 # 预设套件: smoke/core/decision/stress/golden/phaseD
agent_go eval bench --with-delivery                # 本地交付 merge 闭合 accepted_delivery 判定（不推进 target 引用）
agent_go eval bench --source-batch results_v2      # 批次标识（跨批次追溯）
agent_go eval baseline --candidate-models M1,M2    # 对照基线：claude -p 裸跑（不走 harness）
agent_go eval models --results eval_suite/results.jsonl
agent_go eval cost-baseline --results eval_suite/results_v3.jsonl,eval_suite/results_v4_calib.jsonl
agent_go eval judge --results eval_suite/results.jsonl --judge-models M1,M2
agent_go eval judge --judge-subcommand calibrate --llm-scores ... --human-scores ...
agent_go eval calibrate-difficulty --results eval_suite/results.jsonl            # dry-run
agent_go eval calibrate-difficulty --results eval_suite/results.jsonl --apply     # 写回任务 YAML difficulty
agent_go eval metric-freeze --results eval_suite/results.jsonl                   # 可复现 Metric Freeze 报告
agent_go eval batch-manifest                                                    # 批次基线 manifest（M0-10）
agent_go eval validate-schema --results eval_suite/results.jsonl                # Bench schema 校验
agent_go eval gate --baseline 0.05
agent_go eval gate --check-regression --update-baseline

# PR / CI workflow generation / cache / router / skills / agents / config
agent_go pr <task-id> --push
agent_go ci --dry-run
agent_go cache stats
agent_go router show
agent_go router set-role <role> --provider <p> --model <m> --base-url <url>
agent_go router recommend [--results FILE] [--apply] [--force]   # 基于 bench 结果推荐 router.roles + worker_models
agent_go models list                          # 列出模型池（models.json registry）
agent_go models add <id> --provider <p> --base-url <url> [--thinking] [--json-loose] [--tco $] [--tags ...]  # 注册新模型（零代码接入）
agent_go skills list
agent_go skills show <name> [--json]
agent_go skills resolve <name>                 # trace a Skill's symlink resolution chain
agent_go agents
agent_go config                          # 查看当前生效配置
agent_go config local [--url ...]        # 一键生成并激活纯本地 profile（探测代理 + 备份）
agent_go config cloud                    # 恢复云端配置（备份保留）
agent_go config status                   # 当前模式 + plan/worker/evaluator/代理健康检查

# List / show / clean
agent_go list
agent_go show <task-id>
agent_go clean                        # 清理全部任务数据
agent_go clean --older-than 7         # 只清理早于 7 天前的任务（保留期）
agent_go clean --fixture-worktrees    # 只清理 eval_suite/fixtures/ 下失效 worktree 注册（ISSUE-38）

# Web 操作台（观测 + 处置：任务启动/恢复/取消/清理/审批/合并/PR + 配置中心 local⇄cloud + 健康检查 + 🗂 看板任务管理）
agent_go web --host 127.0.0.1 --port 8091   # 打开 http://127.0.0.1:8091
agent_go web --token xxx                     # 可选 Bearer token 鉴权
```
