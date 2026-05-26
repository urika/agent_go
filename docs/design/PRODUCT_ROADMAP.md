# agent_go 产品路线图

> 版本: v0.7  
> 日期: 2026-05-27  
> 视角: 产品经理回顾与后续规划

---

## 一、当前进度 vs 原需求

| 优先级 | 总数 | 已完成 | 进行中 | 待开始 |
|--------|------|--------|--------|--------|
| P0（已实现） | 12 | 12 | — | — |
| P1（高优先级） | 7 | 7 | 0 | 0 |
| P2（中优先级） | 7 | 7 | 0 | 0 |
| P3（远期） | 4 | 0 | 0 | 4 |

### P0 — 已全部实现

| # | 需求 | 状态 |
|---|------|------|
| 1 | Plan Mode：外接 LLM API 生成结构化执行方案 | ✅ |
| 2 | 多供应商 API 支持 (Anthropic/OpenAI/DeepSeek/Custom) | ✅ |
| 3 | 子任务拆解 + 依赖图 + 共享资源清单 | ✅ |
| 4 | Git worktree 隔离执行 + 三层降级策略 | ✅ |
| 5 | 结构化审计日志 (双格式) + 参考文档挂载 | ✅ |
| 6 | --yes 一键自动确认 + 非 TTY safe_input | ✅ |
| 7 | 路径隔离：TASK.md 源项目→worktree 路径替换 | ✅ |
| 8 | 产物传递：上游 worktree 代码 git merge 到下游 | ✅ |
| 9 | 无头模式：claude -p + bypassPermissions + 流式输出 + 交互检测 + 超时重试 | ✅ |
| 10 | 测试验证强制执行 + 失败自动修复重试 | ✅ |
| 11 | 共享上下文 SHARED_CONTEXT.md + 注入下游 TASK.md | ✅ |
| 12 | Git 代码管理：commit + tag + merge | ✅ |

### P1 — 高优先级

| # | 需求 | 状态 |
|---|------|------|
| 13 | --issue 参数：关联 GitHub Issue 编号 | ✅ |
| 14 | 分支命名规范：worktree 使用 feature/fix 前缀 | ✅ |
| 15 | Conventional Commits：feat/fix/refactor 前缀 | ✅ |
| 16 | agent_go pr 命令：生成 PR 描述 + gh pr create | ✅ |
| 17 | PR 模板自动填充：变更摘要、关联Issue、测试结果 | ✅ |
| 18 | **中断恢复**：任务重启后跳过已完成子任务 | ✅ v0.4 — active_pids 机制 kill 子进程 |
| 19 | **验证命令数组支持**：多个串行检查步骤 | ✅ v0.4 — `["build", "test"]` 数组支持 |

### P2 — 中优先级

| # | 需求 | 状态 |
|---|------|------|
| 22 | 多任务并行执行 | ✅ 超出预期（拓扑调度+ThreadPoolExecutor） |
| 26 | Sandbox 增强：Greywall Incus 后端 | ✅ 已集成 |
| 20 | GitHub Actions 工作流生成 | ✅ v0.7 |
| 21 | Projects 看板联动 | 📋 待开始 |
| 23 | Plan 结果缓存 | ✅ v0.6 |
| 24 | agent_go review 命令 | ✅ v0.7 |
| 25 | TASK.md 文件覆盖检查 | 📋 待开始 |

### P3 — 远期

| # | 需求 | 状态 |
|---|------|------|
| 27 | FastAPI + SQLite 编排层 | 📋 |
| 28 | Web 仪表盘 + TUI 观测终端 | 📋 |
| 29 | 多 Agent 并发协作 | 📋 |
| 30 | IDE 插件 (VS Code / JetBrains) | 📋 |

### 超出原计划的增量交付（v0.2 - v0.3）

| 功能 | 说明 |
|------|------|
| 模块化拆分 | 单文件 1129 行拆分为 12 个模块 |
| Skill 类型系统 | 可插拔领域知识注入 |
| Agent 类型系统 | developer/architect/reviewer/tester 角色配置 |
| git worktree 替代 clone | 共享对象库，创建更快、磁盘更省 |
| --parallel 并发执行 | 拓扑排序 + ThreadPoolExecutor |
| --remote 分支推送 | worktree 分支推送到远程仓库 |
| tag 命名空间化 | task_id/sub_id 格式避免跨任务冲突 |
| headless 冲突自动解决 | 保留冲突标记让 Claude Code 现场处理 |
| 路径重写修复 | 支持中文标点和路径子目录场景 |

### 增量交付（v0.4）

