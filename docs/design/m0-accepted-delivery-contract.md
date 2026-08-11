# M0-1 Accepted Delivery 契约

> 状态：冻结（M0-1）· 判定生效（M1 后，2026-08-11）
> 更新日期：2026-08-11

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

## 5. M1 后判定生效说明（2026-08-11）

M1 交付闭环落地后，判定条件 5/6 由「契约要求」变为「机器可产出的真实数据」：

- **条件 5（delivery_branch）**：`pipeline.py:913` 在收尾段调用 `create_delivery_branch`，将全部成功子任务 commit 按序 `--no-ff` merge 汇总，写入 `meta.delivery_branch`。
- **条件 6（pr_url 或 explicit_merge_commit）**：`agent_go pr --push` 创建真实 GitHub PR 后写回 `meta.pr_url`；`agent_go merge` 本地合并后写 `explicit_merge_commit`，PR 已合并时由 `_fetch_merged_pr_commit` 同步为 GitHub 实际 merge commit。
- **pr/merge 互斥**：同一任务两条交付路径互斥（`cli.py:1804`），交付后只能有一条生效，保证判定快照与实际交付 commit 一致。

**实证**（2026-08-11，2 个异构真实仓库）：

| 任务 | 交付路径 | 判定 |
|------|----------|------|
| `task-20260811-220438-059-1c77`（vibe-astock） | `agent_go pr --push` → PR #1（OPEN） | `ACCEPTED_DELIVERY`，pr_url 写回 |
| `task-20260811-220821-792-ec2a`（llama-defender） | PR #8 GitHub 合并 → `agent_go merge` 同步 | `ACCEPTED_DELIVERY`，explicit_merge_commit=2f6fe45 |

两个方向（PR 路径 / merge 同步路径）的 Accepted Delivery 判定均在真实仓库端到端成立。仅 `completed`（无交付）的历史任务保持 `accepted_delivery=false`，语义不变。
