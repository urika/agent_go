# agent_go 架构设计

> 一人维护，写下来是为了 6 个月后做决策时不重新考古

## 一句话

**agent_go 是 Claude Code 的编排层**：LLM 生成执行计划 → 拆解为子任务 → 在隔离 git worktree 中并发执行 → 验证 → commit → PR。

## 核心数据流

```
cmd_run(repo, task)
  ├── analyze_project()          → 项目文件列表
  ├── get_git_info()             → remote, branch, commit
  ├── generate_plan()            → LLM 返回结构化 JSON (plan)
  │     ├── 缓存: SHA256(task+repo) → 24h TTL
  │     ├── 路由: router.enabled 时按 planner 角色走 primary/fallback (降级留痕)
  │     ├── 计量: 每次 API 调用写 metering.jsonl (role/tokens/cost/latency)
  │     └── 降级: 外部API → 本地模型 → DECOMPOSE_RULES匹配 → 单任务兜底
  ├── confirm_plan()             → Y/S/D/E/R/N (--yes 跳过)
  ├── plan_to_subtasks()         → plan.steps → subtasks + 角色-Skill匹配 + difficulty 透传
  ├── estimate_task_duration()   → M4 时间预估 (历史中位数 × 拓扑波次)
  ├── _run_pipeline()
  │     ├── 禁用 gc.auto         → 并发 worktree 安全
  │     ├── 拓扑波次调度          → ThreadPoolExecutor, --parallel N
  │     │     └── 上游 failed/blocked → 下游标 blocked 并跳过 (block_on_failure 可关)
  │     ├── run_subtask()        → 每个子任务:
  │     │     ├── git worktree add -b agent_go/{task_id}/{sub_id}
  │     │     ├── git merge 上游 tag → 产物传递
  │     │     ├── 写 TASK.md (路径已重写到 worktree; --goal 时注入 /goal 循环指令)
  │     │     ├── Stop Hook 注入 (--goal-hook: .claude/settings.json + verify-goal.sh)
  │     │     ├── S4 模型路由: difficulty → worker_models → claude --model
  │     │     ├── claude -p (无头) 或 greywall -- claude (交互)
  │     │     ├── 验证循环: 失败 → 修复(注入 stdout/stderr+diff --stat) → 再验证,
  │     │     │   max_retries 可配(默认3), retry_timeout 硬超时, 达上限标 failed 并阻断下游
  │     │     ├── 可选 LLM 语义评估 (evaluator.enabled, shell 过后才触发)
  │     │     ├── git commit + tag ({task_id}/{sub_id})
  │     │     └── worker 计量: stream-json result → metering.jsonl (含 difficulty/真实模型)
  │     ├── 远程推送 (--remote)
  │     ├── 清理 worktree/tag + 恢复 gc.auto (failed/blocked 的 worktree 保留待审, agent_go inspect 查看)
  │     ├── 完成通知 (notify_event: desktop/webhook/command 三通道, 事件订阅)
  │     └── 最终报告 + meta.json
  └── cmd_pr()                    → PR 描述 + 质量仪表 (通过率/验证率/合并就绪指示)
```

## 关键设计决策

### Worktree 隔离而非 clone
每个子任务通过 `git worktree add -b agent_go/{task_id}/{sub_id}` 在独立分支中执行。所有 worktree 共享对象库，tag 命名空间 `{task_id}/{sub_id}` 防冲突。

### 产物传递：git merge 而非文件拷贝
上游子任务的 tag 直接 `git merge` 到下游 worktree，利用共享对象库的零拷贝特性。

### 并发安全：gc.auto 禁用
并发 worktree 操作共享对象库，执行前 `git config gc.auto 0`，结束时恢复原值。

### 三层降级
外部 LLM API (60s timeout) → 本地模型 (localhost:8000, 10s) → DECOMPOSE_RULES 关键词匹配 → 单任务兜底。

### 安全白名单
LLM 生成的验证命令必经 4 阶段校验：shlex 解析 → 6 类 shell 注入扫描 → 命令白名单查找 (28 种工具) → 逐 token 正则匹配。防御深度，default-deny。

### 沙箱环境
验证命令在净化环境中执行：剔除含 API_KEY/SECRET/TOKEN/PASSWORD 的环境变量 + 强制删除 AGENT_GO_API_KEY。

### 零外部依赖
纯 Python stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`)。

## 数据持久化

```
~/.agent_go/
├── config.json              ← 用户配置 (含 API provider/key/model)
├── role_skill_map.json      ← 角色-Skill 匹配规则
├── skills/<name>/SKILL.md   ← 用户 Skill (YAML frontmatter + Markdown)
├── agents/<type>.md         ← 用户自定义 Agent 类型
├── cache/plans/<sha256>.json ← Plan 缓存 (24h TTL)
├── verification_audit.jsonl ← 被拒验证命令的审计日志
└── task-<id>/
    ├── meta.json            ← 任务元数据 + results 数组 (含 failure_reason)
    ├── execution.log        ← 双格式: INFO人类可读 + DEBUG结构化JSON
    ├── metering.jsonl       ← 结构化计量: 每 API 调用一条 (role/tokens/cost/latency/result)
    └── sub-<n>/
        ├── work/            ← git worktree (执行后清理; failed/blocked 保留)
        ├── .preserved       ← 保留标记 (subtask_id/status/failure_reason/branch)
        ├── verify_state.json ← 验证循环状态 (resume 断点恢复)
        └── result.json      ← 单子任务结果
```

## 测试

```bash
pytest tests/ -q           # 751 tests, ~17s
```

测试策略：mock 所有外部依赖 (git, claude, API)，验证逻辑正确性。NFR 专项测试在 `test_nfr_*.py`。

## 已知问题速查

- 2026-07-23 的 14 项已知问题（含下方原列的 3 项）均已修复，详见 [ISSUES.md](ISSUES.md)
- 当前无未修复的阻塞性问题
