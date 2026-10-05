# ADR-014: 规则集执行面与全局规则数据面

## 状态

**Proposed（2026-10-05 起草；P1 影子接入前必须 Accepted）**——本 ADR 只裁决**边界**（执行面、数据面、两态语义、回滚），不裁决规则内容与阈值。上位：规则集管线概念设计 [rule-set-pipeline-design-20261005](../rule-set-pipeline-design-20261005.md)（§4–§7）、jev 试点需求文档 §18／O-14。**P0 已落地且为零 runtime 接入**（`agent_go/rule_set.py`＋37 例测试）；本 ADR 生效前，runtime 不得调用任何规则求值路径。

## 背景

- **要接的是什么**：规则集管线 P1＝把**影子执行**（只记不动）接到 runtime 后置点，产出"规则 vs 人标/jev"对照；P2 才轮到"active 生效"（届时规则可进 verdict——这是与 jev 的本质区别，jev 永不进）。
- **边界变更点（两处）**：①**执行面**——runtime 内新增一处规则求值调用（即便只记录）；②**数据面**——新增全局目录 `~/.agent_go/rules/`（规则清单）与任务目录内 `rule_decisions.jsonl`（执行记录）。
- **范式先例**：[ADR-012](ADR-012-spec-to-test-pipeline.md)（spec_test）已证明"默认关 opt-in ＋ fail-open ＋ 人审冻结 ＋ sha256 校验"的落地形态可行且不改变默认行为。
- **P0 事实**：受限 DSL（AST、禁 `eval`、三值 fail-open）、清单 `frozen_sha256`、`promote→active` 验证闸（`holdout_n≥100 ∧ holdout_sha ∧ regression_ok`，无 force）均已实现并测试（37 例）；影子纯函数 `shadow_evaluate`／`append_decisions` 已就位但**未被任何 runtime 调用**。

## 决策

1. **数据面**：
   - `~/.agent_go/rules/rules.jsonl`＝规则清单（唯一权威；P0 已用，仅全局；**项目级覆盖合并语义冻结前不得启用**）；
   - `<task_dir>/rule_decisions.jsonl`＝影子/生效执行记录（`rule_id/version/stage/result/reason/ts`）；
   - 均为**本地物料**：不入版本库、不提交、不外发；保留期与结论保鲜期一致（≥1 个季度），清理前归档分析件。
2. **两态语义**：`shadow`（只记不动）→ `active`（生效）；状态流转只经 `promote`／`retire`，无 force；影子期**不得改变任何既有输出**（含排序、失败分类、交付判定）。
3. **配置与默认值**：`rule_set.enabled=false` 默认关；`rule_set.shadow_stages` 默认空（空＝不接入任何后置点）；开启与否**不改变默认行为**（ADR-012 同款纪律）。
4. **fail-open**：规则求值、记录、读取任一异常⇒跳过并静默降级，**绝不阻断主链路**（与 `append_decisions` 的既有语义一致）。
5. **只读与无副作用**：规则只读白名单 state 字段；P1 期间不得产生 verdict、不得写 runtime 其它状态、不得触发任何动作。**规则的 verdict 能力（P2）须另开 ADR**。
6. **冻结与验证闸（P0 已实现，P1 沿用）**：执行前校验 `frozen_sha256`（不匹配⇒跳过该规则并记告警）；`active` 前置＝`holdout_n≥100 ∧ holdout_sha ∧ regression_ok`。**口径（2026-10-05 明确）**：`holdout_sha` 是 `replay` 报告的可复算摘要（`holdout_sha_of()` 纯函数：样本 ref 集＋标签分布＋规则身份＋逐规则/联合统计，剔时间戳）；`promote --report` 先复算校验再盖章 `metrics`，`regression_ok` 仅由人 `--regression-ok` 显式背书；不带 `report` 时闸门退化为"声明值检查"（值可被独立复算，复算责任在调用方）。
7. **角色分离**：候选生成者 ≠ 验证者 ≠ 审批者；晋升记录 `reviewed_by`。
8. **可观测与回滚**：`rule_decisions.jsonl` 为唯一行为证据面；回滚＝`rule_set.enabled=false`（立即回到现状）或 `retire <rule_id>`；删除数据面不影响 runtime。

## 后果

- **正面**：为"从 jev 决策收敛到规则"提供可审计、可回滚、默认关的接入形态；P1 只读影子不改变任何现有行为。
- **代价**：新增一处 runtime 求值调用点与两个本地文件面；需要按 §决策 5/6 的纪律维护（校验哈希、只看不判）。
- **非目标**：本 ADR **不**授权 P2（规则进 verdict）、**不**定义规则内容/阈值、**不**改变 jev 的 CON-1 使用半径。

## 引用

- 概念设计与 P0：`docs/design/rule-set-pipeline-design-20261005.md`（§4–§7、§10）；实现＝`agent_go/rule_set.py`＋`tests/test_rule_set.py`
- 形态先例：`docs/design/adr/ADR-012-spec-to-test-pipeline.md`（默认关 opt-in／fail-open／冻结校验）
- 需求登记：`docs/design/jev-review-triage-pilot-requirements-20261005.md` §18／O-14
- 关联：`docs/design/module-catalog.md`（rule_set.py 条目）、`docs/spec.md`（接口节）
