# agent_go 接口规格速查

> 浓缩自 19 份独立 SPEC。每个模块列出公共接口签名和一行说明。
> **快照日期：2026-08-01** — 接口签名已对齐源码。行号仅供参考（会漂移）。

## cli.py — CLI 入口 (2100 行)

```
cmd_run(args)            → Plan → Execute 主流程
cmd_resume(args)         → 中断恢复
cmd_recover(args)        → SIGKILL 后从 worktree 重建 meta.json (--dry-run 只扫描)
cmd_list()               → 列出历史任务
cmd_show(args)           → 查看任务详情
cmd_status(args)         → 实时监控 (--watch / --no-tui)
cmd_pr(args)             → 生成 PR 描述 (--push 经 gh CLI 创建)
cmd_review(args)         → 审查任务结果(--task/--approve/--reject/--changes-requested) 或代码
cmd_config()             → 查看/编辑配置
cmd_clean()              → 清理任务目录和 tags
cmd_skills()             → 列出已安装 Skill (list / show <name>)
cmd_agents()             → 列出 Agent 类型
cmd_cache(args)          → Plan 缓存管理 (list/clean/clear/stats)
cmd_inspect(args)        → 查看保留的 worktree 现场 (failed/blocked)
cmd_router(args)         → 角色感知模型路由配置 (show/enable/disable/set-role)
cmd_checkpoint(args)     → 检查点快照管理 (list/restore/delete)
cmd_eval(args)           → 离线评估 (quality/perf/cost/reliability/ux/gate/bench/models/judge/all)
plan-history(args)       → Plan 版本历史
plan-diff(args)          → Plan 版本对比 (--v1/--v2)
replay(args)             → 执行回放时间线 (--json)
ci(args)                 → GitHub Actions workflow 生成 (--dry-run)
mcp(args)                → MCP server (stdio / --http HTTP+SSE)
```
`run`/`resume` 支持 `--preserve-worktrees`（保留全部）/ `--no-preserve`（强制清理），
默认仅保留 failed/blocked 子任务的 worktree 供人工审查。
`run` 另支持 `--max-retries` / `--no-verify-block` / `--goal` / `--goal-hook` / `--semantic-eval` /
`--agent-loop` / `--interactive` / `--step-confirm` / `--auto-init` / `--parallel N` / `--remote`。

## api.py — LLM Plan 生成 + 缓存 (583 行)

```
generate_plan(task, repo, config, logger, ...)  → 调用 LLM → 返回 plan dict
call_api(config, messages, logger)              → 统一 Anthropic/OpenAI/DeepSeek/Custom
decompose_fallback(task, repo, config, logger)  → 三层降级：本地模型→规则→单任务
get_cache_key(task, repo)                       → SHA256 缓存键
load_cached_plan(key, task, config, logger)     → 读缓存
save_cached_plan(key, plan, task, repo, config) → 写缓存
```

## pipeline.py — 拓扑波次调度器 (636 行)

```
_run_pipeline(confirmed, repo, task_dir, ..., preserve_worktrees=None) → 核心调度 (内部，cli.py 调用)
  ── Wave 拓扑排序 + ThreadPoolExecutor
  ── SIGINT → _interrupted → 杀子进程 → meta["status"]="paused" → sys.exit(0)
  ── 远程推送、worktree/tag 清理、gc.auto 恢复
  ── preserve_worktrees: None=保留 failed/blocked，True=全保留，False=全清理
  ── mcp_client: MCPClientPool start_all() 启动 / finally stop_all() 回收（外部 MCP 工具）
  ── S9-B 产物导出：config.artifact_dir 时清理 worktree 前调用 artifacts.export，final report 渲染清单
notify_event(event, context, config) → 任务完成/失败通知 (M1)
```

## planning.py — 规划辅助 (M4)

```
estimate_task_duration(subtasks, parallel, tasks_dir) → 历史子任务耗时中位数 × 拓扑波次
```

## executor.py — 子任务执行器 (1459 行)

