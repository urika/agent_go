# 业务架构决策登记

> **文档状态**：决策登记版（2026-08-08 初始，2026-08-13 同步至 v0.5）。完整业务架构章节（角色/实体/过程展开）待 B 类待决策问题讨论完后补充。
>
> **文档目的**：登记 agent_go SDD 改造与闭环优化相关的决策，防止讨论结论在对话上下文压缩后丢失。本文件是后续实施的决策依据。
>
> **迭代归属**：本文档是后置业务架构候选设计。M0-M3 与阶段 A（A1/A2/A3）已 accepted/落地；本文档的 B 类决策（B1-B5）现为阶段 B/C/D 的前置门，已进入讨论期。

---

## 🔄 下次回来怎么继续（恢复指引）

> **如果你是恢复讨论的新会话**，先确认当前在哪个阶段：

### 当前进度（2026-08-13 更新）

**① M0-M3 已 accepted，阶段 A 已落地** —— 交付闭环（M1，3 个真实 PR）、核心可靠性（M2）、真实任务验证（M3，12 任务 91.7%）全部通过；阶段 A 工程闭环三件套 A1 文件所有权（88d0c5a）/ A2 函数级验收契约（f6e2cb0）/ A3 未提交基线（0097816）已实现提交。M4 goal 回溯进行中（并发进程推进）。

**② 后置业务架构决策（当前焦点）** —— 本文档的 B 类决策（B1-B5）是阶段 B/C/D 的前置门，现在**正式进入讨论期**（M3 前置条件已满足）。

### 后置业务架构恢复指引

1. **快速定位**：当前进度 = A 类 6 项已决策（不可推翻）+ B 类已定 B2（交付工具为主）+ B5（循环智能 b 最小止血+收口）+ 待定 B1/B3/B4。
2. **讨论顺序**：B2（已定✅）→ B5（已定✅）→ B4（问题跟踪定位，下一项）→ B1（merge 策略）→ B3（spec ROI）。
3. **讨论产出怎么处理**：每定一项 B 类决策，① 更新本文件对应行（⏳→✅，倾向→确认选择）；② 全部 B 类定完后，补全完整业务架构章节（角色/实体/过程展开）并升级版本号；③ 再进入对应阶段实施。

**恢复的第一句话建议**：
> "M0-M3 已 accepted，阶段 A 已落地；B2（交付工具为主）与 B5（循环智能 b 最小止血+收口）已定，下一项 B4（问题跟踪定位）。"

---

## 背景与引用

本文件登记的决策基于以下三份权威文档的边界定义，不重复其内容：

- [`software-development-lifecycle.md`](software-development-lifecycle.md) — 5-Phase 软件开发生命周期定义（Phase 0 需求 / Phase 1 设计 / Phase 2 任务 Spec / Phase 3 执行 / Phase 4 交付）。agent_go 定位为 Phase 3-4 执行引擎。
- [`project-management-tool-interaction.md`](project-management-tool-interaction.md) — 外部系统边界，明确"agent_go 不往上延伸，交互唯一通道是 MCP 协议 + Task Spec 文件"。
- [`sdd-references-and-frameworks.md`](sdd-references-and-frameworks.md) — SDD 学术研究（10+ 论文综述），agent_go 当前 L2 Spec-First，目标 L3 Spec-Anchored。

---

## A 类：已确认决策

以下 6 项决策在 2026-08 业务架构讨论中已明确，作为后续实施的不变约束。

