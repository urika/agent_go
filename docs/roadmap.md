# agent_go Roadmap：从现状到「周五派发、周一 merge」

> 基线：2026-07-24，v2.0.0，684 测试全绿，14 项已知缺陷清零。
> 目标对齐 [prd.md](prd.md) 的 Q3 / 年度 KPI；差距分析依据见 prd.md「P0 缺失功能」「P1 重点」章节。

## 进度快照（2026-08-01 更新，1387 测试全绿）

| 迭代 | 状态 | 说明 |
|------|------|------|
| **CLI/MCP 交互层** | ✅ 完成（2026-08-01） | MCP 6 tools（新增 `list_tasks` / `cancel_task`）+ Resources 原语（6 个）+ Prompts 原语（3 个 SOP 模板）+ **HTTP/SSE Transport**（`agent_go mcp --http`，Bearer token 鉴权）；错误响应 `fix` 字段（ERROR_TEMPLATES 7 种类型）；ActivityTracker 并行活动追踪（异步任务后台监控）；CLI 失败恢复闭环引导 + 后续操作卡片；任务生命周期 `cancelled` 状态可恢复。设计稿：[design/cli-mcp-design-analysis.md](design/cli-mcp-design-analysis.md) + [design/cli-mcp-interaction-analysis.md](design/cli-mcp-interaction-analysis.md) |
| **CLI/MCP 保留项落地** | ✅ 完成（2026-08-01） | 波次进度卡片（`_estimate_wave_count` + wave N/M 卡片）；`skills show <name>` SKILL.md 自描述；多 profile（`--profile` / `AGENT_GO_PROFILE` → `~/.agent_go/profiles/`）；增量 Plan 迭代 + 实时 Diff（`show_plan_diff` + 菜单 [V] 版本历史）；Sampling 原语（`request_sampling` stdio 双向 + cancel_task `confirm`）。改进清单全部闭环 |
| **测试加固** | ✅ 完成（2026-08-01） | 1387 测试全绿（+25 CLI/MCP 特性测试） |
| S1 计量日志 | ✅ 完成 | planner/worker 双角色 metering.jsonl 全链路（run + resume）；eval cost per-role 拆分；修复 executor 计量路径死代码、api.py router 路径 NameError || S1 M2 失败摘要 | ✅ 完成 | `failure_reason`（验证命令 + exit code + stderr 尾部）写入结果，`show` 展示 |
| S2 验证循环 | ✅ 完成（2026-07-25） | 全链路验收修复 8 项缺口（含 wave 调度排除 blocked 的关键 bug、CLI 配置贯通）+ 剩余项落地：Stop Hook GoalInjector（`--goal-hook`）、retry_timeout 硬超时、goal.enabled 默认对齐 false、`--goal` 开关 |
| M1 完成通知 | ✅ 完成 | `notify.py` 多通道（desktop/webhook/command）+ 事件订阅 + IM 适配器，设计稿：[design/notification-webhook-spec.md](design/notification-webhook-spec.md) |
| S4 模型路由 | 🔶 部分推进 | `router.py`（角色路由 + 熔断 + 降级留痕）已落地，设计稿：[design/router-multi-provider-extension.md](design/router-multi-provider-extension.md)；**复杂度双通道已完成**（2026-07-25：Planner 打 difficulty 标签 → `worker_models` 映射 → claude `--model`，计量记录 difficulty/真实模型） |
| M3 PR 质量仪表 | ✅ 完成 | `_build_quality_dashboard`：通过率/验证率/合并就绪指示 + 子任务明细 + M5 启发式验证警告（2026-07-25 补 blocked 图标与置信度警告） |
| M4 时间预估 | ✅ 完成 | `estimate_task_duration`：历史子任务耗时中位数 × 拓扑波次（考虑并行度），执行前展示 + `time_estimate` 事件 |
| **PRD 分析改进** | ✅ 完成 | OpenChamber 竞品对比分析、四阶段开发流程模型（含 M7 审查阶段缺口识别）、用户介入点设计；已写入 `prd.md` + 排入 `roadmap.md` S5-S7 |
| **测试加固** | ✅ 完成（2026-07-25） | 1130 测试 5 连绿。修复 ISSUE-24（goal watchdog flaky 根治）、ISSUE-25（3 处测试漂移）；新增 72 测试覆盖 agent_loop 集成、5 个未测 CLI 命令、TUI 辅助函数、subtask 超时分支 |
| **$/pass 门禁** | ✅ 完成（2026-07-25） | `eval gate`（绝对阈值 + `--check-regression` 回归对比 + `--update-baseline`）；CI 接入 `eval gate --baseline 0.05`；K5 `resume_success_rate` 派生；修复 ISSUE-26/27/28（计价失真 + evaluator 重复记账 + PRD 语义断裂）。详见 [ISSUES.md](ISSUES.md) |
| **模型分级 + 评估机制设计** | ✅ 完成（2026-07-25） | 三角色 × 三档位分级矩阵 + 三层评估体系（确定性/交叉评判/决策汇总）；完整设计稿 [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md)；**P0 已落地（48 模型定价表 + bench 编排器 + eval models）** |
| **S8 P0 模型评估机制** | ✅ 完成（2026-07-25） | `pricing.py`（48 模型定价表 + MODEL_TIER + 7 provider 默认）；`bench.py`（subprocess 隔离编排器 + `eval bench/models`）；`cross_judge.py`（交叉评判矩阵 P1 简化版：禁绝自评 + 启发式评分 + 人工校准；P2 升级结构化 rubric）；`eval_suite/`（22 任务 + 4 fixtures）；`config.example.json` 三套预设（国际/国内/混合） |
| **核心解耦** | ✅ 完成（2026-07-25） | evaluator/notify/goal/skills/agent_loop 全部动态 import + try/except；`estimate_task_duration` 迁 planning.py；`MODEL_PRICES` 迁 pricing.py；解耦原则固化在 [architecture.md](architecture.md) |
| **M7 结果审查阶段** | ✅ 完成（2026-07-25 核实） | `cmd_review --task <id>`：按文件分组聚合 diff 摘要 + approve/reject/changes-requested 人工审批；`--deep` 独立模型逐子任务分析。PRD Phase 3 缺口关闭 |
| **Plan 版本管理** | ✅ 完成（2026-07-25 核实） | `plan-history <id>` / `plan-diff <id> --v1 --v2` 命令已存在 |
| **PR 自动推送** | ✅ 完成（2026-07-25 核实） | `cmd_pr --push` 通过 gh CLI 自动创建 PR |
| **S6 失败通知增强** | ✅ 完成（2026-07-25 核实） | notify.py 事件已含 `subtask_failed` / `on_blocked`，子任务失败即推送，无需等整体任务结束 |

