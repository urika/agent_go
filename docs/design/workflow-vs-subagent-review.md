# 设计审查：Workflow-vs-Subagent 改进项复杂性分析

> 日期：2026-08-10（初版）；2026-08-11（决策落地后更新）
> 审查对象：[workflow-vs-subagent-architecture.md](workflow-vs-subagent-architecture.md)
> 方法：逐项验证已有实现状态 + 评估问题真实性 + 权衡复杂度收益比

## 先修正一个事实错误

在分析之前，需要纠正设计文档的一个重要前提。文档声称 6 个改进项均为「待实施」，但代码验证表明其中多项**已落地**：

| 改进项 | 文档状态 | 实际状态 | 代码证据 |
|--------|---------|---------|---------|
| E: 认知模式路由 | P2 待实施 | ✅ 已落地 | `executor.py` `_infer_cognitive_mode()` + `config.py` `worker_models_by_cognitive` |
| B-部分: 打地鼠检测 | P1 待实施 | ✅ 已落地 | `executor.py` `_defect_fingerprint()` + `_defect_similarity()` + `config.py` `diverge_similarity_threshold` |
| B-部分: 回退检测 | P1 待实施 | ✅ 已落地 | `executor.py` `_diff_stat_hash()` + `verification.revert_threshold`（默认 2）+ `kill_reason=verify_revert` |
| C: 跨文件 import 关系 | P1 待实施 | ✅ 已落地 | `planning.py` `check_parallel_import_relations()`（纯 AST/regex，零 LLM 依赖，warning） |
| 只读审查 | 文档称缺失 | ✅ 已落地 | `review_agent.py` `run_readonly_review` |
| 工具/权限最小化 | 文档称缺失 | ✅ 已落地 | `allowed_tools` / `permission_mode` 字段 + `--allowedTools` claude -p 透传 |
| D: 上下文去重 | P1 待实施 | ✅ 已落地 | TASK_BASE.md 共享基座（绝对路径引用，不复制进 worktree） |

这意味着设计文档在已有地基上重新发明了部分轮子。下面逐项分析剩余的真正缺口。

## 逐项分析

### A: 三层架构显式化（P0）→ 不建议实施

**声称的问题**：「`_run_pipeline()` 混合了三层职责，改一层容易踩到另一层。」

**实际情况**：三层在物理上已经是分离的——`api.py`（Plan）、`pipeline.py`（调度）、`executor.py`（执行）。`_run_pipeline()` 是胶水代码，它的职责就是串联这三者。将胶水代码替换为 dataclass 接口：

```python
OrchestrationConfig → PlanEngine → SubtaskContext → ExecutionRuntime → SubtaskOutcome
```

这不会改变任何行为，但会引入 3 个 dataclass + 类型注解 + 接口文档 + 测试 —— 用于形式化一个已经在工作的调用链。

**判断**：架构 ceremony。概念上可以保留三层作为心智模型（文档中描述即可），但代码层面的 dataclass 形式化是「为未来重构而重构」——当前没有第三个 consumer 需要这些接口，也没有第二个实现需要多态替换。

**建议**：在文档中保留三层心智模型（对理解系统有价值），不实施代码层的 dataclass 接口。

---

### B: 收敛条件细化（P1）→ 已落地（回退检测）

**当前已有**：`diverge_similarity_threshold` — 连续两次缺陷指纹不同 → 提前终止。

**文档新增**：
1. 缺陷分类（correctness/completeness/compliance/quality/integration）
2. 进步检测（diff 规模递减 → 允许额外重试）
3. 回退检测（diff_stat_hash 重现 → 终止）

逐一评估：

**缺陷分类**：需要对 LLM 语义评估的 reason 文本做分类。但这引入了**第二个分类问题**——解析自然语言归入 5 个类别本身有误差。而且即便正确分类了，可行动的结论仍然只有两个：「继续重试」或「终止」——和当前 `diverge_similarity_threshold` 的二分类相同。5 个类别增加了解释性但没有增加决策精度。

