# agent_go 智能化 Harness 与驱动架构（讨论输入）

> 状态：讨论稿（Draft）
> 日期：2026-08-17
> 目的：作为「agent_go 的智能化 harness 能力 + 如何驱动分析与决策」的结构化讨论输入
> 关联：[model-eval-methodology.md](model-eval-methodology.md)、[model-selection-report.md](model-selection-report.md)、[model-entity-config-design.md](model-entity-config-design.md)、`mcp_server.py`、`task_runner.py`

---

## 1. 智能化 Harness 能力盘点（agent_go 已实现的六类机制）

Harness = 让 LLM 可靠、可测、可控地完成工程任务的"框架与缰绳"。agent_go 已实现六类：

| 能力域 | 具体机制 | 智能化体现 |
|--------|---------|-----------|
| **1. 任务编排** | Plan 生成 → 确认通道（CLI/web 文件协议）→ 拆解（难度标注 + e2e 判定）→ 拓扑波次调度 | 把模糊需求结构化为可并行、有依赖、可验收的执行计划 |
| **2. 执行隔离与上下文** | worktree 隔离、上游产物 tag merge 传递、TASK.md 上下文注入（文件清单/验证/风险/共享资源/do-not-touch） | 控制模型"能看到什么、能改什么"，防越界、保全局一致 |
| **3. 验证与自愈** | 验证命令多轮修复循环、goal 死守循环、evaluator 语义评估 + 低置信度仲裁、失败四分类 | 模型产出被"验收裁判"反复检验，缺陷自动修复或留证据 |
| **4. 模型路由与降级** | 难度/角色路由、多级降级链（K3→GLM→v4-pro→local）、registry 声明式 thinking/JSON | 按任务难度选模型、故障自动切换、推理特性自动适配 |
| **5. 度量与归因** | metering（tokens/cost/latency/route_target）、plan_quality 门禁、R8 路由归因纠偏 | 每次 LLM 调用、每个交付决策都有量化记录，成本真实 |
| **6. 交付治理** | delivery branch、mergeability 预检、review 审批（approve/reject/changes）、Spec 合规 | 交付有显式门禁、可追溯、可回滚 |

## 2. 日志/存储底座（证据收集内建）

**每任务** `~/.agent_go/<task_id>/`：

| 文件 | 支撑的分析 |
|------|-----------|
| `meta.json` | 状态机（8 态）/子任务/结果/计划质量/审批/交付分支 |
| `execution.log` | 结构化事件时间线（可 replay 可视化） |
| `metering.jsonl` | 每调用成本/模型/路由归因（R8 修正） |
| `PLAN.md` + `plans/` | 计划版本历史（plan-diff） |
| `assessment.jsonl` / `deviation.jsonl` | 评估事件 / Spec 偏差 |
| `review.json` / 保留 worktree | 审批决策 / 失败现场复核 |

**全局**：`baselines/`（immutable 批次：manifest/results/summary）、`web_audit.jsonl`（操作审计）、`kanban.json`（看板）。

**失败归因链**：失败 → failure_class(4类) → failure_reason → evaluator reason → retry history → worktree 现场 → 计划质量拦截原因 → 层间归因（修 spec/修 plan/调预算/换模型/环境漂移）。

## 3. 驱动机制：agent_go 是"被驱动体"

agent_go **不重复造 LLM 思考的轮子**——高层决策（选模型、改配置、解读基线）由外部 agent（如 opencode）提供，但**驱动路径已标准化**：

### 3.1 三条驱动通道

| 通道 | 形式 | 适用 |
|------|------|------|
| **CLI** | `agent_go run/resume/review/merge/config/models/report/analyze...` | 人/agent 脚本驱动 |
| **Web API** | HTTP JSON + token 角色（admin/viewer）+ 审计 | 浏览器/自动化 |
| **MCP server** | 7 工具（run_task/resume_task/inspect/review/...），任意 MCP 客户端可消费 | agent 生态（Claude Code/opencode 等） |
| （MCP client 反向） | worker 子任务可调外部 MCP 工具（`mcp__{server}__{tool}`） | 执行期工具扩展 |

### 3.2 决策分层

```
任务内闭环（微决策）—— agent_go 自主：
  验证循环修复 / goal 死守 / evaluator 仲裁 / 降级链切换 / plan 门禁 / 成本熔断 / 锁互斥
  特点：确定性的、可自愈的、无需外部 LLM 抽象推理

任务间策略（宏观决策）—— 外部 agent（当前人肉对话式）：
  读证据(baselines/metering) → 判断(选模型/改配置/定策略) → 改配置 → 重测
  特点：需要 LLM 抽象推理（跨任务规律、权衡取舍）
```