## 总体节奏

```
Q3 2026（信任层 + 成本层）          Q4 2026（体验层 + 规模化）
━━━━━━━━━━━━━━━━━━━━━━━━━━       ━━━━━━━━━━━━━━━━━━━━━━━━━━
验证循环 → 计量日志 → 模型路由       PR 仪表 → 时间预估 → 审查流水线
K1≥92% K8≥80% K4≤$0.05            K1≥97% K4≤$0.03 K3≤1.5min
```

## Q3 2026（7–9 月）：补上信任与成本两根支柱

| 迭代 | 交付物 | 对应缺口 | 预估 | 验收门禁 |
|------|--------|---------|------|---------|
| **S1**（7 月底–8 月初） | 结构化计量日志落地：`role / actual_provider / cost_usd / fallback_reason` 每请求一条；接通 `metrics.extract_usage` | 差距 3/4 的数据源 | ~2 天 | eval cost 报表能看到 per-role 拆分 |
| **S1** | M2 失败原因摘要：meta.json 增加 `failure_summary`（验证命令 + exit code + stderr 尾部），`show`/`status` 直接展示 | M2，K6 7/9→8/9 | ~1 天 | 失败任务不看日志能定位原因 |
| **S2**（8 月上中旬） | 验证循环 Phase 1：VerificationAgent + RepairAgent（fix prompt 注入 stdout/stderr/git diff）+ `max_retries` 可配（默认 3）+ **blocked 阻断下游** | M5/M6，K8 | 2–3 天（设计稿已定，见 [design/verification-agent-goal-spec.md](design/verification-agent-goal-spec.md)） | 注入故障的端到端用例：下游被阻断、worktree 保留待审 |
| **S3**（8 月下旬） | 验证循环 Phase 2：`/goal` 注入 + Stop Hook + watchdog；Phase 4：eval 新指标（首次通过率、重试成功率、阻断率） | K8 度量闭环 | 3–4 天 | K8 首次通过率有可追溯数据源 |
| **S3** | M1 完成通知：任务结束触发 webhook / 系统通知（最小实现，配置驱动） | M1 | ~1 天 | `--yes` 无头跑完能收到通知 |
| **S4**（9 月） | 角色感知模型路由：planner/worker/reviewer 三通道配置 + 降级留痕（`fallback_reason` 必填）+ 本地模型并发上限显式化 | 差距 3，K4 | 3–5 天 | **发布门禁：$/pass rate 不劣化**（对比 S1 基线） |
| **S8**（9 月） | 模型分级 + 评估机制 P0：扩充 `MODEL_PRICES`（48 模型）+ `MODEL_TIER` 元数据；标准任务集种子（22 任务 + ground truth）；`eval bench` 编排器 + `analyze_model_productivity` + `eval models`；`config.example.json` 国际/国内/混合三套预设 | [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md) | ✅ 已完成（2026-07-25） |