| # | 决策点 | 选择 | 理由 | 确认时间 |
|---|--------|------|------|---------|
| A1 | **goal 达成语义** | 纯观测（方案 a）— converge 不改 task.status，合规度是正交维度 | 不破坏现有"verification 决定 status"语义；硬门禁留给 eval gate；task 可 completed 但合规度低（执行都过但漏验收），也可 failed 但合规度高（失败的恰不在 goal 范围） | 2026-08-08 |
| A2 | **§5 验收标准结构化方式** | Markdown checkbox + 反引号命令，不强制 EARS | 保持自然语言可读性；EARS 仅作模板示例；agent_go 不在 Specification 维度与 Spec-Kit/Kiro 竞争 | 2026-08-08 |
| A3 | **converge 判定方式** | 确定性优先，LLM 可选兜底（默认关） | P9 论文证明 AST 检测 97% 精度无需 LLM；成本可控；纯散文验收保持 uncovered 而非烧钱调 LLM | 2026-08-08 |
| A4 | **spec 快照策略** | 任务启动时拷贝 SPEC.md（不引用 source） | SpecSource 可能演化，引用会导致任务不可复现；快照不可变保证可追溯 | 2026-08-08 |
| A5 | **问题实体存储位置** | 全局 `~/.agent_go/problems.jsonl`（跨任务累积） | 支撑跨任务聚合分析（top 失败类别/趋势/复发率）；任务级只存原始，全局聚合 | 2026-08-08 |
| A6 | **issue 自动创建策略** | 默认关，`--track-issues` 显式开启 | 避免 issue 洪水；bench 场景不需要建 issue；真实交付场景才开 | 2026-08-08 |

---

## B 类：待决策问题

以下 4 项问题在讨论中提出但尚未拍板，需要进一步讨论确认。讨论完成后迁入 A 类。

| # | 待决策问题 | 当前倾向 | 需要拍板的关键点 | 状态 |
|---|----------|---------|----------------|------|
| B1 | **M1 交付的 merge 策略**：自动 merge-to-base 遇 main 分叉时怎么办 | A+B 都做（自动 merge + `agent_go merge` 手动命令），分叉时 ff-only 失败提示 | 分叉时是 fast-forward 失败提示（保守，让人处理），还是建 merge commit（自动合并，可能引入冲突）？ | ⏳ 待讨论 |
| B2 | **agent_go 定位**：交付工具 vs bench 工具 | **交付工具为主，bench 为配套验证**——bench 是「体检仪器」，交付工具是「生产本体」；bench 须补「交付闭环自动验证」（fixture 也能产出并验证 PR），否则测不准交付工具真实水平 | 决定 B3/B4/B5 取舍；M1/M3 已 accepted，交付优先已落地 | ✅ 2026-08-13 |
| B3 | **spec 闭环 ROI**：0 次使用的功能值得做 4 天闭环吗 | 先 M3 冒烟验证，跑通了再做 M2/M4 | 如果冒烟发现一堆 bug，M2/M4 是否要重新设计？还是干脆砍掉 spec 闭环，把资源投到循环学习（B5 的 c 选项）？ | ⏳ 待讨论 |
| B4 | **问题跟踪的定位**：GitHub issue 式状态机，还是分析聚合数据，还是 Reflexion 记忆源 | 取决于场景定位 | 真实场景是 bench（问题跟踪=分析模型弱点，只需聚合）还是交付（问题跟踪=跟踪 bug 修复，需状态机+issue 联动）？若选 B5 的 b/c（反思式），Problem 还要服务 Reflexion/KnowledgeStore，需 failure_pattern 分类 | ⏳ 待讨论 |
| B5 | **循环智能层级**：agent_go 要不要从"反应式"升级到"反思式"？（来自 [research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) 调研） | **b 最小止血+收口（已确认）**：无进展检测（revert）与有界 Reflexion 已在 M2 实现，仅做 ①Reflexion 阈值化（retry≥2，零成本修正）②收口为稳定契约 ③verify_state schema 前向兼容 KnowledgeStore；**暂不上局部重规划与 KnowledgeStore**，等 M4/M5 数据再决定是否跳 c | 前提已变：原「缺 3 项 guardrails」过时——revert（executor.py:1586）+ readonly_review（executor.py:1711）已落地；真实分叉只剩「局部重规划（P2，3-4 天，ROI 未证）」是否现在投 | ✅ 2026-08-13 |

