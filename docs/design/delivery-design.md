# agent_go 交付设计

> 状态：当前 M1 交付闭环基线（M1-1/M1-2 已实现，2026-08-09）
> 更新日期：2026-08-09（真实 GitHub PR 端到端验证 + pr/merge 互斥建议）

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

## 7. 真实 GitHub PR 端到端验证记录（2026-08-09 / 2026-08-11 规模化）

在 agent_go 自身仓库（`git@github.com:urika/agent_go.git`，`gh` 已登录）跑通完整交付链路。

### 7.1 验证任务

`task-20260809-164929-400-3507`：新增 `utils.py::slugify_branch_name` 纯函数 + `tests/test_utils_branch.py` 5 个单测。

- 拆 2 subtask（developer + tester），均 `verify_ok=True`
- 生成 delivery branch `agent_go/task-20260809-164929-400-3507/delivery`，领先 main 4 commits

### 7.2 链路步骤与结果

| 环节 | 命令 | 结果 |
|------|------|------|
| 交付分支生成 | pipeline 自动 | `DELIVERY_READY`，delivery branch 存在 |
| push 到远程 | `agent_go pr <tid> --push` | `origin/agent_go/.../delivery` @ `3b1b760` |
| PR 创建 | 同上 | **PR #38**（`MERGEABLE`，head=delivery branch，base=main，+47/-1） |
| meta 写回 | 同上 | `pr_url`/`pr_head`/`pr_base`，status → `ACCEPTED_DELIVERY` |
| 交付合并 | GitHub merge / `agent_go merge` | GitHub merge commit `3bb470e` 合入 main |

### 7.3 发现的问题与建议

**问题 1（P1）：`agent_go pr` 与 `agent_go merge` 是两条互斥交付路径，但可被同时执行。**

本次验证中 `gh pr merge`（GitHub 侧）与 `agent_go merge`（本地侧）先后执行，产生两个不同的 merge commit：

- GitHub 侧：`3bb470e`（正式合入 main）
- 本地侧：`0ebf20d`（meta `explicit_merge_commit` 记录此值）

最终本地 main 对齐到 GitHub 的 `3bb470e`，`0ebf20d` 不在最终历史中，导致 meta 的 `explicit_merge_commit` 与实际交付 commit 不一致（对象尚存时判定通过，GC 后可能失效）。

**建议**：

1. **二选一**：PR 路径（`agent_go pr --push`）与显式 merge 路径（`agent_go merge`）对同一任务互斥，交付时只走一条。
2. **对齐 merge commit**：若本地 `agent_go merge` 后又在 GitHub 侧合并 PR，应将 `meta.explicit_merge_commit` 同步为 GitHub 的实际 merge commit（可用 `gh pr view <n> --json mergeCommit` 取得）。
3. **可重算判定**：`accepted_delivery` 是持久化快照，不随 git 对象变化重算。建议交付判定保存判定时的 `pr_url`/`explicit_merge_commit`，并在统计前对 git 可达性做复查（当前 `evaluate_accepted_delivery(meta, repo)` 支持带 repo 复查，但 meta 快照不会自动失效）。

**问题 2（P2）：历史任务 meta 时效性。**

`task-20260809-104620-524-c43c` 记录 `ACCEPTED_DELIVERY`，但其 delivery branch 已被清理，带 repo 复查时判定 `delivery_failed=True`（`delivery_branch_not_found`）。属判定快照未随 git 状态失效的典型例证（见问题 1-建议 3）。

### 7.4 条件确认

真实 PR 链路需要三要素，本次均确认齐备：

1. **远程仓库**：`origin` 指向真实 GitHub 仓库（`git@github.com:urika/agent_go.git`）。
2. **GitHub 认证**：`gh auth status` 已登录（含 `repo` scope），`gh pr create` / `gh pr merge` 可用。
3. **同步的 base**：本地 main 与 `origin/main` 对齐（PR base=main 反映最新代码）。

若缺少任一要素（如无 remote、gh 未登录、main 落后远程），`agent_go pr` 会给出明确错误（推送失败 / gh 未安装 / mergeability 预检失败），不会静默产生错误交付。

