# agent_go 模块职责目录

> 状态：As-Built 模块映射
> 更新日期：2026-09-05（ISSUE-55 web_server.py 拆分为 web_data/web_ops/web_kanban/web_handler/web_frontend + 组合层；前次：B8 dsh_backend + ADR-010 阶段 1 harvest_trajectory 钩子；B7 zcode_backend + B6 opencode_backend + B4 声明式 backend 路由 + B3 pi_backend + B2 AgentLoop 加固）

| 模块 | 主要职责 | 关键输出 |
|---|---|---|
| `cli.py` | CLI 命令、参数和运行入口 | task/meta/命令结果 |
| `api.py` | LLM API、Plan、降级和缓存 | Plan JSON |
| `ui.py` | Plan/子任务确认和交互 | confirmed plan/tasks |
| `planning.py` | 计划检查、难度和耗时预估 | warnings/estimate |
| `pipeline.py` | DAG wave、并发（含 T09 本地模型限流串行）、生命周期和清理 | meta/results |
| `executor.py` | 单子任务 worktree、Claude、验证和 commit | result/commit/tag |
| `subtask.py` | Claude headless、watchdog、进程控制 | subprocess result/metering |
| `delivery.py` | delivery branch 创建与 commit 汇总、Accepted Delivery 判定、mergeability 预检 | delivery branch / delivery result |
| `governance.py` | M1.4 SDD 治理：spec requirement 提取、架构审查决策、traceability_matrix / architecture_compliance | governance report |
| `recover.py` | 从 worktree 重建中断状态 | recovered meta |
| `git_utils.py` | Git 仓库、worktree 和 gc 操作 | Git operation result |
| `failure.py` | 失败分类和 kill reason 映射 | failure class |
| `config.py` | 配置、日志和计量写入 | config/metering |
| `metrics.py` | 运行时指标和成本计算 | metrics |
| `bench.py` | Bench 编排和单任务结果采集 | results JSONL |
| `bench_schema.py` | Bench record schema 校验 | valid/invalid record |
| `batch_governance.py` | Bench source batch 和 manifest | batch metadata |
| `metric_report.py` | 产品指标汇总 | Accepted Delivery report |
| `cross_judge.py` | 交叉评判和人工校准 | judge scores |
| `evaluator.py` | 语义评估和失败摘要 | semantic result |
| `router.py` | 角色和 provider 路由 | routed model |
| `pricing.py` | 模型价格和档位 | price/tier |
| `mcp_server.py` | 对外 MCP Server | MCP tools/resources |
| `mcp_client.py` | 消费外部 MCP 工具（T08 代理工具模式：单代理工具动态发现 / 全量注入可配） | MCP tool calls |
| `artifacts.py` | 产物收集和导出 | artifact manifest |
| `checkpoint.py` | worktree 快照和恢复 | snapshot |
| `notify.py` | 多通道事件通知（desktop / webhook / command / IM 适配器） | notification events |
| `goal_injector.py` | 在 worktree 内注入 Claude Code `/goal` Stop Hook | goal hook config |
| `role_skill_map.py` | Config-driven 规则匹配：keywords / file patterns / agent type → skills | matched skills |
| `agents.py` | Agent 类型定义（developer/architect/reviewer/tester）的 Claude 配置 | agent config |
| `skills.py` | Skill 加载器：解析 YAML frontmatter + Markdown body 的 SKILL.md | rendered skills |
| `spec.py` | Task Spec 解析与 L1 准入门禁：模板生成/校验/Plan 约束注入 | spec validation result |
| `status.py` | Canonical 8 状态映射与 M0 任务状态判定 | canonical status |
| `utils.py` | 共享工具：验证命令白名单/参考文档读取/commit 格式化/slugify | utility functions |
| `assessment.py` | 语义评估假阳性数据层：事件模型/持久化/假阳性率计算 | assessment events |
| `metadata_migration.py` | 历史任务元数据迁移工具（schema 升级/字段补齐） | migrated metadata |
| `eval.py` | 评估分析：quality / perf / cost (per-role) / reliability / UX + eval gate | eval report |
| `replay.py` | 执行回放时间线：从 meta/metering/results 重建可视化 | timeline (ASCII/JSON) |
| `agent_loop.py` | 自主 agent 循环（`--agent-loop`）：tool-use ReAct 直接 API 调用；B2 stuck 检测/no-progress 信号/explore 只读/scope advisory | loop result |
| `tool_executor.py` | Agent loop 工具注册和执行（Read/Write/Edit/Bash/Grep/Glob/View + 安全规则 + 只读模式） | tool results |
| `backends/` | 阶段十三 worker backend 抽象包：BaseBackend/BackendContext/SubtaskResult（base，含 ADR-010 harvest_trajectory 可选钩子）、BackendRegistry/resolve_backend_name（registry）、ClaudeBackend（claude_backend）、AgentLoopBackend（agent_loop_backend）、PiBackend（pi_backend，B3）、OpenCodeBackend（opencode_backend，B6）、ZCodeBackend（zcode_backend，B7）、DSHBackend（dsh_backend，B8）、修复路径分发 repair_timeout/run_repair（dispatch） | SubtaskResult |
| `console.py` | 统一输出抽象层：quiet / verbose 模式，延迟默认绑定，表格渲染 | console output |
| `tui.py` | Curses 状态仪表盘：实时显示并发子任务进度 | TUI display |
| `workflow_gen.py` | GitHub Actions workflow 自动生成（`ci` 命令） | workflow YAML |
| `web_server.py` | Web 操作台组合/入口层（ISSUE-55 拆分后）：serve_web/main 入口 + 可 patch 叶子符号 + 全量 re-export（公共 API 不变） | HTTP JSON/HTML/SSE |
| `web_data.py` | Web 观测 GET 数据层：17+ api_* 纯函数、任务目录/id 校验、web_audit.jsonl 审计追加 | task/metrics JSON |
| `web_ops.py` | Web 写处置端点 mixin（WebOpsMixin）：do_POST/do_PUT/do_DELETE + run/resume/cancel/clean/review/merge/pr/confirm/notes/insight/config put | op result + audit |
| `web_kanban.py` | Web 看板切面：api_kanban 视图 + 任务状态快照缓存 + WebKanbanMixin（建卡/拆解/导入 spec/流转/归档/审批/派发/降级建议） | kanban JSON/cards |
| `web_handler.py` | Web 传输/鉴权/SSE：WebHandler = WebOpsMixin + BaseHTTPRequestHandler，admin/viewer 多角色守卫 + GET 路由 + SSE 事件流 | HTTP JSON/SSE |
| `web_frontend.py` | Web 单文件前端 SPA 模板（_SPA_HTML，无外部资源依赖） | HTML/JS |
| `lint.py` | AST 静态检查：可疑 Python 代码模式（如循环体截断） | lint warnings |
| `mcp_http.py` | MCP Server HTTP/SSE transport：POST /mcp + GET /mcp (SSE) + /health | HTTP response |
| `kanban.py` | 看板数据层：~/.agent_go/kanban.jsonl 单文件存储（mtime 缓存+原子写+锁），5 阶段列 × 3 类卡片，阶段流转 + reconcile_cards 惰性状态回流 | kanban board state |
| `profiles.py` | Profile 管理：local⇄cloud 一键切换（config local/cloud/status）、健康检查、本地 profile 模板生成 | active profile |
| `task_runner.py` | Web 子进程任务运行器：spawn agent_go --yes --json，meta.json 唯一事实源，SIGINT cancel | subprocess task run |
| `web_confirm.py` | Web 计划确认协议：pending/decision 文件协议 + 阻塞轮询，30min 超时自动取消 | confirm decision |
| `knowledge.py` | C4 KnowledgeStore 注入臂：从 Problem/deviation/verify_state 提取历史经验注入 repair prompt（可开关/suppressed_ids+dormant 可淘汰/knowledge_injected 埋点）；KV-cache 稳定快照（resolve_repair_knowledge，knowledge.snapshot 默认开：首次非空知识块跨重试冻结复用，注入块置于 TASK.md 后稳定前缀位） | knowledge context |
| `knowledge_ab.py` | C4 A/B 判定分析器：两臂 pass_rate/ADR/$/AD 汇总 + 三门槛判定（ADR↑/成本不劣化/可淘汰）→ PRODUCTIZE/ROLLBACK | A/B verdict report |
| `problems.py` | 跨任务 Problem 实体（B4/H3）：三态+复发重开、半衰期 dormant、葬礼 resolution_summary、LLM 根因级 summarize_resolution、全局 problems.jsonl upsert | Problem records |
| `replan.py` | C3 局部重规划（F-VERIFY-6）：无进展触发一次 Plan 拆分建议（LLM+启发式兜底），最多一次/继承父预算 | replan suggestion |
| `deviation.py` | Spec/Architecture/acceptance 偏差记录：模型、持久化、聚合（M2.5） | deviation records |
| `models_registry.py` | 模型池 Model Registry：models.json 加载（mtime 缓存）+ ModelEntity + key_ref 解析（env/secret 不存明文） | model entities |
| `review_agent.py` | 只读独立审查 subagent：两阶段 review | review analysis |
| `diag.py` | llama-defender 诊断数据面客户端（R13-R16 消费侧）：session key/fail-open fetch/ledger/metrics/archive 封装 | diag payloads |
| `exit_codes.py` | 语义化进程退出码（CLI 工具可脚本化判断） | exit code |
| `evidence.py` | M6.1 证据物化层：immutable bench 批次聚合为 LLM 可推理证据包（evidence_hash 校验） | evidence package |
| `decision_log.py` | M6.2 决策记录：关键决策追加 decision_log.jsonl（evidence_refs 绑定 + actual 回填） | decision entries |
| `task_lock.py` | M5.2 任务级互斥锁：is_task_locked 前置探测 + TaskLock 上下文（merge 与 run/resume 互斥） | task lock |
| `task_report.py` | 任务统计报表生成器：只读聚合任务 JSONL（total/completed/tags_distribution，多形态+归一+容错） | stats report |
| `goal_policy.py` | Goal Loop 最终执行策略 resolver（goal-mechanism-design §3.3/§4） | goal policy |

