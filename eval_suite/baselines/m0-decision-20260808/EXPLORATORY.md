# EXPLORATORY — 历史基线（不可作正式决策依据）

> 标记日期：2026-08-09
> 原因：M1 交付闭环实现前生成的基线，`accepted_delivery` 判定使用旧口径。

## 违反的门禁（ADR-009 收敛门禁）

1. **72/96 条 `accepted_delivery=true` 但无 delivery branch/PR** —— M1 前判定不检查
   `delivery_branch`/`pr_created`。M1 后 `delivery.py` 已修复（无 delivery 即不 accepted）。
2. **全部 96 条 `status=None`** —— 旧 schema 未记录任务状态字段。
3. 1 条失败记录 `failure_class=None`（`db-performance-optimization`, kill=infra）。

## 处置

- 保留原文件（immutable），不改写 `results.jsonl` / `manifest.json`。
- 仅作为历史 exploratory 参考，不与任何新 source batch 合并。
- 新 decision 基线必须使用 M1 修复后的口径重新生成（收敛流程阶段 E）。

## 新决策基线要求

- `accepted_delivery=true` 必须同时存在 `delivery_branch` 或 `pr_created`。
- `status` 字段必须完整（无 None）。
- 失败记录 `failure_class` 完整率 100%。
