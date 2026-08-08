# M0-7 Accepted Delivery 计算

> 状态：冻结实现（M0-7）
> 更新日期：2026-08-08

`agent_go.delivery.evaluate_accepted_delivery()` 从任务 `meta.json` 计算交付门禁：

1. 所有必要子任务状态为 `completed` 或 `no_changes`，且 `verify_ok=true`。
2. 所有必要 commit hash 存在；指定 repo 时每个 hash 必须能被 Git 解析。
3. `delivery_branch` 存在；指定 repo 时必须能被 Git 解析。
4. 存在 `pr_url` 或 `explicit_merge_commit`；后者指定 repo 时也必须能解析。
5. 如果记录了 PR head/base，则分别必须等于 `delivery_branch`/`target_branch`。

判定结果写入：

- `accepted_delivery`
- `delivery_failed`
- `accepted_delivery_reasons`

`binary_pass` 和旧 `pass_rate` 不被改变。PR 创建失败只有在 `delivery_attempted=true` 时标记为 `delivery_failed`，未执行交付的任务仅保持未 Accepted。

Bench 聚合输出：

- `accepted_delivery_rate`
- `pr_creation_rate`
- `delivery_failure_rate`
- `cost_per_accepted_delivery_usd`

上述指标均使用有效任务分母，并与 `failure_class` 分布同时输出。
