# Bench v2 数据需求规格

> **文档类型**：产品需求输入（PRD Input）
>
> **来源**：Bench v1 数据分析（[bench-analysis-2026-08-01.md](../archive/reference/bench-analysis-2026-08-01.md)）暴露的数据缺口与方法论缺陷
>
> **目标**：定义下一轮 Bench 测试的数据采集标准、实验设计规范和指标体系，支撑 Q3 KPI 的准确度量和产品决策
>
> **日期**：2026-08-01
>
> **实施状态**：schema、cross_judge、代码质量维度和对照基线已落地；全因子实验和产品交付维度仍需按新版 suite 方案执行。旧批次只作 exploratory 数据，不作为当前产品 KPI 基线。

---

## 一、Record Schema 扩展

### 1.1 当前 Schema（15 字段）

```
task_id, model, task_dir, elapsed_sec, subprocess_exit, completed, failed,
total_subtasks, pass_rate, all_verify_ok, total_retries, total_cost_usd,
total_latency_ms, dollar_per_pass, stderr_tail
```

### 1.2 新增字段

#### P0 — 必须新增（无此字段无法进入下一轮分析）

| 字段 | 类型 | 说明 | 驱动问题 |
|------|------|------|---------|
| `timed_out` | bool | 任务是否因超时被强制终止 | django-blog 有 ~10 条 elapsed≈timeout，无法区分「超时杀掉」vs「刚好跑满时限」。改进手段完全不同 |
| `judge_model` | string | semantic evaluator 使用的模型 ID | cross_judge 前提：必须知道 judge 身份才能量化自评偏差 |
| `planner_model` | string | 生成 plan 的模型 ID | 当前 Planner 和 Worker 表现混在一起，任务失败无法归因 |
| `source_batch` | string | 批次标识（如 `baseline`, `results_v2`, `smoke-20260801`） | 当前隐含在文件名中，批次间不可比时无法追溯。**所有参与全量对比的 record 必须显式标注批次** |

#### P1 — 强烈建议（显著提升分析深度）

| 字段 | 类型 | 说明 | 驱动问题 |
|------|------|------|---------|
| `per_subtask` | json[] | 子任务明细数组，每项含 `{sub_id, model, retries, verify_ok, elapsed_sec, cost_usd}` | `total_retries=3` 无法定位到具体子任务，无法分析「哪种子任务容易失败」 |
| `plan_step_count` | int | Planner 分解出的步骤数 | 与实际 total_subtasks 对比，评估分解质量（过度拆分 vs 拆分不足） |
| `task_version` | string | 任务版本标识（git commit hash of task YAML） | bench 可复现性的基础。task 改了但版本号不变 → 无法判断是模型变好还是任务变简单了 |
| `binary_pass` | bool | 二元通过判定：`all_verify_ok AND semantic_pass` | 连续值 pass_rate 适合成本计算，二元判定适合 K1 等通过率指标。定义需在 PRD 中固化 |
| `semantic_pass` | bool | semantic evaluator 判定结果 | 与 all_verify_ok 互补。fp-sandbox 已验证两者必须分离 |

#### P2 — 建议补齐（完善分析维度）

| 字段 | 类型 | 说明 | 驱动问题 |
|------|------|------|---------|
| `worker_model` | string | 实际执行 Worker 的模型 ID（若启用 difficulty 路由，可能不同于顶层 model 字段） | 验证 difficulty 路由是否按预期工作 |
| `skill_names` | string[] | 实际加载的 skill 列表 | 与 role_skill_map 规则预期对比，评估 skill 匹配质量 |
| `lint_errors` | int | worktree diff 引入的 ruff/mypy 错误数 | 代码质量维度：通过 ≠ 代码干净 |
| `tests_broken` | int | worktree diff 导致的已有测试失败数 | 代码质量维度：完成任务 ≠ 没搞坏别的东西 |
| `blocked_by_upstream` | bool | 是否被上游子任务阻断（级联效应） | PRD M6 级联阻断的设计验证 |
| `false_positive_block` | bool | 是否被误判为失败的 upstream 阻断（假阳性级联） | 量化级联阻断的误伤率 |

---

## 二、实验设计规范

### 1.3 M0 冻结 Schema

当前 Bench record 必须带 `bench_schema_version=1`，并包含：