```
run_subtask(task_id, subtask, repo, task_dir, ..., metering_path="", config=None) → 单子任务端到端
  ── _create_worktree() → _git_merge_upstream() → _build_task_md()
  ── checkpoint 快照（提交前，回滚用）
  ── _run_claude() → _verify_changes() → commit + tag
  ── metering_path → AGENT_GO_METERING_PATH env → worker 计量写入
  ── config → 运行时配置贯通（max_retries/goal/evaluator/worker_models）
  ── S4: difficulty → worker_models 映射 → AGENT_GO_CLAUDE_MODEL env
  ── 失败结果含 failure_reason（验证命令 + exit code + stderr 尾部，M2）
  ── S5: agent_loop 混合策略（_is_simple_task 判定：agent_type 不为 architect/reviewer
        + 关键词不含 探索/调研/重构/迁移/refactor/migrate/explore
        + files_hint ** 通配符 ≤1 + 上游依赖 ≤2）
  ── MCP 消费：claude --mcp-config 透传外部 MCP 工具（mcp_client 配置）
  ── 语义评估：evaluator.enabled → shell 验证过后触发；fail_closed 可阻断
  ── S9-B: config.artifact_dir 时 TASK.md 注入 __artifacts__/ 产物目录约定
_build_sandbox_env()        → 净化环境变量 (敏感词剔除 + AGENT_GO_API_KEY 强制删)
_apply_resource_limits()    → setrlimit (失败不阻塞)
_verify_changes()           → 验证循环 + 修复重试（retry_count 注入 context.md + metering）
```

## subtask.py — Claude 调用原语 (479 行)

```
_run_headless(task_md, worktree, env, logger, ..., hard_timeout=0, config=None) → claude -p 无头模式
  ── 交互检测 (正则 + 退出码 130) → 最多 2 次重试
  ── hard_timeout：硬超时 kill（retry_timeout 接线）
  ── goal 配置优先级：运行时 config > env > 磁盘 (goal.enabled/max_turns/timeout_seconds)
  ── S4: AGENT_GO_CLAUDE_MODEL env → claude --model；计量记录 difficulty/真实模型
  ── stream-json result 事件提取 usage/cost → 写 metering.jsonl (worker 角色)
  ── S12-P0 G1：IDLE/hard_timeout/goal kill 决策点写 kill_state 事件 (kill_reason) 到
     metering.jsonl，返回值附带 kill_reason → executor 归因写入子任务结果
_git_merge_upstream(src, dst, tag, logger, ...)   → 上游产物 merge
```

## ui.py — 终端交互 (779 行)

```
confirm_plan(plan, config, ...)    → Y/S/D/E/R/N 确认
confirm_subtasks(subtasks, ...)    → Y/N/E/A/D 确认
plan_to_subtasks(plan, logger)     → Plan.steps → subtasks (注入 agent_prompt + 资源清单)
```

## config.py — 配置与日志 (258 行)

```
load_config()                → ~/.agent_go/config.json，浅合并 DEFAULT_CONFIG
get_api_key(config)          → env AGENT_GO_API_KEY > config.api_key
setup_logger(task_id, dir)   → 双格式: INFO人类 + DEBUG JSON
log_event(logger, event, d)  → DEBUG JSON 事件
meter_event(path, event)     → 结构化计量事件写 metering.jsonl (role/cost/tokens)
safe_input(prompt)           → input() 包装，EOF → ""
```

## console.py — 输出抽象 (216 行)

```
Console(quiet, verbose)      → print/force/info/success/warning/error/debug
                              → sep/title/subtitle/table/data/data_table
_LazyConsole()               → 代理，每次属性访问动态解析 Console (解决 import 时序)
set_default_console(c)       → 替换全局实例 (cli.py cmd_run 调用)
get_default_console()        → 获取当前实例
```

## git_utils.py — Git 操作 (203 行)

```
analyze_project(repo)        → git ls-files 或 find
get_git_info(repo)           → remote, branch, commit
get_resource_map(repo, info) → 目录 + 关键文件清单
_worktree_create/remove/prune(repo, ...) → worktree 生命周期
_set_gc_auto(repo, "0"|"1") → gc.auto 读写 (并发安全)
```

## utils.py — 共享工具 (464 行)

