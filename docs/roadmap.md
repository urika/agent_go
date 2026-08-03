# agent_go Roadmap：从现状到「周五派发、周一 merge」

> 基线：2026-07-24，v2.0.0，684 测试全绿，14 项已知缺陷清零。
> 目标对齐 [prd.md](prd.md) 的 Q3 / 年度 KPI；差距分析依据见 prd.md「P0 缺失功能」「P1 重点」章节。

## 进度快照（2026-08-01 更新，1554 测试全绿）

| 迭代 | 状态 | 说明 |
|------|------|------|
| **S11 L1.5 AST 冲突检测** | ✅ 完成（2026-08-01） | `detect_step_conflicts()` 用 ast 提取 Python 顶层符号，Plan 确认后拦截多 step 同文件/同符号冲突；符号级（交互确认）与文件级（提示）分级；零 LLM 成本（[arXiv:2603.24284](design/sdd-references-and-frameworks.md) 97% 精度）。1521 测试全绿（+15） |
| **S10-P2 全因子 Bench Tier 1 编排** | ✅ 完成（2026-08-01） | P1 字段采集（`per_subtask`/`binary_pass`/`semantic_pass`/`plan_step_count`，`751ec10`）；语义评估 API 故障跳过信号（`a72d4bf`）；`--parallel 1` 顺序执行消除并发干扰；**代码质量维度**（`_collect_quality`：ruff E/F/W + mypy + pytest → `lint_errors`/`tests_broken`，含代码回归率分析）；**对照基线** `eval baseline`（claude -p 裸跑，stream-json 提 cost + verification 判定 + 质量检查）；动态 timeout（子任务数 × 150s + 120s）。1554 测试全绿（+14 自 1521） |
| **S10-P1 Bench v2 Schema 扩展 + Cross-Judge** | ✅ 完成（2026-08-01） | bench record 新增 `timed_out`/`judge_model`/`planner_model`/`source_batch` 字段（P0）；`eval bench --source-batch`；$/pass 统一口径 = sum(cost)/sum(pass_rate)（§3.1）+ K8 修订 = 通过 record 中 zero-retry 占比（§3.4）；cross_judge 输出 `self_judge_model` + 自评偏差量化报告。1521 测试全绿（+11） |
| **S11-P0 结构化输入 + 准入审查** | ✅ 完成（2026-08-01） | `spec.py`（Task Spec 7 章节解析 + L1 硬门禁 4 项检查）；`--spec`/`--force` CLI 参数；`spec template`/`spec validate` 子命令；generate_plan 接受 `spec_context` 注入 system prompt 硬约束。1521 测试全绿（+31 spec 测试）。设计稿：[design/agent-go-input-spec.md](design/agent-go-input-spec.md) |
| **Bench v1 数据分析** | ✅ 完成（2026-08-01） | 7 模型 × 22 任务 × 5 批次，490 条有效记录。KPI 基线校准（K1 83.9%/K8 88.9%/$pass $0.39）、模型维度（Haiku > Sonnet）、DeepSeek 不可用验证、difficulty 标签偏差识别。4 处数值修正。报告：[bench-analysis-2026-08-01.md](bench-analysis-2026-08-01.md)；数据需求：[design/bench-v2-data-requirements.md](design/bench-v2-data-requirements.md) |
| **输入准则 + 准入审查设计** | ✅ 完成（2026-08-01） | Task Spec 7 章节规范 + Plan prompt 注入映射；Spec Gate L1 硬门禁（4 项）+ L2 软警告（4 项 LLM 辅助）；`--spec` / `spec template` / `scope` 命令设计。设计稿：[design/agent-go-input-spec.md](design/agent-go-input-spec.md)；PRD 已更新结构化输入章节。待落地实施 |
| **CLI/MCP 交互层** | ✅ 完成（2026-08-01） | MCP 6 tools（新增 `list_tasks` / `cancel_task`）+ Resources 原语（6 个）+ Prompts 原语（3 个 SOP 模板）+ **HTTP/SSE Transport**（`agent_go mcp --http`，Bearer token 鉴权）；错误响应 `fix` 字段（ERROR_TEMPLATES 7 种类型）；ActivityTracker 并行活动追踪（异步任务后台监控）；CLI 失败恢复闭环引导 + 后续操作卡片；任务生命周期 `cancelled` 状态可恢复。设计稿：[design/cli-mcp-design-analysis.md](design/cli-mcp-design-analysis.md) + [design/cli-mcp-interaction-analysis.md](design/cli-mcp-interaction-analysis.md) |
| **CLI/MCP 保留项落地** | ✅ 完成（2026-08-01） | 波次进度卡片（`_estimate_wave_count` + wave N/M 卡片）；`skills show <name>` SKILL.md 自描述；多 profile（`--profile` / `AGENT_GO_PROFILE` → `~/.agent_go/profiles/`）；增量 Plan 迭代 + 实时 Diff（`show_plan_diff` + 菜单 [V] 版本历史）；Sampling 原语（`request_sampling` stdio 双向 + cancel_task `confirm`）。改进清单全部闭环 |
| **S9-A MCP 消费层** | ✅ 完成（2026-08-01） | `mcp_client.py`（MCPClientPool + MCPServerConnection）；`mcp_servers` config 节（command/args/env/enabled/tool_filter/scope）；pipeline 启动/收尾连接池管理；外部工具命名空间 `mcp__{server}__{tool}`（agent_loop tools 合并 + claude `--mcp-config` 透传）；故障隔离降级 warning。设计稿：[design/office-capability-extension.md](design/office-capability-extension.md) |
| **S9-B 产物导出** | ✅ 完成（2026-08-01） | `artifacts.py`（collect_from_worktree + export + render_export_summary）；`__artifacts__/` 约定目录（声明制）；`--artifact-dir` CLI + `artifact_dir` config；pipeline 清理 worktree 前收集；TASK.md 注入产物约定；final report 列出导出清单。B1/B2/B3 验收通过 |
| **测试加固** | ✅ 完成（2026-08-01） | 1464 测试全绿（+22 自 1442 基线） |
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
Q3 2026（信任层 + 成本层 + Bench v2）     Q4 2026（体验层 + 规模化）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━       ━━━━━━━━━━━━━━━━━━━━━━━━━━
验证循环 → 计量日志 → 模型路由               PR 仪表 → 时间预估 → 审查流水线
Bench v2 → 交叉评判 → KPI 真实基线          KnowledgeStore → Reviewer 灰度
K1≥92% K8≥80% K4≤$0.05                     K1≥97% K4≤$0.03 K3≤1.5min
⚠️ Bench v1: K1 83.9% K8 ✅ 88.9% K4 🔴 $0.34-0.69
```

> **2026-08-01**：Bench v1 实测 KPI 基线（K1 83.9%、K8 88.9%、K4 $0.34-0.69、$/pass $0.39）。K8 已超额完成 Q3 目标，K1 差距 8.1pp，K4/$/pass 差距 7-14x。Q3 $0.05 成本目标在当前技术栈下极其激进，S10 完成后以 bench 实测数据做最终判定。

## Q3 2026（7–9 月）：补上信任与成本两根支柱

| 迭代 | 交付物 | 对应缺口 | 预估 | 验收门禁 |
|------|--------|---------|------|---------|
| **S1**（7 月底–8 月初） | 结构化计量日志落地：`role / actual_provider / cost_usd / fallback_reason` 每请求一条；接通 `metrics.extract_usage` | 差距 3/4 的数据源 | ~2 天 | eval cost 报表能看到 per-role 拆分 |
| **S1** | M2 失败原因摘要：meta.json 增加 `failure_summary`（验证命令 + exit code + stderr 尾部），`show`/`status` 直接展示 | M2，K6 7/9→8/9 | ~1 天 | 失败任务不看日志能定位原因 |
| **S2**（8 月上中旬） | 验证循环 Phase 1：VerificationAgent + RepairAgent（fix prompt 注入 stdout/stderr/git diff）+ `max_retries` 可配（默认 3）+ **blocked 阻断下游** | M5/M6，K8 | 2–3 天（设计稿已定，见 [design/verification-agent-goal-spec.md](design/verification-agent-goal-spec.md)） | 注入故障的端到端用例：下游被阻断、worktree 保留待审 |
| **S3**（8 月下旬） | 验证循环 Phase 2：`/goal` 注入 + Stop Hook + watchdog；Phase 4：eval 新指标（首次通过率、重试成功率、阻断率） | K8 度量闭环 | 3–4 天 | K8 首次通过率有可追溯数据源 |
| **S3** | M1 完成通知：任务结束触发 webhook / 系统通知（最小实现，配置驱动） | M1 | ~1 天 | `--yes` 无头跑完能收到通知 |
| **S4**（9 月） | 角色感知模型路由：planner/worker/reviewer 三通道配置 + 降级留痕（`fallback_reason` 必填）+ 本地模型并发上限显式化 | 差距 3，K4 | 3–5 天 | **发布门禁：$/pass rate 不劣化**（对比 S1 基线） |
| **S11-P0**（8 月第 1-2 周）✅ | **结构化输入 + 准入审查**：`--spec` 参数（Task Spec 解析注入 Plan prompt）；`agent_go spec template`（模板生成）；`spec validate`（L1 审查）；Spec Gate L1 硬门禁（必填章节/文件路径/白名单/长度下限，确定性检查）；`--force`/`--yes` 行为定义 | 输入质量 → Plan 质量 → 成本控制 | ✅ 完成（1495 测试，+31 spec 测试） |
| **S11 L1.5**（8 月第 3-4 周）✅ | **Spec Gate L1.5 AST 冲突检测**（学术驱动新增）：`detect_step_conflicts()` 用 ast 提取 Python 顶层符号，Plan 确认后、执行前检测多 step 同文件/同符号冲突。符号级（高置信，交互确认）与文件级（提示）分级。零 LLM 成本。学术支撑 [arXiv:2603.24284](design/sdd-references-and-frameworks.md#26-多-agent-协调与规范鸿沟serper-补充关键) 实测 97% 精度 | 多子任务集成冲突前置拦截 | ✅ 完成（1521 测试，+15 L1.5 测试） |
| **S11**（9 月，依赖 S10-P1） | Spec Gate L2 软警告（LLM 辅助：范围完整性/约束一致性/验收可自动化度/历史风险匹配） | 输入质量 → 降低方向偏离和无效重试 | P1 ~1d | L2 警告准确率 >80%（人工抽检 20 条 Spec） |
| **S11**（9 月，依赖 S10-P1） | `agent_go scope`（轻量 Scoping：读代码库 + 追问澄清 → 输出 Task Spec 草稿） | 上游工具链完整性 | P1 ~2d | 端到端：`agent_go scope "需求" → Task Spec → agent_go run --spec → PR` |
| **S10**（8-9 月） | **Bench v2：可信 KPI 基线 + 模型选型决策依据**（详见 [prd.md §Bench v2 计划](prd.md) 和 [design/bench-v2-data-requirements.md](design/bench-v2-data-requirements.md)） | — | — | — |

**S10 分阶段详情：**

| 阶段 | 交付物 | 预估 | 验收门禁 |
|------|--------|------|---------|
| **S10-P1**（8 月第 1-2 周）✅ | **Schema 扩展 + Cross-Judge**：新增 `timed_out`/`judge_model`/`planner_model`/`source_batch` 字段（P0）；统一 $/pass 计算口径 + K8 定义修订为「通过 record 中 zero-retry 占比」；对 v1 已有 output 运行 cross_judge（3-4 judge 模型交叉评判，量化自评偏差） | ✅ 完成（2026-08-01，`551b713`；数据 `929bd58`） | **S10-P2**（8 月第 3-4 周）✅ | **全因子 Bench Tier 1**：Claude 三模型 × 22 任务 × 3 重复 = 198 次运行（`--parallel 1` 顺序执行，消除并发干扰）；新增 `per_subtask`/`binary_pass`/`semantic_pass`/`plan_step_count` 字段（P1）；代码质量维度（lint + test regression 自动检测）；对照基线（`claude -p` 裸跑 5-6 代表性任务） | ✅ 代码完成（2026-08-01，见进度快照 S10-P2 行）；198 次全因子运行待执行 | ✅ 198 条记录完整无缺失字段；对照基线数据到位 |
| **S10-P2b**（与 P2 并行，学术驱动） | **spec 细节梯度对照实验**（[prd.md §Spec Gate 增强](prd.md)）：对 6-8 个多子任务任务，用 4 级 spec 细节（L0 完整 Spec / L1 去约束 / L2 仅目标 / L3 裸 prompt）跑，测 pass_rate 与集成成功率随 spec 细节的变化。**填补「SDD 无对照实验」学术空白**。学术支撑 [arXiv:2603.24284](design/sdd-references-and-frameworks.md)（恢复完整 spec 即恢复 89% 上限） | ~1d（复用 P2 管道） | 输出 spec 细节 → pass_rate 回归曲线；反哺 Spec Gate 阈值校准 |
| **S10-P3**（9 月第 1-2 周） | **分析 + 决策更新**：全量指标计算（含 95% CI + 效应量）；per-task 难度系数发布；模型分级矩阵 bench 实测校准；`router recommend` 基于 bench 数据自动生成路由配置；PRD KPI 基线更新；**semantic evaluator 职责边界明确化**（[prd.md §Spec Gate 增强](prd.md)）：界定为「只查结构残差」，不重复 shell/lint 能确定的验证。学术支撑 [arXiv:2603.25773](design/sdd-references-and-frameworks.md)（AI 审 AI 是结构性循环） | ~2d 分析 + ~1d 文档 | K1/K4/K8/$/pass 四个指标汇报 CI；分级矩阵标注数据来源（bench 实测 vs 厂商声称）；router recommend 输出可复现；semantic evaluator 职责边界文档化 |
| **S10-P4**（9 月第 3-4 周，可选） | **扩展 + 稳定性**：Tier 2+3 模型扩展（DeepSeek/Kimi 补齐全部 22 任务 + ≥3 重复）；高方差任务增加到 5-10 次重复；Plan 质量维度抽样评估；级联效应专项测试 | ~1d 代码 + 按 bench 耗时 | Kimi hard 任务首次有数据；稳定性 CV 报告发布 |

**S10 出关口径**：cross_judge 自评偏差量化完成、KPI 四个指标有 CI、模型分级矩阵以 bench 实测为唯一依据、$pass 计算口径统一且文档化。

**依赖关系**：S10-P1 无前置依赖，可立即启动。S10-P2 依赖 P1 schema 稳定。S10-P3 依赖 P2 数据到位。S10-P4 不阻塞 P3 出关，可并行或延期。**S10 整体不阻塞 S9-B（产物导出）**——两者改动点不重叠。

**S11 依赖链（新增）**：
```
S11-P0 (--spec + spec template + L1 gate) ← 独立，立即启动，~2d
S10-P1 (schema + cross_judge)              ← 独立，立即启动，~3d
    ↓
