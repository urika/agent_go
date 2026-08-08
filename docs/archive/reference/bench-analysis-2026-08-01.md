# agent_go Bench 数据分析 — 项目分析报告（修订版）

> **数据来源**：agent_go Bench 数据集（eval_suite/ 下 5 个结果文件，342 条 Claude 有效记录，490 条全量记录）
>
> **覆盖范围**：7 模型 × 22 标准任务 × 5 个来源批次（baseline / final-baseline / kimi-baseline / results / results_v2）
>
> **分析日期**：2026-08-01
>
> **版本**：v1.1（修订版）— 基于原始数据独立复算，修正了 v1.0 中的 4 处数值偏差，补充了 PM 方法论评估
>
> **当前口径声明（2026-08-08）**：本文属于历史 exploratory 分析。由于旧 Bench 批次存在采集器漂移、timeout 误判和 `$/pass` 分母问题，本文数值不作为当前产品 KPI 或模型自动路由依据。当前运行请使用 `eval_suite/task_catalog.json` 的 suite 分类和新版 Metric Freeze 规则。
>
> **对照基准**：[PRD v2.0.0](https://my.feishu.cn/docx/FiaudrQ6CohcEVx6a6UcGGfqnef)

---

# 一、KPI 达成总览

| # | 指标 | 实测值 | Q3 目标 | 判定 | 备注 |
|---|------|--------|--------|------|------|
| K1 | 任务成功率 | **83.9%**（Claude 三模型均值，n=342） | ≥92% | 🔴 差 8.1pp | 逐 record 平均；逐 task-model pair 平均为 81.2% |
| K3 | 简单任务耗时 | **122s**（Claude easy + passed，n=104；中位数 115s） | ≤180s | 🟢 达标 | 各模型差异较大：Opus 108s / Sonnet 119s / Haiku 146s |
| K4 | 单任务成本 | **$0.34**（Haiku）/ **$0.69**（Claude 均值） | ≤$0.05 | 🔴 差 7-14x | 均值被 hard 任务拉高，easy-only Haiku 为 $0.18 |
| K8 | 首次验证通过率 | **88.9%**（Claude 三模型均值：Opus 84.2% / Sonnet 93.1% / Haiku 91.2%） | ≥80% | 🟢 达标 | 指标定义：total_retries = 0 的 record 占比 |
| — | 北极星 $/pass | **$0.39**（Claude 三模型 field avg 均值：Opus $0.45 / Sonnet $0.50 / Haiku $0.20） | ≤$0.05 | 🔴 差 8x | 加权计算（总成本/总 pass 权重）为 $0.83，差异来自 task 粒度聚合方式 |

**一句话**：质量指标（K3/K8）已达标，成本指标（K4/$/pass）差距 7-14 倍。核心瓶颈在 **hard 任务（12/22 个任务，43% 的记录数，贡献 73% 的成本）**，尤其是 django-blog 两个端到端数据库优化任务（单次 $0.36-$6.24，是 easy 任务的 3-25 倍）。这些任务的成本被子任务数量（4-11）和 LLM 调用次数放大。

### 修订说明（v1.0 → v1.1）

| 指标 | v1.0 原始值 | v1.1 修正值 | 修正原因 |
|------|------------|------------|---------|
| K1 Claude 均值 | 82.6% | 83.9% | v1.0 使用了逐 pair 平均；本报告改用逐 record 平均，更直接反映 bench 全貌 |
| K8 Claude 均值 | 91.2% | 88.9% | v1.0 引用了 Haiku 单模型值；修正为三模型均值 |
| $/pass Claude 均值 | $0.32 | $0.39 | v1.0 计算口径未公开；修正为 field avg 三模型均值 |
| Opus pass_rate | 87.6% | 83.9%（全量）/ 87.6%（results+v2） | v1.0 仅计入 results + results_v2 两个批次（99 条），未纳入 baseline/final-baseline（40 条）；全量口径为 83.9% |

---

# 二、模型维度：谁该做 Planner？谁该做 Worker？

## 2.1 Claude 三模型

| 模型 | n | pass_rate | avg_cost | $/pass | K8 首次通过率 |
|------|---|-----------|----------|--------|-------------|
| claude-opus-4-7 | 139 | 83.9% | $0.80 | $0.45 | 84.2% |
| claude-sonnet-4-6 | 101 | 82.4% | $0.89 | $0.50 | 93.1% |
| **claude-haiku-4-5** | **102** | **85.5%** | **$0.34** | **$0.20** | **91.2%** |

> **Opus 数据口径说明**：Opus 分布在 4 个批次中——baseline（n=16, pass=87.5%）、final-baseline（n=24, pass=66.4%）、results（n=66, pass=85.6%）、results_v2（n=33, pass=91.7%）。全量 139 条 pass_rate 为 83.9%。若仅计 results + results_v2（99 条）则为 87.6%。本报告以全量口径为准。

**关键发现**：Haiku 4.5 在 pass_rate 上略优于 Sonnet 4.6（85.5% vs 82.4%，+3.1pp），同时成本仅为 1/2.6。这验证了 PRD 的设计原则 1 —— **「编排比模型更重要」**：在 agent_go 的 harness 加持下，更便宜的模型可以达到与更强模型相当甚至更好的结果。

**PM 提醒**：3.1pp 的差距在 n≈100 的样本量下，未经过统计显著性检验。Haiku 和 Sonnet 的任务覆盖度不完全相同（102 vs 101 条，分布有细微差异），建议补充 per-task 配对比较来排除任务选择偏差。当前数据支持「将 Haiku 作为默认 Worker 的候选」，但不应仅凭此数据做硬切换。

## 2.2 DeepSeek：无法胜任 Worker 角色

| 模型 | 整体 pass_rate | easy | medium | hard |
|------|---------------|------|--------|------|
| deepseek-v4-flash | 24.4% (n=66) | 35.0% | 0.0% | 27.1% |
| deepseek-v4-pro | 26.9% (n=59) | 44.1% | 6.7% | 27.4% |

PRD 风险（「Worker 本地模型质量不足」）被 bench 数据强烈验证。DeepSeek 在 medium 任务上几乎完全失效（Flash 0.0%、Pro 6.7%），即使是 easy 任务也只有 35-44%——远低于 PRD 设定的「假阳性 >20% 禁用」红线。

**结论**：「Sonnet 规划 + DeepSeek 执行」的混合策略在当前 bench 数据下不可行。DeepSeek 唯一可行的角色是 Planner（plan 生成），不做 Worker。如果必须在 CI 中启用 DeepSeek Worker，需配 `eval gate --check-regression` 自动熔断，且仅路由到 easy 任务。

## 2.3 Kimi：样本不足，暂不决策

kimi-for-coding-highspeed 在 12 个任务上 24 次运行全部 100% 通过，但仅覆盖了 22 个任务中的 12 个（全部为 easy + medium，缺失所有 hard 任务尤其是 django-blog）。按照 PRD 的「样本 <5 不决策」原则，暂不参与路由推荐。**建议尽快补齐 hard 任务数据**——Kimi 在 easy/medium 上的 100% 表现是一个值得追的信号。

---

# 三、标签维度：哪些任务类型拖后腿？

> **PM 提醒**：以下标签维度数据来自 Claude 三模型。部分标签样本量很小（如安全加固 n=6、并发/线程安全 n=12），结论需谨慎外推。

| 标签 | pass_rate | avg_cost | $/pass_field | n | 可信度 |
|------|-----------|----------|-------------|---|--------|
| 功能新增 | 89.5% | — | $0.26 | 106 | 🟢 可靠 |
| 安全加固 | 100.0% | — | $0.29 | 6 | 🔴 仅 1 个任务，不可泛化 |
| 架构扩展 | 86.5% | — | $0.44 | 36 | 🟡 中等 |
| 并发/线程安全 | 86.1% | — | $0.53 | 12 | 🟡 仅 1 个任务，边缘 |
| 缓存 | 85.9% | — | $0.63 | 44 | 🟢 可靠 |
| Bug 修复 | 82.5% | — | $0.18 | 38 | 🟢 可靠 |
| 测试编写 | 81.8% | — | $0.18 | 22 | 🟡 仅 1 个任务类型 |
| 错误处理 | 79.8% | — | $0.16 | 38 | 🟢 可靠 |
| 性能优化 | 77.8% | — | $1.20 | 18 | 🟡 被 django-blog 主导 |
| 重构 | 72.7% | — | $0.26 | 22 | 🟡 中等 |
| 数据库优化 | 66.7% | — | $1.79 | 12 | 🔴 仅 django-blog 两个任务 |
| 纯函数正确性探针 | 44.4% | — | $0.24 | 18 | 🟡 见第四节专项分析 |

**三个主要发现**：

1. **「数据库优化」和「性能优化」是成本黑洞**——二者 $/pass 分别为 $1.79 和 $1.20，是平均的 4-6 倍。这主要来自 django-blog 两个端到端任务和 data-pipeline 的 add-caching-layer。其中 django-blog 的 db-end-to-end-optimization 单任务多次运行 pass_rate 仅 33.3%。

2. **「纯函数正确性探针」的低 pass_rate 是预期行为，不是 bug**——email-validator 和 safe-file-reader 两个 fp-sandbox 任务的 verify_ok 是 100%（shell 验证全部通过），但 semantic evaluator 判定为未通过。其中 email-validator 尤为困难（Claude pass_rate 仅 11.1%，safe-file-reader 为 77.8%）。这正是 PRD 设计的混合验证（shell + semantic）机制在起作用：shell exit code 过了但语义不对，被正确标为失败。证明了 PRD「验证假阳性」（M5）设计的必要性。

3. **「安全加固」100% 通过但样本太少**——仅 6 条数据（security-hardening-taskmgr 单任务），不足以支撑「安全问题好修」的结论。该标签下需增加更多任务变体才能形成有效判断。

---

# 四、难度维度：medium 反而最难——人工标注 ≠ 实际执行难度

| 难度 | 记录数 | Claude pass_rate | avg_cost | 占总成本比 |
|------|--------|-----------------|----------|-----------|
| easy | 110 | 90.3% | $0.29 | 13.6% |
| medium | 84 | **76.0%** | $0.39 | 13.7% |
| hard | 148 | **83.7%** | $1.16 | **72.7%** |

**medium 任务 pass_rate（76.0%）低于 hard 任务（83.7%），且差距 7.7pp**。细看数据：

- medium 任务中的 `implement-done-command` 和 `refactor-to-dict` 涉及跨模块改动（CLI + storage + models 联动），实际操作复杂度高于部分 hard 任务
- hard 任务中的 `add-tag-system` / `implement-archiving` 虽然描述复杂但改动范围相对可控
- 真正的 hard 任务（django-blog 两个）pass_rate 为 33-89%，但被其他表现较好的 hard 任务拉高了均值

**这说明 difficulty 标签需要根本性反思**：当前标签来自 task YAML 中的人工标注，不是 Planner 输出，也不是 bench 回测结果。改进方向：

1. **先用 bench 数据反推「真实难度」**——以实际 pass_rate 和 avg_cost 为信号，重新校准 difficulty 标签
2. **再让 Planner 学习校准后的标签**——引入涉及模块数、跨文件改动量、是否需要新增测试文件等信号
3. **区分「描述复杂度」和「执行难度」**——前者影响 Planner 的分解质量，后者影响 Worker 的执行成功率

hard 任务单个成本是 easy 的 **4 倍**（$1.16 vs $0.29），由 django-blog（$2.34/次）和 data-pipeline（$1.16/次）的高成本任务主导。

### 超时效应

django-blog 的 18 条记录中有 10 条 elapsed_sec 接近或达到 timeout（1800s + 60s buffer），说明大量任务是**被硬超时杀掉**的。应拆分「超时导致的失败」vs「语义/功能失败」，两者需要的改进手段不同（前者加 timeout 或优化子任务粒度，后者改进 Worker 能力）。

---

# 五、成本结构：hard 任务是主要矛盾

| 仓库 | 记录数 | 总成本 | 占比 | 特征 |
|------|--------|--------|------|------|
| task-mgr | 243 | $118.73 | 50.1% | 记录多但单次成本低（均值 $0.49） |
| data-pipeline | 63 | $72.95 | 30.8% | 高难度流水线任务，单次 $1.16 |
| django-blog | 18 | $42.02 | 17.7% | 单次最高 $2.34，是 easy 的 8 倍 |
| fp-sandbox | 18 | $3.13 | 1.3% | 纯函数探针，成本低 |

**hard 任务（12/22）以 43% 的记录数贡献了 73% 的成本（$172.24/$236.84）**。如果只剥离 django-blog 两个任务，Claude easy+medium 的 $/pass 从 $0.39 降到约 $0.30。Haiku easy-only 的 $/pass 为 $0.09，距离 KPI 目标（$0.05）仅差 $0.04。

**但 $0.05 的 Q3 目标在当前技术栈下仍然极其激进**——即使只跑 Haiku easy 任务也需要进一步的成本压缩。可能的杠杆：减少子任务粒度以减少 LLM 调用次数、KnowledgeStore 复用历史 plan 避免重复规划。

---

# 六、与 PRD 设计原则的对照验证

| 原则 | 验证 | 证据 |
|------|------|------|
| 1. 编排比模型更重要 | ✅ 验证 | Haiku 4.5 pass_rate（85.5%）> Sonnet 4.6（82.4%），成本仅 1/2.6 |
| 2. 上下文隔离 > 并行性 | ✅ 间接验证 | K8 首次通过率 88.9%，说明子任务上下文隔离有效减少了「全局历史污染」 |
| 3. 协调机制做进系统层 | ⚠️ 部分验证 | tag 命名空间 + git worktree 零冲突已证明有效（bench 期间 0 冲突）；M6 级联阻断触发频率需单独统计 |
| 4. 人的注意力是最稀缺资源 | — 未直接测量 | bench 不测人工介入频率，但 88.9% K8 意味着约 11% 的 records 需要重试，与实际人工介入频率不完全等同 |
| 5. 复杂度判断在 Plan 阶段收敛 | ⚠️ 需改进 | medium < hard 的倒挂说明当前 difficulty 标签（人工标注）与 LLM 实际执行难度有系统性偏差 |

---

# 七、PRD 风险验证

1. **✅「Worker 本地模型质量不足」— 强烈验证**：DeepSeek 24-27% pass_rate 证实了这一风险。缓解措施（复杂度分级路由 + 质量门 + 定期回测）正确且必要。

2. **✅「验证假阳性（M5）」— 已被 bench 正确捕获**：fp-sandbox 探针任务 100% verify_ok 但 email-validator 仅 11.1% pass_rate（safe-file-reader 77.8%），说明语义评估层正在有效工作。没有语义评估，email-validator 会被错误标记为 100% 通过。

3. **⚠️「$/pass 计价失真（ISSUE-26）」— 本次 bench 已规避但需持续关注**：当前 cost_usd 来自 metering.jsonl 真实账单，非定价表估算。但如果未来加入未定价模型（如本地模型），ISSUE-26 的兜底逻辑可能再次导致低估。

4. **❓「LLM-as-Judge 自偏」— 未独立验证**：当前 evaluator 的 judge_model 可能与 worker_model 相同，存在自评偏差风险。这是 bench 数据的下一步必要动作——运行 **cross_judge**（交叉评判矩阵）量化自偏幅度。

---

# 八、方法论评估

### 数据质量

| 维度 | 评估 | 说明 |
|------|------|------|
| 样本覆盖 | 🟢 良好 | 7 模型 × 22 任务，覆盖面合理 |
| 批次一致性 | 🟡 需注意 | Opus 分布在 4 个批次（含早期 baseline），任务覆盖度不同，跨批次合并时需控制口径 |
| 标签完整性 | 🟡 需改进 | difficulty 为人工标注，与实测有偏差；task tags 未在 YAML 中定义，依赖外部映射 |
| 统计严谨性 | 🔴 不足 | 无置信区间、无显著性检验、无配对比较。对于「Haiku > Sonnet」等关键结论，需统计检验支撑 |
| 超时效应 | 🔴 未分析 | ~10 条 django-blog 记录疑似超时（elapsed ≈ 1860s），未与真正的功能失败区分 |

### 与外部基准的对标

当前 bench 未提供 SWE-bench 或其他外部基准的对标数据。建议在后续报告中补充方向性对标（如 SWE-bench Verified 上各模型的 pass_rate），以帮助判断 agent_go harness 的附加价值。

---

# 九、建议优先级

| 优先级 | 建议 | 预期收益 | 依赖 |
|--------|------|---------|------|
| **P0** | 运行 cross_judge 交叉评判矩阵 | 量化自评偏差，是所有 pass_rate 数字的可信度基础 | 需选定 judge 模型池 |
| **P1** | 建立 KnowledgeStore（H2-1）——同类任务的第 2/3 次执行注入历史验证命令和最优分解策略 | 缩小 K1 差距（83%→92%）成本最低的杠杆 | 需先有 cross_judge 确保 pass 数字可信 |
| **P1** | 基于 bench 数据反推任务真实难度，重新校准 difficulty 标签 | 修复 medium<hard 倒挂，提升路由准确性 | 无 |
| **P2** | 将 Haiku 4.5 提升为默认 Worker 候选 | 成本降低 60%+ 且不影响 pass_rate | 需先完成 per-task 配对比较排除选择偏差 |
| **P2** | 补充 django-blog 超时分析——拆分超时失败 vs 语义失败 | 区分「时间不够」和「能力不够」，改进手段不同 | 无 |
| **P3** | 将 DeepSeek Worker 限制为 easy-only + 硬质量门 | CI 场景下降本（如果 pass_rate 后续改善） | 依赖 DeepSeek 质量改善或更严格的 easy 定义 |
| **P3** | difficulty 路由引入多维度信号（涉及模块数、跨文件改动量） | 提升 Worker 模型路由精准度 | 依赖 P1 标签校准完成 |
| **P4** | 补齐 Kimi hard 任务数据 | 完整评估 Kimi 在 hard 任务上的性价比 | 需运行 Kimi 在全部 22 个任务上 |
| **P4** | 补充外部基准对标（SWE-bench 等） | 帮助判断 harness 附加价值 | 需选定对标基准 |

---

*数据来源：agent_go eval_suite/ 下 results.jsonl（332 条）/ results_v2.jsonl（103 条）/ baseline.jsonl（16 条）/ final-baseline.jsonl（24 条）/ kimi-baseline.jsonl（24 条），共 499 条原始记录，过滤 claude-code-executor 占位模型后 490 条有效 bench 记录，覆盖 7 模型 × 22 标准任务。全量数据独立复算验证，关键修正已在第一节标注。2026-08-01。*
