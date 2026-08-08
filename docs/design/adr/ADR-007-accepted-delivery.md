# ADR-007: Accepted Delivery 作为产品成功定义

## 状态

Accepted

## 决策

任务只有在代码、验证和交付同时完成时，才计为 Accepted Delivery。

```text
accepted_delivery =
  required_subtasks_done
  AND verification_passed
  AND delivery_branch_exists
  AND commits_traceable
  AND (pr_created OR explicit_merge_target_exists)
```

## 原因

子任务通过率和代码提交并不代表用户获得了可合并交付物。产品主线是“交付 PR”，因此指标和状态必须以任务级交付为边界。

## 约束

- 部分子任务完成但无法交付不计为成功。
- PR 创建失败属于 `delivery_failure`。
- `Cost per Accepted Delivery` 是产品成本主指标。
- 旧 `$ / pass` 仅作历史或同批次诊断。

## 实现

`prd.md`、`roadmap.md`、`delivery.py`、`pipeline.py`、`bench.py`。
