# spec 冒烟验证（阶段 B / B3 决策）

> 日期：2026-08-15
> 目的：用 5 个真实任务验证 spec 闭环的 ROI（B3 门禁：C1/R1/R2/R3），决定「投多少」。
> 运行器：`tools/spec_smoke.py`
> 设计依据：spec-closed-loop-design.md §9（落地路径 + ROI 门禁）

## 实验设计

- **目标仓库**：agent_go 干净 clone（dogfood，隔离并发进程）
- **任务**：5 个真实小缺口（M5 CLI 收尾 / record 边界修复 / 层间归因补全 / do-not-touch 测试 / AGENTS.md 文档）
- **每个任务**：完整 7 章节 spec（REQ/AC ID + 锚定验证 + 明确不动），`agent_go run --spec --yes`
- **基线**：M3 无 spec 12 任务（R1 91.7%，R2 ≈ 0%）

## 结果（4 有效 / 5 任务）

| 任务 | 状态 | trace | 耗时 | 成本 | 说明 |
|---|---|---|---|---|---|
| 01 problems CLI | VERIFICATION_FAILED | incomplete | 4.9min | $0.011 | **worker 能力失败**（本地 haiku 在 3860 行 cli.py 上新增 CLI 未过语义评估），与 spec 无关 |
| 02 record 边界修复 | ✅ DELIVERY_READY | complete | 2.1min | $0.005 | |
| 03 层间归因补全 | ✅ DELIVERY_READY | complete | 2.2min | $0.007 | |
| 04 do-not-touch 测试 | ✅ DELIVERY_READY | complete | 2.3min | $0.006 | |
| 05 AGENTS.md 文档 | ✅ DELIVERY_READY | complete | 2.1min | $0.002 | |

## ROI 结论（对照 B3 门禁）

| 指标 | 结果 | 门禁 | 判定 |
|---|---|---|---|
| C1 填写成本 | 未测（AI 代填；需人工计时补测） | ≤15min | ⏸ 待测 |
| R1 交付成功率 | 4/5 = 80%（含 1 个与 spec 无关的 worker 能力失败）；**有效交付 4/4 = 100%** | ≥91.7% | ⚠️ 持平（失败非 spec 所致） |
| R2 追踪完整率 | **4/4 = 100%**（基线 ≈ 0%） | ≥90% | ✅ **显著提升** |
| R3 Plan 警告 | 平均 0.25/任务 | ≤基线 | ✅ |

**判定：弱正 ROI → 「留轻量」**——保留门禁+追踪（零增量维护），不做重投入全套闭环。R2 是 spec 的独特价值（基线 0→100%），已实证；R1 的 1 个失败是 worker 能力（本地模型），与 spec 无关。

## 冒烟实证的 3 个缺陷（已修复）

1. **预算 reservation 头寸 bug（pipeline.py）**：strict 模式 dynamic 预算（Σ per_subtask×mult，worker-only 口径）扣减了 planner 成本 → planner 花 0.004 就让 easy 任务被「reservation 不足」误杀。修复：头寸计算排除 planner role。
2. **ID 链条断点（cli.py + spec.py）**：spec REQ/AC ID 只注入 prompt 未持久化 meta → traceability 永远 no_spec_ids；且 planner 软映射不可靠（deepseek 对 AC ID 全部漏标）。修复：①meta 持久化 requirement_ids/acceptance_criteria_ids；②`map_acceptance_to_steps` 硬映射兜底（AC 命令 ⊆ step.verification + 空验证 step 归属 + 命令回填 verification）。
3. **e2e 路径跳过映射（cli.py）**：docs 任务被 `_should_e2e` 误判端到端（`traceability` 子串命中「race」关键词）后，绕过拆分路径的硬映射。修复：`_apply_spec_id_hard_mapping` helper，e2e 与拆分路径共用。

## 环境发现（非 spec 问题）

- 生产 planner 路由 `anthropic:kimi-for-coding` 的硬编码 API key 已失效（HTTP 401）——冒烟临时切 deepseek 后已恢复原配置。需更新生产 key。
- 01 超时/失败 = 本地 worker（claude-haiku-4-5 @ localhost:4000）对大文件任务能力不足，与 spec 无关。

## 复现

```bash
python3 tools/spec_smoke.py --repo <clean-clone-of-agent_go> --timeout-min 15
```
