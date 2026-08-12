# Immutable Baselines

每个固定批次使用独立目录：

```text
<source_batch>/
  manifest.json
  results.jsonl
  summary.json
```

`manifest.json` 中的 `results_sha256`、schema version、task catalog hash、suite 和
source batch 用于防止跨批次或跨 schema 直接合并。

## 数据治理规则

- 基线不可修改（immutable）。需要新口径时生成新的 source batch，不改写历史。
- **exploratory 基线**（历史分析口径，违反当前门禁）不得作为正式决策依据：

| source_batch | 状态 | 原因 |
|---|---|---|
| `m0-decision-20260808` | **exploratory（历史）** | M1 前旧口径：72/96 条 `accepted_delivery=true` 但无 delivery branch/PR，违反 ADR-009 收敛门禁；全部记录 `status=None`（旧 schema 无任务状态）。仅作历史参考，不与新 batch 合并 |
| `m0-smoke-20260808` | **有效（baseline）** | M1 前生成但 `accepted_delivery` 判定正确（全 False）；`failure_class` 完整。作为 M1 前 smoke 基线 |
| `decision-20260809` | **exploratory（历史）** | M1 前旧批次（16 任务 / 48 records，`accepted_delivery_count=0`、`pr_creation_rate=0.0`，无真实交付闭环）。M0 正式基线冻结前，不能用其作为决策依据 |
| `golden-20260812` | **有效（golden 验证基线）** | M0 冻结前置验证：6 固定 Golden 任务 × 2 重复 = 12 records（deepseek-v4-flash, suite=golden），通过率 88% ≥ 80%。gate 建立基线 $0.023475/次。作为 decision-20260812 的前置冒烟验证 |
| `decision-20260812` | **有效（M0 正式基线）** | M0 决策基准批次：35 任务 / 35 records（deepseek-v4-flash, repeat=1, canonical），S12-P0 口径采集。经 golden-20260812 冒烟验证后跑全量，`eval gate` 通过（$/pass 较 golden 基线劣化 4.7% < 10% 容差）。`pass_rate_diagnostic=0.924`、`first_pass_rate=0.864`。M0 据此 accepted |
| `m3-dogfood-20260812` | **有效（M3 真实仓库基线）** | M3 真实任务验证：12 任务 × 2 真实仓库（vibe-astock / llama-defender）× 6 类场景，通过率 91.7%（11/12），总成本 $0.20（$0.017/任务）。此批次首次在**非 fixture 真实仓库**（多 commit 历史）上运行，发现并修复 evaluator `_get_worktree_diff` 累积基座缺陷（`root..HEAD` → `_base_commit`）。M3 据此 accepted |

## 收敛流程

```text
smoke -> Golden Tasks -> 代表性分层任务 -> decision baseline
```

见 `docs/design/adr/ADR-009-bench-convergence.md` 和 `docs/design/bench-convergence-plan.md`。
