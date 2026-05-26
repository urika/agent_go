# agent_go 产品路线图

> 版本: v0.8
> 日期: 2026-05-27
> 视角: 产品经理回顾与后续规划

---

## 一、当前进度 vs 原需求

| 优先级 | 总数 | 已完成 | 待开始 |
|--------|------|--------|--------|
| P0 | 12 | 12 | 0 |
| P1 | 7 | 7 | 0 |
| P2 | 7 | 7 | 0 |
| P3 | 4 | 0 | 4 |

**P0+P1+P2 全部完成** (34 项，含 8 项增量)。P3 4 项远期。

### P0 — 已全部实现

| # | 需求 | 状态 |
|---|------|------|
| 1-12 | Plan Mode / 多供应商 / 子任务拆解 / worktree / 日志 / --yes / 路径隔离 / 产物传递 / 无头模式 / 验证 / 共享上下文 / Git 管理 | ✅ |

### P1 — 已全部实现

| # | 需求 | 状态 |
|---|------|------|
| 13-19 | --issue / 分支命名 / Conventional Commits / pr 命令 / PR 模板 / 中断恢复 / 验证数组 | ✅ |

### P2 — 已全部实现

| # | 需求 | 状态 |
|---|------|------|
| 20 | CI 工作流生成 | ✅ v0.7 |
| 21 | Projects 看板联动 | 📋 |
| 22 | 多任务并行执行 | ✅ |
| 23 | Plan 缓存 | ✅ v0.6 |
| 24 | agent_go review | ✅ v0.7 |
| 25 | TASK.md 文件覆盖检查 | 📋 |
| 26 | Sandbox Greywall | ✅ |

### P3 — 远期

| # | 需求 | 状态 |
|---|------|------|
| 27 | FastAPI + SQLite 编排层 | 📋 |
| 28 | Web 仪表盘 + TUI 观测终端 | 📋 |
| 29 | 多 Agent 并发协作 | 📋 |
| 30 | IDE 插件 (VS Code / JetBrains) | 📋 |

---

## 二、版本交付历程

| 版本 | 增量交付 |
|------|---------|
| v0.1 | 单文件原型：Plan Mode → 子任务 → Claude Code 执行 |
| v0.2~v0.3 | 模块化拆分、Skill/Agent 类型系统、git worktree、并发执行、中断恢复、tag 命名空间、headless 冲突 |
| v0.4 | 系统设计修复 P0-P3：变更检测、孤儿进程、路径重写、tag 清理、result 独立写入、Agent Type 约束、多级验证、上下文过滤 |
| v0.5 | Phase 1 数据采集层 (metrics.py/timing/change_stats/merge/verify/token) + Phase 2 eval quality/perf |
| v0.6 | S1 Status TUI (curses) + M2 Plan 缓存 (SHA256+24h TTL) |
| v0.7 | Phase 3 eval cost/reliability/ux/all + P2-20 CI 生成 + P2-24 review 命令 |
| v0.8 | 文档收尾 + P0+P1+P2 全部完成 |

**项目规模**: 17 modules / ~4200 lines / 163 tests / 13 test files / 45 commits

---

## 三、当前完成度评估

| 维度 | 完成 | 说明 |
|------|------|------|
| 核心管线 (P0+P1) | ✅ 100% | Plan → Execute → Verify 全链路 |
| 角色/Skill 路由 | ✅ 100% | role_skill_map 配置驱动 |
| 可观测性 | ✅ 100% | eval 全维度 + TUI + 日志事件体系 |
| 成本优化 | ✅ 100% | Plan 缓存 + token 追踪 + 费用估算 |
| 开发体验 | ✅ 100% | CI 生成 + review + PR 模板 |
| 远期规划 (P3) | 📋 0% | 多用户/Web/IDE |

---

## 四、后续方向

### 近期可做

| # | 方向 | 理由 |
|---|------|------|
| R1 | TASK.md 文件覆盖检查 (#25) | 验证 Plan 预期 vs 实际产出 |
| R2 | Goal-Eval-Loop 机制 | PRD 已写，评估闭环 |
| R3 | 测试覆盖率提升 | 163→200+, 集成级别测试 |

### 远期 (P3)

| # | 需求 | 触发条件 |
|---|------|---------|
| P3-27 | FastAPI + SQLite 编排层 | 多用户场景 |
| P3-28 | Web 仪表盘 | 团队使用 |
| P3-29 | 多 Agent 并发协作 | Agent 类型系统成熟 |
| P3-30 | IDE 插件 | 外部用户需求 |

---

*文档结束*