**关键洞察**：
- **证据收集已内建**（每任务 7+ 文件 + immutable 基准）
- **任务内微决策已内建**（自愈闭环，无需外部驱动）
- **任务间策略决策未标准化**——当前靠"人肉对话式驱动"（opencode 找数据→判断→改配置），驱动成本高且不可复现

## 4. 增强方向：策略决策命令族（把宏观决策标准化）

| 命令 | 功能 | 收益 |
|------|------|------|
| `agent_go analyze bench --results X` | 自动失败模式聚合（failure_class × 模型 × 任务类型） | 从"人找规律"到"机器给规律" |
| `agent_go analyze task <id>` | 单任务诊断（归因链一页纸） | 快速定位失败原因 |
| `agent_go recommend [--results X]` | 模型/配置推荐（基于 baselines + 规则） | 决策输入自动化 |
| `agent_go decision log` | 统一决策记录（为何改配置/换模型，可复盘、可审计） | 决策可复现、团队共享上下文 |

**演进路径**：
```
人肉驱动（现状，opencode 对话）
  ↓
自动化决策输入（analyze/recommend/decision log）—— 机器提供证据与建议，人/agent 做最终判断
  ↓
半自主（部分已有：eval gate --check-regression 自动拦回归；成本熔断自动停）
  ↓
（远期）任务级自治（harness 内闭环 + 外部策略 API）
```

## 4.5 目标驱动 LLM 分析器（策略建议的升级形态）

**可以**：基于「目标 + 预设计划 + 证据」让 LLM 给出优化建议与下一步行动。这是策略决策命令族的增强：

```
输入：
  目标 --goal        （人预置的业务目标，如"hard 通过率 ≥95% 且 $/pass ≤ $0.1"）
  预设计划           （行动候选：换模型/降级链/难道路由/e2e 判定阈值…）
  证据               （强制 --results：baselines/metering/失败归因）
      ↓ LLM 推理（仅做分析，不执行）
输出：
  优化建议           （问题 → 证据 → 动作 → 预期影响）
  下一步             （排序后的可执行清单）
```

**三个关键设计约束（防 LLM 越界）**：

| 约束 | 目的 |
|------|------|
| 1. **目标由人预置**（--goal 输入） | LLM 不改目标 —— 避免目标漂移/自我设定目标 |
| 2. **建议层不自动执行**（输出建议由 agent/人确认） | 防自我漂移/循环恶化（LLM 反复改配置导致震荡） |
| 3. **证据先验绑定**（强制 --results，缺证据拒答） | LLM 只能基于真实数据推理，不凭空编造 |

**与现有机制的同构呼应**（证明这是自然延伸而非新范式）：

| 现有机制 | 模式 |
|---------|------|
| eval gate --check-regression | 机器判定 + 建议 = 半自主（已落地） |
| 成本熔断（cost_control） | 机器执行微决策（已落地） |
| evaluator 低置信度仲裁 | 任务内 LLM 判断（已落地） |
| 降级链（fallbacks） | 故障自动切换（已落地） |
| **analyze / recommend（新增）** | 把宏观策略决策纳入同一模式：**证据内建 + 判定半自主 + 人机确认** |

**落地优先级**：`analyze bench`（失败模式+建议，数据已有最快见效）> `decision log`（可审计）> `recommend`（规则+LLM 混合）。

**演进本质**：agent_go 从"被驱动体" → "提供可决策证据与建议、外部只做确认"的**建议体**。

## 5. 讨论问题（供决策）

1. **优先级**：策略决策命令族（analyze/recommend/decision log）哪个最先做？建议 `analyze bench` + `decision log`（数据已有，快速见效，可直接支撑下一次模型选型）
2. **边界**：agent_go 应做到"决策建议"还是"决策执行"？（建议：建议层——改配置/换模型仍由外部 agent/人确认，避免 harness 自我漂移）
3. **trace 与决策日志关系**：是否把 decision log 与端到端 trace（任务→LLM 调用→验证→交付）合为一个统一链路 ID？
4. **消费方**：analyze/recommend 的输出供谁消费？human 看报告 / agent 读结构化 JSON 接决策？

## 6. 已知观测缺口（Harness 可观测性反面）

| 缺口 | 影响 | 建议 |
|------|------|------|
| 端到端 trace 未串起（meta/metering/log 分散） | 完整链路复盘要拼接 | 统一 trace_id + 事件总线 |
| claude 完整对话不落盘 | 无法复盘"模型当时看到什么/做了什么" | 会话 tool-call 轨迹落盘（可选，成本可控） |
| 决策记录分散 | 为何改配置/换模型不可追溯 | decision log |
| worker 路由归因近似（claude -p 拿不到 R8 头） | worker 实际路由依赖探测 | 代理会话级路由记录查询 |