**Q3 出关口径**：K1 ≥92%、K8 ≥80%、K4 ≤$0.05、$/pass ≤$0.05、K6 8/9。达不到则 Q4 不扩新功能，回头补质量。

依赖关系：S1 必须最先（它是 S4 门禁和北极星指标的数据源）；S2/S3 与 S4 可并行，但 S4 的 Reviewer 通道建议等验证循环稳定后再开，控制变量；**S8 依赖 S4（路由机制）+ $/pass 门禁（已落地）**——评估机制需要可切换的模型路由 + 可信的成本计量才能对照运行。

## Q4 2026（10–12 月）：兑现及格线，再扩规模

> **2026-07-25 重排**：S5 全部（M7/M3/M4/Plan 版本管理）、S6 的复杂度双通道与失败通知增强、S7 的 PR 自动推送均已提前落地（见进度快照）。剩余项重新编排如下。

| 迭代 | 交付物 | 对应缺口 | 状态 |
|------|--------|---------|------|
| ~~S5~~ | ~~M7 结果审查 / M3 PR 质量仪表 / M4 时间预估 / Plan 版本管理~~ | — | ✅ 已提前落地 |
| ~~S6~~ | ~~复杂度双通道 / 失败通知增强~~ | — | ✅ 已提前落地 |
| **S6**（11 月） | **KPI 基线采集**：bench 真实执行（3 模型 × 22 任务 × 3 重复）→ `eval models` 决策矩阵 + `eval judge` 交叉评判，建立 K1/K8/K4 真实基线，校验 Q3 出关口径可达性 | KPI 现状值目前为估计 | 待启动（最高优先级） |
| **S6**（11 月） | Reviewer 角色灰度：仅高风险子任务开启审查，审查预算 ≤ 被审查工作的 20% | K4 → ≤$0.03 | 待启动 |
| **S7**（12 月） | 叠加式审查流水线补完：`review --deep` 已具备独立模型评审能力，待补「打回自动回流」；全局决策日志治「脑裂」 | 规模化质量 | 部分 |
| **S7**（12 月） | `router recommend`：基于 bench/judge 评估结果自动生成路由配置 | [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md) §3.5-3.7 | 待实施（交叉评判 + calibrate 已落地） |

