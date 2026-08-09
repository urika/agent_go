# Bench 收敛落地计划

## 阶段 A：数据和状态清理

目标：让失败数据可解释。

- 保留历史结果为 exploratory，不与新 batch 合并。
- 运行 metadata migration 的 dry-run 和备份迁移。
- 修复 task/subtask `failure_class` 缺失。
- 修复 blocked root cause 传播。
- 复核 `accepted_delivery` 必须有 delivery branch/PR。
- 将历史 crash-but-verified 标记为 historical evidence，不伪装为新成功。

验收：新失败记录的 `failure_class`、状态和根因完整；没有错误 Accepted Delivery。

## 阶段 B：验证命令和 Plan 收敛

目标：避免任务进入执行后才发现验证命令或 Plan 不可执行。

- Plan 阶段预检 verification command。
- 命令拒绝在执行前阻断，并归类为 `infrastructure_failure`。
- 检查 `files`、`scope_boundary`、`do_not_touch` 一致性。
- 检查依赖循环。
- 记录 requirement/acceptance coverage。
- 记录 `plan_quality_status`、warning 和 conflict 数量。

验收：收敛批次中不再出现未预检的 rejected verification command；Plan quality 字段完整。

> **执行状态（2026-08-09）**：
> - ✅ scope_conflict 误报修复：`files_hint` 不再并入 `files`，只检查修改文件 ∩ do_not_touch
> - ✅ ISSUE-29 python -c 单行语法预检：`compile()` 拦截 try/except 等非法拼接（计划阶段）
> - ✅ verification_command_rejected 归类 `infrastructure_failure`（failure.py 已处理，不计模型能力失败）
> - ⚠️ "命令被白名单拒绝后跳过 retry" 未采用——compile 预检已拦截不可修复命令；对可修复命令保留 retry 让 Claude 修复
> - 详细记录见 [plan-capability-phaseb-2026-08-09.md](plan-capability-phaseb-2026-08-09.md)

## 阶段 C：Golden Tasks

固定 6 个任务：

```text
easy:
  add-format-helper
  fix-missing-default
medium:
  implement-done-command
  add-simple-caching
hard:
  security-hardening-taskmgr
  conditional-branching-datapipeline
```

运行策略：一个低成本模型、`repeat=3`、`bench-parallel=1`。每个任务失败后先分类和复现，再修一个根因，不同时修改 Planner、Worker、Verifier 和指标。

> **执行状态（2026-08-09）**：
> - 运行：`agent_go eval bench --tasks eval_suite/golden_tasks/ --candidate-models deepseek-v4-flash --repeat 3 --bench-parallel 1 --source-batch golden-20260809`
> - 结果：18 条记录（6 任务 × 3 repeat），基线在 `eval_suite/baselines/golden-20260809/`（results.jsonl + manifest.json + summary.json）
> - **可重复性**：easy 任务（add-format-helper / fix-missing-default）和 add-simple-caching 稳定 3/3 通过；implement-done-command 1/3、conditional-branching 2/3、security-hardening 0/3（均为 verification_failure，原因稳定可解释）
> - pass_rate_diagnostic=0.958，valid_cost_usd=$0.18，timeout_rate=0
> - **结论**：系统逻辑可重复；失败集中在 hard/high_variance 任务且原因一致（verification_failure），符合 ADR-009"失败原因稳定可解释"门禁
>
> **阶段 C Review 修复（2026-08-09）**：
> - **P0 分类缺口**：`classify_failure` 在 `status in {completed, no_changes}` 时直接返回 None，未检查语义评估失败 → implement-done-command r1 / conditional-branching r1 的 `binary_pass=false` 但 `failure_class=null`。
> - **修复**：`failure.py` 在 completed/no_changes 分支检查 `verification_results` 中 `type=="semantic" && passed is False` → 返回 `verification_failure`。提交 609b94a，全量 1967 测试通过。
> - **子集重验**：重跑 implement-done-command + conditional-branching + security-hardening（3 任务 × 3 repeat = 9 条），基线 `eval_suite/baselines/golden-subset-20260809/`。**0/6 失败缺 failure class**（全部 verification_failure），确认修复生效。
> - **P1 记录**：implement-done-command 跨 repeat 不稳定（语义验证时好时坏）；security-hardening 对 deepseek-v4-flash 超出模型能力（应标记为模型局限性，不用于判断产品整体通过率）。

## 阶段 D：代表性实验

使用 7 个 smoke 加 2 个 medium 和 2 个 hard，比较 Plan/Verifier 改动前后：

- `binary_pass`
- `verification_failure`
- `timeout_rate`
- `retry_rate`
- `plan_requirement_coverage`
- `plan_conflict_count`
- `Accepted Delivery`
- `Cost per Accepted Delivery`

## 阶段 E：正式 baseline

只有阶段 A-D 通过后，才运行固定 decision baseline：

```bash
agent_go eval bench \
  --tasks eval_suite \
  --suite decision \
  --candidate-models <fixed-models> \
  --repeat 3 \
  --bench-parallel 1 \
  --source-batch <immutable-batch> \
  --output eval_suite/baselines/<immutable-batch>/results.jsonl
```

随后生成并校验：

```bash
agent_go eval validate-schema --results <results.jsonl>
agent_go eval metric-freeze --results <results.jsonl> --source-batch <batch> --suite decision
agent_go eval batch-manifest --results <results.jsonl> --source-batch <batch>
```

## 实施规则

- 每阶段只解决一种主因。
- 每次运行使用新的不可变 `source_batch`。
- 失败结果必须附带日志和 failure class。
- 不以单次通过率变化判断优化有效，至少比较同任务重复结果。
- 未通过门禁时停留在当前阶段，不扩大任务矩阵。