```
read_reference_docs(paths, repo, logger)     → 参考文档读取 (路径穿越防御)
_is_safe_verification_command(cmd)           → 4 阶段白名单校验 → (bool, reason)
_log_rejected_command(cmd, reason, logger)   → 审计 JSONL 写入
_safe_append_to_file(path, text, logger)     → 锁文件 + 原子追加
_format_commit(title, issue_ref, sub_id)     → Conventional Commits
_detect_commit_prefix(title)                 → feat/fix/refactor/docs/test/chore
_slugify(text)                               → 分支名适用短标识
```

## agents.py — Agent 类型系统 (191 行)

```
load_agent_type(name, project_root)  → 用户定义 > 内置 (developer/architect/reviewer/tester)
list_agent_types()                   → 所有可用类型
get_claude_command(agent, worktree)  → 构建 claude CLI 参数 (headless/交互/greywall)
get_agent_env(agent)                 → AGENT_GO_AGENT_TYPE 环境变量
```

## skills.py — Skill 加载 (238 行)

```
load_skill(name, project_root)      → YAML frontmatter + Markdown body
load_skills(names)                   → 批量加载
render_skill_for_plan(skill)         → Plan prompt 注入格式 (500 字符截断)
render_skill_for_execution(skill)    → TASK.md 注入格式 (完整)
discover_skills(task)                → 关键词自动匹配 (实验性)
```

## role_skill_map.py — 角色-Skill 匹配 (161 行)

```
load_role_skill_map(project_root)    → 加载匹配规则
apply_rules(step, role_map, skills)  → 注入 required/recommended skills + agent_type
```

## metrics.py — 数据采集 (183 行)

```
collect_timing(wt, merge, claude, verify, commit) → 5 阶段 ms 采集
collect_change_stats(worktree)                    → git diff --numstat
collect_merge_result(upstream, success, files)    → merge 成功/冲突
extract_usage(api_response, provider, model)      → token 用量
estimate_cost(provider, model, pt, ct)            → 按定价表估算美元成本
aggregate_metering(metering_path)                 → 汇总 metering.jsonl (总量 + by_role)
```

## router.py — 角色感知模型路由

```
resolve_provider(agent_type, config)  → agent_type → planner/worker/reviewer 路由 (router.enabled 控制)
call_with_role(route, messages, api_key, logger, ...) → primary → fallback 降级，含熔断器
                                      → 返回 (content, metering)，降级留痕 fallback_reason
                                      违规时 metering 含 policy_violation 字段
```

## evaluator.py — 验证评估

```
evaluate_semantic(subtask, worktree, config, logger) → 语义评估 (evaluator.enabled)
  ── API 失败 → passed=False + confidence=0.0（fail_open 时代默认不阻断；fail_closed 阻断）
  ── 响应解析失败 → passed=False（default-deny）
  ── 写 assessment.jsonl（评估假阳性数据，assessment.py 消费）
  ── 计量含 difficulty / 估算 tokens（CJK 感知：CJK 1 token，ASCII 1/4 token）
```

## goal_injector.py — /goal Stop Hook 注入

```
GoalInjector.inject(worktree, cmds, ...)   → 写 .claude/settings.json + scripts/verify-goal.sh
                                            （hook 命令过 4 级白名单，全不合格则不注入）
GoalInjector.build_goal_condition(cmds)    → 生成 /goal condition 字符串
GoalInjector.cleanup(worktree)             → 清理注入文件
```
开关：`goal.enable_goal_hook`（默认 false，CLI `--goal-hook`）；TASK.md goal 指令 `goal.enabled`（默认 false，CLI `--goal`）。

## notify.py — 多通道事件通知 (M1)

```
notify_event(event, context, config)   → 唯一入口：on_complete/on_failed/on_blocked
  ── _resolve_notify_config()          → notify 块解析 + behavior.notify_* 兼容层
  ── build_payload()                   → 聚合 failure_reason/metering/.preserved（白名单字段）
  ── desktop / webhook / command 通道   → webhook 支持 generic/slack/dingtalk/wecom/ntfy
  ── ${VAR} 环境变量插值、https 校验、超时重试、故障隔离
```

## eval.py — 离线评估 (1106 行)