S11-P1 (L2 gate + agent_go scope)          ← 依赖 S10-P1（需 judge 质量可信）
    +
S10-P2 (全因子 bench)                       ← 依赖 S10-P1
    ↓
S10-P3 (分析 + 分级校准)
    ↓
S6 (Reviewer 灰度 + KnowledgeStore) → S7 (router recommend)
```
S11-P0 和 S10-P1 可并行启动，改动点不重叠（S11 改 CLI + api.py prompt 注入，S10 改 eval 数据采集 schema）。

**数据需求**：完整 schema 扩展、实验设计、指标体系、统计规范见 [design/bench-v2-data-requirements.md](design/bench-v2-data-requirements.md)。

**Q3 出关口径（2026-08-01 重评估）**：K1 ≥92%、K8 ≥80%、K4 ≤$0.05、$/pass ≤$0.05、K6 8/9。

> **Bench v1 实测显示**：K1 83.9%（距目标 -8.1pp）、K8 88.9%（✅ 已达标）、K4 $0.34-0.69（距目标 7-14x）、$/pass $0.39（距目标 8x）。K1 差距可通过 KnowledgeStore（H2-1）+ Plan 优化缩小；K4/$/pass 差距在当前技术栈下极其激进，**$0.05 目标可能需要下调或延期到 Q4**。S10 完成后以 bench 实测数据为唯一依据做最终判定。

## Q4 2026（10–12 月）：兑现及格线，再扩规模

> **2026-08-01 重排**：S5 全部（M7/M3/M4/Plan 版本管理）、S6 的复杂度双通道与失败通知增强、S7 的 PR 自动推送均已提前落地（见进度快照）。**原有 S6「KPI 基线采集」已升级为 S10 Bench v2（提前至 Q3 执行）**。剩余项重新编排如下。

| 迭代 | 交付物 | 对应缺口 | 状态 |
|------|--------|---------|------|
| ~~S5~~ | ~~M7 结果审查 / M3 PR 质量仪表 / M4 时间预估 / Plan 版本管理~~ | — | ✅ 已提前落地 |
| ~~S6~~ | ~~复杂度双通道 / 失败通知增强~~ | — | ✅ 已提前落地 |
| **S10** | **Bench v2**（提前至 Q3 8-9 月执行，见上方 S10 详情） | KPI 基线 + 模型选型决策依据 | 🔶 待启动 |
| **S6**（11 月） | **Reviewer 角色灰度**：基于 S10 bench 数据确定 Reviewer 模型池；仅高风险子任务开启审查，审查预算 ≤ 被审查工作的 20% | K4 → ≤$0.03 | 待 S10-P3 模型分级校准 |
| **S6**（11 月） | **KnowledgeStore 加速落地（H2-1）**：Bench v1 分析表明这是缩小 K1 差距（83%→92%）成本最低的杠杆。Factual Memory（项目规则自动维护）+ Experiential Memory（验证命令成功率 / 分解策略有效性） | K1 提升 | 待 S10-P1 cross_judge 确保 pass 数字可信 |
| **S7**（12 月） | 叠加式审查流水线补完：`review --deep` 已具备独立模型评审能力，待补「打回自动回流」；全局决策日志治「脑裂」 | 规模化质量 | 部分 |
| **S7**（12 月） | `router recommend`：基于 S10 bench 评估结果自动生成 + 验证路由配置 | [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md) §3.5-3.7 | 待 S10-P3 |

**年度出关**：K1 ≥97%、K3 ≤1.5min、K8 ≥90%、K5 ≥99.9%（S1 起恢复成功率埋点已积累一个季度数据）。

## Q4 2026 扩展：办公能力（S9）

> **状态**：S9-A（MCP 消费层）✅ 已实现（2026-08-01）；S9-B（产物导出）✅ 已实现（2026-08-01）；S9-C（端到端验证+文档）待启动
> **决策**：不自建 Office 编辑器，补齐 MCP 消费 + 产物导出两个架构能力，复用已成标准的 Office MCP 生态
> **前提**：依赖 S4 路由机制稳定（外部 MCP server 也是模型路由的对象）+ $/pass 门禁不劣化

| 迭代 | 交付物 | 对应缺口 | 预估 | 验收门禁 |
|------|--------|---------|------|---------|
| **S9-A** | **MCP 消费层**：`mcp_client.py`（MCPClientPool + MCPServerConnection，stdlib 实现 JSON-RPC over stdio）；`config.json` 新增 `mcp_servers` 节（command/args/env/enabled/tool_filter/scope）；`pipeline.py` 启动时拉起连接池、结束时 finally 回收；外部工具命名空间 `mcp__{server}__{tool}` 合并进 AgentLoop `tools` 字段 + claude CLI `--mcp-config` 透传；故障隔离（启动失败降级 warning 不阻断 pipeline，与 notify/skills 同级） | 缺口 A：无外部工具消费 | ✅ 已实现（2026-08-01） | ✅ 已通过：配置 excel/ppt MCP server 后子任务可调用 `mcp__excel__read_sheet`；server 启动失败任务正常完成 |
| **S9-B** | **产物导出路径**：新增 `artifacts.py`（collect_from_worktree + export + render_export_summary）；`__artifacts__/` 约定目录（声明制）；`--artifact-dir` CLI 参数 + `artifact_dir` config；`pipeline.py` 清理 worktree 前扫描收集；TASK.md prompt 注入产物目录约定；final report 列出导出清单 | 缺口 B：无产物导出 | ✅ 已实现（2026-08-01） | ✅ 已通过：B1 子任务写 `__artifacts__/report.md` + `--artifact-dir` → 文件出现在目标目录；B2 不指定时向后兼容无导出；B3 失败保留 worktree 产物可收集 |
| **S9-C**（次年 1 月） | **端到端场景验证 + 文档**：Office MCP 集成指南（excel/ppt/ms365 三套配置示例 + openpyxl 公式陷阱说明）；eval_suite 新增"文档生成"类任务（验证产物完整率）；`tool_filter`/`scope` 调优指南 | 闭环验证 | ~3 天 | 端到端：`agent_go run ... --artifact-dir` 生成完整 PPT 报告并导出成功 |

**S9 出关口径**：K12（MCP 工具调用成功率）≥95%、K13（产物导出完整率）=100%、$/pass 不劣化（外部工具调用的 token 计入 metering，受门禁约束）。

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
| **H2-1** | **成本预算硬约束**：`--max-cost $X` 任务级上限 + 事前预估→事中监控→超限熔断 + $/pass 标度律数据积累 | §7.3.1 Cost-aware Agency | ✅ 已提前实现（S10） | ✅ L1 `--max-budget-usd` + L2 子任务累计 + L3 `--max-cost` 任务级熔断；默认关闭，`eval cost-baseline` 删失校正基线校准预算 |
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
10. ~~办公能力扩展设计稿~~ ✅ 已完成（2026-08-01，[design/office-capability-extension.md](design/office-capability-extension.md)；prd.md 新增「办公能力扩展」章节 + K12/K13；roadmap 排入 S9）
11. ~~CLI/MCP 保留项落地~~ ✅ 已完成（2026-08-01，波次进度卡片 / skills show / 多 profile / 增量 Plan Diff / Sampling 原语，1387 测试全绿，改进清单全部闭环）
12. ~~S9-B 产物导出~~ ✅ 已完成（2026-08-01，`artifacts.py` + `__artifacts__/` 声明制 + `--artifact-dir`，B1/B2/B3 验收通过，1464 测试全绿）

**下一批**：
1. ~~S11-P0~~ ✅ 完成（`cd5361c`，`--spec` + `spec template` + L1 硬门禁）
2. ~~S10-P1~~ ✅ 完成（`551b713` + `929bd58`，Bench v2 Schema + Cross-Judge）
3. ~~S11 L1.5~~ ✅ 完成（`216882b`，AST 冲突检测）
4. ~~S10-P2 全因子 Bench Tier 1 编排~~ ✅ 代码完成（2026-08-01：P1 字段 + `--parallel 1` + 代码质量维度 + `eval baseline` 对照基线 + 动态 timeout）。**198 次全因子运行 + 对照基线运行待执行**（hard 任务 timeout 达 30min，预估全量 ~15-20h 墙钟）
5. **S10-P2b spec 细节梯度实验**（与 S10-P2 并行，复刻 [arXiv:2603.24284](design/sdd-references-and-frameworks.md) L0-L3 梯度）— 填补 SDD 无对照实验空白
6. **KnowledgeStore 设计细化（H2-1）**— 在 cross_judge 结果完备后启动数据模型和接口设计
