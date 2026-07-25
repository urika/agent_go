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

### 模型分级策略（2026-07-25 补充）

> 完整设计见 [design/model-evaluation-and-tiering.md](design/model-evaluation-and-tiering.md)。

**一句话：角色决定档位，difficulty 决定升降，评估机制验证选对。**

角色路由解决了"按角色路由不同模型"的机制，但留下两个未闭环问题：(1) 选哪个模型？(2) 怎么知道选得对？分级策略回答前者，评估机制回答后者。

**三角色 × 三档位分级矩阵（要点）：**

| 角色 / 档位 | 主力旗舰 (Frontier) | 性价比 (Value) | 轻量 (Lite) |
|-------------|---------------------|---------------|-------------|
| Planner（不降级） | ★ Sonnet 4 / GPT-4.1 / Qwen-Max | ✗ 铁律禁降级 | ✗ |
| Worker-hard | Opus 4.6 / GPT-5.6 Terra / GLM-4.6 | DeepSeek V3.2 / Qwen-Plus | — |
| Worker-medium | Sonnet 4 / GPT-4.1 | DeepSeek V3.2 / Qwen-Plus | Doubao-lite |
| Worker-easy | — | DeepSeek V3.2 / Doubao-pro | Haiku 4.5 / GPT-4.1 Nano |
| Reviewer（不同源） | Gemini 2.5 Pro / Qwen-Max | Kimi K2 | — |

**关键约束（补充铁律）：**
- **$/pass 计价必须优先用真实 `cost_usd`** — 厂商标称价表无法覆盖所有模型（`claude-code-executor`、本地模型、国产新旗舰），兜底为最便宜模型会让 [$/pass rate 被低估 11-22 倍](ISSUES.md#issue-26)，gate 假性通过
- **Reviewer 与 Worker 不同源** — 编排器强制校验（`judge != candidate`），保证视角低相关，防止同模型既写又审的系统性偏差
- **性价比档必须配质量门** — 省钱不能牺牲 K8 首次通过率；本地/便宜模型接入后必须开 `gate --check-regression`

**成本估算结论**：纯国际分级难以达到 Q3 的 $/pass ≤ $0.05（PRD 承认 K4 现状 ~$0.05-0.15）；国内分级轻松达标（~$0.008）但需质量门；**混合策略**（Sonnet 规划保质量 + DeepSeek 执行省成本，~$0.036/pass）是 P1 最优解。

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

## 产品 KPI（7 个 → 8 个）

| # | KPI | 当前 | Q3 目标 | 年度 |
|---|-----|------|---------|------|
| K1 | 任务成功率 | ~85% | ≥92% | ≥97% |
| K2 | 安全零事故 | ✅ 0 | 保持0 | 保持0 |
| K3 | 简单任务耗时 | ~3-5min | ≤3min | ≤1.5min |
| K4 | 单任务成本 | ~$0.05-0.15 | ≤$0.05 | ≤$0.03 |
| K5 | 中断恢复成功率 | 未知 | ≥99% | ≥99.9% |
| K6 | 可观测性可回答率 | 7/9 | 8/9 | 9/9 |
| K7 | 首次上手时间 | ~10-20min | ≤12min | ≤8min |
| K8 | **首次验证通过率** | ~60% | ≥80% | ≥90% |

## 北极星指标

**单位成本的验收通过率（$/pass rate）** — 将成本纳入质量指标，防止"不计成本堆质量"或"省钱产出垃圾"两种极端。

$$
$/passRate = \frac{单任务总成本}{任务成功率}
$$

当前 ~$0.06-0.18/pass，Q3 目标 ≤$0.05/pass，年度目标 ≤$0.03/pass。

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

## 基础设施化方向（评估中）

> **状态**：设计草案完成（见 [design/infrastructure-api-design.md](design/infrastructure-api-design.md)），待论证必要性和可行性后决定是否投入。

### 定位扩展

```
当前：agent_go = CLI 工具（用户手动输入命令 → 看终端输出）
目标：agent_go = 可编程开发基础设施（CI/CD / IDE / Git Hooks / 项目管理平台可调用）
```

### 新增能力全景

| 能力 | 说明 | 优先级 | 设计文档 |
|------|------|--------|---------|
| **Python API** | `run_task()` 返回结构化 `TaskResult`，替代 CLI 的 `None` 输出 | P0 | §3.1 |
| **CLI --json** | 所有子命令支持 JSON 输出，供外部脚本调用 | P0 | §3.4 |
| **事件总线** | 全生命周期 `emit_event` + `subscribe_event` + `events.jsonl` | P1 | §4 |
| **状态查询 API** | `query_task()` / `query_project_trend()` 统一查询入口 | P1 | §3.1 |
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

## 非目标用户

- 不用 Claude Code 的 — agent_go 是编排层，不是替代品
- 零技术背景的 — 输出是 git diff，需要能 review
- 单步骤任务的 — 裸 Claude Code 更快