**进步检测**：diff 规模递减 ≠ 质量提升。一行关键修复比 50 行表面修补更有价值。diff 行数作为进步信号太弱，会引入误报（diff 变小了但方向错了 → 被误判为「进步中」而浪费额外重试）。

**回退检测**：diff_stat_hash 比较是 O(1) 的确定性检测，信号明确（agent 回到了之前尝试过的状态 → 在循环振荡）。零误报。**已落地**：`_diff_stat_hash(worktree, base_ref)` 对 `git diff --stat` 输出归一化后取 sha1 前 16 字符（忽略列对齐差异）；验证循环失败时相对子任务基座（`_base_commit`）计算累积 diff hash，同一状态出现 ≥ `revert_threshold`（默认 2）次即判定振荡 → 记录 `verification_results` type='revert' + `kill_reason=verify_revert` + 提前终止重试。仅当 `_base_commit` 已知（真实 pipeline 注入）时启用，直接调用/单元测试无基座时跳过。

**判断**：回退检测是唯一值得加的（简单、确定、信号强），已实现。缺陷分类和进步检测是在当前已工作的 `diverge_similarity_threshold` 上叠加噪声，不实施。

---

### C: 合约先行并行执行（P1）→ 已落地（import 关系 warning）

**声称的问题**：并行 worktree 中各自修改同一接口的不同部分，合并后冲突。

**实际情况**：
1. G7 已确保文件级互斥（不同子任务不修改同一文件）
2. 跨文件接口不一致（A 改函数签名，B 调用旧签名）在 agent_go 的线性 DAG 中很少发生——因为修改接口的子任务通常是上游，下游通过 `git merge upstream tag` 自然拿到最新版本
3. 真正需要合约保护的场景是**并行 wave 内**的不同 worktree 各自修改有调用关系的不同文件——而 G7 的文件互斥已经阻止了同文件修改

**文档方案的问题**：合约文件标记依赖 LLM Plan 正确识别 `contracts_provided` / `contracts_consumed`。但产生不完美 Plan 的同一个 LLM，你让它额外标记函数级接口合约——它标记错了怎么办？方案引入了 LLM 依赖来解决一个 G7 已经缓解了大部分的问题。

**更简单的替代方案**：不需要 LLM。在 `plan_to_subtasks()` 中，对并行 wave 内的子任务做 AST 级检测——如果子任务 A 和子任务 B 各自修改的文件之间存在 import 关系，产生 warning。纯粹机械规则，零 LLM 依赖。**已落地**：`planning.py` `_parse_imports()`（正则提取 import/from 模块路径含前缀）+ `_file_imports_module()`（路径↔模块归一化匹配，src/blog/views.py ↔ src.blog.views / src.blog / views）+ `check_parallel_import_relations()`（对无依赖路径的子任务对，A 修改文件被 B 修改文件 import → `parallel_import_relation` warning），`validate_plan_quality` 在 repo 存在时自动调用。纯告警不阻断（集成风险提示，interface 一致性由依赖链 merge 兜底）。

**判断**：合约概念在概念上很好，但实施成本集中在 LLM 依赖上。目前 G7 + 简单 import 关系检测已覆盖主要风险，采用机械规则落地。

---

### D: 上下文基座 + 增量注入（P1）→ 建议实施

**问题真实且可量化**：N 个子任务 = N 份项目概述/角色要求/共享资源清单。以典型 5 步任务计，假设共享部分 ~500 tokens，每步独立指令 ~300 tokens：

```
当前: 5 × (500 + 300) = 4000 tokens
改进: 500 + 5 × 300 = 2000 tokens  （节省 50%）
```

对于 10 步任务：10000 → 3500（节省 65%）。

**上游摘要的 Telephone Game 收益**也是真实的：当前子任务只知道自己的 TASK.md，不知道上游做了什么。注入结构化上游摘要（改了什么文件、加了什么约束）让下游 Claude Code 的初始决策更准确。

**实现复杂度低**：
- TASK_BASE.md 的生成：在 `plan_to_subtasks()` 中提取共享部分，写入一次（~30 行）
- 上游摘要的生成：在 `run_subtask()` 完成后提取 diff files + 约束匹配（~50 行正则，不需要 LLM）
- TASK.md 组装：base + delta + upstream summary（~20 行）

