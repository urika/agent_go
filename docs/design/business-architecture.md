# 业务架构决策登记

> **文档状态**：决策登记版（2026-08-08 初始版本）。完整业务架构章节（角色/实体/过程展开）待 B 类待决策问题讨论完后补充。
>
> **文档目的**：登记 agent_go SDD 改造与闭环优化相关的决策，防止讨论结论在对话上下文压缩后丢失。本文件是后续实施的决策依据。
>
> **迭代归属**：本文档是后置业务架构候选设计。当前实施以 [roadmap.md](../roadmap.md) 的 M0-M4 为准；交付闭环和指标冻结完成前，本文件的 M1-M6 不进入关键路径。

---

## 🔄 下次回来怎么继续（恢复指引）

> **如果你是恢复讨论的新会话**，先确认当前在哪个阶段：

### 当前优先级（2026-08-08 更新）

**① 当前优先级：M0-M1 产品收敛** — 先冻结 Accepted Delivery 和评估口径，再修复 delivery branch、PR head/base 和真实 Git 交付闭环。

**② 后置业务架构候选** — 本文件的 B 类决策和 M1-M6 候选工作，须在 M3 真实任务验证后重新评估；不得替代当前交付闭环。

### 后置业务架构恢复指引（M3 完成后用）

1. **快速定位**：当前进度 = A 类 6 项已决策（不可推翻）+ B 类 5 项待决策（B1-B5，阻塞实施）。
2. **下一步该做什么**：讨论 **B2（agent_go 定位：交付工具 vs bench 工具）** —— 它的答案直接决定 B3/B4/B5 的取舍。讨论顺序：B2 → B5 → B4 → B1 → B3。
3. **讨论产出怎么处理**：每定一项 B 类决策，① 更新本文件对应行（⏳→✅，倾向→确认选择）；② 全部 B 类定完后，补全完整业务架构章节（角色/实体/过程展开）并升级版本号；③ 再进入 M1-M6 实施。

**恢复的第一句话建议**：
> "M3 真实任务验证完成了吗？如果 Accepted Delivery 基线已冻结，我们再继续讨论业务架构候选和 B 类决策。"

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
| B2 | **缺口优先级**：交付（M1）vs spec 闭环（M2-M4）vs 循环智能（后置候选）谁先 | 倾向先 M1（产物出不去其他白搭） | 取决于 agent_go 定位；调研 [research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) 仅作参考 | ⏳ 待讨论 |
| B3 | **spec 闭环 ROI**：0 次使用的功能值得做 4 天闭环吗 | 先 M3 冒烟验证，跑通了再做 M2/M4 | 如果冒烟发现一堆 bug，M2/M4 是否要重新设计？还是干脆砍掉 spec 闭环，把资源投到循环学习（B5 的 c 选项）？ | ⏳ 待讨论 |
| B4 | **问题跟踪的定位**：GitHub issue 式状态机，还是分析聚合数据，还是 Reflexion 记忆源 | 取决于场景定位 | 真实场景是 bench（问题跟踪=分析模型弱点，只需聚合）还是交付（问题跟踪=跟踪 bug 修复，需状态机+issue 联动）？若选 B5 的 b/c（反思式），Problem 还要服务 Reflexion/KnowledgeStore，需 failure_pattern 分类 | ⏳ 待讨论 |
| B5 | **循环智能层级**：agent_go 要不要从"反应式"升级到"反思式"？（来自 [research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) 调研） | 倾向先做最小止血（无进展检测 + 数据埋点） | a) 保持反应式；b) 补全 guardrails；c) 先做无进展检测和埋点，后续依据 M3 数据决定 | ⏳ 待讨论 |

**B 类讨论顺序建议**：B2（定位）→ B5（循环智能层级）→ B4（问题跟踪定位）→ B1（merge 策略）→ B3（spec ROI）。B2 的答案直接决定 B3/B4/B5 的取舍；B5 的答案影响 B4（Problem 实体是否要承载 Reflexion 记忆）。

**B5 选项与调研建议的对应**（[research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) §五·五章）：
- B5-a（保持反应式）= 现状
- B5-c（最小止血）= 调研建议 2（无进展检测，1-2 天）+ 建议 5（数据埋点，1 天）
- B5-b（补全 guardrails）= 调研建议 1（Reflexion）+ 建议 2 + 建议 3（局部重规划）+ 建议 5

---

## 闭环缺口速查

基于代码事实核查（2026-08-08）+ [research-goal-loop-mechanism](../archive/reference/research-goal-loop-mechanism-2026-08-08.md) 调研，agent_go 有 5 个闭环缺口——4 个工程闭环缺口 + 1 个智能闭环缺口。

| 缺口 | 名称 | 类型 | 紧急度 | 代码事实证据 | 对应 Milestone/决策 |
|------|------|------|--------|------------|------------------|
| 1 | **交付闭环断裂** | 工程 | 🔴 最高 | fixture 仓库 main 仅 1 commit，堆积 811 个 agent_go/* 孤立分支；base_branch 字段生产代码零写入点；cmd_pr 推 `HEAD:main`（bug） | M1 |
| 2 | **goal 回溯断裂** | 工程 | 🟡 中 | pipeline.py:645-647 "无 subtask 失败=completed"否定式，不回看 goal/acceptance/overview | M4 |
| 3 | **问题跟踪断裂** | 工程 | 🟡 中 | 单任务内完整，跨任务无 problems 实体；eval 只算聚合率丢弃明细 | M5-M6 |
| 4 | **spec 闭环断裂** | 工程 | 🟢 低 | --spec 代码完整但历史 0 次使用（874 任务无 spec 注入痕迹）；spec 不持久化、§5 不结构化 | M2-M4 |
| 5 | **循环智能断裂** | 智能 | 🟡 中 | 验证循环是"反应式"（注入 stderr 重试同样方法），无无进展检测/无根因分析/无重规划触发；Loop Engineering 6 项 guardrails 缺 3 项（grep `reflect\|self_correct\|meta_learn` 零命中） | B5 决策 |

**缺口性质区分**：
- 缺口 1-4 是**工程闭环**问题（数据没记录/没回流/没跟踪）——"有没有"的问题
- 缺口 5 是**智能闭环**问题（不会从失败学习）——"会不会"的问题，更深一层

**缺口 5 的 KPI 影响**（来自调研 §五·五章，基于 2026-08-06 bench 基线）：
- K8 首次验证通过率 88.9% → 补全 guardrails 后预期 ≥92%（Reflexion 减重复错误 30-50%）
- K4 成本：无进展检测可省 hard 任务 ~20-40% retry 成本（当前 hard max_retries=5，后 2-3 次常是空转）
- K1 任务成功率 83.9% → 局部重规划预期 +3-5pp（解决 plan brittleness 头号失败模式）

---

## 6 个 Milestone 速查

| M | 名称 | 对应缺口 | 工作量 | 依赖 |
|---|------|---------|--------|------|
| **M1** | 交付修复 | 缺口 1 | ~2 天 | 无（必须先做） |
| **M2** | spec 持久化 | 缺口 4 | ~1 天 | 无（可与 M5 并行） |
| **M3** | spec 端到端验证 | 缺口 4 | ~1 天 | M2 |
| **M4** | goal 回溯 | 缺口 2 | ~2 天 | M3 |
| **M5** | 问题跟踪 | 缺口 3 | ~2 天 | 无（可与 M2 并行） |
| **M6** | issue 联动 | 缺口 3 | ~1.5 天 | M5 |

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
