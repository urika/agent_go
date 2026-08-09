# agent_go 模块职责目录

> 状态：As-Built 模块映射
> 更新日期：2026-08-08

| 模块 | 主要职责 | 关键输出 |
|---|---|---|
| `cli.py` | CLI 命令、参数和运行入口 | task/meta/命令结果 |
| `api.py` | LLM API、Plan、降级和缓存 | Plan JSON |
| `ui.py` | Plan/子任务确认和交互 | confirmed plan/tasks |
| `planning.py` | 计划检查、难度和耗时预估 | warnings/estimate |
| `pipeline.py` | DAG wave、并发、生命周期和清理 | meta/results |
| `executor.py` | 单子任务 worktree、Claude、验证和 commit | result/commit/tag |
| `subtask.py` | Claude headless、watchdog、进程控制 | subprocess result/metering |
| `delivery.py` | delivery branch 创建与 commit 汇总、Accepted Delivery 判定 | delivery branch / delivery result |
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
| `mcp_client.py` | 消费外部 MCP 工具 | MCP tool calls |
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
| `agent_loop.py` | 自主 agent 循环（`--agent-loop`）：tool-use ReAct 直接 API 调用 | loop result |
| `tool_executor.py` | Agent loop 工具注册和执行（Read/Write/Edit/Bash + 安全规则） | tool results |
| `console.py` | 统一输出抽象层：quiet / verbose 模式，延迟默认绑定，表格渲染 | console output |
| `tui.py` | Curses 状态仪表盘：实时显示并发子任务进度 | TUI display |
| `workflow_gen.py` | GitHub Actions workflow 自动生成（`ci` 命令） | workflow YAML |
| `web_server.py` | 只读 Web 观察平台：tasks / cost / models / config / timeline | HTTP JSON/HTML |
| `lint.py` | AST 静态检查：可疑 Python 代码模式（如循环体截断） | lint warnings |
| `mcp_http.py` | MCP Server HTTP/SSE transport：POST /mcp + GET /mcp (SSE) + /health | HTTP response |

## 模块变更规则

- 新增核心模块必须更新本目录。
- 公共接口变更必须同步 `docs/spec.md`。
- 状态、数据契约或边界变更必须新增/更新 ADR。
- 实现状态必须区分 `implemented`、`tested`、`dogfooded`、`accepted`。
