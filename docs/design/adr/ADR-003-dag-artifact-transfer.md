# ADR-003: 使用 DAG Wave 和 Git Merge 传递 Artifact

## 状态

Accepted

## 决策

子任务按照 `depends_on` 构成 DAG，调度器按拓扑 wave 执行；下游 worktree 通过 merge 上游 tag 获取代码。

## 原因

- 依赖关系显式化。
- 独立任务可并行。
- 上游提交成为可审计的 artifact。
- 失败可以阻断下游，避免错误扩散。

## 约束

- 依赖循环必须显式失败。
- 上游 failed/blocked 时下游默认 blocked。
- merge 冲突必须进入失败/人工处理路径。

## 实现

`pipeline.py`、`executor.py`、`subtask.py`。