```text
task_id, task_version, suite, source_batch, model,
planner_model, judge_model, repeat, difficulty, failure_class,
accepted_delivery, delivery_branch_created, pr_created,
spec_compliance, architecture_compliance, total_cost_usd, elapsed_sec
```

字段类型和空值语义以 [M0-4 Bench Schema](m0-bench-schema.md) 为准；结果写入前必须通过 `agent_go eval validate-schema`。

### 2.1 任务-模型覆盖矩阵

**要求**：全因子设计（Full Factorial）。每个模型 × 每个任务 ≥ 3 次重复。

```
目标矩阵：7 模型 × 22 任务 × 3 重复 = 462 次运行

模型池：
  Tier 1 — claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5
  Tier 2 — deepseek-v4-flash, deepseek-v4-pro
  Tier 3 — kimi-for-coding-highspeed
  Tier 4 — （可选）glm-4, qwen3, gemini-2.5-pro

任务池：eval_suite/tasks/ 下 22 个标准任务
  easy (5):   add-format-helper, fix-missing-default, add-task-priority,
              add-error-handling, write-storage-test
  medium (5): implement-done-command, refactor-to-dict, add-simple-caching,
              email-validator, safe-file-reader
  hard (12):  add-tag-system, implement-archiving, add-metrics-system,
              add-caching-layer, security-hardening-taskmgr,
              storage-optimization-taskmgr, race-condition-taskmgr,
              stage-validation-refactor-datapipeline,
              conditional-branching-datapipeline,
              integration-tests-datapipeline,
              db-performance-optimization, db-end-to-end-optimization
```

**Suite 分层**：22 个 canonical 任务保留为历史资产，但按 `eval_suite/task_catalog.json` 分成：

| Suite | 用途 | 规模建议 | 是否用于模型排名 |
|---|---|---:|---|
| `smoke` | 每次代码变更快速回归 | 6-8 | 否 |
| `core` | 日常 Harness 回归 | 10-12 | 否，做趋势 |
| `decision` | 模型/路由决策 | 12-16 | 是 |
| `stress` | hard、高方差、长耗时专项 | 4-8 | 单独报告 |

运行示例：

```bash
agent_go eval bench --suite smoke --candidate-models claude-haiku-4-5 --repeat 1
agent_go eval bench --suite decision --candidate-models M1,M2,M3 --repeat 3
agent_go eval bench --suite stress --candidate-models M1,M2 --repeat 5
```

不跑子集时，默认运行全部 canonical 任务；快速 suite 必须写入 `suite` 和 `source_batch`，不得与全量结果混合比较。

### 2.2 重复次数说明

| 任务类型 | 最少重复 | 建议重复 | 理由 |
|---------|---------|---------|------|
| easy / low-variance | 3 | 3 | 方差小，3 次足够 |
| medium / mid-variance | 3 | 3 | 当前数据方差可控 |
| hard / high-variance | 3 | **5** | django-blog 同 task 不同 run 结果 0.0-1.0，n=3 时置信区间过宽 |

### 2.3 对照基线

**选 5-6 个代表性任务**（覆盖 easy/medium/hard），直接用 `claude -p` 裸跑（不走 agent_go harness），记录：

| 对照指标 | 测量目的 |
|---------|---------|
| pass_rate | agent_go harness 是否提升了通过率？（预期：是） |
| elapsed_sec | harness 的多步骤编排是否比裸跑更慢？（预期：可能更慢，但需要量化） |
| total_cost_usd | harness 的子任务拆分是否增加了 LLM 调用成本？（预期：可能更贵，需量化 ROI） |
| 代码质量（lint/tests） | harness 产出的代码是否更规范？（预期：是，因为有验证循环） |

对照模型选 Haiku（性价比基准）和 Sonnet（当前默认 Worker 候选）即可，不需要所有模型都跑。

### 2.4 运行时约束

| 约束 | 值 | 理由 |
|------|-----|------|
| 并发上限 | `--parallel 1`（顺序执行） | 消除并发竞争对 elapsed_sec 和 cost 的干扰。并发测试作为独立维度另行设计 |
| 超时 | 使用 task YAML 中 timeout 字段，不做全局超时 | 每个任务的 timeout 是经过设计的，全局覆盖会丢失信号 |
| 重试上限 | `--max-retries 3`（当前默认值） | 保持不变以兼容 v1 数据 |
| Judge 模型 | 显式指定，不与 Worker 相同 | 消除自评偏差的基础条件 |