```
analyze_quality(meta)           → Q1-Q10 质量指标 + 综合评分
analyze_performance(meta, log)  → P1-P6 性能指标
analyze_cost(tasks_dir)         → API 费用 + per-model/per-role 拆分
                                  计价双轨：优先真实 cost_usd（metering 通道），
                                  缺失时按 MODEL_PRICES × token 重算（rebuilt 通道）
                                  + cost_source_breakdown {metering, rebuilt}
                                  + unknown_model_events（价目表覆盖度监控）
                                  + fallback_events（降级事件计数，PRD 铁律留痕字段）
                                  + $/pass rate（北极星）
gate_cost(baseline, tasks_dir)  → 绝对阈值门禁：actual > baseline → 不通过
                                  无数据（actual=None）→ passed=True（不阻挡 CI）
gate_cost_regression(tasks_dir) → 回归门禁（PRD "不劣化"语义）：
                                  对比 .agent_go/cost_baseline.json 基线，
                                  劣化 > tolerance(10%) → 不通过
                                  首次运行自动建立基线；update=True 强制重置
load/save_cost_baseline(dir)    → 基线文件读写（cost_baseline.json）
analyze_reliability(tasks_dir)  → 任务完成率 + sandbox 分布 + 阻断率
                                  + K5 resume_success_rate（中断恢复成功率，
                                    被中断过的任务中最终 completed 比例）
analyze_ux(tasks_dir)           → 文档使用率 + Agent/Skill 分布
aggregate_quality/perf(dir)     → 跨任务聚合
estimate_task_duration(subtasks, parallel, tasks_dir) → M4 时间预估（历史中位数 × 拓扑波次）
cmd_eval(args)                  → eval CLI，子命令：
                                  quality|perf|cost|reliability|ux|gate|all
                                  + bench|models|judge（见 bench.py / cross_judge.py）
                                  gate 支持 --baseline X（绝对阈值，默认 0.05）
                                            --check-regression（对比历史基线）
                                            --update-baseline（重置基线）

# 模型分级元数据（见 design/model-evaluation-and-tiering.md §1）
MODEL_PRICES                    → {model: {prompt, completion}} 定价表（USD/百万tokens）
MODEL_TIER                      → {frontier/value/lite: [models]} 模型档位（见 pricing.py）
PROVIDER_DEFAULT_MODEL          → provider → 默认模型（旧日志缺 model 字段时回退）
```

## recover.py — 崩溃恢复 (SIGKILL)

```
recover_meta(task_dir, dry_run=False) → 从 worktree 状态重建 meta.json
  ── commit + verify-pass → completed；commit + verify-fail → failed
  ── no commit + orphan changes → reset（resume 重跑）；no commit + no changes → no_changes
  ── 永不代提交 orphan 变更（commit 是唯一完成边界）
```

## replay.py — 执行回放时间线

```
cmd_replay(args) → 读 meta/metering/results，ASCII 时间线 / --json 结构化输出
```

## checkpoint.py — 检查点快照

```
take_snapshot(worktree, task_dir, sub_id)   → 快照文件 (提交前)
restore_snapshot(worktree, checkpoint, ...) → 回滚恢复
list_checkpoints(task_dir)                  → 列出快照
delete_checkpoint(task_dir, sub_id)         → 删除快照
```

## mcp_server.py — MCP server (stdio, JSON-RPC 2.0)

> **状态**：6 工具 + 6 Resources + 3 Prompts 已落地。

```
TOOLS = [run_task, resume_task, inspect_task, review_task, list_tasks, cancel_task]
RESOURCES = [Task List, Task Summary, Latest Plan, Metering Data, Review Status]
PROMPTS = [diagnose_failure, review_and_decide, resume_or_restart]
  ── spawn agent_go 子进程，解析 stdout JSONL → notifications/progress
  ── repo allowlist (AGENT_GO_MCP_ALLOWED_REPOS) fail-closed
  ── wait=true 流式；wait=false 异步返回 task_id 轮询
```

## mcp_http.py — MCP server HTTP/SSE transport

```
agent_go mcp --http --host 127.0.0.1 --port 8090
  ── POST /mcp (JSON-RPC) + GET /mcp (SSE 推送) + GET /health
  ── AGENT_GO_MCP_HTTP_TOKEN → Bearer token 鉴权
```

## mcp_client.py — MCP 消费层

