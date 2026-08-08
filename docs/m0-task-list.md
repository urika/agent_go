# M0 当前阶段工作任务清单

> 阶段：M0 产品契约与指标冻结
> 状态：进行中
> 更新日期：2026-08-08
> 关联：[prd.md](prd.md) · [roadmap.md](roadmap.md) · [bench-v2-data-requirements.md](design/bench-v2-data-requirements.md)

## 1. M0 目标

让团队能够可信回答：

```text
什么算成功？
花了多少钱？
为什么失败？
代码交付到哪里？
```

M0 完成后，系统必须具备稳定的产品成功定义、状态语义、failure class、Bench schema 和可复现指标计算。

## 2. 当前优先级

```text
M0-1 产品契约
  -> M0-2 状态机
  -> M0-3 failure class
  -> M0-6 指标公式
  -> M0-4 Bench schema
  -> M0-5 Suite/任务版本
  -> M0-7 Accepted Delivery 计算
  -> M0-8 指标聚合
  -> M0-9/M0-10 基线治理
  -> M0-11 端到端验证
  -> M0-12 文档收口
```

M0-1、M0-2、M0-3、M0-6 必须先冻结；M0-4、M0-5、M0-7、M0-8 可在规则冻结后并行实施。

## 3. P0 必须完成

### M0-1 Accepted Delivery 契约

- [x] 定义 `Accepted Delivery` 的机器判定条件。
- [x] 定义“有效任务”的排除条件。
- [x] 定义部分完成、blocked、timeout、budget abort 的处理规则。
- [x] 定义 `completed` 与 `accepted_delivery` 的区别。
- [x] 定义 delivery branch、target branch、PR head/base 关系。
- [x] 定义交付失败状态 `delivery_failed`。

实现：[`m0-accepted-delivery-contract.md`](design/m0-accepted-delivery-contract.md)，机器判定入口为
`agent_go.delivery.evaluate_accepted_delivery()`。M1 交付分支和 PR 创建完成前，普通
`completed` 任务不会自动标记为 `accepted_delivery`。

验收：文档、代码和报告中所有“成功/完成/通过”都能映射到明确判定。

### M0-2 状态机统一

- [x] 定义统一状态集合：

```text
DRAFT
SPEC_REVIEW
ARCHITECTURE_REVIEW
PLAN_REVIEW
PAUSED
EXECUTING
VERIFYING
COMMITTED_UNVERIFIED
DELIVERY_READY
ACCEPTED_DELIVERY
VERIFICATION_FAILED
DELIVERY_FAILED
BLOCKED
CANCELLED
```

> M0-2 状态机语义修复（2026-08-08）：新增 `PAUSED` = 中断/暂停可恢复锚点；
> `PLAN_REVIEW` 回归为纯净的规划审查门（此前运行时把中断误写为 PLAN_REVIEW，已改）。

- [x] 绘制状态迁移表。
- [x] 对照 CLI 状态。
- [x] 对照 MCP 状态。
- [x] 对照 `meta.json`。
- [x] 对照 `recover/resume`。
- [x] 删除或标记冲突状态和别名。

实现：[`m0-state-machine.md`](design/m0-state-machine.md)，状态归一化入口为
`agent_go.status.task_status()`；新任务使用 `status_schema_version=1`，历史任务通过显式迁移保留兼容性。

验收：同一任务在 CLI、MCP、meta.json 和 Web 中显示一致状态。

### M0-3 Failure Class 统一

- [x] 固定 failure class：

```text
model_failure
verification_failure
timeout
budget_abort
infrastructure_failure
delivery_failure
user_cancelled
system_error
```

- [x] 定义每种 class 是否计入能力失败分母。
- [x] 定义每种 class 是否计入成本。
- [x] 定义每种 class 是否允许 resume。
- [x] 定义每种 class 是否保留 worktree。
- [x] 将现有 `kill_reason` 映射到 failure class。
- [x] 禁止基础设施失败伪装成模型失败。
- [x] 禁止预算中止伪装成能力失败。

