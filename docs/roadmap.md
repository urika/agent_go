# agent_go Roadmap：从现状到「周五派发、周一 merge」

> 基线：2026-07-24，v2.0.0，684 测试全绿，14 项已知缺陷清零。
> 目标对齐 [prd.md](prd.md) 的 Q3 / 年度 KPI；差距分析依据见 prd.md「P0 缺失功能」「P1 重点」章节。

## 进度快照（2026-07-25 更新，751 测试全绿）

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

**Q3 出关口径**：K1 ≥92%、K8 ≥80%、K4 ≤$0.05、$/pass ≤$0.05、K6 8/9。达不到则 Q4 不扩新功能，回头补质量。

依赖关系：S1 必须最先（它是 S4 门禁和北极星指标的数据源）；S2/S3 与 S4 可并行，但 S4 的 Reviewer 通道建议等验证循环稳定后再开，控制变量。

## Q4 2026（10–12 月）：兑现及格线，再扩规模

| 迭代 | 交付物 | 对应缺口 |
|------|--------|---------|
| **S5**（10 月） | **M7 结果审查阶段**：`agent_go review <task-id>` 聚合所有子任务 diff，按文件分组展示变更摘要，支持 approve/reject | M7 |
| **S5** | M3 PR 质量仪表（diff 统计 + 验证履历 + 审查结论汇入 `cmd_pr`）；M4 时间预估（基于 S1 计量日志的历史分位数） | M3/M4，K6 9/9 |
| **S5** | Plan 版本管理：每次 regenerate 保存旧 Plan 快照，`agent_go plan-history <task-id>` 可回溯，`agent_go plan-diff <task-id>` 对比版本差异 | 借鉴 OpenChamber 分支式时间线 |
| **S6**（11 月） | 复杂度双通道：Planner 打 `difficulty` 标签，hard 自动走强模型；Reviewer 角色灰度（仅高风险子任务，审查预算 ≤20%） | K4 → ≤$0.03 |
| **S6** | **失败通知增强**：子任务失败时主动推送通知（osascript 桌面通知 / webhook），即使整体任务未完成 | M1 补完 |
| **S7**（12 月） | P2 精选：叠加式审查流水线（diff 视角 + 独立模型评审）、全局决策日志治「脑裂」 | 规模化质量 |
| **S7** | **PR 自动推送到 GitHub**：`agent_go pr <task-id> --push` 使用 GitHub CLI 自动创建 PR | P2 体验 |

**年度出关**：K1 ≥97%、K3 ≤1.5min、K8 ≥90%、K5 ≥99.9%（S1 起恢复成功率埋点已积累一个季度数据）。

## 关键风险与对策

- **验证循环 token 爆炸**（PRD 已识别）：`max_retries` 硬上限 + 每迭代超时；S3 用计量日志盯 `cost_usd` 分布，超 P95 告警
- **模型路由拉低通过率**：Worker 走便宜模型必须配质量门 + 抽样回测；Planner 铁律不降级
- **范围蔓延**：Field Guide 跨任务记忆、验证规则生态均列入「验证需求后再投入」，本周期不做
- **Q3 串行风险**：若验证循环延期，优先保 S2（阻断下游）砍 S3 的 `/goal` 注入——阻断是 M6 的根，加速循环可后置

## 立即可做的三件事（本周）

1. ~~S1 计量日志开工~~ ✅ 已完成（2026-07-25）
2. ~~M2 失败摘要~~ ✅ 已完成（2026-07-25）
3. ~~刷新文档数据漂移~~ ✅ 已完成（README/architecture.md/spec.md 同步至 698 测试）

下一批：**M7 审查阶段设计稿**、Router 多 Provider 全链路扩展（设计稿评审后实施）、KPI 数据采集验证（K1/K8 是否因 S2/S4 提升）。