**年度出关**：K1 ≥97%、K3 ≤1.5min、K8 ≥90%、K5 ≥99.9%（S1 起恢复成功率埋点已积累一个季度数据）。

## Q4 2026 扩展：办公能力（S9，设计中）

> **状态**：设计稿完成（2026-08-01，见 [design/office-capability-extension.md](design/office-capability-extension.md)），排入 S9
> **决策**：不自建 Office 编辑器，补齐 MCP 消费 + 产物导出两个架构能力，复用已成标准的 Office MCP 生态
> **前提**：依赖 S4 路由机制稳定（外部 MCP server 也是模型路由的对象）+ $/pass 门禁不劣化

| 迭代 | 交付物 | 对应缺口 | 预估 | 验收门禁 |
|------|--------|---------|------|---------|
| **S9-A**（12 月） | **MCP 消费层**：新增 `mcp_client.py`（MCPClientPool + MCPServerConnection，stdlib 实现 JSON-RPC over stdio）；`config.json` 新增 `mcp_servers` 节（command/args/env/enabled/tool_filter/scope）；`pipeline.py` 启动时拉起连接池、结束时 finally 回收；外部工具命名空间 `{server}__{tool}` 合并进 AgentLoop `tools` 字段 + claude CLI `--mcp-config` 透传；故障隔离（启动失败降级 warning 不阻断 pipeline，与 notify/skills 同级） | 缺口 A：无外部工具消费 | ~1 周 | 配置 excel/ppt MCP server 后，子任务能调用 `excel__read_sheet`；server 启动失败时任务正常完成；无僵尸进程（K10 ≥95%） |
| **S9-B**（12 月） | **产物导出路径**：新增 `artifacts.py`（collect_from_worktree + export + render_export_summary）；`__artifacts__/` 约定目录（声明制）；`--artifact-dir` CLI 参数 + `artifact_dir` config；`pipeline.py` 清理 worktree 前扫描收集；TASK.md prompt 注入产物目录约定；final report 列出导出清单 | 缺口 B：无产物导出 | ~4 天 | 子任务写 `__artifacts__/report.pptx` + `--artifact-dir ~/reports` → 文件出现在目标目录；不指定时向后兼容（K11 = 100%） |
| **S9-C**（次年 1 月） | **端到端场景验证 + 文档**：Office MCP 集成指南（excel/ppt/ms365 三套配置示例 + openpyxl 公式陷阱说明）；eval_suite 新增"文档生成"类任务（验证产物完整率）；`tool_filter`/`scope` 调优指南 | 闭环验证 | ~3 天 | 端到端：`agent_go run ... --artifact-dir` 生成完整 PPT 报告并导出成功 |

**S9 出关口径**：K10（MCP 工具调用成功率）≥95%、K11（产物导出完整率）=100%、$/pass 不劣化（外部工具调用的 token 计入 metering，受门禁约束）。

依赖关系：S9-A 与 S9-B 可并行（A 改 pipeline 启动/收尾的连接管理，B 改 worktree 清理前的产物收集，两者改动点不重叠）；S9-C 依赖 A+B 完成。**S9 整体不阻塞年度出关口径**——它是能力扩展，K1/K8/K4 核心指标不依赖它。

## 2027 Q1 展望：基础设施化（评估中）

> **状态**：设计草案完成（[design/infrastructure-api-design.md](design/infrastructure-api-design.md)），待论证必要性和可行性后决定是否投入。
> 以下排期为假设通过后的预估。若否决，Q4 末方向保持不变。

