# agent_go 产品需求文档 (PRD)

> 版本: v2.0
> 日期: 2026-07-24
> 状态: P0+P1+P2 全部实现 (34 项功能)，P3 4 项远期
> 定位: 产品唯一真相源 (Single Source of Truth)，所有子 PRD 和设计文档的总索引

---

## 目录

1. [产品定位](#一产品定位)
2. [目标用户](#二目标用户)
3. [核心价值链路](#三核心价值链路)
4. [功能全景与优先级](#四功能全景与优先级)
5. [非功能需求与产品 KPI](#五非功能需求与产品-kpi)
6. [子系统 PRD 索引](#六子系统-prd-索引)
7. [产品路线图](#七产品路线图)
8. [附录: 需求追溯矩阵](#附录-需求追溯矩阵)

---

## 一、产品定位

### 一句话价值

> **agent_go 让 Claude Code 从「对话式结对编程」升级为「异步任务委派」——你说需求，它交 PR。**

### 一句话场景

> 你说一遍任务，关掉终端。回来时，拆解好的子任务在隔离环境里执行完毕，代码已提交，验证已通过，PR 已生成。

### 核心用户

**每周用 Claude Code 超过 20 次的工程师** — 他们信任 AI 写代码，但厌倦了手动拆分多步骤任务。

### 差异化定位

| | Claude Code (裸用) | agent_go |
|---|---|---|
| 多步骤任务 | 人工拆分、逐个执行、手动传递上下文 | 一次输入，自动 Plan → Execute → PR |
| 执行过程 | 盯着屏幕，交互确认每一步 | 无头模式全程自主 |
| 产物 | 文件被修改，commit message 自己写 | Conventional Commits + 验证报告 + PR 模板 |
| 安全 | 手动确认每个操作 | 验证命令白名单 + 沙箱 + 审计 |
| 规模 | 一个任务 = 一次对话 | 一个任务 = N 个子任务，可并发 |

### 非目标用户（有意识放弃）

- 不用 Claude Code 的开发者 — agent_go 是编排层，不是替代品
- 零技术背景用户 — 输出是 git diff，需要能 review
- 单步骤任务用户 — 裸 Claude Code 更快

> 详见 [PRODUCT_VISION.md](PRODUCT_VISION.md)

---

## 二、目标用户

| 画像 | 典型场景 | 使用频率 | 核心诉求 |
|------|----------|----------|----------|
| **独立开发者** | 个人项目、side project | 5-10 次/周 | 快、正确、便宜 |
| **团队 Tech Lead** | 团队 repo 批量任务 | 10-30 次/周 | 安全、可控、可审计 |
| **CI/CD Pipeline** | GitHub Actions 自动触发 | 50-200 次/周 | 稳定、幂等、零交互 |
| **平台工程师** | 评估引入、配置规范 | 评估期密集 | 可观测、可定制、成本透明 |

---

## 三、核心价值链路

```
① 用户说任务  →  ② Plan(拆解)  →  ③ Execute(执行)  →  ④ Verify(验证)  →  ⑤ Commit  →  ⑥ PR
   cmd_run          generate_plan      _run_pipeline       _verify_changes     _format_commit   cmd_pr
                    plan_to_subtasks   run_subtask         _is_safe_            git tag
                    decompose_         _run_headless       verification
                    fallback           _git_merge_         _command
                                       upstream            _build_sandbox_env
```

**产品及格线（一句话测试）**:

> 「你敢不敢周五下午 4 点输入 `agent_go run`，关电脑走人，周一早上信心满满地 merge PR？」

---

## 四、功能全景与优先级

### 优先级定义

| 等级 | 定义 | 数量 |
|------|------|------|
| **P0** 核心链路 | 没有它，核心承诺崩塌 | 18 项 |
| **P1** 信任增强 | 让用户「敢关终端」 | 17 项 |
| **P2** 体验完善 | 锦上添花 | 15 项 |
| **P3** 偏离核心 | 做对了，但不是现在 | 9 项 |

### P0 核心链路功能

| # | 功能 | 模块 | 对应步骤 |
|---|------|------|----------|
| 1 | `cmd_run` — 任务入口 | cli.py | ① |
| 2 | `generate_plan` — LLM 执行方案 | api.py | ② |
| 3 | `decompose_fallback` — 三层降级 | api.py | ② |
| 4 | `plan_to_subtasks` — Plan→子任务 | ui.py | ② |
| 5 | `_run_pipeline` — 拓扑调度引擎 | pipeline.py | ③ |
| 6 | `run_subtask` — 子任务端到端 | executor.py | ③④⑤ |
| 7 | `_run_headless` — Claude 无头调用 | subtask.py | ③ |
| 8 | `_git_merge_upstream` — 产物传递 | subtask.py | ③ |
| 9 | `_verify_changes` — 变更验证 | executor.py | ④ |
| 10 | `_run_verification_cmd` — 验证执行 | executor.py | ④ |
| 11 | `_format_commit` — Conventional Commits | utils.py | ⑤ |
| 12 | `cmd_pr` — PR 生成 | cli.py | ⑥ |
| 13 | `cmd_resume` — 中断恢复 | cli.py | ③ |
| 14 | `_build_sandbox_env` — 沙箱净化 | executor.py | ③④ |
| 15 | `_is_safe_verification_command` — 白名单 | utils.py | ④ |
| 16 | `_create_worktree` — 隔离环境 | executor.py | ③ |
| 17 | `load_config` + `get_api_key` — 配置鉴权 | config.py | ①② |
| 18 | `setup_logger` — 日志基础设施 | config.py | 全部 |

### P0 缺失功能（Q3 优先）

| # | 缺失 | 严重度 | 用户痛苦 |
|---|------|--------|----------|
| M1 | **任务完成通知** | 🔴 致命 | "关了终端怎么知道跑完了？" |
| M2 | **失败原因摘要** | 🔴 致命 | "status=failed 但不知道为什么" |
| M3 | **PR 质量仪表** | 🟡 重要 | "我该不该 merge？" |
| M4 | **时间预估** | 🟡 重要 | "能在我走之前跑完吗？" |

> 完整 P0-P3 分级及「需求 vs 实现」错位分析见 [PRODUCT_FEATURE_PRIORITY.md](PRODUCT_FEATURE_PRIORITY.md)

---

## 五、非功能需求与产品 KPI

### 7 个产品级 KPI

| # | KPI | 当前基线 | Q3 目标 | 年度目标 |
|---|-----|----------|---------|----------|
| K1 | **任务成功率** | ~85%（估） | ≥ 92% | ≥ 97% |
| K2 | **安全零事故** (S1/S2/S3) | ✅ 0 | 保持 0 | 保持 0 |
| K3 | **简单任务耗时** | ~3-5 min | ≤ 3 min | ≤ 1.5 min |
| K4 | **单任务成本** | ~$0.05-0.15 | ≤ $0.05 | ≤ $0.03 |
| K5 | **中断恢复成功率** | 未知 | ≥ 99% | ≥ 99.9% |
| K6 | **可观测性可回答率** | 7/9 (78%) | 8/9 (89%) | 9/9 (100%) |
| K7 | **首次上手时间** | ~10-20 min | ≤ 12 min | ≤ 8 min |

### NFR 架构

```
安全性 > 可靠性 > 可观测性 > 性能 > 可用性 > 可伸缩性 > 可移植性
```

### 安全防御体系（产品壁垒）

| 层级 | 机制 | 代码 |
|------|------|------|
| 验证命令白名单 | 4 阶段校验 (shlex → 注入扫描 → 命令查找 → token 正则)，覆盖 28 种工具，6 类注入防御 | `utils.py:145-257` |
| 沙箱环境净化 | 敏感变量剔除 + AGENT_GO_API_KEY 强制删除 + resource limit | `executor.py:64-74` |
| 审计不可抵赖 | `verification_audit.jsonl` 持久化每次拒绝 | `utils.py:260-289` |
| 路径穿越防御 | `startswith(repo.resolve())` 锚定 | `utils.py:12` |
| 配置文件权限 | `os.chmod(0o600)` | `config.py:93` |

> 详见 [PRD-非功能需求指标与目标值.md](PRD-非功能需求指标与目标值.md) 和 [../spec/SPEC-nfr.md](../spec/SPEC-nfr.md)

---

## 六、子系统 PRD 索引

| 子系统 | PRD | 状态 | 一句话 |
|--------|-----|------|--------|
| **Agent 角色与 Skill 分配** | [PRD-智能Agent角色与Skill分配.md](PRD-智能Agent角色与Skill分配.md) | ✅ 已实现 | LLM 知道有哪些 Skill 可用，按规则自动匹配角色 |
| **Plan 缓存** | [PRD-Plan缓存机制.md](PRD-Plan缓存机制.md) | ✅ 已实现 | SHA256 缓存键 + 24h TTL，相同任务不重复调 API |
| **项目评估体系** | [PRD-项目评估体系设计.md](PRD-项目评估体系设计.md) | ✅ 已实现 | 三层架构: 采集(metrics) → 存储(result/execution.log) → 分析(eval) |
| **Status TUI** | [PRD-Status-TUI可视化.md](PRD-Status-TUI可视化.md) | ✅ 已实现 | curses 多面板实时任务监控 |
| **Goal-Eval-Loop** | [PRD-Goal-Eval-Loop子任务目标评估循环机制.md](PRD-Goal-Eval-Loop子任务目标评估循环机制.md) | 📋 Draft | 从 fire-and-forget 到闭环评估，子任务执行质量自动迭代 |

### 子系统与核心价值链路的映射

```
① 用户说任务
    └── [Agent/Skill PRD] — LLM 知道可用的 Agent 和 Skill
    └── [Plan 缓存 PRD] — 相同任务不重复调 API

② Plan 拆解
    └── [Goal-Eval-Loop PRD] — 目标可量化、验证闭环

③ Execute 执行
    └── [Agent/Skill PRD] — 每步加载对应角色和知识
    └── [Status TUI PRD] — 实时观看执行进度
    └── [Goal-Eval-Loop PRD] — 失败自动诊断+修复

④ Verify 验证  →  ⑤ Commit  →  ⑥ PR
    └── [评估体系 PRD] — 采集 timing/change_stats/merge 结果
    └── [评估体系 PRD] — eval quality/cost/perf 报表
```

---

## 七、产品路线图

### 版本交付历程

| 版本 | 交付 |
|------|------|
| v0.1 | 单文件原型：Plan Mode → Claude Code 执行 |
| v0.2~v0.3 | 模块化拆分、Skill/Agent 系统、git worktree、并发执行、中断恢复 |
| v0.4 | 系统设计修复：变更检测、孤儿进程、路径重写、tag 清理、Agent Type 约束 |
| v0.5 | 数据采集层 (metrics.py) + eval quality/perf |
| v0.6 | Status TUI + Plan 缓存 |
| v0.7 | eval cost/reliability/ux/all + CI 生成 + review 命令 |
| v0.8 | 文档收尾 + NFR 规约 + 测试增强 (639 tests) |

### Q3 优先投入

| 优先级 | 事项 | 类型 |
|--------|------|------|
| P0 | **M1 任务完成通知** | 新功能 |
| P0 | **M2 失败原因摘要** | 新功能 |
| P0 | **Plan 缓存 fix** (cache key 去 commit hash) | Bug fix — 单行改动，成本减半 |
| P0 | **任务成功率提升至 92%** | 可靠性 — 修复 flaky 源 |
| P1 | M3 PR 质量仪表 + M4 时间预估 | 新功能 |
| P1 | 成本可见性改进 (预算告警) | 增强 |

> 详细路线图见 [../design/roadmap.md](../design/roadmap.md)

---

## 附录: 需求追溯矩阵

### 功能需求 → 实现 → 测试

| 需求 ID | 需求 | 模块 | 测试文件 |
|---------|------|------|----------|
| 1 | Plan Mode LLM 生成方案 | api.py | test_api.py |
| 2 | 多供应商 API | api.py | test_api.py |
| 3 | 子任务拆解 + 依赖图 | ui.py | test_plan_to_subtasks.py |
| 4 | Git worktree 隔离 | git_utils.py, executor.py | test_git_worktree_ops.py |
| 5 | 双格式审计日志 | config.py | test_config_helpers.py |
| 6 | --yes 无头模式 | cli.py, ui.py | test_cli.py, test_integration.py |
| 7 | 路径隔离 (TASK.md 重写) | executor.py | test_executor.py |
| 8 | 产物传递 (git merge) | subtask.py | test_subtask.py |
| 9 | Headless Claude 调用 | subtask.py | test_subtask.py |
| 10 | 验证执行 + 失败重试 | executor.py | test_executor.py |
| 11 | 共享上下文注入 | executor.py | test_executor.py |
| 12 | Git commit + tag 管理 | utils.py, executor.py | test_format_commit.py |
| 13 | --issue 参数 | cli.py | test_cli.py |
| 14-15 | Conventional Commits | utils.py | test_format_commit.py |
| 16-17 | PR 生成 + 模板 | cli.py | test_cli.py |
| 18 | 中断恢复 | pipeline.py | test_pipeline.py, test_nfr_reliability.py |
| 19 | 验证命令数组 | executor.py | test_executor.py |
| 20 | CI 工作流生成 | workflow_gen.py | test_workflow_gen.py |
| 22 | 并发执行 | pipeline.py | test_pipeline.py, test_integration.py |
| 23 | Plan 缓存 | api.py | test_api.py |
| 31-51 | 模块化/Skill/Agent/role_skill/TUI/eval | 多模块 | 各对应 test |

### NFR → 实现 → 指标

| NFR ID | 维度 | KPI |
|--------|------|-----|
| N1-N4 | 安全性 | K2 安全零事故 |
| N5-N9 | 可靠性 | K1 任务成功率 + K5 中断恢复 |
| N10-N12 | 可观测性 | K6 可回答率 |
| N13-N15 | 性能 | K3 任务耗时 + K4 单任务成本 |
| N16-N19 | 可用性 | K7 首次上手时间 |
| N20-N21 | 可伸缩性 | K1 (大规模场景) |
| N22-N24 | 可移植性 | — |

---

## 文档导航

```
docs/
├── prd/                              ← 产品文档 (本文档所在)
│   ├── PRD.md                        ← ★ 主 PRD (你正在读)
│   ├── PRODUCT_VISION.md             ← 核心定位
│   ├── PRODUCT_FEATURE_PRIORITY.md   ← 功能优先级分析
│   ├── PRD-非功能需求指标与目标值.md  ← NFR KPI
│   ├── PRD-智能Agent角色与Skill分配.md
│   ├── PRD-Plan缓存机制.md
│   ├── PRD-项目评估体系设计.md
│   ├── PRD-Status-TUI可视化.md
│   └── PRD-Goal-Eval-Loop子任务目标评估循环机制.md
├── spec/                             ← 技术规格 (每模块一份)
│   ├── README.md
│   ├── SPEC-nfr.md                   ← NFR 技术规格
│   └── SPEC-*.md                     ← 各模块规格
├── design/                           ← 设计文档
│   ├── README.md                     ← 设计文档索引
│   ├── architecture.md               ← 架构设计
│   ├── workflow.md                   ← 用户工作流
│   ├── data-architecture.md          ← 数据架构
│   ├── roadmap.md                    ← 路线图
│   ├── requirements.md               ← 需求清单
│   ├── review/                       ← 设计审查
│   │   ├── design-review.md
│   │   └── data-architecture-review.md
│   ├── quality/                      ← 质量与测试
│   │   └── nfr-testing-strategy.md
│   └── archive/                      ← 历史归档
│       └── github-workflow-alignment.md
└── archive/                          ← 历史归档
```

---

*PRD 版本 v2.0 — 2026-07-24 — 合并 6 份子 PRD + 产品定位 + 功能优先级 + NFR KPI*