---

## 三、指标体系标准化

### 3.1 诊断指标：$/pass

**兼容旧 Bench 的诊断定义**：

```
$/pass = sum(total_cost_usd) / sum(pass_rate)
```

该指标只用于同一 suite、同一 source_batch 内的相对诊断，不再作为产品主 KPI。

产品主 KPI 使用任务级：

```text
Cost per Accepted Delivery = valid_cost / accepted_delivery_count
```

- `pass_rate = 0` 的 record：贡献 cost 但不贡献 pass，自动纳入计算
- `pass_rate = 0.5` 的 record：贡献 cost，贡献 0.5 个 pass
- 不需要单独处理 null 或边界情况

**不复用 `dollar_per_pass` field**：该字段的计算逻辑随版本变化，不保证跨批次可比。以 raw cost 和 raw pass_rate 为准，$/pass 在分析阶段计算。

### 3.2 K1：任务成功率

**二元定义**：`binary_pass = all_verify_ok AND semantic_pass`

**汇报格式**：
- 整体：二元 pass 比例 + 连续值 pass_rate mean + 95% Wilson CI
- 按模型：同上
- 按 difficulty：同上
- 按 task：同上（per-task 的 CI 会很宽，如实汇报）

### 3.3 K3：简单任务耗时

**定义**：difficulty=easy AND binary_pass=true 的 record 的 elapsed_sec

**汇报**：mean + median + P95（识别被异常拖慢的情况）

### 3.4 K8：首次验证通过率

**修订定义**：

```
K8 = count(total_retries == 0 AND binary_pass == true) / count(binary_pass == true)
```

分母只含通过的 record，分子是其中 zero-retry 的。排除了「还没开始就失败」的情况（v1 算法会将 total_subtasks=0, pass_rate=0, retries=0 的 record 计入「首次通过」，夸大约 3-5pp）。

**汇报**：按模型和按 difficulty 分别汇报。

### 3.5 新增指标

| 指标 | 定义 | 用途 |
|------|------|------|
| 代码回归率 | `tests_broken > 0` 的 record 占比（仅计通过 record） | 衡量「完成任务但搞坏别的东西」的频率 |
| Plan 效率 | `total_subtasks / plan_step_count` | >1 说明 Planner 拆少了（Worker 自动补步骤），<1 说明拆多了但部分步骤被合并/跳过 |
| 级联阻断率 | `blocked_by_upstream == true` 的 record 占比（仅计未通过的 record） | 衡量 pipeline 中的浪费：有多少失败不是自己造成的 |
| 超时率 | `timed_out == true` 的 record 占比（按 difficulty） | 识别超时配置不当的任务 |
| 方差系数 | per-task-model 的 `std(pass_rate) / mean(pass_rate)` | 识别高方差任务（CV > 0.3），标注为「结果不稳定」 |
| Accepted Delivery | `accepted_delivery` | 验证代码是否真正形成可交付 branch/PR |
| PR 创建率 | `pr_created` / 有效任务数 | 验证交付闭环，而非仅验证代码修改 |
| Spec 合规率 | `spec_compliance` | 验证实现是否满足需求契约 |
| 架构合规率 | `architecture_compliance` | 验证实现是否遵守架构约束 |

---

## 四、新增评估维度

### 4.1 代码质量

**采集方式**：每个 subtask 完成后，对 worktree diff 自动运行：

```
ruff check --select=E,F,W --diff <diff_range>
mypy --ignore-missing-imports <changed_files>
pytest <existing_tests>  # repo 原有测试套件
```

记录 `lint_errors`（新增 lint 错误数）和 `tests_broken`（新增测试失败数）。

**分析视角**：
- 按模型：哪个模型产出的代码更干净？
- 按 difficulty：hard 任务是否更容易引入 lint 错误？
- 与 pass_rate 的交叉：高通过率 + 高 lint 错误 = 「完成任务但代码质量差」型模型

### 4.2 Plan 质量

**采集方式**：每条 record 的 meta.json 中已有 plan 数据。额外采集：

