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
| `m4-local-full-goal` | **有效（本地模型基线）** | 本地 Qwen3.6-35B（unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit）完整 12 任务集 × goal force：9/12 通过（0.792），$/pass=$0.000842（**TCO 口径**，比云端 deepseek-v4-flash $0.0185 便宜 ~22 倍）。数据驱动决策依据：本地模型可作为执行主力候选，复杂任务建议配 goal |
| `m4-local-sample` | **有效（本地模型对比基线）** | 本地 Qwen3.6-35B 非 goal 5 任务子集：3/5（0.600）——无 goal 时完成率明显低于 goal 模式（0.792 vs 0.600），证明 goal 循环对本地模型提升显著。$/pass=$0.0005 |
| `m4-local-goal` | **有效（本地模型 goal 复验基线）** | 本地 Qwen3.6-35B + goal 重跑 2 个普通模式失败案例：2/2（1.000）——goal 模式能补齐本地模型初始不足。$/pass=$0.0005 |
| `m4-local-2refactor` | **有效（本地重构对比基线）** | 本地 Qwen3.6-35B + goal 2 个跨文件重构任务：1/2（0.750）——ld-refactor PASS，va-refactor FAIL（本地重写 tx_symbol 映射语义与原实现不一致，被 evaluator 捕获）。$/pass=$0.001333 |
| `m4-local-hard-goal` | **有效（本地 hard 能力边界基线）** | 本地 Qwen3.6-35B + goal 6 个 canonical hard 任务（跨 task-mgr/data-pipeline/django-blog）：**0/6 通过**（5 个 capability_failure + 1 infrastructure_failure）——失败模式为复杂代码产出不稳定（未写文件/越界）与超时。证明 **hard 难度（功能系统级）超出 35B 本地模型能力边界**：本地适用 medium 及以下（0.792），hard 需 ≥70B 模型或混合路由（hard → 云端）。$/pass=$0（全失败无有效 pass） |
| `m4-portal-local-qwen35b` | **有效（本地门户任务基线）** | 本地 Qwen3.6-35B 企业门户新闻中心任务：1/1（1.000）。$/pass=$0（早期 27B 时代无 metering 数据，无法折算 TCO） |

> **本地模型基线说明**：6 个 m4-local-* 批次均为 `claude-sonnet-4-6` 路由名 → 本地 Qwen3.6-35B（worker_backends 指向 localhost:4000）。$/pass 已按 `config.local_model_cost` TCO 口径折算（电费+硬件折旧），非免费。对比口径：云端 M3 同任务集 pass_rate 0.917、$/pass $0.0185。能力梯度：medium 0.792（goal）> 非 goal 0.600 > hard 0/6。

## 收敛流程

```text
smoke -> Golden Tasks -> 代表性分层任务 -> decision baseline
```

见 `docs/design/adr/ADR-009-bench-convergence.md` 和 `docs/design/bench-convergence-plan.md`。
