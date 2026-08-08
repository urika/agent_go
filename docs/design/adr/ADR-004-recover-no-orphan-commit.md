# ADR-004: Recover 不自动提交孤儿改动

## 状态

Accepted

## 决策

`recover` 只根据 worktree、commit 和验证状态重建 meta，不替用户提交 orphan changes。

## 原因

commit 是代码完成边界。自动提交可能把半成品、敏感文件或错误改动伪装成已完成代码。

## 约束

- 未提交改动只能 reset 或标记为 reset_failed。
- 已提交但未验证标记 `committed_unverified`。
- recover 与 run/resume 使用同一 task lock。

## 实现

`recover.py`、`pipeline.py`。
