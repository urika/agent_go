# agent_go 产品文档

## 定位

> **agent_go 让 Claude Code 从「对话式结对编程」升级为「异步任务委派」——你说需求，它交 PR。**
>
> 基础设施化方向评估见 [design/infrastructure-api-design.md](design/infrastructure-api-design.md)

核心用户：每周用 Claude Code 超过 20 次的工程师。他们信任 AI 写代码，但厌倦了手动拆分多步骤任务。

## 设计原则

借鉴多智能体编排领域的最佳实践，agent_go 遵循以下设计原则：

1. **编排比模型更重要** — 差异化押注编排层。模型会换代，编排能力可跨模型复用，构成长期护城河。
2. **上下文隔离 > 并行性** — 规划者不实现，执行者不规划。每个子任务只接收窄化后的上下文，避免全局历史污染。
3. **协调机制做进系统层** — 用 git worktree 隔离、tag 命名空间、分支合并等系统机制代替 Agent 自觉，不写进提示词。
4. **人的注意力是最稀缺资源** — 默认聚合、异常下钻。只把需要人决策的事推到人面前，且人做选择题而非问答题。
5. **复杂度判断在规划阶段收敛** — 规划质量是总成本的前置变量。Plan 阶段把不确定性转为明确指令，是成本节省的根本来源。

## 目标场景

你说一遍任务，关掉终端。回来时，代码已提交，验证已通过，PR 已生成。

## 差异化

### vs Claude Code 裸用

| | Claude Code 裸用 | agent_go |
|---|---|---|
| 多步骤任务 | 人工拆分，逐个执行 | 一次输入，自动 Plan → Execute → PR |
| 执行过程 | 盯着屏幕确认每一步 | 无头模式全程自主 |
| 产物 | commit message 自己写 | Conventional Commits + 验证报告 + PR |
| 安全 | 手动确认每个操作 | 命令白名单 + 沙箱 + 审计日志 |
| 规模 | 一次对话 = 一个任务 | 一次对话 = N 个子任务，可并发 |

### vs OpenChamber（Agentic Development Environment）

OpenChamber（https://openchamber.dev）是 OpenCode 的开源可视化操作界面，提供 Desktop/Web/Mobile/VS Code 四端。两者定位互补而非竞争。

| 维度 | OpenChamber | agent_go |
|------|-------------|----------|
| **定位** | OpenCode 的 GUI 界面 — "看着 Agent 干活" | 工作流编排引擎 — "用 Agent 干活" |
| **用户介入** | 全程可视化审查（diff、成本、进度） | 战略决策点介入（Plan 确认、结果审查、PR merge） |
| **执行方式** | 可视化多 Agent 并行 | 结构化 Plan → Decompose → Execute 流水线 |
| **远程访问** | Cloudflare tunnel + QR 扫码配对（一等公民） | 本地 CLI，无远程（低优先级） |
| **多表面** | Desktop / Web / Mobile / VS Code | CLI（curses TUI + 文本模式） |
| **技术栈** | TypeScript + React + Electron + Bun | Python stdlib（无外部依赖） |
| **Agent 引擎** | 基于 `@opencode-ai/sdk` | 直接 `subprocess.run(["claude", ...])` |
| **收费** | 免费开源（MIT） | 免费开源（MIT） |
| **社区** | 20k+ GitHub Stars, v1.16.3, Homebrew 上架 | 个人项目 |

### 可借鉴的功能

| OpenChamber 能力 | agent_go 对应 | 借鉴建议 |
|---|---|---|
| 分支式对话时间线（/undo/redo/fork） | 无，regenerate 覆盖旧 Plan | Plan 版本管理（P1） |
| 多 Agent 并行（同一 prompt 多方案） | `--parallel N` 子任务并发 | 方案探索模式（P2） |
| 可视化 Diff 审查 | `review --task` 聚合 diff 审查 | ✅ 已实现 |
| GitHub 原生集成（从 Issue/PR 启动） | `--issue` 参数 + `cmd_pr --push` | ✅ 已实现（gh CLI） |
| 成本/Token 可视化 | metering.jsonl + status 命令 | 已够用 |
| Skills 目录 | 已有 Skills 系统 | 已做得更深（role-skill 规则引擎） |
| 远程访问 / Tunnel | 无 | 低优先级（定位不匹配） |

### 核心差异：用户介入点的设计哲学

OpenChamber 是"看着干"——用户全程在 GUI 中审查每个操作。agent_go 是"派活后检查结果"——用户只在**战略决策点**介入：

| 介入点 | OpenChamber | agent_go（当前） | agent_go（理想） |
|--------|-------------|------------------|-----------------|
| Plan 确认 | 实时修改 Plan | ✅ Y/S/D/E/R/N | 保留 |
| 执行过程 | 实时流式查看 diff | ⚠️ status --watch | 加强失败通知 |
| 结果审查 | 可视化 diff 面板 | ✅ review --task（文本模式） | 已是当前形态 |
| PR 创建 | 内置 GitHub 操作 | ✅ cmd_pr --push（gh CLI） | 已实现 |

## 功能优先级

| 等级 | 定义 | 数量 | 示例 |
|------|------|------|------|
| **P0** | 核心链路，缺了承诺崩塌 | 18 项 | cmd_run, generate_plan, _run_headless, cmd_resume, cmd_pr |
| **P1** | 信任增强 + 成本控制，让用户敢用且用得起 | 19 项 | Plan 缓存, --yes 无头, 并发, 远程推送, 角色感知模型路由, 结构化计量日志 |
| **P2** | 体验完善，锦上添花 | 15 项 | cmd_list/show, eval 分析, TUI 面板 |
| **P3** | 偏离核心，做对了但不急 | 7 项 | CI 生成, cmd_review, agent/skill 浏览 |

## P0 缺失功能

> **2026-07-25 更新：M1-M7 已全部落地**（多通道通知、失败摘要、PR 质量仪表、时间预估、验证循环防假阳性、级联阻断、聚合结果审查），详见 [roadmap.md](roadmap.md) 进度快照。下表保留为历史记录。

| # | 缺失 | 严重度 | 状态 |
|---|------|--------|------|
| M1 | 任务完成通知 — "关了终端怎么知道跑完了？" | 🔴 | ✅ notify.py 三通道 |
| M2 | 失败原因摘要 — "status=failed 但不知道为什么" | 🔴 | ✅ failure_reason + show |
| M3 | PR 质量仪表 — "我该不该 merge？" | 🟡 | ✅ _build_quality_dashboard |
| M4 | 时间预估 — "能在我走之前跑完吗？" | 🟡 | ✅ estimate_task_duration |
| M5 | 验证假阳性 — "验证通过了但功能不对" | 🔴 | ✅ 验证循环 + 置信度评估 |
| M6 | 级联失败 — "一个子任务失败拖垮全部" | 🔴 | ✅ blocked 阻断 + wave 排除 |
| M7 | **结果审查阶段缺失** — "子任务都完成了，但变更汇总在哪里？" | 🔴 | ✅ `review --task` 聚合 diff + 审批 |

## 整体开发流程：四阶段模型

agent_go 的工作流分为四个阶段，用户介入点集中在**战略决策点**：

```
Phase 1: 规划阶段 (Planning)       Phase 2: 执行阶段 (Execution)
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ [Plan 生成] → [Plan 确认]    │  │ [子任务 1] → [验证] → [子任务 2] → [验证]  │
│   ↑ LLM API    ↑ 人做选择题   │  │   ↑ Claude   ↑ auto     ↑ Claude   ↑ auto  │
│                S=跳过         │  │                              │
│                D=手动编辑     │  │  异常时: 自动修复 → 重试 → 阻断+通知用户   │
│                E=重新生成     │  │                              │
└──────────────────────────────┘  └──────────────────────────────┘
                                          ↓ 全部完成
Phase 3: 审查阶段 (Review)       Phase 4: 交付阶段 (Delivery)
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ [聚合 Diff 审查] → [人工审批] │  │ [PR 生成] → [PR 创建] → [Merge]  │
│   ↑ 自动汇总变更   ↑ approve  │  │   ↑ 已有      ↑ 可推送到     ↑ 人做决定   │
│   ↑ 按文件分组     /reject   │  │   cmd_pr     GitHub        │
│   ↑ 行内评论      /changes   │  │                              │
└──────────────────────────────┘  └──────────────────────────────┘
```

