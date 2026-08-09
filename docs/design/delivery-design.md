# agent_go 交付设计

> 状态：当前 M1 交付闭环基线（M1-1/M1-2 已实现，2026-08-09）
> 更新日期：2026-08-09

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

M1 新增：

- `pr_head`（PR head，应为 delivery_branch）
- `pr_base`（PR base，应为 target_branch/base_branch）
- `explicit_merge_commit`（cmd_merge 成功后记录的 merge commit hash）
- `delivery_attempted`
- `delivery_error`（delivery branch 创建失败 / PR 失败 / merge 冲突原因）

子任务级：

- `subtask_id`
- `branch`
- `commit_hash`
- `verify_ok`
- `status`
- `failure_reason`
- `kill_reason`

## 3. M1 实现（2026-08-09）

### 3.1 交付分支创建（M1-1）

`delivery.py::create_delivery_branch()` 在 pipeline 完成且无能力失败时自动执行：

- 分支名：`agent_go/{task_id}/delivery`
- 锚定：`base_commit`
- 汇总：按 `results[]` 顺序将每个成功子任务（`completed`/`no_changes` 且有 `commit_hash`）的 commit 以 `git merge --no-ff` 汇入 delivery branch
- 隔离：使用临时 worktree（`--detach`）执行 merge，主仓库工作区不被污染
- 幂等：`git branch -f` 重置已存在的 delivery branch
- 失败：merge 冲突时中止并停止汇总，写入 `meta.delivery_error`，不阻断主流程

### 3.2 PR 交付（M1-2）

`cmd_pr` 修复：

- `--push` 推送 `delivery_branch:delivery_branch` 到远程（**不再推 `HEAD:{base}`**）
- `gh pr create --base {base} --head {delivery_branch}` 显式指定 head/base
- PR 成功：写入 `pr_url`/`pr_head`/`pr_base`，状态置 `ACCEPTED_DELIVERY`
- PR 失败：写入 `delivery_attempted=true`/`delivery_failed=true`/`delivery_error`，状态置 `DELIVERY_FAILED`（不能报告 completed）
- gh 未安装：备份 PR.md，标记 `delivery_failed`

`cmd_merge`（新命令，`agent_go merge <task-id>`）：

- 用临时 worktree 在 target branch 上执行 `git merge --no-ff` delivery branch
- 成功：`git update-ref refs/heads/{target}` 推进目标分支，记录 `explicit_merge_commit`，状态置 `ACCEPTED_DELIVERY`
- 冲突：保留现场（worktree 不清理），写入 `delivery_error`，状态置 `DELIVERY_FAILED`
- `--push`：merge 后推送 target branch 到远程

### 3.2b mergeability 预检（2026-08-09）

`delivery.py check_mergeability(repo, delivery_branch, target_branch)`：PR/merge 执行前的 dry-run merge 检查。

- 临时 worktree（detached at target）执行 `git merge --no-ff --no-commit`，主工作区和两个分支均不被修改
- 返回 `{mergeable, conflicts, ahead, base_sha, head_sha, error}`
- `ahead`（delivery 领先 target 的 commit 数）= 0 时视为可合并（空操作）

集成：

- `cmd_pr`：创建 PR 前检查 mergeability，冲突则阻断（`sys.exit(1)`）；`head` 必须是 `delivery_branch`（无则阻断）；`ahead=0` 时警告 PR 可能为空
- `cmd_merge`：合并前检查 mergeability，冲突则阻断且不污染 target / 不改 meta

### 3.3 交付状态与恢复（M1-3）

commit / verification / delivery 三层状态分离：

| 维度 | 字段 | 判定 |
|---|---|---|
| commit | `commit_hash`（子任务级）| 代码已保存 |
| verification | `verify_ok`（子任务级）| 代码满足验证 |
| delivery | `delivery_branch` / `pr_url` / `explicit_merge_commit`（任务级）| 代码可取得 |

recover 交付维度语义（2026-08-09）：

- 全部子任务 `completed` + 验证通过 → **保留交付状态**（`ACCEPTED_DELIVERY` / `DELIVERY_FAILED`）或 `DELIVERY_READY`，不降级为 `EXECUTING`
- 有 `committed_unverified` 子任务（有 commit 无 verify 记录）→ 任务级 `EXECUTING`，resume 重新验证
- recover 写回 meta 保留全部 delivery 字段（`delivery_branch` / `pr_url` / `pr_head` / `pr_base` / `explicit_merge_commit` / `delivery_error`）

delivery 失败恢复：

- PR 失败 / merge 冲突 → `DELIVERY_FAILED` + `delivery_error`，delivery branch 保留
- `agent_go pr <task-id>` / `agent_go merge <task-id>` 可直接重试，无需重跑 Claude

## 4. Accepted Delivery 判定

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

## 5. 交付失败恢复

交付失败不得重新运行 Claude。系统应保留：

- 已完成 commit。
- delivery branch。
- PR 创建错误。
- target branch。
- 可重试的 delivery command。

M1 实现：delivery branch 在 pipeline 完成后保留在仓库中；`agent_go pr <task-id>` 可从 delivery branch 直接重试 PR，`agent_go merge <task-id>` 可执行显式 merge 交付，均不需重跑 Claude。

## 6. 验收场景

- 单子任务真实 Git 仓库创建正确 delivery branch。✅
- 多子任务依赖结果完整汇总。✅
- 非 `main` base branch 可交付。
- PR 创建失败后只重试交付。✅
- merge 冲突明确阻断并保留现场。✅
