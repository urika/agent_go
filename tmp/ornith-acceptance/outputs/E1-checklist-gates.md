# M0-11 验收 Checklist（新人版）

按顺序逐项核对，打勾后截图留档。

## 1. 门禁覆盖
- [ ] 确认门禁由 `tests/test_m0_e2e.py` 覆盖。
- [ ] 单任务**有效**元数据 → 决策字段为 `Accepted Delivery`。
- [ ] **缺少** commit → 决策字段**非** `Accepted Delivery`。
- [ ] 验证**失败** → 决策字段**非** `Accepted Delivery`。

## 2. 失败类型判定
- [ ] PR 交付失败必须是 `delivery failure`，**不得**伪装成 `model failure`。
- [ ] `infrastructure failure` **不计入**能力失败分母。

## 3. 冒烟测试
- [ ] 运行 `--suite smoke`，**仅**调度 catalog 中的 smoke 任务。

## 4. 结果 Schema
- [ ] 每条结果必须通过 Bench Schema。
- [ ] 每条结果包含 `suite`、`source_batch`、schema version。

---

> 提示：若任意项未打勾，则门禁未通过，需修复对应用例再复跑。