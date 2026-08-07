# K4 单任务成本指标重新校准

> 基线：2026-08-01 bench 实测（7 模型 × 22 标准任务 × 485 条记录）
> 对照：PRD v2.0.0 §产品 KPI + model-evaluation-and-tiering.md §1.5 成本估算

## 一、原始 K4 的推导链

PRD 当前 K4 目标（Q3 ≤$0.05 / 年度 ≤$0.03）的推导逻辑：

```
model-evaluation-and-tiering.md §1.5 成本估算
  → 假设典型任务结构：1 planner + 3 worker + 1 reviewer
  → 代入模型标准定价（$/1M tokens）
  → 估算各角色 token 消耗
  → 得出混合策略 $/pass ≈ $0.036
  → PRD 取整为 $0.05
```

推导依赖**两个关键前提**：

| 前提 | 假设 | bench 实测 | 偏差 |
|------|------|-----------|------|
| DeepSeek 可作为 Worker | pass_rate 可接受（成本估算未量化质量折损） | pass_rate 23-27%，完全不可用 | 前提不成立 |
| Token 消耗接近纸面估算 | 单 worker 子任务 ~5k-20k output tokens | agentic 任务的修复循环 + 长上下文放大 3-5x（1 个 easy 子任务实测 $0.125，hard 多子任务任务可达 $1-3） | 被低估 5-10x |

两个前提均被 bench 实测证伪。原始 K4 ≤$0.05 仍然是一个合理的**远期目标**（如果未来 DeepSeek 质量提升 + token 效率优化），但不适合作为 Q3 的**操作目标**。

## 二、Bench 实测的成本驱动因素

### 2.1 子任务数量是第一驱动力

以 Haiku 4.5 为基准：

| 子任务数 | avg_cost | pass_rate | $/pass | N |
|---------|----------|-----------|--------|---|
| 1 subtask | $0.21 | 97.4% | — | 39 |
| 2-3 subtasks | $0.36 | 87.8% | — | 48 |
| 4+ subtasks | $1.02 | 77.8% | — | 9 |

成本随子任务数**线性增长**（每个额外的 subtask 增加 $0.15-0.25），这是 agentic 任务的固有属性——多步骤任务就是比单步骤任务贵。

### 2.2 难度维度的成本分层

| 难度 | Haiku cost | Haiku pass_rate | Haiku $/pass | Claude 三模型均值 cost |
|------|-----------|----------------|-------------|---------------------|
| easy | $0.125 | 96.7% | $0.079 | $0.24 |
| medium | $0.176 | 74.3% | $0.131 | $0.30 |
| hard | $0.564 | 84.0% | $0.319 | $1.17 |

hard 任务成本是 easy 的 4.5 倍（Haiku）至 5 倍（Claude 均值）。

### 2.3 极端任务的放大效应

django-blog 两个端到端优化任务（db-performance-optimization / db-end-to-end-optimization）是成本分布的离群点：

| 任务 | 子任务数 | 典型耗时 | Claude cost 范围 |
|------|---------|---------|-----------------|
| db-performance-optimization | 4-7 | 900s | $1.0-2.5 |
| db-end-to-end-optimization | 7-11 | 1800s | $1.5-3.4 |

两个任务占总任务数的 9%（2/22），但占 hard 任务总成本的 ~70%。剥离后，Claude easy+medium 的 $/pass 从 $0.32 降至 $0.26。

### 2.4 不同模型的实际成本效率

| 模型 | all_cost | no-dblog_cost | easy_only_cost | easy_pass_rate |
|------|---------|--------------|---------------|---------------|
| claude-haiku-4-5 | $0.34 | $0.30 | $0.125 | 96.7% |
| claude-sonnet-4-6 | $0.85 | $0.69 | $0.36 | 90.0% |
| claude-opus-4-7 | $1.02 | $0.92 | $0.49 | 90.0% |

Haiku 在所有维度上成本最低，且 easy 任务 pass_rate 最高（96.7%）。

### 2.5 优化路由策略的可达边界

以当前最优模式（easy→Haiku / medium→Opus / hard→Opus）估算：

| 路由 | 任务数 | pass_rate | cost/task | $/pass |
|------|--------|-----------|-----------|--------|
| easy→Haiku | 30 | 96.7% | $0.125 | $0.079 |
| medium→Opus | 24 | 85.4% | $0.601 | $0.402 |
| hard→Opus | 45 | 87.2% | $1.594 | $0.716 |
| **加权平均** | **99** | **89.6%** | **$0.908** | **$0.402** |

