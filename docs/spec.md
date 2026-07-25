# agent_go 接口规格速查

> 浓缩自 19 份独立 SPEC。每个模块列出公共接口签名和一行说明。
> **快照日期：2026-07-25** — 接口签名已对齐源码。行号仅供参考（会漂移）。

## cli.py — CLI 入口 (1607 行)

```
cmd_run(args)            → Plan → Execute 主流程
cmd_resume(args)         → 中断恢复
cmd_list()               → 列出历史任务
cmd_show(args)           → 查看任务详情
cmd_status(args)         → 实时监控 (--watch)
cmd_pr(args)             → 生成 PR 描述
cmd_review(args)         → 审查任务结果(--task/--approve/--reject/--changes-requested) 或代码
cmd_config()             → 查看/编辑配置
cmd_clean()              → 清理任务目录和 tags
cmd_skills()             → 列出已安装 Skill
cmd_agents()             → 列出 Agent 类型
cmd_cache(args)          → Plan 缓存管理
cmd_inspect(args)        → 查看保留的 worktree 现场 (failed/blocked)
cmd_router(args)         → 角色感知模型路由配置
cmd_eval(args)           → 离线评估 (quality/perf/cost/reliability/ux/gate/all)
plan-history(args)       → Plan 版本历史
plan-diff(args)          → Plan 版本对比 (--v1/--v2)
```
`run`/`resume` 支持 `--preserve-worktrees`（保留全部）/ `--no-preserve`（强制清理），
默认仅保留 failed/blocked 子任务的 worktree 供人工审查。

## api.py — LLM Plan 生成 + 缓存 (423 行)

```
generate_plan(task, repo, config, logger, ...)  → 调用 LLM → 返回 plan dict
call_api(config, messages, logger)              → 统一 Anthropic/OpenAI/DeepSeek/Custom
decompose_fallback(task, repo, config, logger)  → 三层降级：本地模型→规则→单任务
get_cache_key(task, repo)                       → SHA256 缓存键
load_cached_plan(key, task, config, logger)     → 读缓存
save_cached_plan(key, plan, task, repo, config) → 写缓存
```

## pipeline.py — 拓扑波次调度器 (386 行)

```
_run_pipeline(confirmed, repo, task_dir, ..., preserve_worktrees=None) → 核心调度 (内部，cli.py 调用)
  ── Wave 拓扑排序 + ThreadPoolExecutor
  ── SIGINT → _interrupted → meta["status"]="paused" → sys.exit(0)
  ── 远程推送、worktree/tag 清理、gc.auto 恢复
  ── preserve_worktrees: None=保留 failed/blocked，True=全保留，False=全清理
_notify_complete(task_id, total, completed_ids, has_failed) → 任务完成通知 (M1)
```

## executor.py — 子任务执行器 (989 行)

```
run_subtask(task_id, subtask, repo, task_dir, ..., metering_path="", config=None) → 单子任务端到端
  ── _create_worktree() → _git_merge_upstream() → _build_task_md()
  ── _run_claude() → _verify_changes() → commit + tag
  ── metering_path → AGENT_GO_METERING_PATH env → worker 计量写入
  ── config → 运行时配置贯通（max_retries/goal/evaluator/worker_models）
  ── S4: difficulty → worker_models 映射 → AGENT_GO_CLAUDE_MODEL env
  ── 失败结果含 failure_reason（验证命令 + exit code + stderr 尾部，M2）
  ── S5: agent_loop 混合策略（_is_simple_task 判定：agent_type 不为 architect/reviewer
        + 关键词不含 探索/调研/重构/迁移/refactor/migrate/explore
        + files_hint ** 通配符 ≤1 + 上游依赖 ≤2）
_build_sandbox_env()        → 净化环境变量 (敏感词剔除 + AGENT_GO_API_KEY 强制删)
_apply_resource_limits()    → setrlimit (失败不阻塞)
```

## subtask.py — Claude 调用原语 (387 行)

```
_run_headless(task_md, worktree, env, logger, ..., hard_timeout=0) → claude -p 无头模式
  ── 交互检测 (正则 + 退出码 130) → 最多 2 次重试
  ── hard_timeout：硬超时 kill（retry_timeout 接线）
  ── S4: AGENT_GO_CLAUDE_MODEL env → claude --model；计量记录 difficulty/真实模型
  ── stream-json result 事件提取 usage/cost → 写 metering.jsonl (worker 角色)
_git_merge_upstream(src, dst, tag, logger, ...)   → 上游产物 merge
```

## ui.py — 终端交互 (418 行)