**判断**：唯一同时满足「问题真实」「方案简单」「收益可量化」三项的改进。

**建议**：实施。不需要 dataclass 接口，直接在 `plan_to_subtasks()` 和 `run_subtask()` 中做字符串拼接。

---

### E: 认知模式三级路由（P2）→ 已落地，文档移除

`_infer_cognitive_mode()` + `worker_models_by_cognitive` 已工作。文档中新增的 `COGNITIVE_MODE_PATTERNS` 正则推断规则可以作为 fallback（当 subtask 不携带 `cognitive_mode` 且 agent_type 推断也不明确时），但这属于已有实现的微调而非新功能。

**建议**：从设计文档中移除此项，避免混淆。

---

### F: Pipeline/Barrier 自适应（P2）→ 不建议实施

文档自身承认：「对于 agent_go 的典型工作负载（Plan 生成的 DAG 大多是线性的），Pipeline 模式收益有限。」

这就够了——如果一个改进的**设计者自己**都认为收益有限，它不应该留在方案里。

额外考虑：Pipeline 模式需要增量 tag merge（每次只 merge 新完成的依赖）+ 合约冻结（防止后续 merge 覆盖已冻结文件）。这些机制增加了 tag 管理的状态空间和 bug 面。对线性 DAG 零收益，对复杂 DAG 偶尔有收益——不值得。

**建议**：移除。

---

## 汇总：真正值得做的

| 改进 | 行动 | 原因 |
|------|------|------|
| A: 三层形式化 | ❌ 不实施 | 架构 ceremony，概念保留在文档即可 |
| B: 收敛细化 | ✅ 回退检测已落地 | 缺陷分类/进步检测信号弱。回退检测简单确定 |
| C: 合约先行 | ✅ import 关系 warning 已落地 | LLM 依赖不可靠。机械规则覆盖主要风险 |
| D: 上下文去重 | ✅ 已落地 | 唯一三方都在线的改进（真实问题/简单方案/可量化收益） |
| E: 认知路由 | ✅ 已落地 | 已有实现，从文档移除避免重复 |
| F: Pipeline/Barrier | ❌ 移除 | 设计者自认收益有限 |

## 决策落地记录（2026-08-11）

审查结论全部实施完毕。剩余 2 项缺口（B 回退检测、C import 关系）已落地，D/E 已核实存在：

- **B 回退检测**：`_diff_stat_hash`（归一化 `git diff --stat` → sha1 前 16 字符）+ `verification.revert_threshold=2`。仅 `_base_commit` 已知时启用（真实 pipeline 场景），避免单元测试误判。终止信号：`verification_results` type='revert' + `kill_reason=verify_revert`。
- **C import 关系**：`check_parallel_import_relations`（零 LLM 依赖机械规则）。并行 wave 内无依赖子任务对之间若存在跨文件 import 关系 → `parallel_import_relation` warning（不阻断）。
- 测试：`TestVerifyRevertDetection`（3）+ `test_parallel_import_relation_*`（3），全量 2057 passed。

## 核心洞察

设计文档的 6 个改进项在审查后收缩为 **1.5 个**（D + B 的回退检测）——现在 D、B、C 全部落地，改进项闭环。这不是设计失败——恰恰相反，这说明 agent_go 当前架构已经解决了很多问题。G6/G7/G8 拆分算法、`diverge_similarity_threshold`、回退检测、`worker_models_by_cognitive`、`review_agent`、`allowed_tools`/`permission_mode`、TASK_BASE.md 上下文去重、import 关系 warning——这些已全部落地。

剩下的真正缺口是**信息传递效率**：上下文重复膨胀 + 上游知识丢失。一个简单的 TASK_BASE.md + delta + 上游摘要方案就能解决，不需要架构重构。

**Workflow vs 动态 Subagent 的调研价值不在引出 6 个改进项，而在验证 agent_go 当前架构方向的正确性**——层级分派（Hierarchical）+ 拓扑 waves + worktree 隔离 + 重试收敛检测，这套组合已经匹配了社区最佳实践。不需要大幅改造。