| 迭代 | 交付物 | 预估 | 验收门禁 |
|------|--------|------|---------|
| **I9**（1 月） | Python API 增强：`run_task()` 返回 `TaskResult` + CLI `--json`（所有子命令） | ~3d | 外部 Python 脚本 `from agent_go import run_task; result = run_task(...)` 能拿到结构化结果（CLI `--json` 全局标志 ✅ 已落地，MCP 已提供结构化 tool 接口） |
| **I10**（1 月） | 事件总线：`emit_event` / `subscribe_event` + `events.jsonl` + Webhook 生命周期事件 | ~2d | 全生命周期事件（plan.generated → subtask.started → subtask.completed → pipeline.completed）可订阅、可落盘 |
| **I10** | 状态查询 API：`query_task()` / `query_project_trend()` | ~1d | `query_task("task-xxx").status` 返回 "completed"或"failed" |
| **I11**（2 月） | 知识存储：`KnowledgeStore` 数据模型 + 文件读写 + `_extract_patterns` 增量更新 + Plan 注入 | ~3d | 连续跑 3 个同类 task，第 4 个的 Plan prompt 包含历史验证命令 |
| **I11** | `agent-go-action` GitHub Action（独立仓库） | ~2d | CI 中 `uses: agent-go/action@v1` 能跑通完整 pipeline |
| **I12**（3 月） | `pre-commit-agent-go` hook（独立仓库） | ~1d | `git commit` 前自动跑验证命令，失败阻止提交 |
| **I12** | `vscode-agent-go` extension（独立仓库，薄壳） | ~3d | 面板展示当前任务进度 + 历史列表 + 一键运行 |

**I9-I12 出关口径**：K9 集成接入数 ≥10（含 CI + IDE + Webhook 三类），知识注入采纳率 K10 ≥60%。

依赖关系：I9 是 I10 的前置（`TaskResult` 数据结构被后续所有模块依赖）；I10/I11 可并行；I12 依赖 I9（CLI `--json`）+ I10（事件进度）。

## 长程 Agent 演进路线（论文对照，2026 Q3–2027+）

> 基于综述论文 *Towards Long-Horizon Agents: A Survey* 的统一框架（`Agent = πθ ⊕ H`），将 agent_go 的能力建设映射到 H1→H2→H3 递进路线。详见 [prd.md](prd.md)「长程 Agent 演进路线（论文对照）」完整分析。

### 阶段一：补齐 H2 能力 — 让单次任务更可靠（2026 Q3–Q4）

目标：从 H1（单任务可靠）跨越到 H2（跨上下文记忆 + 自适应策略）。

| 迭代 | 交付物 | 论文对应 | 预估 | 验收门禁 |
|------|--------|---------|------|---------|
| **H2-1**（10–11 月） | **KnowledgeStore 落地**：Factual Memory（项目规则自动维护）+ Experiential Memory（验证命令成功率 / 分解策略有效性）+ Memory Maintenance（合并/去重/过期） | §4.2.2 Persistent Memory | ~3d | 同类任务第 3 次执行时 Plan prompt 自动包含历史验证命令模式 |
| **H2-1** | **成本预算硬约束**：`--max-cost $X` 任务级上限 + 事前预估→事中监控→超限熔断 + $/pass 标度律数据积累 | §7.3.1 Cost-aware Agency | ~2d | `--max-cost 0.30` 执行中超限自动熔断并输出已花费明细 |
| **H2-2**（11–12 月） | **分支式工作流（Branching）**：Plan 阶段对 `difficulty=hard` 步骤生成备选路径 + 验证失败时回退到分叉点尝试替代策略 + 轻量评估选最优 | §4.1.3 Branching Workflows | ~3d | 注入故障的端到端用例：验证失败→自动切换备选方案→第二路径成功 |
| **H2-2** | **Runtime-adaptive Hooks**：Hook 根据执行状态动态调整（如「连续 3 次验证失败→自动降低 difficulty 并换模型」） | §4.5.3 Runtime-adaptive Hooks | ~2d | 配置可切换的动态 Hook 规则，日志记录触发原因 |