**B 类讨论顺序建议**：B2（定位）→ B5（循环智能层级）→ B4（问题跟踪定位）→ B1（merge 策略）→ B3（spec ROI）。B2 的答案直接决定 B3/B4/B5 的取舍；B5 的答案影响 B4（Problem 实体是否要承载 Reflexion 记忆）。

**B5 选项与调研建议的对应**（[research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) §五·五章；2026-08-13 按实际代码状态更新）：
- B5-a（保持反应式）= 现状（revert 止血已够，Reflexion 继续关）
- B5-b（最小止血+收口）= Reflexion 阈值化（retry≥2）+ 收口为稳定契约 + verify_state schema 前向兼容；**不上局部重规划/KnowledgeStore**（推荐）
- B5-c（补全 guardrails 反思式）= B5-b 全部 + 局部重规划（P2，3-4 天）+ KnowledgeStore A/B

> 注：原调研建议 2（无进展检测，1-2 天）与建议 1（Reflexion）的骨架已在 M2 落地，因此「最小止血」的增量成本从原估 2-3 天降至 <1 天（仅阈值化+收口）。

---

## 闭环缺口速查

基于代码事实核查（2026-08-08）+ [research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) 调研，agent_go 有 5 个闭环缺口——4 个工程闭环缺口 + 1 个智能闭环缺口。

| 缺口 | 名称 | 类型 | 紧急度 | 代码事实证据（2026-08-13 复核） | 对应 Milestone/决策 |
|------|------|------|--------|--------------------------------|------------------|
| 1 | **交付闭环断裂** | 工程 | ✅ 已修复 | M1 accepted（3 真实 PR）；pipeline.py:923-926 已有 `ACCEPTED_DELIVERY`/`DELIVERY_FAILED` 状态（status_schema_version + accepted_delivery 字段）。遗留：bench 流水线不建 PR → `accepted_delivery=0`（B2 已定：bench 须补交付自动验证） | M1（roadmap） |
| 2 | **goal 回溯断裂** | 工程 | 🔄 进行中 | pipeline.py:898 仍是「无失败=completed」否定式（`"failed" if has_failed else "completed"`），不回看 goal/acceptance/overview | M4（roadmap） |
| 3 | **问题跟踪断裂** | 工程 | 🟡 中 | 单任务内完整，跨任务无 problems 实体；`grep problems.jsonl/Problem` 零命中 | M5-M6 |
| 4 | **spec 闭环断裂** | 工程 | 🟢 低 | --spec 代码完整但历史 0 次使用；spec 不持久化（`grep spec_snapshot/SPEC.md 拷贝` 零命中）、§5 不结构化 | M2-M4 |
| 5 | **循环智能断裂** | 智能 | 🟡 部分已做 | 已实现：无进展检测（`_diff_stat_hash`+revert_threshold，executor.py:1586）、有界 Reflexion（`readonly_review` 独立只读审查，executor.py:1711）。缺失：Reflexion 阈值化（当前每次 retry 触发+默认关）、局部重规划、KnowledgeStore 消费者 | B5 决策 |

**缺口性质区分**：
- 缺口 1-4 是**工程闭环**问题（数据没记录/没回流/没跟踪）——"有没有"的问题
- 缺口 5 是**智能闭环**问题（不会从失败学习）——"会不会"的问题，更深一层

**缺口 5 的 KPI 影响**（来自调研 §五·五章，基于 2026-08-06 bench 基线）：
- K8 首次验证通过率 88.9% → 补全 guardrails 后预期 ≥92%（Reflexion 减重复错误 30-50%）
- K4 成本：无进展检测可省 hard 任务 ~20-40% retry 成本（当前 hard max_retries=5，后 2-3 次常是空转）
- K1 任务成功率 83.9% → 局部重规划预期 +3-5pp（解决 plan brittleness 头号失败模式）

