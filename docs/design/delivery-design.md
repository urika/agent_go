# agent_go 交付设计

> 状态：当前 M1 交付闭环基线
> 更新日期：2026-08-08

## 1. 交付边界

```text
base_commit
  -> subtask branches / commits
  -> delivery_branch
  -> target_branch / PR base
```

代码完成、质量完成和产品交付是三个独立边界：

```text
commit        = 代码已保存
verification  = 代码满足验证要求
delivery      = 代码已到达用户可取得的位置
```

## 2. 必须持久化的字段

任务级：

- `base_commit`
- `base_branch`
- `delivery_branch`
- `target_branch`
- `commit_hash`
- `pr_url`
- `accepted_delivery`
- `failure_class`

子任务级：

- `subtask_id`
- `branch`
- `commit_hash`
- `verify_ok`
- `status`
- `failure_reason`
- `kill_reason`

## 3. Accepted Delivery 判定

```text
accepted_delivery =
  required_subtasks_done
  AND verification_passed
  AND delivery_branch_exists
  AND commits_traceable
  AND (pr_created OR explicit_merge_target_exists)
```

以下任一情况都不得标记 Accepted Delivery：

- 只有 worktree，没有 commit。
- 有 commit，但没有验证结果。
- 子任务部分完成且下游被阻断。
- PR head/base 不正确。
- 变更无法从 delivery branch 取得。

## 4. 交付失败恢复

交付失败不得重新运行 Claude。系统应保留：

- 已完成 commit。
- delivery branch。
- PR 创建错误。
- target branch。
- 可重试的 delivery command。

## 5. 验收场景

- 单子任务真实 Git 仓库创建正确 delivery branch。
- 多子任务依赖结果完整汇总。
- 非 `main` base branch 可交付。
- PR 创建失败后只重试交付。
- merge 冲突明确阻断并保留现场。