- `plan_step_count`：计划步骤数
- `plan_skill_match`：计划中指定的 skill 是否存在于 skill inventory 中？（Planner 幻觉检测）
- `plan_has_verification`：每个子任务是否有对应的验证步骤？

**分析视角**：
- Plan 正确性抽样：随机抽 20 条 record，人工判「如果严格按 plan 执行，能否完成任务？」
- Plan 粒度：子任务平均耗时与 timeout 的比值。>0.8 说明拆分过粗（单步接近超时），<0.1 说明碎片化过度
- Plan vs 实际：`plan_step_count` 与 `total_subtasks` 的差异分布

### 4.3 级联效应

**采集方式**：
- `blocked_by_upstream`：下游子任务在 meta.json 中被标记为 blocked
- 额外记录阻断链：`[upstream_sub_id, downstream_sub_id, block_reason]`

**分析视角**：
- 级联阻断率（占所有失败的比例）
- 假阳性阻断率：upstream verify_ok=true 但仍被标记为失败 → 下游被错误阻断
- 级联恢复成本：如果是假阳性阻断，解除阻断重跑的成本是多少？

### 4.4 稳定性

**采集方式**：同一 task-model 组合的多次重复（≥3）。

**汇报**：
- 每 task-model 组合的 pass_rate 标准差和 CV
- CV > 0.3 的任务标注为「高方差」
- 高方差任务列表作为 bench 可靠性附录

---

## 五、统计规范

### 5.1 汇报格式

所有比率指标（pass_rate、K8、代码回归率等）必须同时汇报：

| 项 | 格式 |
|-----|------|
| 点估计 | 百分比，保留 1 位小数 |
| 95% 置信区间 | Wilson score interval（非 Normal approx，小样本下 Normal CI 可能超出 [0,1]） |
| 样本量 | n（record 数）和 N（task-model 组合数）同时汇报 |

### 5.2 模型对比

- **配对比较**：对 per-task pass_rate 做配对检验（Wilcoxon signed-rank 或 paired t-test，取决于分布），而非简单比较均值。报告 p 值和效应量（Cohen's d 或 Cliff's delta）
- **多比较校正**：当同时比较 7 模型 × 5 标签 × 3 难度时，使用 Benjamini-Hochberg 校正（控制 FDR），报告校正前后均显著的差异
- **效应量阈值**：Cohen's h < 0.2 视为「可忽略差异」，即使 p < 0.05 也不作为路由决策依据

### 5.3 样本量告警

当 n < 10 时，所有推导性统计（CI、p 值、效应量）必须标注「样本不足，仅供参考」。当 n < 5 时，只汇报描述性统计，不做任何统计推断。

---

## 六、实施优先级

### P0（下一轮 bench 必须完成）

| 项 | 工作量估计 | 依赖 |
|-----|-----------|------|
| Schema 扩展：`timed_out`, `judge_model`, `planner_model`, `source_batch` | 小（4 字段） | 无 |
| $/pass 统一计算口径 | 小（文档化） | 无 |
| K8 定义修订（分母只含 passed records） | 小（文档化） | 无 |
| cross_judge 交叉评判（用不同 judge 模型重判同批输出） | 中（需选定 judge 模型池 + 实现重判脚本） | `judge_model` 字段 |
| 全因子设计：Tier 1 三模型 × 22 任务 × 3 重复 | 大（198 次运行 × ~$0.50 = ~$99） | 无 |

### P1（P0 完成后立即启动）

| 项 | 工作量估计 | 依赖 |
|-----|-----------|------|
| Schema 扩展：`per_subtask`, `binary_pass`, `semantic_pass`, `plan_step_count` | 中（嵌套结构 + 新增判定逻辑） | P0 schema 稳定 |
| 全因子设计扩展到 Tier 2+3 | 中（额外运行）+ 成本 | P0 全因子验证通过 |
| 代码质量维度：lint + test 检测 | 中（需集成 ruff/mypy/pytest 到 subtask 流程） | 无 |
| 对照基线：`claude -p` 裸跑 5-6 任务 | 小（~18 次运行） | 需选定代表性任务 |
| 统计规范：CI + 效应量 + 配对检验 | 小（分析脚本，非采集侧） | P0 数据到位 |

