# 参考资料归档

本目录保存历史分析、外部调研和旧版基线。它们用于查阅背景、复盘决策和寻找设计依据，不直接定义当前产品行为、指标或路线。

## 使用规则

- 当前产品规则以 `docs/prd.md` 为准。
- 当前路线以 `docs/roadmap.md` 为准。
- 当前 M0 任务以 `docs/m0-task-list.md` 为准。
- 当前实现设计以 `docs/architecture.md`、`docs/design/` 和 `docs/design/adr/` 为准。
- 归档资料中的 KPI、阶段编号和方案不应直接用于当前验收。

## 目录内容

| 文档 | 用途 |
|---|---|
| `bench-analysis-2026-08-01.md` | 旧 Bench 模型、成本和难度分析 |
| `research-goal-loop-mechanism-2026-08-08.md` | Goal/Loop、Reflexion、重规划方向调研 |
| `research-sdd-landscape-2026-08-08.md` | 主流 Agent 和 SDD 能力对比调研 |
| `product-status-assessment-2026-08-08.md` | 产品成熟度评估（2026-08-08 快照，~60% 完成度） |
| `web-golden-path-acceptance-2026-08-13.md` | 纯本地 Golden Path 真实验收报告（Web 操作台 R1-R17 全链路） |
| `local-model-hard-tasks-prompts-20260812.md` | 本地模型困难任务 prompts 集（Goal A/B 实验数据源） |
| `case-study-skill-a-b.md` | Skill A/B 对照实验：22% 成本降低/44% token 降低/4→0 retries |

更早的历史设计继续保留在 `docs/archive/` 对应子目录。
