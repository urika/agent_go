# M0-1 Accepted Delivery 契约

> 状态：冻结（M0-1）
> 更新日期：2026-08-08

## 1. 定义

任务只有同时满足以下条件，才是 `accepted_delivery=true`：

1. 任务是有效任务，没有被显式排除。
2. 所有必要子任务为 `completed` 或 `no_changes`。
3. 所有必要验证均通过，且没有未处理的高风险告警。
4. 存在可验证的 commit hash。
5. 存在可验证的 `delivery_branch`。
6. 存在 `pr_url`，或存在 `explicit_merge_commit`。

`completed` 只表示执行和子任务验证完成；它不代表用户已经获得可审查、可合并的交付物。

## 2. 排除与失败

- `valid_task=false` 或 `excluded=true` 的任务不进入有效任务分母。
- 部分完成、下游 blocked、timeout、budget abort、取消和基础设施异常均不能 Accepted Delivery。
- 没有 commit、验证失败、delivery branch 不存在、PR 创建失败，均不能 Accepted Delivery。
- `delivery_failed=true` 表示任务执行结果可能已完成，但交付物无法通过交付门禁；它不覆盖模型或验证失败。
- `accepted_delivery` 不改变既有 `binary_pass` / `pass_rate` 的计算语义。

机器判定由 `agent_go.delivery.evaluate_accepted_delivery()` 产生，失败原因写入 `accepted_delivery_reasons`。

## 3. Git 关系

- `target_branch` 是任务开始时的目标 base 分支。
- `delivery_branch` 是汇总全部必要变更、供审查和交付的分支。
- PR 的 `base` 必须等于 `target_branch`，PR 的 `head` 必须等于 `delivery_branch`。
- `commit_hash` 或 `commit_hashes` 必须能在仓库对象库中解析。
- 仅存在孤立 worktree 或子任务分支，不构成 Accepted Delivery。

## 4. 元数据字段

任务级 `meta.json` 使用：

```json
{
  "status": "completed",
  "commit_hash": "...",
  "delivery_branch": "agent_go/task-xxx/delivery",
  "target_branch": "main",
  "pr_url": "https://...",
  "explicit_merge_commit": "...",
  "accepted_delivery": true,
  "delivery_failed": false,
  "accepted_delivery_reasons": []
}
```

字段缺失时按失败处理，不按成功猜测。M1 负责创建和汇总 `delivery_branch` 以及 PR；在 M1 完成前，普通 `completed` 任务应保持 `accepted_delivery=false`。
