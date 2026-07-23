# Subtask 版本管理机制 — 潜在问题与改进建议

> 基于 `agent_go` 当前代码（模块化架构，`agent_go/` 包）分析  
> 日期: 2026-05-26  
> 关联文档: [架构设计](../design/architecture.md), [工作流程](../design/workflow.md)

---

## 🔴 高优先级

### 1. Git Tag 跨任务冲突

**问题描述**：Tag 名仅使用 `sub_id`（如 `sub-1`），不包含任务标识。共享对象库下，不同任务（`task-A` 和 `task-B`）的 `sub-1` tag 会互相覆盖。

```python
# executor.py:146
subprocess.run(["git", "tag", "-f", sub_id], cwd=str(worktree), ...)
# sub_id = "sub-1" → 多个任务都会创建 "sub-1" 标签
```

**影响**：
- 并行执行多个任务时,后执行的 tag 会覆盖前一个任务的 tag
- 恢复任务时，tag 可能指向错误的提交
- 如果在 worktree 清理后运行 `git tag -l` 会发现 tag 混乱

**建议修复**：tag 名包含 `task_id` 前缀，如 `task-20260526-093045/sub-1`

```python
# 改为
subprocess.run(["git", "tag", "-f", f"{task_id}/{sub_id}"], ...)
# _git_merge_upstream 中对应也要使用完整 tag 名
```

---

### 2. Merge 冲突在 Headless 模式下无解

**问题描述**：`_git_merge_upstream()` 检测到冲突后执行 `merge --abort`，将冲突信息写入 `.MERGE_CONFLICT` 文件，然后在 TASK.md 中注入解决指引。

**当前行为**：
```python
# subtask.py:30-32
conflict_file = dst_worktree / ".MERGE_CONFLICT"
conflict_file.write_text(conflict_info, encoding="utf-8")
subprocess.run(["git", "merge", "--abort"], ...)
```

**影响**：
- **交互模式**：用户可手动解决冲突
- **Headless 模式**：Claude Code 收到含冲突指引的 TASK.md，但实际工作区**并无冲突状态**（已被 abort），Claude Code 需要重新手动 patch 合并上游代码，过程脆弱且易出错

**建议改进**：为 headless 模式增加自动解决策略——不 abort，而是生成冲突标记文件后让 Claude Code 直接面对冲突状态。

```python
# 改进方案：headless 模式下保留冲突状态，让 Claude Code 现场解决
if headless:
    # 不 abort，保留冲突状态，Claude Code 会看到冲突标记并自动解决
    # 在 TASK.md 中添加冲突解决指引
    conflict_instructions = (
        f"## ⚠️ 上游合并冲突\n"
        f"合并 {tag} 时产生冲突，冲突文件:\n" +
        "\n".join(f"- {f}" for f in conflict_files) +
        "\n\n请在文件中解决冲突标记 (<<<<<<< / ======= / >>>>>>>)，"
        "完成后执行: git add . && git commit -m 'resolve merge conflicts'"
    )
else:
    # 交互模式下 abort，让用户手动重新 merge
    subprocess.run(["git", "merge", "--abort"], ...)
```

---

### 3. Worktree 分支不推送到远程，单点丢失风险

**问题描述**：worktree 创建的分支 (`agent_go/{task_id}/{sub_id}`) 仅存在于本地，未推送到远程仓库。如果本地硬盘故障或误删 `.git/worktrees/`，所有中间变更将完全丢失。

**影响**：
- 长耗时的任务（如大型重构，数小时）存在单点故障风险
- 开发者无法在另一台机器上恢复正在执行的任务
- CI/CD 场景下工作节点崩溃后完全无法恢复

**建议改进**：添加可选的 `--remote` 参数，支持将 worktree 分支推送到远程：

```python
# pipeline.py 入口处增加
if remote:
    for st in confirmed:
        branch = f"agent_go/{task_id}/{st['id']}"
        subprocess.run(
            ["git", "push", remote, f"{branch}:{branch}"],
            cwd=str(repo), capture_output=True
        )
    logger.info(f"worktree 分支已推送到: {remote}")
```

---