### Phase 3：审查阶段 — 已落地（2026-07-25）

**当前状态：** `agent_go review --task <task-id>` 已实现聚合结果审查——按文件分组展示各子任务变更摘要，支持 `--approve` / `--reject` / `--changes-requested` 人工审批；`--deep` 用独立模型逐个子任务分析 diff。

**能力清单：**

| 能力 | 描述 | 状态 |
|------|------|------|
| 聚合 Diff 展示 | 将所有子任务的变更汇总，展示完整 diff | ✅ 已实现（`review --task`） |
| Diff 按文件分组 | 同一文件被多个子任务修改时，展示最终结果 | ✅ 已实现 |
| 审查命令 | `agent_go review --task <task-id>` 进入审查 | ✅ 已实现 |
| 审批状态 | approved / changes-requested / rejected | ✅ 已实现 |
| 深度分析 | 独立模型逐子任务分析 diff（`--deep`） | ✅ 已实现 |
| 行内评论 | 在 diff 上添加评论，可反馈给 AI | P3 未实现 |

### 流程状态机

```
DRAFT → PLANNING → PLAN_REVIEWED → DECOMPOSED → READY → EXECUTING → REVIEW → DELIVERY → COMPLETED
  ↑         ↑           ↑               ↑           ↑        ↑          ↑         ↑
 用户输入   LLM 生成   用户确认        LLM 分解    用户确认  子任务运行  用户审查   PR 推送/merge
```

### 关键设计原则

1. **人的注意力是最稀缺资源** — 默认聚合、异常下钻。只把需要人决策的事推到人面前，且人做选择题而非问答题。
2. **失败后阻断+通知** — 子任务验证失败达到最大重试次数后，阻断下游并通知用户。保留 worktree 供 `agent_go inspect` 审查。
3. **审查是必选环节** — 即使 `--yes` 全自动模式，也生成审查摘要供用户事后查看。`--headless` 模式下审查自动通过。

## P1 重点：角色感知模型路由

> **2026-07-25 进展**：`router.py`（角色路由 + 熔断 + 降级留痕）与**复杂度双通道**（Planner 打 difficulty 标签 → `worker_models` 映射 → claude `--model`）已落地；Router 多 Provider 全链路扩展见 [design/router-multi-provider-extension.md](design/router-multi-provider-extension.md)。

**一句话：Plan 走前沿模型，Execute 走便宜模型，成本降 5 倍，质量不降。**

当前所有子任务用同一模型（`plan_api` 配置），简单任务和复杂任务花一样的钱。角色感知路由让不同角色走不同成本和能力的模型：

| 角色 | 模型策略 | 理由 |
|------|---------|------|
| **Planner**（规划） | 前沿模型，不做降级 | 规划质量是总成本的前置变量，规划 token 省钱 → Worker token 数倍膨胀 |
| **Worker**（执行） | 快速/本地模型优先，失败降级 API | 执行被窄化的具体子任务，不需要全局推理能力 |
| **Reviewer**（审查） | 与 Worker 不同源的模型 | 保证视角低相关，防止同一模型既写又审 |

**落地路径（利用现有基础设施）：**

```
现有 call_api() 入口
  → 扩展为 router 配置：planner / worker / reviewer 分别配置 provider
  → 执行阶段按 agent_type 路由到不同模型
  → 计量日志记录 role, actual_provider, cost_usd, fallback_reason
```

**关键约束（铁律）：**
- Planner 不给配降级到弱模型 — 规划 token 省小钱，Worker token 数倍膨胀
- 每次降级必须留痕 — `fallback_reason` 必填，否则 $/pass rate 指标失真
- 本地模型并发上限显式设置 — MacBook 本地模型吞吐有限，`--parallel` 增大不会自动提升本地并发

**P1 配套：结构化计量日志** — 每 API 请求落一条：

```
ts, task_id, role, virtual_model, actual_provider, difficulty,
prompt_tokens, completion_tokens, cost_usd, latency_ms,
result(success|fallback|quality_fail), fallback_reason
```

这是成本看板的数据源，也是发布门禁「$/pass rate 不劣化」的判定依据。

### 模型分级策略（2026-08-01 更新：Bench v1 数据校准）

> 完整设计见 [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md)。本节以 bench 实测数据校准分级矩阵。

**一句话：角色决定档位，difficulty 决定升降，bench 实测数据校准选型。**

**Bench v1 关键发现（影响分级决策）：**

| 发现 | 影响 |
|------|------|
| Haiku 4.5 pass_rate（85.5%）> Sonnet 4.6（82.4%），成本仅 1/2.6 | Haiku 从「轻量档」升级为「性价比主力」候选 |
| DeepSeek 整体 pass_rate 24-27%，medium 近零 | DeepSeek 不可做 Worker，仅可做 Planner (plan 生成) |
| hard 任务贡献 73% 成本但仅 43% 记录数 | difficulty 路由的降本杠杆最大 |

**三角色 × 三档位分级矩阵（Bench v1 校准版）：**

| 角色 / 档位 | 主力旗舰 (Frontier) | 性价比 (Value) | 轻量 (Lite) |
|-------------|---------------------|---------------|-------------|
| Planner（不降级） | Sonnet 4 / Opus 4.7 / GPT-4.1 | ✗ 铁律禁降级 | ✗ |
| Worker-hard | Opus 4.7 / GPT-5.6 Terra | Sonnet 4.6 / DeepSeek V3.2（需配硬质量门） | — |
| Worker-medium | Sonnet 4.6 / Haiku 4.5 | DeepSeek V3.2（需配硬质量门） | — |
| Worker-easy | Haiku 4.5 / DeepSeek V3.2 | — | GPT-4.1 Nano / Doubao-lite |
| Reviewer（不同源） | Gemini 2.5 Pro / Qwen-Max | Kimi K2（待补齐 hard 任务数据） | — |

**v1.1 关键调整（vs v1.0 设计稿）：**
- **Haiku 4.5 升至 Worker-medium 主力旗舰候选**（bench 实测 pass_rate 85.5%，优于 Sonnet 82.4%）
- **DeepSeek 所有角色加「需配硬质量门」约束**（bench 实测 pass_rate 24-27%，不可在无门禁下自动路由）
- **Kimi 保留在性价比档但标注「待补齐 hard 数据」**（当前仅覆盖 easy+medium，12/22 任务，缺失所有 hard 任务）

