# agent_go 文档

> 一人项目，文档从简。6 个月后的自己能看懂当时的决策就够了。

## 核心文档（4 个，维护此即可）

| 文档 | 内容 | 何时更新 |
|------|------|----------|
| [CLAUDE.md](../CLAUDE.md) | AI 和人的共用入口：架构、命令、约定 | 每次改代码 |
| [architecture.md](architecture.md) | 核心架构、关键设计决策、数据流 | 架构变更时 |
| [prd.md](prd.md) | 产品定位、功能优先级、NFR KPI | 方向变更时 |
| [spec.md](spec.md) | 所有模块的接口速查（浓缩版） | 公共接口变更时 |
| [design/functional-architecture.md](design/functional-architecture.md) | 功能阶段、角色边界、状态和回退流程 | 流程变更时 |
| [design/module-catalog.md](design/module-catalog.md) | As-Built 模块职责与代码入口（37 模块完整目录） | 模块变更时 |
| [design/config-schema.md](design/config-schema.md) | 配置 Schema 参考文档（20 个配置块完整字段表） | 配置变更时 |
| [design/result-schema.md](design/result-schema.md) | result.json 完整字段定义和 Synthetic 变体 | result 结构变更时 |
| [design/runbook.md](design/runbook.md) | 运维与故障排查操作手册（恢复流程/日志/成本/worktree 清理） | 运维流程变更时 |
| [design/bench-convergence-plan.md](design/bench-convergence-plan.md) | Bench 收敛阶段计划和正式 baseline 门禁 | Bench 策略变更时 |
| [design/agent-go-execution-capability-assessment-2026-08-09.md](design/agent-go-execution-capability-assessment-2026-08-09.md) | agent_go 执行 Stage B 的能力边界和失败复盘 | 执行编排能力变化时 |
| [design/adr/](design/adr/) | 系统级技术决策记录 | 关键设计决策时 |

## 其他

| 文件 | 说明 |
|------|------|
| [ISSUES.md](ISSUES.md) | 已知 bug 和改进项清单 |
| [roadmap.md](roadmap.md) | 当前路线：M0 指标冻结 -> M1 交付闭环 -> M2 可靠性 -> M3 真实任务验证 |
| [m0-task-list.md](m0-task-list.md) | 当前阶段 M0 的执行任务清单和完成门禁 |
| [m1-task-list.md](m1-task-list.md) | M1 交付闭环任务清单（M1.1-M1.4） |
| [design/software-development-lifecycle.md](design/software-development-lifecycle.md) | 软件开发全流程：五阶段模型、agent_go 分工、上下游接口契约 |
| [design/](design/) | 设计文档：功能扩展和架构改进的设计方案 |
| [case-study-skill-a-b.md](case-study-skill-a-b.md) | Skill A/B 对照实验：22% 成本降低/44% token 降低/4→0 retries |
| [product-status-assessment-2026-08-08.md](product-status-assessment-2026-08-08.md) | 产品成熟度评估（2026-08-08 快照，~60% 完成度） |
| [web-golden-path-acceptance-2026-08-13.md](web-golden-path-acceptance-2026-08-13.md) | 纯本地 Golden Path 真实验收报告（Web 操作台 R1-R17 全链路） |
| [design/production-model-config.md](design/production-model-config.md) | 生产模型配置指南（方案 B：planner=K3 + evaluator=GLM 混合，6/6 hard 100%） |
| [archive/reference/bench-analysis-2026-08-01.md](archive/reference/bench-analysis-2026-08-01.md) | 历史 Bench 分析：仅作 exploratory 数据，不作为当前 KPI 基线 |
| [archive/](archive/) | 历史文档：旧 PRD、旧 spec、设计审查，不再维护 |