## 模块变更规则

- 新增核心模块必须更新本目录。
- 公共接口变更必须同步 `docs/spec.md`。
- 状态、数据契约或边界变更必须新增/更新 ADR。
- 实现状态必须区分 `implemented`、`tested`、`dogfooded`、`accepted`。

---

## 附录：模块详细职责（原 AGENTS.md Key Modules 表，2026-09-02 迁入）

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI commands: run, resume, recover, list, show, status, pr, merge, config, clean, inspect, review, router, cache, eval, ci, skills, agents, spec, governance, deviation, problems, migrate, plan-history, plan-diff, replay, checkpoint, mcp, web |
| `api.py` | LLM API: generate_plan, call_api, decompose_fallback, plan cache |
| `ui.py` | Interactive prompts: confirm_plan, confirm_subtasks, plan_to_subtasks |
| `executor.py` | Core subtask runner: worktree create, skill load, claude spawn, verify loop |
| `pipeline.py` | Wave scheduler, concurrency, worktree preservation/cleanup, remote push, SIGINT |
| `subtask.py` | claude -p headless runner, git merge upstream, worker metering, difficulty env；goal watchdog（goal turn=验证循环轮数：Bash 命中 AGENT_GO_VERIFY_HINT token 交集≥2 才计数，hint 空回退全工具计数；GOAL_TIMEOUT 按难度缩放 ×1/1.5/2.5） |
| `notify.py` | Multi-channel event notification: desktop/webhook/command, IM adapters |
| `goal_injector.py` | /goal Stop Hook injection: .claude/settings.json + verify-goal.sh |
| `goal_policy.py` | Goal Loop final execution policy resolver (goal-mechanism-design §3.3/§4) |
| `git_utils.py` | Project analysis, worktree create/remove/prune, gc.auto control |
| `skills.py` | Skill loading, discovery, rendering (YAML frontmatter + Markdown), symlink resolution |
| `agents.py` | Agent type system: developer/architect/reviewer/tester; claude/greywall command |
| `role_skill_map.py` | Config-driven rule matching: keywords, file patterns, agent type |
| `router.py` | Role-aware model routing: planner/worker/reviewer, fallback + circuit breaker |
| `models_registry.py` | 模型池（① Model Registry）：models.json 加载（mtime 缓存）+ ModelEntity（endpoint/thinking/JSON 遵从/TCO/quality_tags）+ key_ref 解析（env/secret，不存明文） |
| `evaluator.py` | LLM semantic evaluation + failure summary for verification loop；策略注册表（`EvalStrategy`：default/visual/chain），config `evaluator.strategy` 路由 |
| `verify_chain.py` | AG-2 验证机械前置层（吸收 llama-defender verification_chain L1）：MechanicalGate 空/畸形 diff 零成本短路 + ChainEvalStrategy（机械闸→委托 default LLM 评估），opt-in `evaluator.strategy="chain"` |
| `llama_contracts.py` | AG-1 llama-defender 共享契约包（叶子模块）：SignalSnapshot + EscalationDecision（任务级语义），CONTRACT_VERSION=1 对齐；漂移检测 `tools/check_llama_contracts.py` |
| `metrics.py` | Data collection: timing/change stats, estimate_cost, aggregate_metering, trust metrics (#49 放行门：review/交付后返工率/复发可见率/盲区命中率) |
| `config.py` | Config loading, logging, API key resolution, meter_event |
| `utils.py` | Commit formatting, slugify, shell safety, version detection, tool version probing |
| `spec.py` | Task Spec parsing + L1 admission review (S11-P0) |
| `delivery.py` | Task-level delivery contract (M1.2) |
| `governance.py` | SDD traceability matrix + architecture compliance (M1.4) |
| `deviation.py` | Spec/Architecture/acceptance deviation records: model, persistence, aggregation (M2.5) |
| `problems.py` | Cross-task Problem entity (B4/H3): 三态+复发重开, 半衰期(stale_after_days→dormant), 葬礼(resolution_summary), 全局 ~/.agent_go/problems.jsonl upsert；「越用越聪明」数据层；C4 葬礼回写（record_resolution：重试后成功回写「模式+解法」；summarize_resolution LLM 根因级总结，knowledge.resolution_llm 开关，fail-open 降级 diffstat 级） |
| `replan.py` | C3 局部重规划（F-VERIFY-6）：无进展触发一次 Plan 拆分建议（LLM+启发式兜底），最多一次/继承父预算/默认人工确认/不扩大任务图；AG-3 确定性决策层（`decide_escalation` 决策表 + `TaskCircuitBreaker` 熔断 + 幂等闸，agent 侧自有失败信号口径，输出 EscalationDecision 契约）；AG-4/5 reload 动作（task-context 证据包 + pin 锚点，task_context.enabled 默认关，fail-open 降级 split/retry） |
| `knowledge.py` | C4 KnowledgeStore A/B 注入臂：从 Problem/deviation/verify_state 提取历史经验注入 repair prompt（可开关/可淘汰/knowledge_injected 埋点）；KV-cache 稳定快照（resolve_repair_knowledge，knowledge.snapshot 默认开，C4 前置修订） |
| `status.py` | Canonical task state machine (M0-2, 8 states) |
| `exit_codes.py` | Semantic process exit codes for CLI tools |
| `failure.py` | Stable failure classes and policy (M0-3) |
| `eval.py` | Quality/perf/cost (per-role)/reliability/UX analysis + eval gate ($/pass baseline + regression) |
| `planning.py` | Planning helpers: estimate_task_duration |
| `pricing.py` | Model price table (52 models), MODEL_TIER, provider defaults |
| `replay.py` | Execution replay timeline: load meta/metering/results, ASCII/JSON visualization |
| `checkpoint.py` | Worktree file snapshot manager: take/restore/delete |
| `recover.py` | Rebuild meta.json from worktree state after SIGKILL/abnormal interruption |
| `metadata_migration.py` | Auditable failure-metadata migration for historical task dirs (`migrate failure-metadata`) |
| `mcp_server.py` | MCP server over stdio: 7 tools (run_task/resume_task/inspect_task/review_task/governance_task/list_tasks/cancel_task) + resources + prompts |
| `mcp_http.py` | MCP server HTTP/SSE transport: POST /mcp + GET /mcp (SSE) + GET /health, Bearer auth |
| `mcp_client.py` | MCP consumption layer: subtasks call external MCP tools, namespaced `mcp__{server}__{tool}`；T08 代理工具模式（默认）：agent_loop 注入单个 `mcp__proxy` 代理工具（op=list/describe/call 动态发现），`mcp_client.tool_mode=full` 回退全量 schema 注入 |
| `bench.py` | Model benchmark orchestrator: eval bench over eval_suite tasks；子进程强制 --no-goal（对照实验不引入 goal 变量，防 watchdog 误杀口径噪声）；语义判定取末次有效 verdict（verification_results 跨重试累积）；`plan_quality_blocked` 单列 kill_reason=plan_gate_blocked（不计能力失败） |
| `bench_schema.py` | Versioned Bench record schema + JSONL validator (M0-4) |
| `batch_governance.py` | Result batch governance + immutable baseline manifests (M0-10) |
| `metric_report.py` | Reproducible Metric Freeze report generation (M0-9) |
| `cross_judge.py` | Cross-model judgment matrix (self-bias prevention) + human calibration |
| `assessment.py` | False-positive evaluation data layer: AssessmentEvent model, persistence, aggregation |
| `artifacts.py` | Artifact export (S9-B): collect worktree/__artifacts__/ into --artifact-dir before cleanup |
| `diag.py` | llama-defender 诊断数据面客户端（R13-R16 消费侧 C1-C7）：session key 构造/截断口径、fail-open fetch、ledger/metrics/archive/ctx_config/props 封装 |
| `agent_loop.py` | Autonomous agent loop (--agent-loop): tool-use ReAct loop；B2 hardening: stuck 检测（连续重复调用先提醒后终止）/ no-progress 信号 / explore 只读模式 / scope advisory（files_hint 越界写入提示） |
| `tool_executor.py` | Tool registry for agent loop: Read/Write/Edit/Bash/Grep/Glob/View + bash safety rules + readonly mode |
| `backends/` | 阶段十三 worker backend 抽象包（B1/B2/B3/B4/B6/B7/B8）：base（BaseBackend/BackendContext/SubtaskResult + ADR-010 阶段 1 harvest_trajectory 可选钩子，默认 [] fail-open）、registry（BackendRegistry/resolve_backend_name，显式声明 + B4 by_type/by_difficulty 声明式路由）、claude_backend（claude -p/greywall，progress 开关）、agent_loop_backend（直接 API 路径）、pi_backend（B3：pi -p --mode json NDJSON 事件流解析 + 聚合计量 + 零产出错误映射，仅 headless）、opencode_backend（B6：opencode run --format json --auto --pure，step_finish 计量 + readonly 映射 plan agent + Go 挂起 hard_timeout 兜底，仅 headless）、zcode_backend（B7：ELECTRON_RUN_AS_NODE zcode.cjs --json --mode plan\|yolo，单 JSON 输出 + 模型由用户 config 决定无 per-run 标志，仅 headless）、dsh_backend（B8：npx @deepseek-ai/dsh@0.1.2-rc.1 --profile headless，cwd=worktree、positional task；非 readonly 注入 DSH_PERMISSION_MODE=danger-full-access、readonly 审批失败闭合天然只读；计量/轨迹读 session 日志（projectKey cwd 编码 + zstd -dc + version 探测，全程 fail-open），actual_model 从日志回读；harvest_trajectory 防腐翻译落盘 trajectory/{sub_id}.jsonl；仅 headless）、dispatch（修复路径 fix/replan/reload 统一分发 + repair_timeout 消重） |
| `console.py` | Console output abstraction: quiet/verbose modes, lazy default binding, tables |
| `tui.py` | Curses status dashboard |
| `review_agent.py` | Read-only independent review subagent, two-phase review |
| `workflow_gen.py` | GitHub Actions workflow generation (ci command) |
| `web_server.py` | Web 操作台组合/入口层（ISSUE-55 于 2026-09-05 T12 拆分，4903 → 238 行）：保留 serve_web/main 入口、被测试 monkeypatch 的叶子符号（AGENT_GO_DIR/CONFIG_PATH/load_config/probe_local_models/_run_cli/_bench_results_path/_resolve_workspace_file），原公开符号全量 re-export——调用方与测试 import 路径零改动 |
| `web_data.py` | Web 观测 GET 数据层（拆分自 web_server.py）：17+ api_* 纯函数（tasks/detail/log/metering/replay/plan/overview/cost/models/assessment/cross-judge/bench/baseline/profiles/health/storage/notes/insights/config 等）、_task_dir/_list_task_dirs 路径校验（防穿越）、_audit 审计追加；对可 patch 叶子符号经 `_root()` 在组合层命名空间运行时解析（monkeypatch 语义不变） |
| `web_ops.py` | Web 写处置端点 mixin（WebOpsMixin(WebKanbanMixin)，拆分自 web_server.py）：do_POST/_route_write_api/do_PUT/do_DELETE + 任务生命周期（run/resume/cancel/clean-old）、审批/交付（review/review-decision/merge/pr/confirm）、notes/blind-spot 归因/insight 生成、PUT /api/config 白名单编辑；全操作写 web_audit.jsonl |
| `web_kanban.py` | Web 看板切面（拆分自 web_server.py）：api_kanban 卡片分组视图 + _task_status_snapshot（meta mtime:size 签名缓存）+ WebKanbanMixin 看板写端点（create/decompose/import-spec/update/move/archive/delete/review/dispatch/suggest-degrade，含 W2 异步派发回调与状态回流） |
| `web_handler.py` | Web 传输/鉴权/SSE 层（拆分自 web_server.py）：WebHandler = WebOpsMixin + BaseHTTPRequestHandler；_auth_role/_auth_guard（admin/viewer/open 三态 + Bearer/X-Api-Key/query token）、_reply* 响应工具、do_GET/_route_api 观测路由、_stream_events SSE（任务目录 + kanban.json 签名轮询推送） |
| `web_frontend.py` | 单文件前端 SPA 模板（拆分自 web_server.py）：_SPA_HTML 内嵌 HTML/JS，无外部资源依赖，由 WebHandler `/` 路由原样返回 |
| `kanban.py` | 看板数据层：~/.agent_go/kanban.json 单文件存储（mtime 缓存 + 原子写 + 锁），5 阶段列（brainstorm→operations）× 3 类卡片（discussion/implementation/periodic），阶段流转 + history，task_ids 软链接执行任务（与 status.py 执行态正交，不动 meta.json） |
| `profiles.py` | Profile 管理：local⇄cloud 一键切换（config local/cloud/status）、.current_profile、健康检查（mismatch 检测）、本地 profile 模板生成 |
| `task_runner.py` | Web 子进程任务运行器（Thin shell 同哲学）：spawn agent_go --yes --json，meta.json 唯一事实源，SIGINT cancel |
| `web_confirm.py` | R5b Web 计划确认协议：pending/decision 文件协议 + 阻塞轮询，30min 超时自动取消 |
| `lint.py` | AST-based static checks: suspicious for-loop body truncation |
| `decision_log.py` | M6.2 决策记录：模型推荐/配置修改/profile 切换/交付决策追加 `~/.agent_go/decision_log.jsonl`（evidence_refs 绑定依据，actual 复跑后回填），可审计可复盘 |
| `evidence.py` | M6.1 证据物化层：immutable bench 批次聚合为 LLM 可推理的结构化证据包（evidence_hash 校验 manifest），eval insight 只能基于真实数据推理 |
| `task_lock.py` | M5.2 任务级互斥锁：is_task_locked 前置探测（web 409 检查）+ TaskLock 上下文管理器，merge 与 run/resume 并发改 worktree 互斥 |
| `knowledge_ab.py` | C4 KnowledgeStore A/B 判定分析器：两臂 pass_rate/ADR/$/AD 汇总 + 三门槛判定（ADR↑ + 成本不劣化 + 错误知识可淘汰）→ PRODUCTIZE/ROLLBACK |
| `attribution_watch.py` | P2 盲区归因监视（opt-in）：trust --watch-repo 开启；watch index（repo→交付任务文件集/盲区项）+ Stop Hook 合并式注入（幂等/可卸载）+ 会话改动交集聚合提醒（无命中静默） |
| `task_report.py` | 任务统计报表生成器：generate_task_report 只读聚合任务 JSONL（total/completed/uncompleted/tags_distribution，多形态兼容 + 标签归一 + 解码失败跳过） |
