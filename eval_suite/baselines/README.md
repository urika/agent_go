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
| `m4-mixed-hard-goal` | **有效（混合路由触发条件验证基线）** | 同 6 个 hard 任务，`--hard-model claude-opus-4-7`（代理 force_fallback→云端 deepseek-v4-flash）：**仍 0/6，且 opus-4-7 从未触发**——per_subtask 显示 **0 个 difficulty=hard 子任务**（本地 planner 拆解时全标 easy/medium）。**关键发现：单纯 `--hard-model` 无效**，瓶颈是 planner 的子任务难度标注质量，不是 worker 模型；混合路由触发链 `子任务 hard → worker_models.hard → opus-4-7` 依赖准确的难度标注，而本地 planner 倾向保守标注。验证结论：要让 hard 走云端，须强制 worker 全用强模型（不分难度）或升级 planner |
| `m4-e2e-hard-pro-v2` | **有效（e2e+云端 v4-pro 基线）** | 同 6 个 hard 任务，e2e 端到端 + planner/evaluator 云端 deepseek-v4-pro（修复代理 thinking/response_format 透传缺陷后）：**3/6 通过（50%）**——add-tag、race-condition、conditional-branching 通过。对比：本地 0/6 → e2e(v4-flash) 2/6 → e2e+云端 v4-pro 3/6。关键修复：代理 FormatConverter/convert_openai_request_to_anthropic 不透传 thinking/response_format 导致 v4-pro 空响应/evaluator 误判，修复后 evaluator 正常评估（conditional-branching 假阳性消除）。剩余失败：stage-validation（真代码缺陷）、security-hardening、db-performance |
| `m4-e2e-hard-goal` | **有效（端到端模式 hard 突破基线）** | 同 6 个 hard 任务，**e2e 端到端模式**（difficulty=hard 自动触发，不拆分子任务保留全局上下文，worker 用 opus-4-7→云端 deepseek-v4-flash）：**2/6 通过（33%）**——add-tag、race-condition 通过（race-condition 此前拆分模式连续失败 3 次），其余 4 个 verification_failure。对比本地 0/6、混合 v2 0/6：**端到端模式实现 hard 从 0 到有的突破**。证明 hard 失败主因是 Plan→拆分丢失全局上下文，非模型能力；端到端是 hard 的有效路径。详见 feat(e2e) e283184 |
| `m4-portal-local-qwen35b` | **有效（本地门户任务基线）** | 本地 Qwen3.6-35B 企业门户新闻中心任务：1/1（1.000）。$/pass=$0（早期 27B 时代无 metering 数据，无法折算 TCO） |

> **本地模型基线说明**：6 个 m4-local-* 批次均为 `claude-sonnet-4-6` 路由名 → 本地 Qwen3.6-35B（worker_backends 指向 localhost:4000）。$/pass 已按 `config.local_model_cost` TCO 口径折算（电费+硬件折旧），非免费。对比口径：云端 M3 同任务集 pass_rate 0.917、$/pass $0.0185。能力梯度：medium 0.792（goal）> 非 goal 0.600 > hard 0/6。

## 收敛流程

```text
smoke -> Golden Tasks -> 代表性分层任务 -> decision baseline
```

见 `docs/design/adr/ADR-009-bench-convergence.md` 和 `docs/design/bench-convergence-plan.md`。
