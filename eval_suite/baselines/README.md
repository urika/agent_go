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

## 收敛流程

```text
smoke -> Golden Tasks -> 代表性分层任务 -> decision baseline
```

见 `docs/design/adr/ADR-009-bench-convergence.md` 和 `docs/design/bench-convergence-plan.md`。