| 功能 | 说明 |
|------|------|
| P0: 变更检测修复 | `git status --porcelain` 替代 `git diff --stat`，正确识别新文件 |
| P1: 中断孤儿进程清理 | `active_pids` 追踪 + SIGKILL，Ctrl+C 不残留子进程 |
| P1: 验证命令路径重写 | 验证命令中 repo 路径自动替换为 worktree 路径 |
| P2: cmd_clean tag 清理 | 清理任务目录时同步删除 task tags |
| P3: result.json 独立写入 | 每个 subtask 独立写 result，减少并发竞争 |
| D-4: Agent Type 约束生效 | `--allowedTools` 传递 + headless 尊重 Agent 权限 |
| D-5: Skill 可观测性 | `AGENT_GO_SKILLS` 环境变量 + 加载/未命中追踪 |
| D-6: 多级验证 | verification 支持命令数组，全部通过才算成功 |
| D-8: 上下文过滤 | 独立 `context.md` + 仅注入直接上游依赖上下文 |

### 增量交付（v0.5）

| 功能 | 说明 |
|------|------|
| Phase 1 数据采集层 | metrics.py: timing/change_stats/merge/verify/token 采集 |
| results[] 扩展 | retry_count + timing{} + change_stats{} + merge_results[] + verification_results[] |
| api_call token 记录 | prompt_tokens/completion_tokens/model 写入 execution.log |
| api_error 事件 | HTTP status_code 捕获记录 |
| plan_duration_ms | Plan 耗时写入 plan_complete event |
| Phase 2 统计分析层 | eval.py: analyze_quality/perf + aggregate + 评分算法 |
| `agent_go eval` 命令 | eval quality/perf [--all] 质量+性能报告 |

### 增量交付（v0.6）

| 功能 | 说明 |
|------|------|
| Status TUI | curses 多面板实时监控面板 |
| Plan 缓存 | SHA256 缓存键 + 24h TTL + cache 管理命令 |

### 增量交付（v0.7）

| 功能 | 说明 |
|------|------|
| Phase 3 评估全维度 | eval cost/reliability/ux/all 命令 |
| CI 工作流生成 | `agent_go ci` 5 种语言自动检测 |
| `agent_go review` | Claude 代码审查命令 |

**P0+P1+P2 全部完成** (19+8+7=34 项)。P3 4 项远期。

---

## 二、后续排期建议

### 短期（1-2 个迭代，优先级最高）

| # | 需求 | 理由 |
|---|------|------|
| **S1** | 完善 `cmd_status` 实时状态可视化 | 当前 status 已有轮询基础，但多任务并发时用户无法直观看到 worktree、进度、日志流。可升级为 curses TUI 面板 |
| ~~S2~~ | ~~#19 验证命令数组支持~~ | ✅ v0.4 已完成 |
| ~~S3~~ | ~~中断恢复完善~~ | ✅ v0.4 — active_pids 机制 |
| **S4** | 测试覆盖率提升 | 150 测试覆盖工具函数和 mock 流程，但并发、中断恢复、worktree 创建/清理缺少真实验证 |

### 中期（3-5 个迭代）

| # | 需求 | 理由 |
|---|------|------|
| **M1** | TUI 观测终端（P3 #28 精简版） | `agent_go status --watch` 已有基础，升级为 curses 终端面板可大幅提升多任务管理体验 |
| **M2** | Plan 缓存（P2 #23） | API 调用是最大成本和时间消耗，相同任务描述+项目 hash 可复用 Plan |
| **M3** | `agent_go review` 命令（P2 #24） | 已有 `--issue` 和 PR 生成，补充代码审查能力可形成完整 PR 工作流 |
| **M4** | #25 TASK.md 文件覆盖检查 | 验证 Plan 预期文件 vs 实际产出，提升任务完成质量 |

### 远期（视需求触发）

| # | 需求 | 触发条件 |
|---|------|---------|
| **L1** | FastAPI + SQLite 编排层（P3 #27） | 需要多用户或 Web UI 时启动 |
| **L2** | IDE 插件（P3 #30） | 有外部用户后评估 |
| **L3** | CI/CD 工作流生成（P2 #20） | 用户反馈需要时启动，当前 `--remote` 推送已部分满足需求 |
| **L4** | Projects 看板联动（P2 #21） | 团队协作场景需要时启动 |
| **L5** | 多 Agent 并发协作（P3 #29） | Agent 类型系统成熟后探索 |

---

## 三、产品定位判断

当前产品已经**超出原型阶段**。模块化拆分、并发执行、worktree 隔离、Skill/Agent 类型系统、中断恢复这套能力组合，已经是一个可用的 CLI 编排工具。

**下一阶段的定位**：从「能跑」到「可靠」。重点方向：

1. **可观测性** — 用户需要知道多个 worktree 里同时在发生什么
2. **鲁棒性** — 中断恢复、错误处理、边界情况覆盖
3. **性能** — Plan 缓存减少 API 调用、大仓库 worktree 性能

如果资源有限，最优先做 **S1（status 可视化）** 和 **S2（验证命令数组）**，这两个对日常使用体验提升最大。

---

*文档结束*