这种"easy 省钱、hard 保质量"的路由策略可以提升 pass_rate（82.6% → 89.6%），但**成本反而上升**（$0.54 → $0.91）——因为 hard 任务被路由到更贵的 Opus。

相反的策略：**Haiku 全任务**（不分难度全用 Haiku）是当前成本最优解：

| 策略 | pass_rate | cost/task | $/pass |
|------|-----------|-----------|--------|
| Haiku 全任务 | 85.5% | $0.344 | $0.202 |
| 优化路由（Haiku/Sonnet/Opus 分级） | 89.6% | $0.908 | $0.402 |

**结论**：在当前模型能力下，**成本最低的策略是好模型统一用、不分级**。角色路由的"省钱"效果（用便宜模型做简单任务）被 hard 任务强制升级到贵模型的成本完全抵消。

## 三、建议的分级 K4 校准

### 3.1 核心原则

K4 不能是单一数值，必须按任务复杂度分级。理由：
1. 子任务数是成本的第一驱动力，这是任务的固有复杂度，不是产品能消除的。
2. 单一 K4 会导致"不做 hard 任务"的激励——这与 PRD"处理复杂多步骤任务"的定位矛盾。

### 3.2 校准后的 K4

以 Haiku 4.5 实测为基线，考虑 KnowledgeStore + Plan 质量提升（预计 20-30% 成本降幅），设定 Q3 目标：

| 指标 | 当前（Haiku 实测） | Q3 目标 | 年度目标 | 改善杠杆 |
|------|-------------------|---------|---------|---------|
| **K4-easy**（≤2 subtasks） | $0.21 | **≤$0.15** | **≤$0.10** | 当前 easy-only 已 $0.125，Plan 质量小幅优化即可 |
| **K4-medium**（3-5 subtasks） | $0.36 | **≤$0.25** | **≤$0.18** | KnowledgeStore 减少无效重试（当前 medium pass_rate 74% 是主要改善空间） |
| **K4-hard**（6+ subtasks） | $1.02 | **≤$0.70** | **≤$0.45** | 角色路由（hard→强模型）+ Plan 分解质量提升 + KnowledgeStore |
| **K4-blended**（按 bench 任务分布加权） | $0.34 | **≤$0.25** | **≤$0.18** | 以上三项加权 |

**改善杠杆详解**：

- **K4-easy 的改善空间小（15%）**：easy 任务已经接近最优点——Haiku 96.7% pass_rate、$0.125 cost、116s latency，几乎没有浪费。进一步降本需要模型降价或 token 效率革命。
- **K4-medium 的改善空间大（30%）**：当前 medium pass_rate 仅 74.3%，远低于 easy（96.7%）和 hard（84.0%）。这说明 medium 任务不是"更难"而是"Plan 质量差"——Planner 对 medium 任务的分解策略不如 easy（模板化）和 hard（投入更多规划 token）精确。KnowledgeStore 记录最优分解策略后，pass_rate 提升 + 重试减少可降本 25-30%。
- **K4-hard 的改善空间居中（30%）**：hard 任务的高成本主要来自多子任务（每增一个 sub +$0.15-0.25）和长耗时（django-blog 1800s）。改善路径是 Plan 阶段合并可并行子任务 + KnowledgeStore 注入历史最优验证命令，减少修复循环的 token 消耗。

### 3.3 北极星 $/pass 同步校准

| 指标 | 当前（Haiku 实测） | Q3 目标 | 年度目标 |
|------|-------------------|---------|---------|
| $/pass-easy | $0.079 | **≤$0.06** | **≤$0.04** |
| $/pass-medium | $0.131 | **≤$0.10** | **≤$0.07** |
| $/pass-hard | $0.319 | **≤$0.22** | **≤$0.15** |
| **$/pass-blended** | **$0.202** | **≤$0.14** | **≤$0.10** |

### 3.4 与原始目标的差异解释

| 指标 | 原始 Q3 | 校准后 Q3 | 差异 | 原因 |
|------|---------|----------|------|------|
| K4 | ≤$0.05 | ≤$0.25（blended） | 5x | 原始基于纸面估算（假设 DeepSeek 可行 + token 消耗接近定价表）；校准基于 485 条 bench 实测 |
| $/pass | ≤$0.05 | ≤$0.14（blended） | 2.8x | 同上 + easy $/pass 已经 $0.08（接近原始目标的 1.6x，说明 easy 场景的原始估计相对准确） |