```
MCPClientPool(config)          → 多 server 连接池 (start_all/stop_all/get_server)
MCPServerConnection(command)   → 单 server 生命周期 (subprocess + JSON-RPC initialize 握手)
_tool_prefix = "mcp__{server}__{tool}"  → 外部工具命名空间（agent_loop tools 合并 + claude --mcp-config 透传）
  ── server 启动失败降级 warning，不阻断 pipeline
```

## assessment.py — 评估假阳性数据层

```
write(task_dir, event)         → 追加 AssessmentEvent 到 assessment.jsonl
load_all(task_dir)             → 读取全部事件
compute_false_positive_rate(task_dir) → 语义评估假阳性率（eval/bench 消费）
```

## artifacts.py — 产物导出（S9-B）

```
ARTIFACT_DIR_NAME = "__artifacts__"     → worktree 内约定产物目录（声明制）
collect_from_worktree(worktree, sub_id) → 扫描 worktree/__artifacts__/** 返回产物列表
export(task_id, results, artifact_dir, task_dir) → 复制到 artifact_dir/{task_id}/{sub_id}/（含保留 worktree）
render_export_summary(export_result)    → 生成导出清单（final report 展示）
  ── pipeline 清理 worktree 前调用；导出失败降级 warning 不中断任务
```

## lint.py — AST 静态检查

```
lint_for_loop_truncation(path) → 检测 for 循环体被截断（循环变量在循环外使用）
```

## web_server.py — 只读 Web 观察平台（agent_go web）

```
api_tasks()             → 遍历 AGENT_GO_DIR/task-* 读 meta.json 返回任务清单（含 metering 聚合成本）
api_task(task_id)       → 任务详情（subtasks[] + results[]，按 subtask_id 匹配，含 agent_type_source/skills/difficulty）
api_subtask_detail(task_id, sub_id) → 子任务验证结果/改动统计/worktree/agent prompt
_extract_subtask_log(task_id, sub_id) → 从 execution.log 提取子任务日志段（边界正则匹配，防 sub-1/sub-10 误中）
api_metering(task_id)   → metering.jsonl 按 role 聚合（count/cost/tokens/latency）+ 明细
api_replay(task_id)     → 复用 replay.py _build_timeline/_collect_summary
api_plan(task_id)       → PLAN.md + plans/v{ver}.json
api_overview/cost/models → 全局聚合视图；api_assessment/cross_judge/bench_results/baseline → 评估数据
api_config()            → config.json 只读展示（api_key/token 递归脱敏，短 key 全遮蔽）
api_storage()           → 磁盘占用 Top20 + 孤儿目录检测
WebHandler(BaseHTTPRequestHandler) → GET 路由 + Bearer token / ?token= query 鉴权 + SSE /api/events（轮询 mtime 刷新）
serve_web(host, port, token) → ThreadingHTTPServer 启动（默认 127.0.0.1:8091）
  ── 前端：单文件内嵌 HTML SPA（任务清单→展开任务→子任务→tab: 概览/验证/日志/计量/时间线 + 总览/成本/模型/配置/运维视图）
  ── 前端鉴权：401 时 prompt 输入 token 存 sessionStorage，fetch 带 Authorization 头，SSE 走 ?token= query
  ── 设计：只读全 GET、不触碰 worktree/git、无框架仅 stdlib
```

## agent_loop.py — 自主 Agent 循环 (--agent-loop)

```
run_agent_loop(task_md, worktree, env, logger, config) → 工具调用 ReAct 循环
  ── _is_simple_task() 判定：简单任务走直接 API，复杂任务保留 claude -p
  ── tools 合并 ToolRegistry.definitions() + mcp_client 外部工具（mcp__ 命名空间）
  ── 每轮写 metering（virtual_model=agentgo-worker，含 token 统计）
  ── 上限：max_turns（默认 20）/ max_duration（默认 600s）/ api_timeout（120s）
```

## tool_executor.py — Agent 循环工具注册表

```
ToolRegistry() → Read/Write/Edit/Bash 等工具定义 + bash 安全规则 + 文件操作
execute_tool(name, args, worktree, ...) → 工具分发执行（返回 ToolResult 结构化结果）
```

## tui.py — 状态面板