```
confirm_plan(plan, config, ...)    → Y/S/D/E/R/N 确认
confirm_subtasks(subtasks, ...)    → Y/N/E/A/D 确认
plan_to_subtasks(plan, logger)     → Plan.steps → subtasks (注入 agent_prompt + 资源清单)
```

## config.py — 配置与日志 (116 行)

```
load_config()                → ~/.agent_go/config.json，浅合并 DEFAULT_CONFIG
get_api_key(config)          → env AGENT_GO_API_KEY > config.api_key
setup_logger(task_id, dir)   → 双格式: INFO人类 + DEBUG JSON
log_event(logger, event, d)  → DEBUG JSON 事件
meter_event(path, event)     → 结构化计量事件写 metering.jsonl (role/cost/tokens)
safe_input(prompt)           → input() 包装，EOF → ""
```

## console.py — 输出抽象 (156 行)

```
Console(quiet, verbose)      → print/force/info/success/warning/error/debug
                              → sep/title/subtitle/table/data/data_table
_LazyConsole()               → 代理，每次属性访问动态解析 Console (解决 import 时序)
set_default_console(c)       → 替换全局实例 (cli.py cmd_run 调用)
get_default_console()        → 获取当前实例
```

## git_utils.py — Git 操作 (114 行)

```
analyze_project(repo)        → git ls-files 或 find
get_git_info(repo)           → remote, branch, commit
get_resource_map(repo, info) → 目录 + 关键文件清单
_worktree_create/remove/prune(repo, ...) → worktree 生命周期
_set_gc_auto(repo, "0"|"1") → gc.auto 读写 (并发安全)
```

## utils.py — 共享工具 (383 行)

```
read_reference_docs(paths, repo, logger)     → 参考文档读取 (路径穿越防御)
_is_safe_verification_command(cmd)           → 4 阶段白名单校验 → (bool, reason)
_log_rejected_command(cmd, reason, logger)   → 审计 JSONL 写入
_safe_append_to_file(path, text, logger)     → 锁文件 + 原子追加
_format_commit(title, issue_ref, sub_id)     → Conventional Commits
_detect_commit_prefix(title)                 → feat/fix/refactor/docs/test/chore
_slugify(text)                               → 分支名适用短标识
```

## agents.py — Agent 类型系统 (188 行)

```
load_agent_type(name, project_root)  → 用户定义 > 内置 (developer/architect/reviewer/tester)
list_agent_types()                   → 所有可用类型
get_claude_command(agent, worktree)  → 构建 claude CLI 参数 (headless/交互/greywall)
get_agent_env(agent)                 → AGENT_GO_AGENT_TYPE 环境变量
```

## skills.py — Skill 加载 (213 行)

```
load_skill(name, project_root)      → YAML frontmatter + Markdown body
load_skills(names)                   → 批量加载
render_skill_for_plan(skill)         → Plan prompt 注入格式 (500 字符截断)
render_skill_for_execution(skill)    → TASK.md 注入格式 (完整)
discover_skills(task)                → 关键词自动匹配 (实验性)
```

## role_skill_map.py — 角色-Skill 匹配 (139 行)

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
LLM 语义评估 + 失败原因摘要 (failure_summary)，供修复循环与 eval 指标使用
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

## eval.py — 离线评估 (1038 行)

```
analyze_quality(meta)           → Q1-Q10 质量指标 + 综合评分
analyze_performance(meta, log)  → P1-P6 性能指标
analyze_cost(tasks_dir)         → API 费用 + per-model/per-role 拆分
                                  + cost_source_breakdown(metering/rebuild)
                                  + policy_violations + $/pass rate
gate_cost(baseline, tasks_dir)  → 北极星门禁：$/pass rate 与基线比对
                                  → {passed, actual, baseline, reason}
                                  无数据时 passed=True（不阻挡 CI）
analyze_reliability(tasks_dir)  → 任务完成率 + sandbox 分布 + 阻断率
analyze_ux(tasks_dir)           → 文档使用率 + Agent/Skill 分布
aggregate_quality/perf(dir)     → 跨任务聚合
estimate_task_duration(subtasks, parallel, tasks_dir) → M4 时间预估（历史中位数 × 拓扑波次）
cmd_eval(args)                  → eval CLI（子命令含 gate --baseline X）
```

## tui.py — 状态面板 (199 行)

```
cmd_status_tui()  → curses 多面板实时监控
```

## workflow_gen.py — CI 生成 (80 行)

```
detect_language(repo)       → python/go/node/rust/java
generate_workflow(repo)     → .github/workflows/test.yml 内容
cmd_ci(args)                → CLI 入口
```