### 7.5 M1 规模化验证（2026-08-11）：2 个异构真实仓库

在 agent_go 自身仓库之外，选择 2 个用户自有、可推送、测试干净的真实仓库跑通端到端交付，验证 delivery branch 生成、PR 创建、pr/merge 互斥在异构仓库的表现。

**目标仓库（异构度）：**

| 仓库 | 结构 | 任务 | 验证命令 |
|------|------|------|----------|
| `urika/vibe-astock` | Python + React 混合（FastAPI 后端 + tsx 前端） | `util.py` 新增防御式纯函数 `is_weekend_safe`（非法日期返回 False 不抛异常）+ 4 个单测 | `python3 -c "from duanxian.util import is_weekend_safe; ..."` 单行断言 |
| `urika/llama-defender` | 纯 Python 代理（anthropic_proxy 变体） | 修复 `tool_parser.py` `_is_truncated_json` 第 101 行括号笔误（`("{","[","{")` 第三个 `{` 应为 `]`）+ 单测 | worker 自主选择 `python3 -m unittest discover -s test/unit` |

**关键结果：**

| 环节 | 任务 A（vibe-astock） | 任务 B（llama-defender） |
|------|----------------------|--------------------------|
| 任务 | `task-20260811-220438-059-1c77` | `task-20260811-220821-792-ec2a` |
| 执行 | 单 subtask，58s，+45/-0（2 文件） | 单 subtask，42s，+15/-1（2 文件） |
| pipeline 状态 | `DELIVERY_READY` → delivery branch 生成 | `DELIVERY_READY` → delivery branch 生成 |
| PR 创建 | `agent_go pr --push` → **PR #1** | `agent_go pr --push` → **PR #8** |
| GitHub 状态 | `MERGEABLE`，+45/-0，base=main | `MERGEABLE`，+15/-1，base=main |
| meta 写回 | `pr_url`/`pr_head`/`pr_base`，`ACCEPTED_DELIVERY` | 同上，`ACCEPTED_DELIVERY` |
| pr/merge 互斥 | OPEN PR 时 `agent_go merge` **被阻断**（提示走 PR 路径） | PR #8 在 GitHub 合并后 `agent_go merge` **同步** `explicit_merge_commit=2f6fe455994f` |

**验证结论：**

1. **异构仓库交付链路全部闭环**。delivery branch 生成（worktree 隔离 + `--no-ff` merge）、`agent_go pr --push`（check_mergeability 预检 + PR head/base 校验 + 推送 + PR 创建）、meta 写回与 `ACCEPTED_DELIVERY` 判定在 Python+React 混合与纯 Python 两类仓库均正常工作，无需仓库特化配置。
2. **pr/merge 互斥两个方向都验证通过**：
   - OPEN PR 时 `agent_go merge` 阻断（互斥的「pr 优先」方向）；
   - 已合并 PR 时 `agent_go merge` 调用 `_fetch_merged_pr_commit` 同步 GitHub 的 merge commit，消除 7.3-问题 1 中「两个不同 merge commit」的隐患（正是该建议的落地验证）。
3. **验证命令的仓库适配由 worker 自主完成**。任务 B 未用任务描述给出的 `python3 -c` 断言，而是识别仓库用 unittest 后选择 `python3 -m unittest discover`，说明验证命令选择具备 LLM 自适应，不依赖 planner 显式指定。
4. **确认 7.4 三要素普适性**：真实 PR 链路的三要素（远程仓库 / gh 认证 / 同步 base）在非 agent_go 仓库同样适用，缺少任一要素时 `agent_go pr` 明确报错而非静默错误交付。

**遗留观察（非阻断）：**

- 任务 A 的 PR #1 保持 OPEN（作为 OPEN-PR 互斥的活样本），未走 merge 收尾；如需收尾可 GitHub 侧合并后 `agent_go merge` 同步。
- 本地 llama-defender clone 的 delivery branch 落后 origin/main 1 个 commit（GitHub 侧 merge 产生 `2f6fe45`），属正常现象——交付由 GitHub PR 完成，本地 delivery branch 无需再同步。