原始 ≤$0.05 的两个前提均被 bench 证伪：
- **DeepSeek 作为 Worker 不可行**（实测 23-27% pass_rate——远低于假阳性 >20% 禁用红线，且失败任务仍然产生 full cost）
- **Token 消耗被低估 5-10x**（纸面估计 1 planner + 3 worker ~50k tokens，实际 agentic 任务常因修复循环 + 长上下文达到 150k-500k tokens）

校准后 blended $/pass = $0.14 约为原始的 2.8x。easy 场景的 $/pass（$0.08）最接近原始估计，说明原始估算对简单任务相对准确——问题在于**全任务混合后，hard/medium 任务主导了加权均值**。

## 四、对产品迭代的影响

### 4.1 不受影响的部分

- **K8（首次通过率）未变**：bench 实测 91.2%（Claude 均值），已超额完成 Q3 ≥80%。这是验证循环设计有效性的直接证据。
- **K3（简单任务耗时）未变**：116s vs 180s 目标，已达标。Haiku easy 任务 latency 无瓶颈。
- **K1 目标保持**：≥92% 的 pass_rate 仍需要通过 Plan 质量提升 + KnowledgeStore 来实现，K4 校准不改变 K1 的难度。

### 4.2 需要调整的部分

- **PRD 的 KPI 表**（§产品 KPI）：更新 K4 行，补充分级维度和校准后的目标值。
- **model-evaluation-and-tiering.md §1.5 成本估算**：标记原始估算的前提已被 bench 证伪，引用本文档的校准结果。
- **roadmap S6 KPI 基线采集**：验收门禁从"校验 Q3 出关口径可达性"调整为"以本文档校准后的目标为基准，验证 K1/K4/K8 的改善趋势"。
- **eval gate 门禁阈值**：`eval gate --baseline` 的默认 $/pass 阈值从 $0.05 调整为 $0.14（blended），可按 `--difficulty easy/medium/hard` 指定分档阈值。

### 4.3 对混合策略路径的影响

bench 数据明确了一个选择：**当前条件下，"Haiku 全任务"是兼顾成本和质量的 Pareto 最优点，而非"Sonnet 规划 + DeepSeek 执行"的混合策略。**

```
                        成本低 ↔ 成本高
                 DeepSeek    Haiku    Sonnet    Opus
                      ↓         ↓        ↓        ↓
  pass_rate  23% ←——→ 85% ←→ 83% ←→ 88%
  $/pass    $0.25   $0.20   $0.50   $0.54
```

未来混合策略可行的前提（至少满足一项）：
- DeepSeek / 国内模型的 pass_rate 达到 ≥75%（需要模型能力代际提升）
- 更便宜的模型在 easy 任务上验证 pass_rate ≥90% 且 cost < $0.05
- KnowledgeStore 使 medium/hard 任务的重试次数减少 50%+（降低 token 浪费）

在这些前提满足前，**简单就是最优：Haiku 4.5 全任务，不做角色分级路由**。

## 五、数据来源与方法

- **数据集**：`eval_suite/` 下 5 个结果文件（results.jsonl / results_v2.jsonl / baseline.jsonl / final-baseline.jsonl / kimi-baseline.jsonl），共 485 条 bench 记录
- **覆盖**：7 模型（claude-haiku-4-5 / claude-sonnet-4-6 / claude-opus-4-7 / opus-4-7 / deepseek-v4-flash / deepseek-v4-pro / kimi-for-coding-highspeed）× 22 标准任务 × 3 难度（easy/medium/hard）
- **成本口径**：所有 cost_usd 来自 metering.jsonl 的真实账单（非定价表估算）；dollar_per_pass = total_cost_usd / verified_passes（当次运行的精确值，非 post-hoc 折算）
- **分析工具**：Python stdlib + lark-cli base（飞书多维表格 + 仪表盘）
- **关联文档**：[bench 对比仪表盘](https://my.feishu.cn/base/CObqbC6iLa00ZGs1ex7cwaWHnwc) · [KPI 达成分析](https://my.feishu.cn/docx/HFZpd6l0Wo27pJxgRpKc5pvfnzd) · [PRD v2.0.0](../prd.md) · [模型评估设计](model-evaluation-and-tiering.md)