**H2 出关口径**：K1 ≥93%、同一项目第 3 次执行 Plan 注入历史经验、超 $0.50 任务自动熔断、hard 任务至少尝试 2 条路径。

### 阶段二：开启 H3 能力 — 让 Agent 随时间变强（2027 Q1–Q2）

目标：从 H2（跨上下文记忆）跨越到 H3（跨任务经验积累 + Harness 自进化）。

| 迭代 | 交付物 | 论文对应 | 预估 | 验收门禁 |
|------|--------|---------|------|---------|
| **H3-1**（1–3 月） | **Harness 参数自动调优**：基于 metering.jsonl + meta.json 历史数据，自动优化并发度、max_retries、验证策略选择 | §7.1.1 Self-evolving Harness (Level 1) | ~3d | 同项目 10 次执行后自动参数 vs 默认参数的 $/pass 降低 ≥15% |
| **H3-1** | **分解模式库**：对「重构/新增功能/Bug 修复/迁移」四类任务的 Plan 分解策略沉淀 + Plan 阶段自动注入最佳分解模板 | §4.2.2 Experiential Memory | ~2d | 同类任务 Plan 首次通过率（用户直接确认，无需 edit）提升 ≥20% |
| **H3-2**（3–5 月） | **编排拓扑自演化**：Agent 自主决定子任务数量、分组方式、Reviewer 范围、验证步骤剪枝 | §7.1.1 Self-evolving Harness (Level 2) + §4.4.3 Orchestration Optimization | ~4d | 自动编排 vs 人工编排的 $/pass 不劣化且耗时 ≤ 人工的 80% |
| **H3-2** | **失败模式识别**：提前预警「此任务特征历史上成功率 < 40%」→ 建议人工介入或切换策略 | §7.4.1 Error Robustness | ~2d | 高风险任务执行前展示风险评分 + 历史相似任务成功率 |
| **H3-3**（5–6 月） | **Skill 自主蒸馏**：从成功执行轨迹中自动提取可复用 Skill（验证命令组合 / 代码模式 / 修复策略），写入 Skill 库 | §7.1.1 Self-evolving Harness (Level 3) + §4.3.3 Skill Libraries | ~4d | 蒸馏出的 Skill 被后续任务自动匹配使用，人工审核通过率 ≥80% |

**H3 出关口径**：K1 ≥95%、自进化 Harness 使 $/pass 再降 20%、跨项目经验可迁移、失败预警准确率 ≥70%。

### 阶段三：基础设施化与开放性（2027 Q3+）

| 迭代 | 交付物 | 论文对应 | 预估 |
|------|--------|---------|------|
| **F-1** | **Harness 协议标准化**：Plan/Execute/Verify 接口抽象为开放协议（类似 MCP 对 Tool 的标准化） | §7.1.2 Harness Generalization + §4.4.4 Agent Protocols | ~5d |
| **F-1** | **多 Runtime Worker 支持**：除 Claude Code 外，兼容 OpenCode / aider / Codex CLI 作为 Worker | §7.1.2 多 Harness 训练 | ~4d |
| **F-2** | **独立安全验证**：安全评分与成功率并列的第一类指标 + 独立 Verifier（不由执行 Agent 自审） | §7.4.2 Safety & Governance | ~4d |
| **F-2** | **可移植 Skill 格式**：Skills 产出符合 Agent Skills 标准的跨 Runtime 可复用制品 | §7.1.2 Portable Skills | ~2d |

### 演进总览

```
2026 Q3-Q4          2027 Q1-Q2           2027 Q3+
H2 补齐              H3 开启              Frontier
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
记忆 + 分支 + 预算    自进化 + 经验积累     开放协议 + 安全 + 多Runtime
K1≥93%              K1≥95%              生态可移植
K4≤$0.04            K4≤$0.02            K9≥10
```

### 论文关键启示（约束设计决策）

