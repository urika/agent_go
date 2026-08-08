# ADR-001: 使用 Git Worktree 隔离子任务

## 状态

Accepted

## 决策

每个子任务使用独立 worktree 和命名空间 branch：

```text
agent_go/{task_id}/{subtask_id}
```

## 原因

- 防止并发子任务互相污染。
- 复用同一 Git object database。
- 支持 commit/tag/merge 形式的 artifact 传递。
- 失败 worktree 可以保留给人工审查。

## 约束

- worktree 必须在 task lock 保护下创建和清理。
- 任务结束后成功 worktree 清理，失败/blocked 按策略保留。
- 上游结果通过 commit/tag merge，不通过非审计文件拷贝。

## 实现

`git_utils.py`、`executor.py`、`pipeline.py`。