---

## 6 个 Milestone 速查

> ⚠️ 命名注意：本文档的 M1-M6 是「后置业务架构候选」的独立编号，**与 roadmap.md 的 M0-M4 编号不同**（roadmap M1=交付闭环，本文 M1=交付修复≈同一件事；但本文 M2=spec 持久化 ≠ roadmap M2=核心可靠性）。阅读时勿混淆。

| M | 名称 | 对应缺口 | 工作量 | 依赖 | 状态（2026-08-13） |
|---|------|---------|--------|------|------------------|
| **M1** | 交付修复 | 缺口 1 | ~2 天 | 无（必须先做） | ✅ 已通过 roadmap M1（3 真实 PR） |
| **M2** | spec 持久化 | 缺口 4 | ~1 天 | 无（可与 M5 并行） | ⏳ 未做（等 B3 决策） |
| **M3** | spec 端到端验证 | 缺口 4 | ~1 天 | M2 | ⏳ 未做（等 B3） |
| **M4** | goal 回溯 | 缺口 2 | ~2 天 | M3 | 🔄 进行中（roadmap M4） |
| **M5** | 问题跟踪 | 缺口 3 | ~2 天 | 无（可与 M2 并行） | ⏳ 未做（等 B4 决策） |
| **M6** | issue 联动 | 缺口 3 | ~1.5 天 | M5 | ⏳ 未做（等 B4） |

**两条并行链**：
- 交付+spec 链：M1 → M2 → M3 → M4
- 问题跟踪链：M5 → M6（与 spec 链无相互依赖，可并行）

**可复用基础**（基于工程核查）：
- 交付：cmd_pr 改 2 行 + subtask.py:43-81 merge 骨架；merge-to-base 原语从零新建
- spec 闭环：parse_spec 框架 + _extract_verification_commands(spec.py:332) + _save_meta_atomic(pipeline.py:21)
- 问题跟踪：_log_rejected_command(utils.py:353-382) JSONL 模式直接复刻 + analyze_reliability 加 8-12 行
- dashboard：_route_api if/elif + nav-tab+switchView+loadX 三件套，加端点/tab 是机械扩展

---

## 5 个不变量速查

业务规则必须成立的约束，实施时不可违反。

| 不变量 | 内容 |
|--------|------|
| **IV-1 交付明确性** | 任务完成后产物必须能到达 main（或显式 PR 目标），不允许烂在 worktree 分支 |
| **IV-2 goal 正交观测** | converge 不改变 task.status（completed/failed 仍由 verification 决定），合规度是正交维度（见 A1） |
| **IV-3 向后兼容** | 所有新字段可空、新实体可选、新流程可跳过——无 spec 的任务（当前 99.5%）行为完全不变 |
| **IV-4 问题一等公民** | 失败不只是 result 字段，Problem 是独立实体，有 id/状态/生命周期（见 A5） |
| **IV-5 边界克制** | agent_go 不做需求管理/排期/人员分工，issue 联动是"输出"不是"管理"（见 project-management-tool-interaction.md） |

---

## 关联调研

本方案的决策受以下调研文档直接影响，讨论 B 类问题时须参照：

### [research-goal-loop-mechanism-2026-08-08.md](../archive/reference/research-goal-loop-mechanism-2026-08-08.md)

**核心结论**：agent_go 的 goal/loop 机制有扎实"骨架"但缺关键"神经"——能"执行到验证通过"，但不能"从失败中学习"和"自适应调整"。处于 ReAct 裸循环与 Loop Engineering 最佳实践之间的中间位置。

**对 B 类决策的输入**：