```
cmd_status_tui()  → curses 多面板实时监控（agent_go status --watch）
```

## bench.py — 模型对照评估编排器 (585 行)

> **状态**：S8 P0 已落地 + S10-P1 schema 扩展 + S10-P2 P1 字段/代码质量/对照基线/动态 timeout。子进程隔离（不 import 核心），读 metering.jsonl + meta.json 数据契约。

```
cmd_bench(args)                            → 对照运行编排器
  ── --tasks eval_suite/                   标准任务集（YAML，带 ground-truth 验证）
  ── --models M1,M2,M3                     被评模型（每模型跑全部任务）
  ── --repeat N                            每任务重复 N 次（默认 3）
  ── --output results.jsonl                JSONL 落盘
  ── --source-batch NAME                   批次标识（baseline / results_v2 / smoke-*）
  ── 内部 subprocess 调 agent_go run（--yes --headless --preserve-worktrees --parallel 1）
       --parallel 1：S10-P2 顺序执行，消除并发对 elapsed/cost 的干扰
  ── 动态 timeout（S10-P2）：_dynamic_timeout = max(任务YAML配置, 子任务数×150s+120s)
       _estimate_subtasks_from_history 从已有 results.jsonl 推断子任务数，避免多子任务被截断
  ── 读 AGENT_GO_DIR/task-*/meta.json + metering.jsonl
  ── record 字段（S10-P1 扩展）：
       timed_out     bool   任务是否因超时被强制终止（cooperative timeout SIGTERM/SIGKILL）
       judge_model   string semantic evaluator 模型（role=evaluator 的 actual_model）
       planner_model string plan 生成模型（role=planner 的 actual_model）
       source_batch  string 批次标识（跨批次追溯）
  ── record 字段（S10-P2 P1 扩展）：
       semantic_pass Optional[bool]  全部子任务语义评估显式通过（跳过/未启用→None）
       binary_pass   bool    all_verify_ok AND semantic_pass is not False（二元通过，K1 口径）
       per_subtask   json[]  每子任务 {sub_id,status,retries,verify_ok,semantic_ok}
       plan_step_count int    Planner 分解步骤数（subtasks 长度）
  ── record 字段（S10-P2 代码质量 §4.1）：
       lint_errors   int    _collect_quality：各保留 worktree 的 ruff(E/F/W)+mypy 错误数之和
       tests_broken  int    worktree pytest 失败用例数之和（基线全绿→失败=回归）

cmd_baseline(args)                         → 对照基线编排器（S10-P2 §2.3）
  ── claude -p 裸跑（不走 agent_go harness），临时副本中执行
  ── stream-json 提取 total_cost_usd；任务 YAML verification 全绿→pass
  ── 对临时副本跑 ruff/mypy/pytest → lint_errors / tests_broken
  ── 默认输出 eval_suite/baseline.jsonl（--output 可覆盖）
  ── 用于量化 harness 相对裸跑的 pass_rate / 耗时 / 成本 / 代码质量 ROI

cmd_models(args)                           → 决策矩阵展示
  ── --results results.jsonl               读取 bench 产出
  ── 按模型聚合：pass_rate / dollar_per_pass / k8 / sample_size / recommendation
  ── $/pass 统一口径（§3.1）= sum(total_cost_usd) / sum(pass_rate)
  ── K8 修订（§3.4）= 通过 record 中 total_retries==0 占比
  ── 代码质量（S10-P2）：avg_lint_errors / avg_tests_broken / code_regression_rate
       （通过 record 中 tests_broken>0 占比，§3.5 代码回归率）
  ── 决策规则：pass_rate<60%→discouraged, >=85%→recommended, <3样本→insufficient

analyze_model_productivity(path) → 与 cmd_models 同逻辑，返回 dict 供编程调用
_collect_quality(task_dir)         → 聚合保留 worktree 的 {lint_errors, tests_broken}
_lint_errors_for_worktree(wt)     → ruff E/F/W + mypy 对变更 .py 文件的错误数（工具缺失→0）
_tests_broken_for_worktree(wt)    → pytest 失败用例数（工具缺失→0）
_git_diff_files(wt)               → 变更 .py 文件列表（HEAD~1..HEAD）
```

