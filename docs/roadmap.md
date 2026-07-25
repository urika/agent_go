# agent_go Roadmap：从现状到「周五派发、周一 merge」

> 基线：2026-07-24，v2.0.0，684 测试全绿，14 项已知缺陷清零。
> 目标对齐 [prd.md](prd.md) 的 Q3 / 年度 KPI；差距分析依据见 prd.md「P0 缺失功能」「P1 重点」章节。

## 进度快照（2026-07-25 更新，1130 测试全绿）

| 迭代 | 状态 | 说明 |
|------|------|------|
| S1 计量日志 | ✅ 完成 | planner/worker 双角色 metering.jsonl 全链路（run + resume）；eval cost per-role 拆分；修复 executor 计量路径死代码、api.py router 路径 NameError |
| S1 M2 失败摘要 | ✅ 完成 | `failure_reason`（验证命令 + exit code + stderr 尾部）写入结果，`show` 展示 |
| S2 验证循环 | ✅ 完成（2026-07-25） | 全链路验收修复 8 项缺口（含 wave 调度排除 blocked 的关键 bug、CLI 配置贯通）+ 剩余项落地：Stop Hook GoalInjector（`--goal-hook`）、retry_timeout 硬超时、goal.enabled 默认对齐 false、`--goal` 开关 |
| M1 完成通知 | ✅ 完成 | `notify.py` 多通道（desktop/webhook/command）+ 事件订阅 + IM 适配器，设计稿：[design/notification-webhook-spec.md](design/notification-webhook-spec.md) |
| S4 模型路由 | 🔶 部分推进 | `router.py`（角色路由 + 熔断 + 降级留痕）已落地，设计稿：[design/router-multi-provider-extension.md](design/router-multi-provider-extension.md)；**复杂度双通道已完成**（2026-07-25：Planner 打 difficulty 标签 → `worker_models` 映射 → claude `--model`，计量记录 difficulty/真实模型） |
| M3 PR 质量仪表 | ✅ 完成 | `_build_quality_dashboard`：通过率/验证率/合并就绪指示 + 子任务明细 + M5 启发式验证警告（2026-07-25 补 blocked 图标与置信度警告） |
| M4 时间预估 | ✅ 完成 | `estimate_task_duration`：历史子任务耗时中位数 × 拓扑波次（考虑并行度），执行前展示 + `time_estimate` 事件 |
| **PRD 分析改进** | ✅ 完成 | OpenChamber 竞品对比分析、四阶段开发流程模型（含 M7 审查阶段缺口识别）、用户介入点设计；已写入 `prd.md` + 排入 `roadmap.md` S5-S7 |
| **测试加固** | ✅ 完成（2026-07-25） | 1130 测试 5 连绿。修复 ISSUE-24（goal watchdog flaky 根治）、ISSUE-25（3 处测试漂移）；新增 72 测试覆盖 agent_loop 集成、5 个未测 CLI 命令、TUI 辅助函数、subtask 超时分支 |
| **$/pass 门禁** | ✅ 完成（2026-07-25） | `eval gate`（绝对阈值 + `--check-regression` 回归对比 + `--update-baseline`）；CI 接入 `eval gate --baseline 0.05`；K5 `resume_success_rate` 派生；修复 ISSUE-26/27/28（计价失真 + evaluator 重复记账 + PRD 语义断裂）。详见 [ISSUES.md](ISSUES.md) |
| **模型分级 + 评估机制设计** | ✅ 完成（2026-07-25） | 三角色 × 三档位分级矩阵 + 三层评估体系（确定性/交叉评判/决策汇总）；完整设计稿 [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md)；**P0 已落地（22 模型定价表 + bench 编排器 + eval models）** |
| **S8 P0 模型评估机制** | ✅ 完成（2026-07-25） | `pricing.py`（22 模型定价表 + MODEL_TIER + 7 provider 默认）；`bench.py`（subprocess 隔离编排器 + `eval bench/models`）；`cross_judge.py`（交叉评判矩阵 P1 + 禁绝自评 + 人工校准）；`eval_suite/`（8 任务 + fixtures）；`config.example.json` 三套预设（国际/国内/混合） |
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
| **S8**（9 月） | 模型分级 + 评估机制 P0：扩充 `MODEL_PRICES`（22 模型）+ `MODEL_TIER` 元数据；标准任务集种子（8 任务 + ground truth）；`eval bench` 编排器 + `analyze_model_productivity` + `eval models`；`config.example.json` 国际/国内/混合三套预设 | [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md) | ✅ 已完成（2026-07-25） |

**Q3 出关口径**：K1 ≥92%、K8 ≥80%、K4 ≤$0.05、$/pass ≤$0.05、K6 8/9。达不到则 Q4 不扩新功能，回头补质量。

依赖关系：S1 必须最先（它是 S4 门禁和北极星指标的数据源）；S2/S3 与 S4 可并行，但 S4 的 Reviewer 通道建议等验证循环稳定后再开，控制变量；**S8 依赖 S4（路由机制）+ $/pass 门禁（已落地）**——评估机制需要可切换的模型路由 + 可信的成本计量才能对照运行。

## Q4 2026（10–12 月）：兑现及格线，再扩规模

> **2026-07-25 重排**：S5 全部（M7/M3/M4/Plan 版本管理）、S6 的复杂度双通道与失败通知增强、S7 的 PR 自动推送均已提前落地（见进度快照）。剩余项重新编排如下。