## 🟡 中优先级

### 4. 并行模式下 Merge 冲突率偏高

**问题描述**：当前依赖分析是**步骤级**（`depends_on` 字段），同一 Wave 内的并行 subtask 如果修改同一文件，下游 merge 时冲突概率高。

```
Wave 0: sub-1 (改 src/auth.ts), sub-2 (改 src/auth.ts)  ← 并行
Wave 1: sub-3 (merge sub-1 + sub-2)                     ← 必然冲突
```

**影响**：
- 并行执行时冲突率不可预测
- 同一 Wave 内完全独立的 subtask 理论上无冲突，但缺少文件级分析来保证

**建议改进**：引入文件级依赖分析，在 `plan_to_subtasks()` 阶段或并行调度时检测文件冲突：

```python
# 方案：并行调度前做文件碰撞检测
def _detect_file_conflicts(subtasks):
    file_map = {}
    for st in subtasks:
        for f in st.get("files", []):
            file_map.setdefault(f, []).append(st["id"])
    return {f: ids for f, ids in file_map.items() if len(ids) > 1}
# 有文件碰撞的 subtask 降级为串行
```

---

### 5. 缺少版本间 Diff 对比机制

**问题描述**：任务完成后，worktree 被清理，原始项目无法直接对比 subtask 之间的增量变化。`git diff --stat` 仅提供摘要行数统计。

**当前能力**：
```
✅ git diff --stat → 文件级行数摘要
❌ 无法查看 subtask 间逐行变更
❌ 无法对比不同执行方案（Plan 版本）的效果
```

**影响**：
- Code Review 时 reviewer 看不到 subtask 的演进过程
- 无法回退到某个中间 subtask 的独立变更

**建议改进**：任务完成后归档 patch 文件：

```python
# pipeline.py 清理 worktree 前
for st in confirmed:
    wt_path = task_dir / st["id"] / "work"
    if (wt_path / ".git").exists():
        patch_dir = task_dir / "patches"
        patch_dir.mkdir(exist_ok=True)
        # 生成完整 patch（相对于主仓库的分支起点）
        subprocess.run(
            ["git", "format-patch", "--root", "-o", str(patch_dir)],
            cwd=str(wt_path), capture_output=True
        )
        # 或者仅生成当前任务相关的变更 patch
        branch = f"agent_go/{task_id}/{st['id']}"
        subprocess.run(
            ["git", "diff", f"main...{branch}", "--output",
             str(patch_dir / f"{st['id']}.patch")],
            cwd=str(repo), capture_output=True
        )
```

---

### 6. 增量执行能力有限

**问题描述**：当前仅支持通过 `cmd_resume()` 恢复**被中断的任务**，但不支持：

- 任务完成后选择性地重跑某个失败了但已修复的 subtask
- 跳过某个已验证不需要改动的 subtask
- 在已有任务基础上追加新的 subtask

**建议改进**：增加 `--skip` 和 `--only` 参数：

```bash
# 跳过指定 subtask
agent_go resume task-xxx --skip sub-2

# 只执行指定 subtask
agent_go resume task-xxx --only sub-3

# 查看任务时标记某个 subtask 为已跳过
agent_go show task-xxx --mark-skip sub-2
```

---

## 🟢 低优先级

### 7. `SHARED_CONTEXT.md` 缺乏大小管理

**问题描述**：每个 subtask 完成后都追加上下文到 `SHARED_CONTEXT.md`，随着 subtask 数量增加，文件会持续增长。

```python
# executor.py:193-194
shared_ctx_file = (task_dir / "SHARED_CONTEXT.md")
_safe_append_to_file(shared_ctx_file, "\n".join(ctx_parts) + "\n", logger)
```

**影响**：
- 20+ subtask 后，上下文可能超过 TASK.md 的合理长度
- Claude Code 加载过长的共享上下文可能降低指令遵循质量
- 无自动裁剪机制

**建议改进**：
- 追加前检查文件行数，超过阈值（如 200 行）时做摘要裁剪
- 只保留最近的 N 个 subtask 的上下文
- 或：将完整上下文移到单独文件，TASK.md 中只保留摘要链接