实现：[`m0-failure-class.md`](design/m0-failure-class.md)，分类入口为
`agent_go.failure.classify_failure()`，任务级聚合入口为
`agent_go.failure.aggregate_failure_class()`。

验收：任意失败任务都能得到稳定、可聚合的 `failure_class`。

### M0-4 Bench Schema 冻结

- [x] 定义 `bench_schema_version`。
- [x] 固定必填字段：
  - `task_id`
  - `task_version`
  - `suite`
  - `source_batch`
  - `model`
  - `planner_model`
  - `judge_model`
  - `repeat`
  - `difficulty`
  - `failure_class`
  - `accepted_delivery`
  - `delivery_branch_created`
  - `pr_created`
  - `spec_compliance`
  - `architecture_compliance`
  - `total_cost_usd`
  - `elapsed_sec`
- [x] 定义字段类型和空值语义。
- [x] 增加 schema 校验脚本。
- [x] 禁止不同批次复用不同字段含义。

实现：[`m0-bench-schema.md`](design/m0-bench-schema.md)，校验命令为
`agent_go eval validate-schema --results <path>`。

验收：缺少必填字段或类型错误的 Bench record 被拒绝。

### M0-5 Suite 与任务版本冻结

- [x] 保留 22 个 canonical 任务。
- [x] 建立 `eval_suite/task_catalog.json`。
- [x] 支持 `smoke/core/decision/stress` suite。
- [x] 支持 `agent_go eval bench --suite ...`。
- [x] 增加 suite 分类测试。
- [x] 为每个任务补齐 `task_version`。
- [x] 为每个任务补齐 `business_relevance`。
- [x] 为每个任务补齐 `runtime_cost`、`high_variance`。
- [x] 为每个任务补齐 `semantic_probe`、`delivery_probe`。
- [x] 固定当前 M0 基线使用的任务集合。
- [x] 明确 stress 任务不进入普通平均值。

实现：`eval_suite/task_catalog.json` 补齐 22 个任务元数据，
`eval_suite/m0_baseline.json` 固化 M0 基线集合；Bench 聚合将 `stress` 或
`high_variance=true` 的任务单独报告，不进入普通平均值。

验收：每次 Bench 都能说明 suite、任务版本和 source batch。

### M0-6 指标公式冻结

- [x] 固定产品主指标：

```text
Accepted Delivery Rate
= accepted_delivery_count / valid_task_count
```

```text
Cost per Accepted Delivery
= valid_cost / accepted_delivery_count
```

- [x] 固定辅助指标：
  - First-pass Rate。
  - Time to Accepted Delivery。
  - Human Intervention Minutes。
  - Timeout Rate。
  - Retry Rate。
  - Delivery Failure Rate。
- [x] 将旧 `$ / pass` 降级为诊断指标。
- [x] 明确 `pass_rate` 只用于同 suite、同 source batch 内比较。
- [x] 禁止用部分子任务完成率作为产品成功率。

实现：[`m0-metric-freeze.md`](design/m0-metric-freeze.md)，计算入口为
`agent_go.metrics.compute_frozen_metrics()`。

验收：同一数据重复计算，指标结果一致。

### M0-7 Accepted Delivery 计算

- [x] 从 `meta.json` 读取 delivery branch。
- [x] 检查 commit hash 是否存在。
- [x] 检查验证是否通过。
- [x] 检查 PR 或 delivery branch 是否存在。
- [x] 计算 `accepted_delivery`。
- [x] 计算 `delivery_failure`。
- [x] 不让 Accepted Delivery 改变旧 `binary_pass` 语义。
- [x] Bench 报告增加 Accepted Delivery Rate。
- [x] Bench 报告增加 PR 创建率。
- [x] Bench 报告增加 Delivery Failure Rate。
- [x] Bench 报告增加 Cost per Accepted Delivery。

实现：[`m0-accepted-delivery-computation.md`](design/m0-accepted-delivery-computation.md)。

验收：代码验证通过但没有 delivery branch/PR 的任务不能标记为 `accepted_delivery=true`。

### M0-8 Failure Class 聚合