| 启示 | agent_go 的设计约束 |
|------|-------------------|
| Harness 是长期护城河，不是模型 | 编排层投入优先级 > 模型适配；架构保持 model-agnostic |
| 自进化需防过拟合 | 自进化目标函数 = 真实任务成功率（非 benchmark 分）；需人工抽检验证 |
| 安全是 Harness 层问题 | 安全机制必须独立于执行 Agent；后续所有自进化特性配独立安全验证 |
| 持久化记忆的维护比存储更难 | KnowledgeStore 重点投入合并/去重/过期策略，而非存储容量 |
| 分支探索成本需显式控制 | Branching 仅对 hard 子任务开启；单次分支 token 预算 ≤ 主路径 30% |
| $/pass 是系统级指标 | 所有新功能必须以 $/pass 不劣化为前置门禁 |

## 关键风险与对策

- **验证循环 token 爆炸**（PRD 已识别）：`max_retries` 硬上限 + 每迭代超时；用计量日志盯 `cost_usd` 分布，超 P95 告警
- **模型路由拉低通过率**：Worker 走便宜模型必须配质量门 + 抽样回测；Planner 铁律不降级
- **范围蔓延**：H3 自进化特性必须分阶段验收，每阶段以 $/pass 不劣化为门禁；未经 bench 验证不进入下一阶段
- **自进化过拟合风险**（论文警告）：优化目标 = 真实任务成功率，非 benchmark 分；每 50 次执行人工抽检 10%
- **持久化记忆质量退化**（论文警告）：KnowledgeStore 必须有自动去重/合并/过期策略，防止噪声积累
- **安全问题**：后续每个自进化特性必须配独立安全验证（不由执行 Agent 自审）

## 立即可做的三件事（本周）

1. ~~S1 计量日志开工~~ ✅ 已完成（2026-07-25）
2. ~~M2 失败摘要~~ ✅ 已完成（2026-07-25）
3. ~~刷新文档数据漂移~~ ✅ 已完成（README/architecture.md/spec.md 同步至 698 测试）
4. ~~测试加固 + $/pass 门禁~~ ✅ 已完成（2026-07-25，1130 测试 5 连绿，ISSUE-24~28 修复）
5. ~~模型分级 + 评估机制设计稿~~ ✅ 已完成（2026-07-25，[design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md)）
6. ~~S8 P0 模型评估机制落地~~ ✅ 已完成（2026-07-25，pricing.py + bench.py + eval_suite + cross_judge.py P1）
7. ~~核心解耦~~ ✅ 已完成（2026-07-25，evaluator/notify/goal/skills/agent_loop 全部动态 import + try/except）
8. ~~CLI/MCP 交互层改进~~ ✅ 已完成（2026-08-01，MCP 6 tools + Resources/Prompts 原语 + HTTP/SSE transport + 错误 fix 字段 + CLI 恢复引导，1362 测试全绿）
9. ~~PRD/Roadmap 文档同步~~ ✅ 已完成（2026-08-01，prd.md 新增「CLI 与 MCP 交互层」章节，roadmap 快照更新）
10. ~~办公能力扩展设计稿~~ ✅ 已完成（2026-08-01，[design/office-capability-extension.md](design/office-capability-extension.md)；prd.md 新增「办公能力扩展」章节 + K10/K11；roadmap 排入 S9）
11. ~~CLI/MCP 保留项落地~~ ✅ 已完成（2026-08-01，波次进度卡片 / skills show / 多 profile / 增量 Plan Diff / Sampling 原语，1387 测试全绿，改进清单全部闭环）

**下一批**：对照 bench 真实执行（3 模型 × 22 任务 × 3 重复）→ `eval models` 决策矩阵 + `eval judge` 交叉评判，建立 K1/K8/K4 真实基线；KPI 数据采集验证（K1/K8 是否因 S2/S4 提升）；**S9-A MCP 消费层**（`mcp_client.py` + `mcp_servers` config）待启动。