---

### 8. Worktree 清理任务中断风险

**问题描述**：`pipeline.py` 的 worktree 清理在任务完成后执行，如果清理过程中脚本崩溃，会留下孤儿 worktree 和分支。

```python
# pipeline.py:101-107
for st in confirmed:
    wt_path = task_dir / st["id"] / "work"
    if wt_path.exists():
        _worktree_remove(repo, wt_path)
_worktree_prune(repo)
```

**影响**：
- 残留的 worktree 占用磁盘空间
- 残留的分支名引起 `git branch` 输出混乱
- 需用户手动 `git worktree prune` 和 `git branch -D` 清理

**建议改进**：
- 清理前先记录待清理列表
- 添加 `agent_go cleanup <task-id>` 命令进行延迟清理
- 或者：在 `cmd_status` 中检测孤儿 worktree 并提示清理

---

### 9. Tag 清理缺失

**问题描述**：Worktree 和分支清理了，但 git tag 未清理。长此以往，`git tag -l` 中会积累大量 `sub-1` 类标签。

**建议改进**：在 `pipeline.py` 清理阶段添加 tag 删除：

```python
# 清理 worktree 的同时删除 tag
for st in confirmed:
    tag = f"{task_id}/{st['id']}"  # 假设按建议1修复了 tag 名
    subprocess.run(["git", "tag", "-d", tag],
                   cwd=str(repo), capture_output=True)
```

---

### 10. 无 Hook 机制

**问题描述**：subtask 执行前后没有任何扩展点，无法插入自定义逻辑（如 pre-commit lint、变更通知、CI 触发）。

**影响**：
- 无法在 subtask 提交前自动运行 lint/format
- 无法在 subtask 完成后自动触发 CI 或通知
- 限制了 CI/CD 集成的可能性

**建议改进**：增加简单的 hook 配置：

```json
{
  "hooks": {
    "pre_subtask": ["make lint"],
    "post_subtask": ["curl -X POST https://ci.example.com/trigger"],
    "post_merge_conflict": ["slack-notify --channel dev --msg '冲突待解决'"]
  }
}
```

---

## 优先级汇总

| # | 问题 | 优先级 | 影响面 | 工作量估计 |
|---|------|--------|--------|------------|
| 1 | Tag 跨任务冲突 | 🔴 | 数据正确性 | ~10 行（改 tag 命名 + merge 调用方） |
| 2 | Headless merge 冲突无解 | 🔴 | Headless 可用性 | ~20 行（改冲突处理分支逻辑） |
| 3 | Worktree 分支不推远程 | 🔴 | 数据安全 | ~15 行（新增 `--remote` 参数） |
| 4 | 并行模式下冲突率偏高 | 🟡 | 执行效率 | ~30 行（文件碰撞检测函数） |
| 5 | 缺少版本 Diff 归档 | 🟡 | Code Review | ~20 行（patch 导出逻辑） |
| 6 | 增量执行能力有限 | 🟡 | 用户体验 | ~40 行（`--skip`/`--only` 参数） |
| 7 | SHARED_CONTEXT 大小管理 | 🟢 | 质量（长期） | ~15 行（阈值裁剪） |
| 8 | Worktree 清理中断风险 | 🟢 | 运维 | ~20 行（延迟清理命令） |
| 9 | Tag 清理缺失 | 🟢 | 运维 | ~10 行（清理循环加 tag -d） |
| 10 | 无 Hook 机制 | 🟢 | 可扩展性 | ~30 行（hook 配置 + 执行） |

---

## 快速修复建议（单次改动）

如果时间有限，建议优先修复前三项（🔴），它们的改动量小但影响大：

| 修复 | 文件 | 改动 |
|------|------|------|
| Tag 加 task_id 前缀 | `executor.py:146`, `subtask.py:12` | `sub_id` → `f"{task_id}/{sub_id}"` |
| Headless 冲突保留 | `subtask.py:27-32` | `is_headless` 参数，非交互不 abort |
| 远程推送 | `pipeline.py` 入口 + `cli.py` 参数 | 新增 `--remote <url>` 参数 |