| 调研内容 | 影响的决策点 | 输入要点 |
|---------|------------|---------|
| 第 5 个缺口（循环智能） | **B5（新增）** | 揭示了"反应式 vs 反思式"的智能层级选择，是前 4 个工程缺口之外的更深问题 |
| 建议 2 无进展检测 P0 + 建议 5 数据埋点 P0 | **B2（缺口优先级）** | 基于成本 ROI 主张"无进展检测优先"，与本方案"交付优先"判断有分歧——B2 决策需权衡"工程闭环完整性" vs "成本优化 ROI" |
| 建议 5 verify_state.json 前向兼容 KnowledgeStore | **B3（spec ROI）** | 提供了 spec 闭环的替代路径——砍 spec 闭环，资源投到循环学习数据积累 |
| 建议 1 Reflexion + 建议 5 failure_pattern | **B4（问题跟踪定位）** | 若选 B5-b/c（反思式），Problem 实体要承载 Reflexion 记忆，需 failure_pattern 分类（比 issue 状态机更丰富） |

**调研 5 条建议与 6 个 Milestone 的对照**（仅供 B2/B5 决策参考，**未纳入实施计划**）：

| 调研建议 | 优先级 | 预估 | 对应本方案的 Milestone | 关系 |
|---------|--------|------|---------------------|------|
| 建议 5 数据埋点（verify_state schema 扩展） | P0 | 1 天 | 无对应（新增 M7 候选） | 独立，为 H2/H3 积累数据 |
| 建议 2 无进展检测（diff 哈希比对） | P0 | 1-2 天 | 无对应（新增 M8 候选） | ROI 最高，止血 hard 任务空转成本 |
| 建议 1 Reflexion 批评层（retry≥2 插入 LLM 分析） | P1 | 2-3 天 | 无对应（新增 M9 候选） | 依赖建议 5；K8 +2-4pp |
| 建议 4 语义 goal（自然语言成功标准） | P1 | 2 天 | 与 M4（goal 回溯）部分重叠 | 若做 M4，语义 goal 是其自然组成 |
| 建议 3 局部重规划（失败触发 Plan 拆分） | P2 | 3-4 天 | 无对应（新增 M10 候选） | 依赖建议 2；H2-2 Branching 前置 |

**关键提醒**：调研是"方向性启发"（文档自标"非设计文档"），本方案的 M1-M6 是"工程闭环"。两者维度不同，**不应直接合并**。待 B2（定位）+ B5（循环智能层级）决策后，再决定是否把调研建议纳入实施计划（可能新增 M7-M10）。

---

## 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-08-08 | v0.1 决策登记版 | 初始版本：登记 A 类 6 项已决策 + B 类 4 项待决策 + 四大缺口 + 6 个 Milestone + 5 个不变量。完整业务架构章节待 B 类讨论完后补充 |
| 2026-08-08 | v0.2 补充循环智能缺口 | 纳入 [research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) 调研输入：新增第 5 个缺口（循环智能断裂）、新增 B5 待决策（循环智能层级）、缺口表区分工程/智能两类、新增"关联调研"章节（含调研建议与 Milestone 对照）、B2/B3/B4 更新调研输入引用 |
| 2026-08-13 | v0.3 决策 B2 定位 | B2 拍板：**交付工具为主，bench 为配套验证**（M3 已 accepted，交付优先已落地）。确认后 B3/B4/B5 均围绕「提升交付率」服务 |
| 2026-08-13 | v0.4 进度同步 | 按 2026-08-13 实际代码状态复核：缺口 1 已修复（M1 accepted）、缺口 2 进行中（M4）、缺口 5 部分已做（revert + readonly_review 已在 M2 落地）；M1-M6 表补状态列 + 命名冲突警示；B5 前提更新（原「缺 3 项 guardrails」过时） |
| 2026-08-13 | v0.5 决策 B5 循环智能 | B5 拍板：**b 最小止血+收口**——Reflexion 阈值化（retry≥2）+ 收口稳定契约 + verify_state 前向兼容；暂不上局部重规划与 KnowledgeStore，等 M4/M5 数据再定是否跳 c |