## cross_judge.py — 交叉评判矩阵 (485 行)

> **状态**：S8 P1 简化版已落地 + S10-P1 自评偏差量化。N 模型互评（禁绝自评）+ 人工校准。

```
cmd_judge(args)                            → 交叉评判 + 校准 CLI
  ── --results results.jsonl               bench 产出（含 task_dir 路径）
  ── --judge-models M1,M2                  评判模型列表（逗号分隔）
  ── --output cross_judge_scores.jsonl     评分落盘
  ── --judge-subcommand calibrate           人工校准模式

cross_judge_results(bench_results, judges) → 逐条调用 evaluate_semantic（读 worktree git diff）
  ── 硬约束：judge 与 candidate 不同 provider（禁绝自评，LLM-as-Judge 自偏防护）
  ── 评分尺度（目标 rubric）：correctness/completeness/code_quality（1-5）+ false_positive(bool)
  ── 当前实现（P1 简化）：四维退化为单一 semantic_score（由 reason 文本启发式提取），
     false_positive = not passed。P2 计划升级 evaluator.py prompt 为结构化 rubric，
     产出独立四维分，届时 semantic_score = avg(correctness, completeness, code_quality)。
  ── S10-P1：每条结果携带 self_judge_model（bench record 的 judge_model，即自评模型身份）

_print_self_bias_report(bench_results, scores) → 自评偏差量化报告（S10-P1）
  ── 口径：自评通过（pass_rate>0）的 record 中，
       被 cross-judge 判 false_positive 的占比 + cross-judge 评分 <3/5 的占比
  ── 解读：false_positive 率越高 → semantic evaluator 自评越乐观（漏检越多）

calibrate_judge(llm_path, human_csv)       → 人工校准
  ── human CSV: task_id,candidate_model,correctness,... 
  ── 输出每 judge: avg_divergence / agreement_rate / verdict
  ── 分歧 ≤1.0→✓reliable, 1.0-1.5→⚠marginal, >1.5→✗unreliable
```

## pricing.py — 大模型定价表 (152 行)

> **状态**：从 eval.py 迁出。S8 P0 落地。48 个模型（2026-07 最新）+ 档位元数据。

```
MODEL_PRICES        → {model: {prompt, completion}} 定价表（USD/百万tokens）
MODEL_TIER          → {frontier/value/lite: [models]} 模型档位（供 difficulty 路由）
PROVIDER_DEFAULT_MODEL → provider → 默认模型（7 个 provider 含 google/volcengine/moonshot/zhipu）
```

## query.py — 结构化查询 API（待落地）

> **状态**：设计完成。封装现有的 `_read_meta` / `_scan_task_dirs` / `_read_log_events`。

```
query_task(task_id)                            → Optional[TaskResult]
query_project_trend(repo, days=30)              → task trend dict
```

## events.py — 事件总线（待落地）

> **状态**：设计完成。全生命周期事件订阅 + `events.jsonl` 持久化。

```
emit_event(event)                              → 同步触发已注册 handler
subscribe_event(event_type, handler, once)     → 注册事件监听器（glob 支持）
EVENT_TYPES                                    → "plan.generated"|"subtask.started"|... 枚举
events.jsonl 文件                               → ~/.agent_go/task-xxx/events.jsonl
```

## knowledge/ — 知识存储（待落地）

> **状态**：设计完成。项目级经验沉淀 + Plan prompt 自动注入。纯 JSON 文件，零外部依赖。

```
KnowledgeStore(repo_path)                      → 项目级知识存储
  ├── record_success(category, content, src)   → 记录成功模式
  ├── record_failure(category, signal, src)    → 记录失败信号
  ├── get_relevant(desc, max=5)                → 获取最相关的历史经验
  └── get_project_profile()                    → 项目特征推断
```

文件结构：
```
~/.agent_go/knowledge/<repo-hash>/
  ├── patterns.json           # 成功模式（验证命令/分解策略）
  ├── failure-signals.json    # 失败信号（高频失败原因）
  ├── verified-cmds.json      # 已验证命令（exit_code=0）
  └── project-meta.json       # 项目特征（语言/框架/构建工具）
```

## workflow_gen.py — CI 生成 (80 行)
