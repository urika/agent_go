# ADR-010 阶段 3 fork-retry A/B 实证（2026-09-06）

> 批次：adr010-p3-forkretry（注入臂，`--fork-retry`）vs adr010-p2-oczen（对照臂）。
> 两臂均为 opencode/Zen 免费模型 mimo-v2.5-free × golden 6 任务 × 1 重复，$0。

## 结果

| 任务 | 对照臂 binary_pass | 注入臂 binary_pass | 注入臂 retries |
|---|---|---|---|
| add-format-helper | True*[假阳性剔除] | True | 0 |
| fix-missing-default | True*[假阳性剔除] | True | 0 |
| add-simple-caching | False | True | 0 |
| security-hardening-taskmgr | False（retry 1 仍败） | True | 0 |
| implement-done-command | False（PWD 空通过，语义判负） | True | 0 |
| conditional-branching-datapipeline | True（retry 1） | True | 0 |
| **合计** | **2/6**（剔除假阳性后） | **6/6** | **0** |

## 结论

1. **fork-retry 续跑路径本批量零触发**（全部一把过，无修复重试）——臂间差异
   **不能**归因于 fork-retry。
2. 真实驱动因素是 **ISSUE-58 PWD 泄漏修复**（`5ecea8f` 前的对照臂 vs 修复后的
   注入臂）：对照臂三个失败/空通过案例（PWD 泄漏写主仓库 → worktree 零改动 →
   no_changes/验证失败）在修复后全部消失。6/6 应视为 **PWD 修复后的 opencode
   Zen 免费臂新基线**。
3. fork-retry 省 token 的验证需要「有重试发生」的批量；本批量证明的是机制不
   破坏正常路径（注入臂全部通过、无异常）。

## CLI 原语独立冒烟（补续跑语义证据）

批量外直接验证 `opencode run --session` 原语（/tmp/oc-resume-smoke，$0）：

- run 1：让模型创建 `secret.txt`（内容 magic-number-42）→ 捕获
  `ses_f8a34e7d7ffecRcIfKNCaEb5nc`；
- run 2：`--session <同 id>` + 「不要读任何文件，凭刚才的对话回答 magic
  number」→ **同一 sessionID 续跑确认，回答 42 正确**（会话上下文保留生效）。

## 后续

- fork-retry 的 token 节省量级（预估 30-50%）待有重试的批量实证——可在下次
  出现重试密集的弱模型批量时开 `--fork-retry` 对照。
- dsh 臂待 dsh 后续版本给 headless profile 加 resume 原语再评估。