| 迭代 | 交付物 | 对应缺口 | 状态 |
|------|--------|---------|------|
| ~~S5~~ | ~~M7 结果审查 / M3 PR 质量仪表 / M4 时间预估 / Plan 版本管理~~ | — | ✅ 已提前落地 |
| ~~S6~~ | ~~复杂度双通道 / 失败通知增强~~ | — | ✅ 已提前落地 |
| **S6**（11 月） | **KPI 基线采集**：bench 真实执行（3 模型 × 8 任务 × 3 重复）→ `eval models` 决策矩阵 + `eval judge` 交叉评判，建立 K1/K8/K4 真实基线，校验 Q3 出关口径可达性 | KPI 现状值目前为估计 | 待启动（最高优先级） |
| **S6**（11 月） | Reviewer 角色灰度：仅高风险子任务开启审查，审查预算 ≤ 被审查工作的 20% | K4 → ≤$0.03 | 待启动 |
| **S7**（12 月） | 叠加式审查流水线补完：`review --deep` 已具备独立模型评审能力，待补「打回自动回流」；全局决策日志治「脑裂」 | 规模化质量 | 部分 |
| **S7**（12 月） | `router recommend`：基于 bench/judge 评估结果自动生成路由配置 | [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md) §3.5-3.7 | 待实施（交叉评判 + calibrate 已落地） |

**年度出关**：K1 ≥97%、K3 ≤1.5min、K8 ≥90%、K5 ≥99.9%（S1 起恢复成功率埋点已积累一个季度数据）。

## 2027 Q1 展望：基础设施化（评估中）

> **状态**：设计草案完成（[design/infrastructure-api-design.md](design/infrastructure-api-design.md)），待论证必要性和可行性后决定是否投入。
> 以下排期为假设通过后的预估。若否决，Q4 末方向保持不变。

| 迭代 | 交付物 | 预估 | 验收门禁 |
|------|--------|------|---------|
| **I9**（1 月） | Python API 增强：`run_task()` 返回 `TaskResult` + CLI `--json`（所有子命令） | ~3d | 外部 Python 脚本 `from agent_go import run_task; result = run_task(...)` 能拿到结构化结果 |
| **I10**（1 月） | 事件总线：`emit_event` / `subscribe_event` + `events.jsonl` + Webhook 生命周期事件 | ~2d | 全生命周期事件（plan.generated → subtask.started → subtask.completed → pipeline.completed）可订阅、可落盘 |
| **I10** | 状态查询 API：`query_task()` / `query_project_trend()` | ~1d | `query_task("task-xxx").status` 返回 "completed"或"failed" |
| **I11**（2 月） | 知识存储：`KnowledgeStore` 数据模型 + 文件读写 + `_extract_patterns` 增量更新 + Plan 注入 | ~3d | 连续跑 3 个同类 task，第 4 个的 Plan prompt 包含历史验证命令 |
| **I11** | `agent-go-action` GitHub Action（独立仓库） | ~2d | CI 中 `uses: agent-go/action@v1` 能跑通完整 pipeline |
| **I12**（3 月） | `pre-commit-agent-go` hook（独立仓库） | ~1d | `git commit` 前自动跑验证命令，失败阻止提交 |
| **I12** | `vscode-agent-go` extension（独立仓库，薄壳） | ~3d | 面板展示当前任务进度 + 历史列表 + 一键运行 |

**I9-I12 出关口径**：K9 集成接入数 ≥10（含 CI + IDE + Webhook 三类），知识注入采纳率 K10 ≥60%。

依赖关系：I9 是 I10 的前置（`TaskResult` 数据结构被后续所有模块依赖）；I10/I11 可并行；I12 依赖 I9（CLI `--json`）+ I10（事件进度）。

## 关键风险与对策

## 关键风险与对策

- **验证循环 token 爆炸**（PRD 已识别）：`max_retries` 硬上限 + 每迭代超时；S3 用计量日志盯 `cost_usd` 分布，超 P95 告警
- **模型路由拉低通过率**：Worker 走便宜模型必须配质量门 + 抽样回测；Planner 铁律不降级
- **范围蔓延**：Field Guide 跨任务记忆、验证规则生态均列入「验证需求后再投入」，本周期不做
- **Q3 串行风险**：若验证循环延期，优先保 S2（阻断下游）砍 S3 的 `/goal` 注入——阻断是 M6 的根，加速循环可后置

## 立即可做的三件事（本周）

1. ~~S1 计量日志开工~~ ✅ 已完成（2026-07-25）
2. ~~M2 失败摘要~~ ✅ 已完成（2026-07-25）
3. ~~刷新文档数据漂移~~ ✅ 已完成（README/architecture.md/spec.md 同步至 698 测试）
4. ~~测试加固 + $/pass 门禁~~ ✅ 已完成（2026-07-25，1130 测试 5 连绿，ISSUE-24~28 修复）
5. ~~模型分级 + 评估机制设计稿~~ ✅ 已完成（2026-07-25，[design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md)）
6. ~~S8 P0 模型评估机制落地~~ ✅ 已完成（2026-07-25，pricing.py + bench.py + eval_suite + cross_judge.py P1）
7. ~~核心解耦~~ ✅ 已完成（2026-07-25，evaluator/notify/goal/skills/agent_loop 全部动态 import + try/except）

**下一批**：对照 bench 真实执行（3 模型 × 8 任务 × 3 重复）→ `eval models` 决策矩阵 + `eval judge` 交叉评判，建立 K1/K8/K4 真实基线；KPI 数据采集验证（K1/K8 是否因 S2/S4 提升）。