**关键约束（补充铁律）：**
- **分级矩阵以 bench 实测数据为唯一更新依据** — 厂商 benchmark 不作为选型参考（bench v1 已验证 Haiku vs Sonnet 的排序与直觉相反）
- **所有「性价比档」模型必须配质量门** — bench 数据更新后自动检查 $/pass 是否劣化
- **$/pass 计价必须优先用真实 `cost_usd`** — 厂商标称价表无法覆盖所有模型（`claude-code-executor`、本地模型、国产新旗舰），兜底为最便宜模型会让 [$/pass rate 被低估 11-22 倍](ISSUES.md#issue-26)
- **Reviewer 与 Worker 不同源** — 编排器强制校验（`judge != candidate`），保证视角低相关
- **样本 <5 不决策** — 标记 low_confidence，不参与自动路由

**成本估算结论（Bench v1 实测数据）：**
- 纯 Haiku easy-only：$/pass ≈ $0.09（距离 KPI $0.05 差 $0.04）
- Claude 三模型全量：$/pass ≈ $0.39（field avg）/ $0.83（加权）
- hard 任务是主要成本驱动（73% 成本），difficulty 路由的降本杠杆最大
- 混合策略（Sonnet/Opus 规划 + Haiku 执行 easy/medium + Opus 执行 hard）：预计 $/pass ≈ $0.15-0.20，仍高于 $0.05 目标。需 KnowledgeStore + Plan 优化进一步压缩

### 成本控制策略：冷启动 + 分层启用（2026-08-07 新增）

> 关联：[S12 度量修复 + 成本控制启用](roadmap.md#s12)，[timeout-kill-strategy §表面C](design/timeout-kill-strategy-2026-08-06.md)

**核心矛盾**：成本控制是"判死"机制（超预算即熔断/降级）。用拍脑袋的默认值开启，会**误杀本可成功的任务**——这比"没保护"更糟（让人误以为有护栏）。因此成本控制必须**分层启用**：误杀风险低的层冷启动即开，风险高的层须基线校准后才开。

**三层架构与启用策略：**

| 层 | 机制 | 冷启动默认 | 误杀风险 | 启用条件 |
|----|------|-----------|---------|---------|
| **L1** | 单次调用 `claude --max-budget-usd`（按 difficulty） | **✅ 开**（`l1_enabled=True`） | 最低（只杀单次异常调用） | 冷启动即开，防"一次失控烧钱" |
| **L2** | 子任务累计（跨重试，`per_subtask × multiplier`） | ❌ 关（`enabled=False`） | 中（封顶重试循环） | 须 `eval cost-baseline` 校准 |
| **L3** | 任务级熔断（跨子任务，`--budget`/`--max-cost`） | ❌ 关（`enabled=False`） | 最高（整个任务 block/degrade） | 须冻结基线 |

**关键设计：L1 与 L2/L3 解耦**
- `l1_enabled`（默认 True）独立控制 L1——冷启动安全的最低限度防护
- `enabled`（默认 False）控制 L2/L3——判死机制，基线不可信时误杀率高
- 用户三态：冷启动（默认配置即可）/ 有基线（`enabled=True` + 校准预算）/ 完全关（`l1_enabled=False`）

**L1 冷启动宽松预算（宁可多花不多杀）：**

| 难度 | 冷启动默认 | 基线 P90×1.5（参考） | 设计倍数 |
|------|-----------|---------------------|---------|
| easy | $0.20 | ~$0.10 | 2× 留余量 |
| medium | $0.40 | ~$0.10-0.17 | 2-4× |
| hard | $1.00 | ~$0.19-0.36 | 3-5× |

给未知任务充足空间。基线建立后，用 `eval cost-baseline` 的 P90×tolerance 替换为精准值。

**启用路径（chicken-egg 破局）：**
1. 冷启动：默认配置 → L1 防失控，L2/L3 不误杀（**当前状态**）
2. 立基线：`eval bench` 全因子 + `eval cost-baseline`（排除 timed_out 右删失，P90×1.5）
3. 开 L2/L3：`enabled=True` + 校准预算，`over_budget` 单列不污染能力失败分母

**与测量解耦**：预算熔断写 `cost_censored` 事件到 metering.jsonl 并继续累计（控制不中断测量）。`eval cost-baseline` 仅用自然成本记录（timed_out 排除），避免右删失污染。

### Bench v2 计划（2026-08-01 新增）

> **一句话：Bench v1 暴露了数据采集、实验设计和统计方法的系统性缺口。Bench v2 在 v1 基础上补齐这些缺口，建立可信的 KPI 基线和模型选型决策依据。**

**背景**：v1 分析（[bench-analysis-2026-08-01.md](bench-analysis-2026-08-01.md)）发现 4 处数值偏差、多个指标定义歧义、任务-模型覆盖矩阵不完整、无统计检验。下一轮 bench 必须修复这些方法论缺陷才能支撑 KPI 度量和模型路由决策。

**数据需求规格**：[design/bench-v2-data-requirements.md](design/bench-v2-data-requirements.md) 定义了完整的数据采集标准、实验设计规范和指标体系。

**核心改进（vs v1）：**

| 维度 | v1 问题 | v2 改进 |
|------|---------|---------|
| Schema | 缺 `timed_out`/`judge_model`/`planner_model`，无法区分超时失败、无法量化自评偏差 | 新增 6 个 P0 + 5 个 P1 字段 |
| 实验设计 | 任务-模型覆盖不完整（Kimi 缺 hard，Opus 跨 4 批次），重复次数不足 | 全因子设计：7 模型 × 22 任务 × ≥3 重复 |
| 指标定义 | $/pass 3 种算法、K8 定义含糊 | 统一定义 + 新增 5 个指标（代码回归率、Plan 效率等） |
| 对照基线 | 无 | `claude -p` 裸跑 5-6 任务作为对照组 |
| 统计方法 | 点估计无 CI、无显著性检验 | Wilson CI + 配对检验 + 效应量 + 多比较校正 |
| 评估维度 | 仅 pass/cost/time | 新增代码质量、Plan 质量、级联效应、稳定性 |

**分阶段实施：**

```
Phase 1（P0，2 周）：Schema 扩展 + cross_judge 交叉评判
  → 新增 timed_out/judge_model/planner_model/source_batch 字段
  → 统一 $/pass 计算口径 + K8 定义修订
  → 对 v1 数据运行 cross_judge（不同 judge 模型重判同批输出）

Phase 2（P1，2 周）：全因子 Bench 执行
  → Tier 1（Claude 三模型）× 22 任务 × 3 重复 = 198 次运行
  → 新增 per_subtask/binary_pass/semantic_pass/plan_step_count 字段
  → 代码质量维度（lint + test regression 检测）
  → 对照基线：claude -p 裸跑 5-6 代表性任务

Phase 3（P1，1 周）：分析与决策更新
  → 全量指标计算（含 CI + 效应量）
  → 模型分级矩阵校准
  → Per-task 难度系数发布
  → router recommend 自动生成路由配置

Phase 4（P2，2 周，可选）：扩展与稳定性
  → Tier 2+3 模型扩展（DeepSeek / Kimi 补齐全部 22 任务）
  → 高方差任务 5-10 次重复
  → Plan 质量维度 + 级联效应专项测试
```

**验收门禁：**
- cross_judge 自评偏差量化完成（偏差幅度 + 受影响的 task 列表）
- K1/K4/K8/$/pass 四个指标汇报 95% CI
- 模型分级矩阵标注「bench 实测」vs「厂商声称」数据来源
- $/pass 不因 schema 扩展而计算口径倒退

### 结构化输入与准入审查（2026-08-01 新增）

> **一句话：Spec 是 agent_go 和上游流程的接口契约。拿到 Spec 后先审查再执行——L1 硬门禁阻断不合规的，L2 软警告提示风险的。**

**背景**：Bench v1 分析表明，输入质量是 Plan 质量和执行成本的最前端杠杆。一句话 prompt 让 Planner 在大量不确定性中猜测，导致分解偏差、重试增加、成本膨胀。将「需求→代码」链路中「理解-探查-决策」过程的结构化产物（Task Spec）作为 agent_go 的标准输入，并在 Plan 前做准入审查。

**设计文档**：[design/agent-go-input-spec.md](design/agent-go-input-spec.md) — 完整输入准则、Task Spec 7 章节规范、前序流程工作目标。

**Task Spec 的 7 个章节：**

```markdown
# Task Spec: <名称>
## 1. 目标（做什么）          * 必填
## 2. 动机（为什么）          * 必填
## 3. 范围（动哪里/不动哪里）  * 必填
## 4. 约束                    建议
## 5. 验收标准                 * 必填
## 6. 参考资料                可选
## 7. 已知风险                可选
```

各章节映射到 Plan prompt 的特定注入位置——目标替代自由文本 task、范围和约束成为 Planner 的硬约束、验收标准派生成验证命令。

**使用方式：**

```bash
# 生成 Task Spec 模板（预填 repo 结构）
agent_go spec template --repo ./my-repo --output docs/tasks/task-xxx.md

# 基于 Spec 执行
agent_go run ./my-repo --spec docs/tasks/task-xxx.md

# 轻量 Scoping（交互式生成 Spec 草稿）
agent_go scope ./my-repo "加 email 验证" --output docs/tasks/task-email-verify.md
```

**准入审查（Spec Gate）：两级审查，不做自动修复。**

```
Spec 提交 → L1 硬门禁（确定性检查，0 误判）
              ├─ 必填章节完整性（§1/§2/§3/§5 必存在且非空）
              ├─ 文件路径有效性（Spec 引用的文件在 repo 中存在）
              ├─ 验证命令白名单（遵循 SAFE_VERIFICATION_PREFIXES）
              └─ 章节长度下限（防敷衍："修 bug" → 不通过）
                ↓
            通过后 → L2 软警告（LLM 辅助，不阻断）
              ├─ 范围完整性（依赖模块是否遗漏？）
              ├─ 约束一致性（约束是否互相矛盾？）
              ├─ 验收可自动化度（能否写为验证命令？）
              └─ 历史风险匹配（相似 Spec 历史 pass_rate < 50%？）
                ↓
            [C] 确认风险后继续  [E] 编辑 Spec  [F] 强制执行
```

**关键设计决策：**

| 决策 | 选择 | 理由 |
|------|------|------|
| 不强制 Spec | 自由文本 prompt 和 `--spec` 两种输入并存 | 简单任务不增加负担 |
| 只审查不修复 | Spec 打回后由人修正 | 范围/约束/验收标准的修改是人（PM/工程师）的决策，AI 不应越权 |
| `--yes` 只跳 L2 | L1 硬门禁在 headless 模式仍阻断 | L1 是确定性检查，0 误判，不应跳过 |
| `--force` 全跳过 | 给有经验用户的后门 | 不强制 |
| L2 依赖 S10-P1 | cross_judge 确保 judge 模型质量后再上 L2 | 避免用「自己都不可信的模型」做审查 |

**成本杠杆：**

| | 每次开销 | 节省的潜在成本 |
|---|---------|-------------|
| L1 硬门禁 | ~0（字符串匹配） | 每次阻断节省 $0.3-6（一次失败执行） |
| L2 软警告 | ~$0.002-0.005（轻量 LLM 判断） | 每次警告避免 $0.5-2（执行中方向偏离+重试） |

按每月 50 任务估算：准入审查 LLM 成本 ≈ $0.15/月，避免的浪费 ≈ $7/月。ROI ≈ 45x。

### Spec Gate 增强：学术驱动的三层扩展（2026-08-01 新增）

> SDD 论文调研（[design/sdd-references-and-frameworks.md](design/sdd-references-and-frameworks.md)）给出三个可直接落地的增强，零或极低成本、有受控实验背书。

**增强 1：AST 冲突检测层（L1.5，零 LLM 成本）**

在 L1 硬门禁和 L2 软警告之间，加一个**基于 AST 的子任务文件冲突检测**：

```
Spec 提交 → L1 硬门禁（字符串/路径检查）
          → L1.5 AST 冲突检测（新增）
          │   解析 Spec §3 标注的「动什么」文件列表
          │   检测多个子任务是否会在同一文件的同一符号上产生冲突
          │   无需 LLM 调用，纯静态分析
          → L2 软警告（LLM 辅助）
```

- **学术支撑**：arXiv:2603.24284（The Specification Gap）实测 AST 冲突检测器在最弱 spec 级别达 **97% 精度，无需额外 LLM 调用**。
- **价值**：在 LLM 调用之前拦截多子任务集成冲突——这是 bench v1 中「跨模块改动」类失败的主因。

**增强 2：spec 细节梯度对照实验（bench v2）**

bench v2 全因子设计中，加入一个**spec 细节梯度维度**，复刻 arXiv:2603.24284 的实验设计：

```
对同一批任务，分别用 4 级 spec 细节跑：
  L0: 完整 Task Spec（7 章节）
  L1: 目标 + 范围（去掉约束/风险）
  L2: 仅目标（一句话 prompt + 文件列表）
  L3: 裸任务描述（当前默认 prompt）
→ 测量每级的 pass_rate / $/pass / 多子任务集成成功率
```

- **学术支撑**：arXiv:2603.24284 证明双 Agent 集成准确率随 spec 细节减少从 58% 跌到 25%，恢复完整 spec 即恢复到 89% 上限。
- **价值**：**填补「SDD 无对照实验」的学术空白**（见 SDD 局限）。agent_go 用自己的 bench 证明 Task Spec 的价值，并把结果反馈到 Spec Gate 的阈值校准。

**增强 3：semantic evaluator 职责边界明确化**

根据 arXiv:2603.25773（Specification as Quality Gate），重新界定 semantic evaluator 的职责：

```
验证三层架构（论文推荐的非循环架构）：
  Layer 1: 可执行规范验证（shell exit code）  ← 确定性，覆盖能用验证命令表达的
  Layer 2: 确定性管道（lint/type/test）       ← 确定性，覆盖结构和类型
  Layer 3: AI 审查（semantic evaluator）      ← 只用于「结构残差」
           ↑
           职责边界：只查 Layer 1-2 触及不到的语义/架构问题
           禁止：重复 Layer 1 能确定的验证（避免循环论证）
           铁律：judge != worker（同源审查是「回响」非「抵消」）
```

- **学术支撑**：arXiv:2603.25773 论证「AI 审 AI 产的代码在无可执行规范时是结构性循环」，推荐「规范优先 → 确定性验证 → AI 审查只用于残差」架构。
- **价值**：降低 semantic evaluator 的 LLM 调用成本（不重复确定性验证），同时提升其有效性（聚焦真正需要语义判断的残差）。

### 模型生产力评估机制（2026-07-25 补充）

> 完整设计见 [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md) §3。

**核心矛盾**：厂商 benchmark（SWE-bench）与真实 agentic 场景存在巨大鸿沟——Qwen3-Coder-30B-A3B 官方上榜，独立测试真实解决率仅 7%。**不能靠厂商 benchmark 决策模型选型**。

**三层评估体系（用 agent_go 自跑数据，不信厂商分）：**
1. **第 1 层 确定性评估**（客观）：标准任务集（带 ground-truth 验证命令）× N 模型 → pass_rate / first_pass_rate / latency / cost
2. **第 2 层 语义评估**（跨模型交叉评判规避自偏）：对"通过但有疑问"的产出，用 A 模型评 B 模型产出 → semantic_score / false_positive_rate
3. **第 3 层 决策汇总**：`analyze_model_productivity` 聚合 → recommendation（recommended/conditional/discouraged）+ 推荐角色

**关键设计约束（铁律）：**
- **LLM-as-Judge 禁绝自评** — 研究显示模型对自产输出评分偏高 10-30%；编排器硬约束 `judge != candidate`，每产出 ≥2 不同 provider 评判
- **样本 <5 不决策** — 标记 low_confidence，不参与自动降级（避免小样本噪声误判）
- **假阳性 >20% 禁用** — 验证通过但功能错误的模型不可信（验证命令覆盖不全是常态）
- **人工抽检 10% 校准** — LLM 评判者与人工分歧 >30% 时标记 judge unreliable，回退到仅第 1 层

**落地形态**：`agent_go eval bench --tasks eval_suite/ --models M1,M2,M3 --repeat 3 --judge-model Mj` → `agent_go eval models` 输出决策矩阵 → `agent_go router recommend` 自动生成路由配置。

## P1 重点：验证 Agent 循环（Verification Loop）

> **2026-07-25 进展**：双层验证循环已落地并全链路验收——可配置修复循环（max_retries + retry_timeout 硬超时）、全量失败反馈（stdout/stderr + diff --stat）、blocked 阻断下游、`/goal` 文本注入 + Stop Hook（`--goal` / `--goal-hook`）、LLM 语义评估、verify_state.json 断点恢复。实施偏差见 [design/verification-agent-goal-spec.md](design/verification-agent-goal-spec.md) §11.4。

**一句话：验证一次不够就自动修，修到通过或上限；实在修不好就阻断下游，不让错误扩散。**

### 核心矛盾

当前验证是单向的 pass/fail + 1 次通用重试，不解决问题也不阻断扩散：

```
执行 → 验证 → 失败 → "请修复"（缺乏具体上下文）→ 重试 1 次 → 还失败 → 放弃，继续走下游
                                                          ↓
                                                    下游基于错误代码继续 → 级联失败
```

### 解决思路：双层验证循环

借鉴 Claude Code 的 `/goal` 机制和行业"verify → fix → verify"循环模式：

```
┌── Claude Code 内部循环 ─────────────────────────┐
│  TASK.md 末尾注入 "/goal <condition>"             │
│  → Claude 自主迭代：编码 → Stop Hook 验证 → 再编码 │
│  → 加速：不反复起停 claude 进程                     │
└──────────────────────┬──────────────────────────┘
                       ↓ Claude 退出完成主逻辑
┌── agent_go 外部循环 ────────────────────────────┐
│  Verification Agent                               │
│  ├─ 安全校验（4 级命令白名单兜底）                  │
│  ├─ 确定性验证（真实 shell exit code）             │
│  ├─ 失败 → RepairAgent 注入完整上下文修复            │
│  │   （含 stdout/stderr/git diff/历史摘要）         │
│  ├─ 再验证 → 直到通过或 max_retries（可配 3-5 次）  │
│  └─ 最终失败 → 阻断下游（blocked 状态）             │
└──────────────────────────────────────────────────┘
```

### 关键设计

| 决策 | 选择 | 理由 |
|------|------|------|
| 为什么不只用 /goal？ | 混合模式 | /goal 评估器不执行工具只读对话，agent_go 需要真实 exit code 做确定性验证 |
| 验证失败后怎么办？ | 阻断下游 | 防止错误代码被下游继承导致不可调试的级联失败 |
| 修复 prompt 如何构建？ | 注入完整失败上下文 | 当前"请修复"太通用，需要 stdout/stderr/git diff 让 Claude 精准定位 |
| 最大重试次数？ | 可配置，默认 3 | 防止 token/时间爆炸 |
| 双模式支持？ | shell exit code + LLM 语义 | shell 确定性强，LLM 能判断语义完整性（如"重构完成且风格一致"） |

### 落地路径

```
Phase 1（3-4 天）:
  VerificationAgent + RepairAgent 基础循环
  → 替代现有 _verify_changes() 单次重试
  → pipeline 依赖阻断（blocked 状态）
  → 保留失败 worktree 供人工审查（agent_go inspect）
  → 配置 + CLI 参数（--preserve-worktrees / --no-preserve）

Phase 2（1-2 天）:
  GoalInjector — worktree 内注入 /goal + Stop Hook
  → TASK.md 追加 /goal condition
  → watchdog 超时保护

Phase 3（2 天）:
  LLMEvaluator — 语义评估
  → hybrid 模式编排
  → 评估模型配置

Phase 4（1 天）:
  Resume 兼容 + Metrics
  → verify_state.json 持久化
  → eval 新指标（首次通过率、重试成功率、阻断率）
```

### 关键约束

- `/goal` 是 Claude 内部的加速循环，**不替代**外部的 Verification Agent（安全兜底不能丢）
- 验证命令必须通过 agent_go 的 4 级安全白名单，Stop Hook 脚本也需校验
- `blocked` 状态的 subtask 保留 worktree 供人工审查，不清除
- 每次重试验证结果记入日志，供 eval 系统分析

## 产品 KPI（8 个）

> **2026-08-01 更新**：K1/K3/K4/K8 基线值替换为 Bench v1 实测数据（342 条 Claude 记录，7 模型 × 22 任务）。此前为估计值。

| # | KPI | 当前（Bench v1 实测） | Q3 目标 | 年度 | 差距 |
|---|-----|----------------------|---------|------|------|
| K1 | 任务成功率 | **83.9%**（Claude 三模型均值，n=342） | ≥92% | ≥97% | 🔴 -8.1pp |
| K2 | 安全零事故 | ✅ 0 | 保持0 | 保持0 | 🟢 |
| K3 | 简单任务耗时 | **122s**（Claude easy+passed，n=104） | ≤3min（180s） | ≤1.5min | 🟢 达标 |
| K4 | 单任务成本 | **$0.34**（Haiku）/ **$0.69**（Claude 均值） | ≤$0.05 | ≤$0.03 | 🔴 7-14x |
| K5 | 中断恢复成功率 | 未知 | ≥99% | ≥99.9% | — |
| K6 | 可观测性可回答率 | 7/9 | 8/9 | 9/9 | 🟡 |
| K7 | 首次上手时间 | ~10-20min | ≤12min | ≤8min | 🟡 |
| K8 | **首次验证通过率** | **88.9%**（Claude 三模型均值。Opus 84.2%/Sonnet 93.1%/Haiku 91.2%） | ≥80% | ≥90% | 🟢 达标 |

> **K8 定义（修订）**：`total_retries == 0` 的 record 占比。更精确的修订定义为「通过 record 中 zero-retry 占比」，但当前 schema 缺少 `binary_pass` 字段，暂时沿用原定义。详见 [bench-v2-data-requirements.md](design/bench-v2-data-requirements.md) §3.4。

> **⚠️ 2026-08-06 度量有效性警示（覆盖上表达标/差距状态）**：上述 Bench v1 基线经度量审计发现**不可直接采信**，K1/K4/K8 的达标与差距状态均为**临时**：
> - `binary_pass` 实现缺陷——55% 的 v3 记录"全子任务失败但 binary_pass=True"（计算时序错位 + `all([])` 空真值陷阱）→ K1/K8 的"通过"判定不可信；
> - `pass_rate` 假失败——v3 名义通过率 34%，修正（计入已完成已验证却被收尾超时 zeroed 的记录）后真实 ~67% → K1 基线可能偏低约 37pp；
> - v1/v2/v3/v4 是**四套不同采集器**，PRD 基线钉在 v1、代码已演进到 v3/v4，跨版本不可比 → K8"🟢达标"建立在另一把尺子上；
> - 20-24% 的 cost=0 记录（infra/API 故障）混入失败分母 → 把基础设施可用性与模型能力混为一谈。
>
> **结论**：K8 的"🟢达标"、K1 的"🔴-8.1pp"、K4 的"7-14x"在度量修复前**均非定论**，不应据此刻板考核。修复路径见 [design/bench-metric-validity-2026-08-06.md](design/bench-metric-validity-2026-08-06.md)（度量诊断）与 [design/timeout-kill-strategy-2026-08-06.md](design/timeout-kill-strategy-2026-08-06.md)（kill/成本策略）。**修正口径下的历史重估基线**见 [design/bench-baseline-corrected-2026-08-06.md](design/bench-baseline-corrected-2026-08-06.md)（v3 修正通过率 ~67%、$/pass ~$0.053；权威基线待用修复后采集器重跑冻结）。对应 roadmap 已排入 **S12**。

## 北极星指标

**单位成本的验收通过率（$/pass）** — 将成本纳入质量指标，防止「不计成本堆质量」或「省钱产出垃圾」两种极端。

**统一定义（2026-08-01 修订）**：

```
$/pass = sum(total_cost_usd) / sum(pass_rate)
```

含义：「获得一个等效完全通过所需的美元成本」。以 raw cost 和 raw pass_rate 为准，在分析阶段计算，不复用 `dollar_per_pass` field。

> **⚠️ 分母概念缺陷（2026-08-06 识别）**：`sum(pass_rate)` 把"部分完成"当成"等效完全通过"——upstream→downstream 依赖链下，1/4 子任务完成被记为 0.25 个 pass，但下游被 blocked 时实际是 **0 个可交付物**。这系统性低估真实单交付成本，使 $/pass 偏乐观。修正方向：分母改为**任务级成功计数**（全部子任务通过才算 1），并叠加 `kill_reason` 过滤（`cleanup_race` 计通过、`over_budget` 单列、infra 不计分母）。修正前 $/pass 的绝对值不可作为达标判定，仅适合同口径内的相对比较（如模型排序）。详见 [design/bench-metric-validity-2026-08-06.md](design/bench-metric-validity-2026-08-06.md) 缺陷 2。

| | 当前（Bench v1 实测） | Q3 目标 | 年度 |
|---|----------------------|---------|------|
| $/pass（Claude 均值） | **$0.39**（field avg 三模型均值：Opus $0.45 / Sonnet $0.50 / Haiku $0.20） | ≤$0.05 | ≤$0.03 |
| $/pass（加权计算） | **$0.83**（总成本 / 总 pass 权重） | ≤$0.05 | ≤$0.03 |

> **两种口径差异说明**：field avg（$0.39）先逐 record 算 $/pass 再取模型均值，对高成本 record 有稀释效应；加权计算（$0.83）直接总成本/总 pass 权重，更反映实际成本效率。差异主要来自 django-blog 高成本任务（单次 $0.36-$6.24）在加权计算中的高权重。建议以加权计算为北极星主要口径，field avg 为辅助（便于跨模型比较）。

## 反指标（明确不看）

这些指标看起来重要，但优化它们会伤害真正的产品价值：

| 反指标 | 为什么不看 |
|--------|-----------|
| 原始提交速率 | 参考案例：旧系统提交速度是新系统 70 倍，绝大多数是无效忙碌。高提交速率 ≠ 高产出 |
| 子任务数量 | 拆得越多不代表做得越好，有效提交率才是关键 |
| API 调用次数 | 减少调用不代表省钱，一次高质量大调用比多次小修小补更高效 |

## 产品及格线

> 「你敢不敢周五下午 4 点输入 `agent_go run`，关电脑走人，周一早上信心满满地 merge PR？」

## 风险与开放问题

| 风险/问题 | 说明 | 缓解 |
|-----------|------|------|
| 长任务上下文污染 | 单 Agent 同时维持全局目标与局部实现，上下文被塞满，质量随时间衰减 | 子任务上下文隔离（worktree + TASK.md 窄化），不传递全局历史 |
| Worker 本地模型质量不足 | 复杂 Worker 任务走本地模型可能拉低通过率 | 复杂度分级路由（easy→本地，hard→API）；质量门 + 定期抽样回测 |
| 审查成本失控 | 引入 Reviewer 角色后 token 成本可能翻倍 | 审查预算上限 ≤ 被审查工作的 20%；仅对高风险子任务开启审查 |
| 协调机制过度设计 | 小规模任务引入重机制得不偿失 | P0 仅做最简版本，按规模驱动演进 |
| **验证循环 token 爆炸** | 多次验证-修复循环可能消耗远超预期的 token | `max_retries` 硬上限（默认 3）+ 每迭代超时控制；启用 /goal 模式减少外部循环次数 |
| **修复 Agent 引入新问题** | 修复可能引入回归，新旧问题叠加 | 每次重试全量运行所有验证命令，不是只验上次失败的；达到上限后阻断下游 |
| **/goal 循环不终止** | Claude 内部 goal 循环可能超出 agent_go 的控制 | 外部 watchdog（全局超时 + max_goal_turns）强制 kill 进程 |
| **$/pass 计价失真** | 未知模型兜底为最便宜单价（ISSUE-26），$/pass rate 被低估 11-22 倍，gate 假性通过 | `analyze_cost` 优先用真实 `cost_usd`，token 重算仅兜底；本地模型强制 `local_model_hourly_cost` 估算并记入 metering |
| **本地模型"免费"错觉** | 本地模型真实成本=时间×硬件折旧，4090 跑 27B 约 $0.24/pass（比 DeepSeek API 贵 240 倍）；延迟击穿 K3 | 评估机制实测后再接入；本地模型仅用于 easy worker / reviewer 等低频延迟不敏感角色 |
| **厂商 benchmark 失真** | Qwen3-Coder-30B 官方上榜 vs 独立测真实 7%，SWE-bench 与 agentic 场景鸿沟大 | 三层评估机制（§评估机制），只用 agent_go 自跑数据，不信厂商分 |
| **LLM-as-Judge 自偏** | 用 Sonnet 评 Sonnet 产出会系统性偏高 10-30%，评估结果不可信 | 交叉评判矩阵禁绝自评（`judge != candidate`）+ 人工抽检 10% 校准 |
| 开放：跨任务记忆沉淀 | Agent 间经验随会话结束丢失，每次从零开始 | 远期探索 Field Guide 机制（Agent 自主维护的项目级记忆） |

## CLI 与 MCP 交互层（已落地 2026-08-01）

> **一句话：agent_go 同时是「人类可用的 CLI」和「Agent 可用的 MCP Server」——错误带修复指令、上下文按需加载、支持远程接入。**
>
> 设计分析见 [design/cli-mcp-design-analysis.md](design/cli-mcp-design-analysis.md)（范式总结 + 最佳实践）与 [design/cli-mcp-interaction-analysis.md](design/cli-mcp-interaction-analysis.md)（7 个交互场景分析 + 改进方案 + 落地记录）。

### 背景

CLI 正在成为 AI Agent 的事实标准接口（2026 年 Q1 的 Agent-Native CLI 浪潮：larksuite/cli、Google Workspace CLI、CLI-Anything 等 90 天 130k stars）。agent_go 需要同时服务**人类**（终端操作）和 **AI Agent**（自主调用）两类调用者，二者对交互协议的需求不同：

| 维度 | 人类用户 | AI Agent |
|------|---------|----------|
| 交互方式 | 交互确认 + 终端输出 | 工具调用 + 结构化响应 |
| 错误处理 | 可读的错误信息 | 可执行的修复指令（`fix` 字段） |
| 上下文获取 | 看终端 | 按需读取（Resources 原语） |
| 进度感知 | 终端进度条 / TUI | 轮询 + 推送通知 |

### 已落地能力清单（2026-08-01）

#### MCP 协议完备性（6 tools + 三大原语 + 双 Transport）

| 能力 | 说明 | 状态 |
|------|------|------|
| **6 个工具** | `run_task` / `resume_task` / `inspect_task` / `review_task` / `list_tasks` / `cancel_task` | ✅ 已落地 |
| **Resources 原语** | 6 个只读资源（summary / plan / metering / log/recent / review / list），按需加载，轮询 token 消耗降低约 80% | ✅ 已落地 |
| **Prompts 原语** | 3 个标准操作规程模板（diagnose_failure / review_and_decide / resume_or_restart），Agent 无需自己编写诊断流程 | ✅ 已落地 |
| **stdio Transport** | JSON-RPC 2.0 over stdio（默认） | ✅ 已落地 |
| **HTTP/SSE Transport** | `agent_go mcp --http`：POST /mcp 处理请求 + GET /mcp SSE 推送 + GET /health 健康检查；Bearer token 鉴权（`AGENT_GO_MCP_HTTP_TOKEN`）；支持远程接入 | ✅ 已落地 |

#### Agent 可恢复性（错误自修复）

| 能力 | 说明 | 状态 |
|------|------|------|
| **错误 `fix` 字段** | 错误响应携带可执行修复指引（`ERROR_TEMPLATES` 预定义 7 种错误类型），Agent 收到错误后可自主恢复 | ✅ 已落地 |
| **任务发现** | `list_tasks` 支持状态过滤 + 分页，Agent 无需提前知道 task_id | ✅ 已落地 |
| **任务取消** | `cancel_task` 终止子进程 + meta.json 标记 `cancelled`（保留已完成结果与 metering） | ✅ 已落地 |
| **生命周期状态机** | `cancelled` / `stale_aborted` 状态可恢复（`resume` 支持） | ✅ 已落地 |

#### 可观测性

| 能力 | 说明 | 状态 |
|------|------|------|
| **并行活动追踪** | ActivityTracker per-subtask 活动（时间戳 + 单调序号），异步任务也有实时活动可查（`_start_activity_monitor`） | ✅ 已落地 |
| **进度推送** | `notifications/progress` 实时推送 + SSE 广播到所有已连接客户端 | ✅ 已落地 |

#### CLI 交互引导

| 能力 | 说明 | 状态 |
|------|------|------|
| **失败恢复闭环引导** | 失败后输出可复制执行的完整操作路径（inspect → review → resume → 不阻断重试） | ✅ 已落地 |
| **后续操作卡片** | 报告末尾输出 review / approve / pr / resume 命令清单 | ✅ 已落地 |

### 设计原则（本次落地沉淀）

1. **错误不是终点，是指引** — 每个错误响应携带 `fix` 字段（可执行修复指令），让 Agent 能自主恢复而非死循环
2. **上下文按需加载** — Resources 原语替代 tool 全量返回，减少 Agent 的 token 消耗和推理负担
3. **人类和 Agent 用同一套核心，不同交互壳** — stdio / HTTP 共用 `handle_message`，差异只在传输层
4. **安全默认本地** — HTTP 默认绑定 127.0.0.1，token 鉴权可选；repo allowlist 保护文件系统边界

### 后续迭代（待评估）

> **2026-08-01**：下表方向已全部落地（见上文「已落地能力清单」与 roadmap 快照），暂无待评估项；Activity store 持久化列为远期候选。

| 方向 | 说明 | 状态 |
|------|------|------|
| Sampling 原语 | Server 向 Agent 反向询问（破坏性操作确认） | ✅ 已落地（`request_sampling` + cancel_task `confirm`） |
| 增量 Plan 迭代 + 实时 Diff | 人类修改 Plan 时展示变更差异 | ✅ 已落地（`show_plan_diff` + 菜单 [V] 版本历史） |
| 波次进度卡片 | wave N/M 分组进度显示 | ✅ 已落地 |
| 多 profile | `--profile` 切换配置（~/.agent_go/profiles/） | ✅ 已落地 |
| SKILL.md 自描述 | `agent_go skills show <name>` 输出完整 SKILL.md | ✅ 已落地 |
| Activity store 持久化 | 服务重启不丢失活动追踪 | 远期候选 |

## 办公能力扩展：MCP 消费 + 产物导出（S9）

> **状态**：能力 A（MCP 消费层）✅ 已落地（2026-08-01，`mcp_client.py` + `mcp_servers` config）；能力 B（产物导出）✅ 已落地（2026-08-01，`artifacts.py` + `--artifact-dir`），设计稿见 [design/office-capability-extension.md](design/office-capability-extension.md)
> **决策结论**：不自建 Office 编辑器，补齐"搬运"（MCP 消费）与"交付"（产物导出）两个架构能力

### 背景

agent_go 当前是**代码 diff 导向的编排器**——交付物只有 git commit/PR。但知识工作中"报告、演示、数据表"等产物同样需要自动化。业界已通过 MCP 协议将 Office 文档操作标准化（excel-mcp-server 4084★、office-powerpoint-mcp-server 1847★），出现"CLI + MCP 双模工具层"范式。

agent_go 的护城河是 Plan → Decompose → Execute 编排层，不是文档引擎。因此正确定位是**跨层搬运者**：补齐两个结构性缺口，复用生态工具。

### 两个结构性缺口

| 缺口 | 现状 | 后果 |
|------|------|------|
| **A. 无外部工具消费** | ✅ **已关闭（2026-08-01）**：MCP 消费层已落地，子任务可调用外部 server 工具；另暴露 MCP server 6 tools | 历史：无法接入 Office MCP 生态，子任务只能改代码 |
| **B. 无产物导出** | ✅ **已关闭（2026-08-01）**：`artifacts.py` + `__artifacts__/` 声明制 + `--artifact-dir` 已落地，pipeline 清理前收集导出 | 历史：生成的文档随清理丢失，无法交付用户 |

### 能力 A：MCP 消费层（✅ 已落地）

让子任务调用用户配置的外部 MCP server 工具，如同原生工具。

**配置契约**（`config.json` 新增 `mcp_servers`）：

```jsonc
{
  "mcp_servers": {
    "excel": {"command": "uvx", "args": ["excel-mcp-server", "stdio"], "scope": "worker"},
    "ppt": {"command": "uvx", "args": ["--from", "office-powerpoint-mcp-server", "ppt_mcp_server"]}
  }
}
```

- 命名空间约定：外部工具暴露为 `mcp__{server}__{tool}`（如 `mcp__excel__read_sheet`），避免重名
- `tool_filter` 白名单收窄能力（省 token），`scope` 控制可见性（worker/planner_only）
- 故障隔离：server 启动失败降级 warning，不阻断 pipeline（与 notify/skills 同级）
- 实现：`agent_go/mcp_client.py`（MCPClientPool + MCPServerConnection），pipeline 启动/收尾管理连接池

### 能力 B：产物导出路径（✅ 已落地）

区分 code-diff（worktree→commit）与 artifact（文件→用户目录）。

- **声明制**：子任务写入 `worktree/__artifacts__/` 的文件视为产物
- **收尾收集**：pipeline 清理 worktree 前扫描 `__artifacts__/`，复制到 `--artifact-dir`
- **CLI**：`--artifact-dir ~/reports` 显式指定；不指定则向后兼容（产物留 worktree）
- **实现**：`agent_go/artifacts.py`（collect_from_worktree + export + render_export_summary）；TASK.md 注入产物目录约定引导子任务

```bash
agent_go run ./repo "读取 sales.xlsx 生成季度汇报 PPT" \
  --yes --artifact-dir ~/reports --config office.json
# → ~/reports/{task_id}/{sub_id}/Q2_report.pptx
```

### 不做什么（防范围蔓延）

| 排除项 | 理由 |
|--------|------|
| ❌ 内建 Office 编辑器 | 生态成熟，自建是重复造轮子 |
| ❌ 云端文档协作（OneDrive/SharePoint） | 走 ms365 MCP server，agent_go 不感知云协议 |
| ❌ 产物版本管理 / 在线预览 | 产物是表达层输出，交给用户侧 DMS / 关联应用 |

### 新增验收 KPI

> 编号接续现有 K1–K9，避开基础设施化方向的 K10（知识注入采纳率）。

| # | 指标 | 目标 |
|---|------|------|
| K12 | MCP 工具调用成功率 | ≥95%（排除用户配置错误） |
| K13 | 产物导出完整率 | 100%（声明产物必达用户目录） |

详细设计见 [design/office-capability-extension.md](design/office-capability-extension.md)。

## 基础设施化方向（评估中）

> **状态**：设计草案完成（见 [design/infrastructure-api-design.md](design/infrastructure-api-design.md)），待论证必要性和可行性后决定是否投入。
>
> **2026-08-01 进展**：本方向的核心接口层已先行落地——**CLI `--json`**（全局标志 + Console 抽象）与 **MCP Server**（JSON-RPC 2.0，6 tools + Resources/Prompts 原语 + stdio/HTTP 双 transport）均已可用，详见上文「CLI 与 MCP 交互层」。下方表格保留为完整规划。

### 定位扩展

```
当前：agent_go = CLI 工具（用户手动输入命令 → 看终端输出）
目标：agent_go = 可编程开发基础设施（CI/CD / IDE / Git Hooks / 项目管理平台可调用）
```

### 新增能力全景

| 能力 | 说明 | 优先级 | 设计文档 |
|------|------|--------|---------|
| **Python API** | `run_task()` 返回结构化 `TaskResult`，替代 CLI 的 `None` 输出 | P0 | §3.1 |
| **CLI --json** | 所有子命令支持 JSON 输出，供外部脚本调用 | P0 | §3.4（✅ 全局 `--json` 标志已落地） |
| **MCP Server** | JSON-RPC 2.0 over stdio / HTTP+SSE，6 tools + Resources/Prompts 原语 | P0 | ✅ 已落地（2026-08-01） |
| **事件总线** | 全生命周期 `emit_event` + `subscribe_event` + `events.jsonl` | P1 | §4 |
| **状态查询 API** | `query_task()` / `query_project_trend()` 统一查询入口 | P1 | §3.1（MCP `inspect_task` / `list_tasks` / Resources `summary` 已提供等价能力） |
| **知识存储** | `KnowledgeStore` 项目级经验沉淀，Plan prompt 自动注入 | P2 | §5 |
| **CI/CD 集成** | GitHub Action / pre-commit hook / GitLab CI 模板 | P3 | §6 |
| **IDE 插件** | VS Code Extension（进度面板 + 一键运行 + 审查入口） | P4 | §6.3 |

### 新增场景

| # | 场景 | 用户 | 当前方案 | 基础设施化后 |
|---|------|------|---------|-------------|
| N1 | CI 门禁自动拦截 | 团队 | 人工 `eval gate` | CI pipeline 自动跑门禁，失败阻断发布 |
| N2 | 开发流程嵌入 | 个人 | 切换到终端输入命令 | pre-commit hook + VS Code 面板 |
| N3 | 项目管理同步 | 组织 | 无 | Jira Task → agent_go → 状态自动流转 |
| N4 | 知识沉淀 | 个人+团队 | 每次从零开始 | 自动提取历史模式，Plan 注入经验 |
| N5 | 多工具编排 | 平台 | 脚本包装 CLI | Python API 嵌入自有工作流 |

### 新增 KPI

| # | KPI | 当前 | 目标 | 说明 |
|---|-----|------|------|------|
| K9 | 集成接入数 | 0 | 10 → 50 | 累计集成的外部系统数（CI / IDE / Webhook） |
| K10 | 知识注入采纳率 | — | ≥60% | Plan 中知识注入段被 Planner 有效使用的比例 |

### 新增风险

| 风险 | 说明 | 缓解 |
|------|------|------|
| API 接口不稳定 | 外部调用方频繁适配 | 结构化结果版本化 `schema_version` |
| 知识注入误导 Planner | 错误经验降低 Plan 质量 | JSON 校验失败跳过注入，回退无知识模式 |
| IDE 插件维护成本高 | VS Code API 频繁变动 | 插件仅做薄壳，核心逻辑走 CLI `--json` |
| 范围蔓延 | 基础设施化吞噬核心迭代资源 | 明确 P0-P4 分期，每期验收后再投入下一期 |

## 远期方向（P2+）

当前聚焦 CLI 编排层，以下方向验证用户需求后再投入：

| 方向 | 说明 | 参考 PRD 对应 |
|------|------|--------------|
| 叠加式审查流水线 | 多视角并行审查 Worker 产出（diff 视角 + 独立模型评审），打回自动回流 | F3 |
| 全局决策日志 | Planner 的设计决策写入共享日志，Worker 启动时自动注入，治「脑裂」 | F4 |
| Field Guide 共享记忆 | Agent 自主维护的项目级知识目录，跨任务沉淀经验，只记「出乎意料的情况」 | F8 |
| 复杂度双通道 | Planner 给子任务打 `difficulty: easy/medium/hard` 标签，hard 任务自动走强模型通道 | 7.3 |
| **验证 Agent 生态系统** | 社区贡献的验证规则包，分三阶段演进：Phase 1 复用 Skill 体系（`type: verification`），零新增基础设施；Phase 2 打包为 Claude Code Plugin 分发；Phase 3 若生态足够大则独立为 Verifier 体系 | — |

## 长程 Agent 演进路线（论文对照）

> 基于 2026 年 7 月综述论文 *Towards Long-Horizon Agents: A Survey*（人大/北大/清华/港科大/新加坡国立联合发布）的系统框架，对照 agent_go 当前能力，规划下一步演进方向。

### 论文核心框架

论文提出长程 Agent 的**统一定义**：

> **长程 Agent = 基础策略 ⊕ 外部编排层**  （`Agent = πθ ⊕ H`）

长程能力不是模型的属性，而是**模型-Harness 耦合系统**的属性。两者协同进化：
- **Pillar I（外部化 Harness）**：Loops & Workflows → Context & Memory → Tools/MCP/Skills → Orchestration → Hooks & Middleware → Verification
- **Pillar II（内部化模型优化）**：Architecture → Data/Env Synthesis → Pre/Mid-Training → Fine-Tuning → Agentic RL → Self-Evolution

能力按三个嵌套层级递进：

| 层级 | 能力 | 含义 | agent_go 当前 |
|------|------|------|--------------|
| **H1** 上下文内交互推理 | 单窗口内的多步推理+工具使用+环境交互 | 子任务上下文隔离 + 验证循环 | ✅ 核心链路 |
| **H2** 跨上下文状态与记忆 | 跨越多个上下文窗口/会话，维持任务状态与记忆 | git worktree 传递产物 + KnowledgeStore 设计 | ⚠️ 部分（缺持久记忆） |
| **H3** 跨任务经验积累 | 从历史任务中学习，持续提升未来表现 | Field Guide 远期规划 | ❌ 未开始 |

### agent_go 与论文框架对照

| 论文 Harness 组件 | agent_go 对应 | 状态 |
|-------------------|--------------|------|
| Loops & Workflows (§4.1) — Plan-Execute | Plan → Decompose → Execute 四阶段 | ✅ |
| Loops & Workflows (§4.1) — Branching | 仅线性，无多路径探索 | ❌ |
| Context & Memory (§4.2) — Working Context | worktree 隔离 + TASK.md 窄化 | ✅ |
| Context & Memory (§4.2) — Persistent Memory | KnowledgeStore 设计完成，待落地 | ⚠️ |
| Tools, MCP & Skills (§4.3) | Role-Skill 规则引擎 + MCP 集成 | ✅ |
| Orchestration (§4.4) — Decomposition & Roles | Agent Type 系统 + Role-Skill 匹配 | ✅ |
| Orchestration (§4.4) — Coordination Topologies | 拓扑波次调度 + 并发 + 级联阻断 | ✅ |
| Hooks & Middleware (§4.5) | Stop Hook / GoalInjector / Notify 事件通道 | ✅ |
| Hooks & Middleware (§4.5) — Runtime-adaptive | 仅静态 Hook | ❌ |
| Verification (§4.6) | 双层验证循环 + LLM 语义评估 + cross_judge 交叉评判 | ✅ |
| Cost-aware Agency (§7.3.1) | 角色路由 + 复杂度双通道 + metering | ⚠️ 缺预算强制 |
| Self-evolving Harness (§7.1.1) | 无，Harness 手工维护 | ❌ |
| Harness Generalization (§7.1.2) | 与 Claude Code + git worktree 紧耦合 | ❌ |

### 三阶段演进路线

#### 阶段一：补齐 H2 能力 — 让单次任务更可靠（近期 3–6 月）

**1. 分支式工作流（Branching Workflows）**
- Plan 阶段对高不确定性步骤生成备选路径，轻量评估后选最优
- 验证失败时回退到分叉点尝试替代策略（而非反复修复同一方案）
- 仅对 `difficulty=hard` 子任务开启，控制成本

**2. 持久化记忆落地（Persistent Memory）**
- **Factual Memory**：项目级 CLAUDE.md / AGENTS.md 自动维护（从当前手工编写升级为 Agent 自主更新）
- **Experiential Memory**：记录「验证命令模式 → 成功率」「分解策略 → 首次通过率」，Plan 阶段自动注入
- **Memory Maintenance**：记忆合并、去重、过期清理（论文强调这是持久化记忆的核心工程挑战）

**3. 成本预算强约束（Budget-aware Agency）**
- `--max-cost $0.50`：任务级硬上限，达到即熔断并汇报
- 成本从「事后分析」升级为「事前承诺 + 事中监控 + 超限熔断」
- 建立 agent_go 自身的 $/pass 标度律数据

#### 阶段二：开启 H3 能力 — 让 Agent 随时间变强（中期 6–12 月）

**4. 自我进化的 Harness（Self-evolving Harness）**
- **Phase 1 参数自动调优**：基于历史 metering.jsonl + meta.json，自动调整并发度、验证策略、max_retries
- **Phase 2 编排拓扑自演化**：Agent 自主决定子任务分组、Reviewer 范围、验证步骤剪枝
- **Phase 3 Skill 自主蒸馏**：从成功执行轨迹中自动提取可复用 Skill，写入 Skill 库

**5. 跨任务经验积累（H3 Cross-task Experience）**
- **验证命令知识库**：哪些验证命令在哪些类型任务上最有效？跨项目迁移
- **分解模式库**：对「重构类」「新增功能类」「Bug 修复类」任务的最佳分解策略
- **失败模式识别**：提前预警「此任务特征历史上成功率 < 40%」，建议人工介入

#### 阶段三：基础设施化与开放性（长期 12+ 月）

**6. Harness 可迁移性（Harness Generalization）**
- Harness 协议标准化：将 Plan/Execute/Verify 接口抽象为开放协议
- 多 Runtime 支持：不绑定 Claude Code，同时兼容 OpenCode、aider、Codex CLI 等 Worker
- 可移植 Skill 格式：产出符合 Agent Skills 标准的可复用制品

**7. 安全治理升级**
- 外部输入作为不可信数据：sandbox 隔离 + 来源追踪
- 安全评分作为第一类指标：与成功率并列评估
- 独立安全验证：不由执行 Agent 自我审查

### 能力-时间矩阵

| 时间 | 论文层级 | 核心命题 | 关键交付 |
|------|---------|---------|---------|
| **现在** | H1 完善 | 单任务可靠性 85%→92% | 验证循环、级联阻断、聚合审查 |
| **近期** | H2 补齐 | 记忆 + 分支 + 预算 | KnowledgeStore、Branching、--max-cost |
| **中期** | H3 开启 | 自进化 + 经验积累 | Harness 自动调优、分解模式库、Skill 蒸馏 |
| **长期** | Frontier | 可迁移 + 安全 + 开放 | 开放协议、独立安全验证、多 Runtime |

### 关键设计约束（论文启示）

- **Harness 而非模型，是长期护城河**：模型会换代，编排能力可跨模型复用。论文明确指出「长程能力越来越多地是 Runtime 的属性，而非模型的属性」。
- **自进化需小心过拟合**：论文警告自进化系统的三个核心限制 — 优化目标仍是人工设定的 benchmark、泛化不超出训练分布、长期可能漂移。agent_go 的自进化应以真实任务成功率（而非 benchmark 分）为目标函数。
- **安全是 Harness 层问题，非模型层问题**：论文强调「Harness 而非模型，是生产环境中 Agent 的主要对齐和权限控制面」。已有的 4 级命令白名单是正确的方向，需向独立安全验证演进。

## 非目标用户

- 不用 Claude Code 的 — agent_go 是编排层，不是替代品
- 零技术背景的 — 输出是 git diff，需要能 review
- 单步骤任务的 — 裸 Claude Code 更快
