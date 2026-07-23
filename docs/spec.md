# agent_go 接口规格速查

> 浓缩自 19 份独立 SPEC。每个模块列出公共接口签名和一行说明。行号仅供参考（会漂移）。

## cli.py — CLI 入口 (860 行)

```
cmd_run(args)            → Plan → Execute 主流程
cmd_resume(args)         → 中断恢复
cmd_list()               → 列出历史任务
cmd_show(args)           → 查看任务详情
cmd_status(args)         → 实时监控 (--watch)
cmd_pr(args)             → 生成 PR 描述
cmd_review(args)         → Claude 代码审查
cmd_config()             → 查看/编辑配置
cmd_clean()              → 清理任务目录和 tags
cmd_skills()             → 列出已安装 Skill
cmd_agents()             → 列出 Agent 类型
cmd_cache(args)          → Plan 缓存管理
```

## api.py — LLM Plan 生成 + 缓存 (423 行)

```
generate_plan(task, repo, config, logger, ...)  → 调用 LLM → 返回 plan dict
call_api(config, messages, logger)              → 统一 Anthropic/OpenAI/DeepSeek/Custom
decompose_fallback(task, repo, config, logger)  → 三层降级：本地模型→规则→单任务
get_cache_key(task, repo)                       → SHA256 缓存键
load_cached_plan(key, task, config, logger)     → 读缓存
save_cached_plan(key, plan, task, repo, config) → 写缓存
```

## pipeline.py — 拓扑波次调度器 (216 行)

```
_run_pipeline(confirmed, repo, task_dir, ...)   → 核心调度 (内部，cli.py 调用)
  ── Wave 拓扑排序 + ThreadPoolExecutor
  ── SIGINT → _interrupted → meta["status"]="paused" → sys.exit(0)
  ── 远程推送、worktree/tag 清理、gc.auto 恢复
```

## executor.py — 子任务执行器 (496 行)

```
run_subtask(task_id, subtask, repo, task_dir, ...) → 单子任务端到端
  ── _create_worktree() → _git_merge_upstream() → _build_task_md()
  ── _run_claude() → _verify_changes() → commit + tag
_build_sandbox_env()        → 净化环境变量 (敏感词剔除 + AGENT_GO_API_KEY 强制删)
_apply_resource_limits()    → setrlimit (失败不阻塞)
```

## subtask.py — Claude 调用原语 (273 行)

```
_run_headless(task_md, worktree, env, logger, ...) → claude -p 无头模式
  ── 交互检测 (正则 + 退出码 130) → 最多 2 次重试
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

## metrics.py — 数据采集 (76 行)

```
collect_timing(wt, merge, claude, verify, commit) → 5 阶段 ms 采集
collect_change_stats(worktree)                    → git diff --numstat
collect_merge_result(upstream, success, files)    → merge 成功/冲突
extract_usage(api_response, provider, model)      → token 用量
```

## eval.py — 离线评估 (606 行)

```
analyze_quality(meta)           → Q1-Q8 质量指标 + 综合评分
analyze_performance(meta, log)  → P1-P6 性能指标
analyze_cost(tasks_dir)         → API 费用 + per-model 拆分
analyze_reliability(tasks_dir)  → 任务完成率 + sandbox 分布
analyze_ux(tasks_dir)           → 文档使用率 + Agent/Skill 分布
aggregate_quality/perf(dir)     → 跨任务聚合
cmd_eval(args)                  → eval CLI 入口
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
