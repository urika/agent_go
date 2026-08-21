# Bench 修正基线（S12-P0 口径）

> 日期：2026-08-06
> 口径：S12-P0 修正后（cleanup_race 计通过、binary_pass 修 all([])+时序、kill_reason 分类）
> 产生方式：`eval_suite/recompute_corrected.py` 对历史 v2/v3/v4 数据离线重算
> 关联：[bench-metric-validity-2026-08-06.md](bench-metric-validity-2026-08-06.md)、[timeout-kill-strategy-2026-08-06.md](../../design/timeout-kill-strategy-2026-08-06.md)

> **数据状态**：历史 exploratory 分析。本文的 K1/K4/K8 和 `$ / pass` 数值不作为当前产品 KPI；当前以 Accepted Delivery、Cost per Accepted Delivery 和 M0 Metric Freeze 报告为准。

## ⚠️ 基线性质

这是**修正口径下的"历史数据重估"**，不是"用修复后的采集器重跑的权威基线"。原因：

- v2/v3/v4 的原始记录是用**旧（有 bug）采集器**收集的；本文数字是对其聚合结果套用修正逻辑得到的**重估值**。
- **权威基线**须用 S12-P0 已修复的 `_collect_result`（已落地）**重新跑一轮** bench 后冻结。重跑前，本表是当前最佳估计。
- **v2 不可修正**（0/198 有 per_subtask）——它是旧采集器，与 v3/v4 不同 instrument，已从基线候选中剔除。

PRD/roadmap 的 KPI 表值暂不替换为本表数字（待重跑），但本表是"KPI 真实值最接近的估计"，引用时以本表为准。

## 修正后基线（v3 = 全 22 任务 × 3 模型 × 3 重复 = 198，canonical）

| 模型 | 修正通过率 | headline 通过率 | Δ | 修正 $/pass | headline $/pass |
|------|-----------|----------------|---|------------|-----------------|
| Sonnet-4-6 | **70%** | 35% | +35pp | **$0.028** | $0.061 |
| Haiku-4-5 | **68%** | 36% | +32pp | **$0.034** | $0.067 |
| Opus-4-7 | **64%** | 32% | +32pp | **$0.101** | $0.228 |
| **合计** | **67%** | 34% | +33pp | **$0.053** | $0.112 |

**kill_reason 分布（v3 全样本）**：none 68 / cleanup_race 65 / stuck_or_hardtimeout 37 / infra 20 / interrupted 8。
→ 能力失败（stuck 类）≈ 37/198 = 19%；假失败（cleanup_race）65 条已剔除；infra 20 条单列。

## v4_calib 修正后（6 任务子集，仅作校准参照）

| 模型 | 修正通过率 | 修正 $/pass |
|------|-----------|-------------|
| Sonnet-4-6 | 94% | $0.052 |
| Haiku-4-5 | 83% | $0.048 |
| Opus-4-7 | 78% | $0.118 |
| **合计** | **85%** | **$0.072** |

kill_reason：none 43 / cleanup_race 3 / infra 3 / interrupted 5（校准后假失败已大幅减少——印证 v4 的 timeout 校准本身有效）。

## 关键解读（对 PRD KPI 的影响）

1. **K1 任务成功率**：v3 修正后 ~67%（v4 校准集 85%）。原 PRD 基线 K1=83.9%（Bench v1）与新口径不可比；真实值需重跑后定。
2. **K4/$/pass 北极星**：v3 修正后 $0.053、v4 $0.072。注意 v3（全任务集）反而比 v4（6 任务子集）$/pass 更低——因子集含更难的校准任务。**距 PRD Q3 目标 ≤$0.05 已非常接近**（v3 $0.053），不再是"差 7-14x"。
3. **模型排序稳健**：修正后 Sonnet ≥ Haiku > Opus 在 v3/v4 一致；**Opus 在所有口径下被支配**（$/pass 是 Sonnet 的 3-4x，通过率更低）。
4. **v4"大幅提升"部分是回收假象**：v3 修正 $0.053 ≈ v4 $0.072——v3 实际没那么差，v4 的 timeout 校准主要消掉了 cleanup_race（65→3），而非提升真实能力。

## 待办（冻结权威基线）

- [ ] 用 S12-P0 修复后的 `_collect_result` 重跑一轮全因子 bench（22 任务 × 3 模型 × 3 重复）
- [ ] 重跑结果与本重估值交叉核对（偏差应 <5pp）
- [ ] 核对通过后，冻结为权威基线，回写 PRD §产品 KPI 表 + roadmap 出关口径
- [ ] 重定 K4 目标：废弃单一 $0.05 虚目标，按难度分档（easy/medium/hard）设成本带
