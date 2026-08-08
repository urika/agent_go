# agent_go 文档

> 一人项目，文档从简。6 个月后的自己能看懂当时的决策就够了。

## 核心文档（4 个，维护此即可）

| 文档 | 内容 | 何时更新 |
|------|------|----------|
| [CLAUDE.md](../CLAUDE.md) | AI 和人的共用入口：架构、命令、约定 | 每次改代码 |
| [architecture.md](architecture.md) | 核心架构、关键设计决策、数据流 | 架构变更时 |
| [prd.md](prd.md) | 产品定位、功能优先级、NFR KPI | 方向变更时 |
| [spec.md](spec.md) | 所有模块的接口速查（浓缩版） | 公共接口变更时 |

## 其他

| 文件 | 说明 |
|------|------|
| [ISSUES.md](ISSUES.md) | 已知 bug 和改进项清单 |
| [roadmap.md](roadmap.md) | 当前路线：M0 指标冻结 -> M1 交付闭环 -> M2 可靠性 -> M3 真实任务验证 |
| [m0-task-list.md](m0-task-list.md) | 当前阶段 M0 的执行任务清单和完成门禁 |
| [design/software-development-lifecycle.md](design/software-development-lifecycle.md) | 软件开发全流程：五阶段模型、agent_go 分工、上下游接口契约 |
| [design/](design/) | 设计文档：功能扩展和架构改进的设计方案 |
| [bench-analysis-2026-08-01.md](bench-analysis-2026-08-01.md) | 历史 Bench 分析：仅作 exploratory 数据，不作为当前 KPI 基线 |
| [archive/](archive/) | 历史文档：旧 PRD、旧 spec、设计审查，不再维护 |