### P2（资源允许时追加）

| 项 | 工作量估计 |
|-----|-----------|
| Schema 扩展：`worker_model`, `skill_names`, `lint_errors`, `tests_broken`, 级联字段 | 中 |
| Plan 质量维度：抽样人工评估 + 自动指标 | 中（人工评估 20 条 ~1h） |
| 级联效应专项测试（故意构造 upstream 失败场景） | 大（需设计失败注入机制） |
| 稳定性专项：对高方差任务增加到 5-10 次重复 | 中（额外运行成本） |

---

## 七、成本估算

### 7.1 单轮全因子 Bench

| 模型组 | 模型数 | 任务数 | 重复 | 总运行 | 均价 | 总成本 |
|--------|--------|--------|------|--------|------|--------|
| Tier 1（Claude 三模型） | 3 | 22 | 3 | 198 | $0.69 | ~$137 |
| Tier 2（DeepSeek） | 2 | 22 | 3 | 132 | 按 API 定价 | 待定 |
| Tier 3（Kimi） | 1 | 22 | 3 | 66 | 按 API 定价 | 待定 |
| **合计** | **6** | **22** | **3** | **396** | — | — |

> 注：均价来自 v1 Claude 全量数据。hard 任务实际成本会显著拉高总成本（easy ~$0.29, hard ~$1.16），建议按 $1.00/次做预算。

### 7.2 对照基线

2 模型（Haiku + Sonnet）× 6 任务 × 3 重复 = 36 次裸跑。预计成本 < $30。

### 7.3 Cross-Judge

对 v1 中 saved output 做 cross-judge（不重新跑任务），预计需 3-4 个 judge 模型 × 已有的 ~490 条 record 的 output。Judge 成本远低于 Worker 成本（只做判定，不做代码生成），预计 < $20。

---

## 八、已知限制与置信度边界（2026-08-07 审计）

> 除模型计价外，影响 bench 结果可信度的因素已审计。已修复项见下方"已修复"；"已知限制"项记录为后续优化方向，不影响当前可信基线采集（但解读结果时须知悉）。

### 已修复（本批审计闭环）
- **P0-1 验证命令假通过**：`add-metrics-system` / `add-caching-layer` 原用 `pytest tests/ -q`（fixture 预置测试全过 → agent 不干活也 pass）。已改为指向任务要求新建的 `tests/test_metrics.py` / `tests/test_cache.py`——agent 必须实际实现才能通过。
- **P0-2 fixture 状态一致性**：8 个 task-mgr 任务 repo 从绝对路径 `/Users/jinsongwang/test-target/task-mgr` 统一为相对 `eval_suite/fixtures/task-mgr`（内容已核实一致），消除人工污染风险 + 跨批次可比。

### 已知限制（记录，不阻塞采集）
| # | 限制 | 影响 | 缓解 |
|---|------|------|------|
| L1 | **无 temperature/seed 显式设置** | LLM 采样随机 → 重复 N 次 pass 波动混入"模型能力差异" | `--repeat 3` 缓解；云端 API 多不支持 seed |
| L2 | **cache 计价未覆盖**（GLM/DS cache_read_input_tokens）| 长任务多调用时成本高估 | 当前任务多为单次调用，影响小 |
| L3 | **semantic evaluator 质量依赖** | semantic_pass 波动影响 binary_pass | S12-P0 已 fail-closed（评估失败不判过）|
| L4 | **任务描述无统一验收标准** | 部分任务 agent 自由发挥，产出不可比 | 与已采集数据不可比，改描述会破口径 |
| L5 | **batch 口径混用**（v2/v3/v4 采集器版本不同）| 跨 batch 分析 schema 不一致 | 用 `--source-batch` 标识隔离，分析按 batch 过滤 |

### 置信度边界
- pass_rate 的可信度取决于任务验证命令质量（已验证：除 2 个已修任务外，其余 20 个任务验证均含新功能断言或指向需新建的测试文件）
- 成本可信度依赖模型定价覆盖（已补全 glm-4.7/5.1/5.2/4.5-air + 运行前预检护航）

---

*关联文档：[bench-analysis-2026-08-01.md](../archive/reference/bench-analysis-2026-08-01.md) — v1 数据分析报告，本文档的需求来源。*