- [x] 按 failure class 分组统计。
- [x] 单独展示 model、verification、timeout、budget、infrastructure、delivery failure。
- [x] 输出有效任务分母。
- [x] 输出被排除任务数量和原因。
- [x] 明确 timeout 是失败、删失还是 cleanup race。

实现：`agent_go.metrics.aggregate_failure_classes()`，Bench 批次和模型报告均输出 `failure_class_summary`。

验收：报告不把 infra、timeout、budget abort 合并成模型失败。

## 4. P1 并行任务

### M0-9 Metric Freeze 报告

- [x] 新增固定基线报告模板。
- [x] 报告包含 schema version、task catalog hash、suite、model、config hash、repeat。
- [x] 报告包含运行时间范围和 failure class 分布。
- [x] 报告包含 Accepted Delivery 和成本指标。
- [x] 每批数据使用不可变 `source_batch`。

实现：[`m0-metric-freeze-report.md`](design/m0-metric-freeze-report.md)，命令为
`agent_go eval metric-freeze --results <path> --source-batch <batch>`。

### M0-10 结果和批次治理

- [x] 统一结果目录结构：

```text
eval_suite/
  task_catalog.json
  baselines/<source_batch>/
    manifest.json
    results.jsonl
    summary.json
  exploratory/
    results_v1.jsonl
```

- [x] 历史 results v1/v2/v3/v4 标记为 exploratory。
- [x] 不删除原始数据。
- [x] 禁止不同 schema 的结果直接合并。

实现：`agent_go.batch_governance` 提供 manifest、baseline 归档和批次合并校验；
`eval_suite/exploratory/manifest.json` 标记历史结果，新的固定批次归档到
`eval_suite/baselines/<source_batch>/`。

### M0-11 最小端到端验证

- [x] 单任务产生正确 `meta.json`。
- [x] 多任务产生明确最终结果。
- [x] commit 失败不能 accepted。
- [x] verification 失败不能 accepted。
- [x] 无 delivery branch 不能 accepted。
- [x] PR 创建失败分类为 `delivery_failure`。
- [x] infrastructure failure 不计入模型失败分母。
- [x] `--suite smoke` 只执行 smoke 任务。
- [x] 每条结果包含 suite、source_batch 和 schema version。

实现：[`m0-e2e-gates.md`](design/m0-e2e-gates.md)，测试入口为 `tests/test_m0_e2e.py`。

### M0-12 文档收口

- [x] `prd.md` 与 `roadmap.md` 指标一致。
- [x] `spec.md` 与 CLI 参数一致。
- [x] Bench 数据需求与代码字段一致。
- [x] 旧 K1/K4/K8 仅保留为历史 exploratory 数据。
- [x] 历史报告均有 exploratory 标记。
- [x] `README.md` 入口说明正确。
- [x] Markdown 链接检查通过。
- [x] `git diff --check` 通过。

实现：`tools/check_markdown_links.py`，文档入口见 `docs/design/m0-batch-governance.md` 和
`docs/design/m0-metric-freeze-report.md`。

## 5. M0 完成门禁

M0 只有在以下条件全部满足后才算完成：

- [x] Accepted Delivery 定义已冻结。
- [x] 状态机已统一。
- [x] failure class 已统一。
- [x] Bench schema 已冻结。
- [x] Suite 和任务版本已冻结。
- [x] Accepted Delivery 可以自动计算。
- [x] Cost per Accepted Delivery 可以自动计算。
- [x] 旧 `$ / pass` 不再作为产品主 KPI。
- [ ] 至少一批新的固定基线数据已生成。
- [x] 单元、集成和最小端到端测试通过。
- [x] PRD、roadmap、spec 和 Bench 文档一致。

当前唯一未满足的 M0 完成门禁是生成一批新的固定 baseline 数据；历史结果仍为 exploratory，不能替代正式基线。

## 6. M0 完成后的下一阶段

M0 完成后进入 M1：

```text
delivery branch
  -> commit 汇总
  -> PR head/base
  -> 交付失败恢复
  -> 可合并 PR
```
