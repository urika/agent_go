# SDD（Specification-Driven Development）参考资料与论文框架

> **版本**：v1.2（serper 补充调研，新增 8 篇论文 + 对比仓库）
>
> **目的**：系统整理 SDD 相关的学术论文、开源框架、行业实践，形成可复用的参考资料库。供 agent_go 的 Task Spec 设计、Spec Gate 机制、架构分解能力演进提供理论和实践参照。
>
> **日期**：2026-08-01

---

## 一、SDD 定义与三级模型

Spec-Driven Development（规范驱动开发），又称 Specification-Driven Development（SDD）。核心主张：**规范（Specification），而非代码，应成为软件的主要制品。代码服务于规范，而非反过来。**

论文 **arXiv:2602.00180** 提出了 SDD 的三级模型：

| 级别 | 名称 | 定义 | 适用场景 |
|------|------|------|---------|
| **Level 1** | Spec-First（规范先行） | 编码前写 Spec，指导首次实现。Spec 可能在实现后被丢弃 | SDD 的入门级，适合探索性任务 |
| **Level 2** | Spec-Anchored（规范锚定） | Spec 与代码一起维护，作为「活文档」。自动化检查确保一致性 | **多数生产系统的最佳平衡点** |
| **Level 3** | Spec-as-Source（规范即源） | 人类只编辑 Spec，代码完全由 AI 从 Spec 生成，从不手动修改 | 安全关键领域（OpenAPI→stub, Simulink→嵌入式 C） |

**agent_go 的定位**：Task Spec 7 章节 + `--spec` → agent_go Plan → Execute。处于 **Level 2（Spec-Anchored）**：Spec 是 agent_go 的输入契约，执行后 Spec 归档，bench 数据反馈验证一致性。

---

## 二、核心论文

### 2.1 综述与框架论文

#### P1: Spec-Driven Development: From Code to Contract in the Age of AI Coding Assistants
- **arXiv**: 2602.00180 (2026.1)
- **作者**: Deepak Babu Piskala（提交至 AIWare 2026）
- **页数**: 8 pages, cs.SE / cs.AI
- **链接**: https://arxiv.org/abs/2602.00180
- **核心贡献**:
  - 提出 SDD 三级模型（Spec-First / Spec-Anchored / Spec-as-Source）
  - 四阶段工作流：Specify → Plan → Implement → Validate
  - 「Self-spec」方法：LLM 先自写 Spec，人 review 后实现
  - 工具调查：BDD（Cucumber/Gherkin）、GitHub Spec Kit、Kiro、Tessl、Specmatic
  - 关键数据：人类精炼的 Spec 可减少错误高达 50%
- **对 agent_go 的参考价值**: SDD 三级模型直接映射到 agent_go 的输入策略。Spec-Anchored = agent_go 的目标定位。

#### P2: Specification-Driven Development as the Foundation of AI-Native Enterprise Software Engineering
- **arXiv**: 2607.16680 (2026.7)
- **核心贡献**:
  - 提出 **SGRM（Specification Governance Reference Model）**——工具无关的参考模型
  - 定义了四组件规范契约、三级严格度、闭环架构（生成→验证→治理）
  - 对照 ISO/IEC 25010 评估
  - 关键数据：**宪法级约束下安全缺陷减少 73%**，规范治理的 Agent 交付缩短 **50% 上市时间**
- **对 agent_go 的参考价值**: SGRM 的「生成→验证→治理」闭环 = agent_go 的 Plan → Execute → Verify → bench 反馈。SGRM 的四组件规范契约可用作 Task Spec 7 章节的理论验证。

#### P3: Constitutional Spec-Driven Development: Enforcing Security by Construction in AI-Assisted Code Generation
- **arXiv**: 2602.02584 (2026.2)
- **作者**: Srinivas Rao Marri
- **核心贡献**:
  - 将不可协商的安全原则（基于 CWE/MITRE Top 25）嵌入规范层
  - 版本化的、机器可读的「宪法」（Constitution）
  - 银行微服务案例研究
  - 关键数据：宪法约束下**安全缺陷减少 73%**，同时保持开发速度，从原则到代码位置完整可追溯
- **对 agent_go 的参考价值**: 「宪法」= agent_go 的 Skill 系统 + Task Spec §4 约束。agent_go 的 Skill（domain/convention 类型）可以对应 CWE/MITRE 的安全原则。

#### P4: AfterVibe: What Remains When the Conversation Ends
- **arXiv**: 2607.09900 (2026.7)
- **作者**: Paltenghi & Chandra (Meta)
- **核心贡献**:
  - **回顾性规范恢复**：从 vibe coding 会话（代码+对话轨迹）中提取自然语言 Spec
  - **再生测试**（Regeneration Test）：盲 AI Agent 仅凭 Spec 重新实现 → 三层验证器（灵活测试执行、验证条件、ground-truth 对齐）评估等价性
  - 实验：72 个真实 vibe-coded 任务，恢复的 Spec 平均再生得分 **5.06/6.0**，迭代强化至 **5.74/6.0**
  - Spec 比原始 diff 简洁 **5.6 倍**
  - 核心主张：「规范，而非代码，应成为 AI 辅助开发时代人类创作的主要制品」
- **对 agent_go 的参考价值**: 「再生测试」是验证 Spec 质量的方法——agent_go 的 bench 框架可以用来做类似验证（同一 Spec × 不同模型 → 检查一致性）。

### 2.2 需求对齐与代码生成

#### P5: Aligning Requirement for Large Language Model's Code Generation (Specine)
- **arXiv**: 2509.01313 (2025.9), ICSE '26
- **核心贡献**:
  - 识别「规范不对齐」——输入 Spec 与 LLM 感知的 Spec 之间的差距
  - Specine 将 LLM 感知的 Spec 提升为 DSL 表示，应用十条对齐规则
  - 关键数据：4 个 LLM × 5 个编程基准，**平均 Pass@1 提升 29.60%**
- **对 agent_go 的参考价值**: 「规范不对齐」= agent_go 的 Plan prompt 注入质量问题的理论解释。Specine 的对齐规则可以部分转化为 agent_go 的 system prompt 指令（REQ-1~4）。

### 2.3 审查与审计

#### P6: From Code Review to Spec-Driven Contracts: A Vision for Auditable AIWare Systems
- **发表**: ACM AIWare 2026 (3rd ACM International Conference on AI-Powered Software)
- **作者**: Hamdaqa & Chouchen
- **链接**: https://dl.acm.org/doi/10.1145/3805760.3814898
- **核心贡献**:
  - 论证**仅靠代码审查无法实现可审计性**——需要基于规范的、契约驱动的系统
  - 定义允许、要求、禁止的行为 → 在 CI/CD 流水线、运行时、事后审计中强制执行
- **对 agent_go 的参考价值**: agent_go 的 P2 REQ-7（架构一致性验证）和 REQ-8（设计文档与代码双向追溯）的理论基础。

### 2.4 开源框架比较研究

#### P7: From Prompt to Process: a Process Taxonomy and Comparative Assessment of Frameworks Supporting AI Software Development Agents
- **arXiv**: 2606.04967 (2026.6)
- **核心贡献**:
  - 对主流 AI 软件开发框架的**流程分类学**和**比较评估**
  - 覆盖六个维度：规范（Specification）、上下文（Context）、角色（Roles）、执行（Execution）、验证（Validation）、可移植性（Portability）
  - 结论：**没有任何框架在所有六个维度上都强**——流程深度和可移植性之间存在结构性权衡
- **对 agent_go 的参考价值**: 验证了 agent_go 的定位选择（在 Execution 和 Validation 维度上深度投入，在 Portability 维度上保持 model-agnostic）。

### 2.5 理论基础与范式转变（serper 补充，2026-08-01）

#### P8: Rethinking Software Engineering for Agentic AI Systems ⭐ 高被引综述
- **arXiv**: 2604.10599 (2026.4) | **作者**: Mamdouh Alenezi | **被引**: 11 次
- **核心论点**: 代码正从「稀缺、精心手工的制品」转变为「充裕、日益即用即弃的商品（commodity）」
- **三大核心能力重组**:
  1. **编排（Orchestration）**：多 Agent 系统的有效编排
  2. **验证（Verification）**：AI 生成产出的严格验证
  3. **人机协作（Human-AI Collaboration）**：结构化的人机协同
- **研究挑战**: verification-first 生命周期、prompt 可追溯性、工程劳动力长期演进
- **关键主张**: 「这一转变不是削弱工程师角色，而是将其职责提升到系统级设计、语义验证和有责监督」
- **对 agent_go 的参考价值**: **这是 agent_go 整体架构的理论基础**。agent_go 的 Plan→Execute→Verify 流水线 + bench 评估 + 人审查 = 论文主张的「编排+验证+人机协作」三能力的工程实现。Alenezi 是该领域最高产的研究者（本目录收录其 3 篇论文）。

### 2.6 多 Agent 协调与规范鸿沟（serper 补充，关键）⭐⭐⭐

#### P9: The Specification Gap: Coordination Failure Under Partial Knowledge in Code Agents
- **arXiv**: 2603.24284 (2026.3) | **作者**: Camilo Chacón Sartori
- **直接对应 agent_go 的 Planner 问题**：多个 LLM code agent 独立实现同一类的不同部分时，必须对共享的内部表示达成一致——即使 spec 把这些选择留作隐式
- **实验设计**: 51 个类生成任务，逐步剥离 spec 细节（L0 完整 docstring → L3 裸签名），并引入对立的结构偏差（list vs dict）压测集成
- **三个发现**:
  1. **持续的规范鸿沟**: 双 Agent 集成准确率从 58% 跌到 25%（spec 细节移除），单 Agent 基线只从 89% 跌到 56% → **25-39pp 的协调鸿沟**，在 Sonnet + Haiku + 3 次独立运行中一致
  2. **AST 冲突检测器在最弱 spec 级别达 97% 精度，无需额外 LLM 调用**；但恢复实验显示：仅恢复完整 spec 就能恢复到单 Agent 上限（89%），冲突报告无额外收益
  3. 鸿沟可分解为：协调成本（+16pp）+ 信息不对称（+11pp），两者独立且近似可加
- **核心结论**: 「鸿沟不仅是隐藏信息的后果，更反映了在没有共享决策的情况下产出兼容代码的困难。更丰富的规范既是**主要协调机制**，也是**充分的恢复手段**。」
- **对 agent_go 的参考价值**:
  - **直接验证了 Task Spec 7 章节设计的必要性**——spec 越丰富，多子任务协调越成功
  - **验证了 REQ-1~4（prompt engineering）的最高 ROI**——恢复完整 spec 即可恢复到单 Agent 上限，不需要额外的冲突检测机制
  - **给 git worktree + tag merge 提供了理论解释**——agent_go 用 git 传递产物（共享决策的具体化）而非 prompt 传递，恰好是论文主张的「共享决策」机制
  - **AST 冲突检测器是 P2 候选**——97% 精度无需 LLM，可作为 Spec Gate 的增强

### 2.7 规范作为质量门禁（serper 补充，关键）⭐⭐⭐

#### P10: The Specification as Quality Gate: Three Hypotheses on AI-Assisted Code Review
- **arXiv**: 2603.25773 (2026.3) | **作者**: Christiaan Zietsman | **被引**: 4 次
- **核心论点**: 用 AI 审查 AI 生成的代码，在没有可执行规范时是**结构性循环（structurally circular）**——生成 Agent 和审查 Agent 从同一制品推理、共享同一训练分布、产生相关失效。「审查是代码对自己做检查，而不是对意图做检查。」
- **三个假设**:
  1. **同质 LLM 流水线中的相关错误是「回响」而非「抵消」**——Claude 审 Claude 产的代码、跨 4 模型 3 家族的实验支持此说
  2. **可执行规范执行领域转换（Cynefin 意义上）**——把 enabling constraints 转为 governing constraints，将问题从 complex 域移到 complicated 域；AI 让这种转换在大规模上经济可行
  3. **可执行规范触及不到的缺陷类构成一个明确定义的「残差（residual）」**——这才是 AI 审查合法且有界的靶标
- **推荐架构**: **规范优先 → 确定性验证流水线其次 → AI 审查只用于结构和架构残差**
- **对 agent_go 的参考价值**:
  - **直接验证了 agent_go 的验证架构**: shell 验证（确定性）+ semantic 评估 + Reviewer 不同源 = 论文推荐的「规范→确定性验证→AI 审查残差」三层
  - **强烈支持 cross_judge 和「Reviewer 不同源」铁律**——同源审查是「回响」，必须跨家族
  - **界定了 semantic evaluator 的职责边界**——只查「结构和架构残差」，不要试图用 LLM 审查可确定性验证的部分

### 2.8 其他补充论文（serper 检索，未深读）

| 论文 | 编号 | 要点 |
|------|------|------|
| The Productivity-Reliability Paradox: Specification-Driven Governance for AI-Augmented Software Development | arXiv:2605.01160 (2026.5, 被引 4) | 提出「生产率-可靠性悖论」——LLM 代码生成器的非确定性意味着加速个体生成而不加治理会损害可靠性；为 SDD 提供理论基础 |
| Specification-Driven Development Benchmark: Security Knowledge Transition | arXiv:2606.00167 (2026.6) | SDD 中安全知识如何跨 spec 传递的 benchmark；multi-agent orchestration 研究 |
| From Determinism to Delegation: AI-Native Software Engineering and the Evolution of the Agentic Engineer | arXiv:2606.28791 (2026.6, Alenezi) | 「从确定性到委派」——Alenezi 系列第三篇，讨论 Agentic Engineer 的演进 |
| Specification-Driven Generation and Evaluation of Discrete-Event World Models via the DEVS Formalism | arXiv:2603.03784 (2026.3) | 形式化方法角度——用 DEVS 形式体系做规范驱动的生成与评估，分离结构推理与组件逻辑 |
| Specification-Driven Application Skeleton Generation Using a Multi-agent System | Springer 2025 (Fekih, Kamoun) | 多 Agent 系统做规范驱动的应用骨架生成，含 LLM 评估器 |

---

## 三、开源框架

### 3.1 三大主流

| 框架 | 创建者 | Stars | 定位 | 工作流 |
|------|--------|-------|------|--------|
| **Spec-Kit** | GitHub | ~69k-92k | 结构化、阶段门禁、面向新项目 | Constitution → Specify → Clarify → Plan → Tasks → Implement |
| **OpenSpec** | Fission-AI (YC W26) | ~44.5k | 轻量、变更驱动、面向存量项目 | Propose → Apply → Archive（3 个核心命令） |
| **BMAD-METHOD** | Brian (bmadcode) | ~35k-43k | 多 Agent 全生命周期 | 12+ Agent 角色分工（Analyst→Architect→PM→Developer→QA） |

### 3.2 框架选择指南

| 条件 | 推荐 |
|------|------|
| 存量代码库、快速迭代、个人开发者 | **OpenSpec**（最低摩擦，5 分钟启动） |
| 新项目、团队协作（2-5+ 人）、需要标准化 | **Spec-Kit**（Constitution 机制是核心创新） |
| 企业级、合规要求、需要完整审计追溯 | **BMAD**（产物可直接作为 SOC2/HIPAA 审计证据） |
| 不确定 | **OpenSpec**（最低锁定，同时适用存量和新项目） |
| 混合策略 | OpenSpec（Bug）+ Spec-Kit（新功能）+ BMAD（重大企业级项目） |

### 3.3 三个框架的共同结构（同构性）

BMAD 社区提出了一个关键观察：**三者结构同构（Structurally Isomorphic）**——命名和抽象级别不同，但都表达同一个三元组：

```
Agent（执行者）× Workflow（流程）× Skill（能力）
```

差异是「表达的」而非「结构的」。这意味着**三者可以互通**——一个框架的 Spec 经过适配可以在另一个框架中执行。

### 3.4 其他相关框架

| 框架 | 定位 | 特点 |
|------|------|------|
| **Tessl** | 商业 spec-as-source 平台 | 受监管行业的审计追踪 |
| **AWS Kiro** | spec-driven IDE | EARS 符号的结构化验收标准；两周功能缩短至两天 |
| **Specmatic / TypeSpec / OpenAPI** | 可执行契约 | API 层面的契约驱动 |
| **Superpowers** | 跨平台方法论 | 最灵活，方法论而非工具；**166k stars（serper 补充，规模最大）** |
| **GSD** | SDD 框架 | **~48k stars（serper 补充）** |
| **MumuSpec** | 中文社区 SDD 工具 | 类似目标，独立实现 |
| **Fully-Coding** | Claude Code 端到端 Skill | 与 OpenSpec/Spec-Kit/BMAD 的对比和互补 |

### 3.5 框架规模与增速（serper 补充，2026-08-01）

各家 stars 数据各来源不一致，取区间值：

| 框架 | Stars（区间） | 备注 |
|------|-------------|------|
| Superpowers | ~166k | 跨平台方法论，规模最大 |
| Spec-Kit | ~80k-93k | GitHub 官方背书，分发优势 |
| GSD | ~48k | |
| BMAD-METHOD | ~37k-45k | 全生命周期，v6 stable |
| OpenSpec | ~5.8k-52k | 数值差异最大（可能含 fork）；活跃维护 |

**品类增速信号**（YouTube/行业报告）：top 5 框架的 stars 在 12 个月内从 87k 跳到 202k；其中一个框架 6 个月增长 863%。**SDD 已从博客话题变成 AI coding 的默认架构选项**（Thoughtworks、Martin Fowler 背书）。

### 3.6 直接可用的对比研究

serper 检索到一个高价值仓库，与 agent_go 的 bench 框架思路高度一致：

- **[cameronsjo/spec-compare](https://github.com/cameronsjo/spec-compare)** — 用 **git worktree 分析**比较 6 个 SDD 工具（Spec-Kit、Spec Kitty、BMad、OpenSpec、Kiro、Tessl），含决策框架
- **ranthebuilder.cloud 实测**：同一真实功能上对 BMAD/Spec-Kit/OpenSpec 在 13 个维度打分
- **Medium: Comparing 15 SDD Frameworks** — 最宽口径对比

**对 agent_go 的参考价值**: spec-compare 用 git worktree 做工具评估——与 agent_go 的 bench（worktree 隔离 + 评估）方法同构。可作为 bench v2 设计的参照。

---

## 四、关键经验数据

### 4.1 SDD 有效的证据

| 数据 | 来源 | 可靠性 |
|------|------|--------|
| 宪法约束下安全缺陷减少 **73%** | arXiv:2602.02584（银行微服务案例） | 🟡 单案例研究 |
| 规范治理的 Agent 交付缩短 **50%** 上市时间 | arXiv:2607.16680（SGRM 评估） | 🟡 理论推导 |
| 人类精炼的 Spec 可减少错误高达 **50%** | arXiv:2602.00180（综合引用） | 🟡 二次引用 |
| 恢复的 Spec 再生得分 **5.06/6.0**，强化后 **5.74/6.0** | arXiv:2607.09900（72 个真实项目） | 🟢 有对照实验 |
| Specine 平均 Pass@1 提升 **29.60%** | arXiv:2509.01313（4 LLM × 5 基准） | 🟢 有对照实验 |
| **恢复完整 spec 即恢复单 Agent 上限 89%（vs 裸签名 56%）** | **arXiv:2603.24284（51 任务受控实验）** | 🟢 **最强实验证据** |
| **AST 冲突检测器 97% 精度，无需额外 LLM 调用** | **arXiv:2603.24284** | 🟢 受控实验 |
| Shopify 结构化 AI 集成减少开发时间 **38%** | 企业报告 | 🟡 企业自报 |
| Box 85% 开发者用 Cursor rules，路线图吞吐量提升 **30-50%** | 企业报告 | 🟡 企业自报 |

### 4.2 Vibe Coding 的问题证据

| 数据 | 来源 | 可靠性 |
|------|------|--------|
| AI 生成代码中安全漏洞率 **9.8%-42.1%**（跨基准） | Yan et al. (2025)，多论文引用 | 🟢 多来源验证 |
| AI 协作代码重大issue率 **1.7x**，安全漏洞率 **2.74x** | SonarQube/CodeRabbit（470 PRs） | 🟢 有数据分析 |
| 截至 2026.2，生产仓库中 **110,000+** 存活的 AI 引入问题 | arXiv 2026 实证研究 | 🟡 二次引用 |
| 开发者使用 AI 工具后实际耗时**多 19%**，但主观感觉快了 24% | METR RCT（随机对照试验） | 🟢 RCT |
| Llama 3.2 90B 超 70% 检测到的漏洞为 BLOCKER 级别 | SonarQube 分析 | 🟢 有数据分析 |
| 重构活动占比从 25% 降至 <10%，复制粘贴代码增 4x | GitClear（211M 行代码） | 🟢 有数据分析 |

### 4.3 SDD 的局限（诚实面对）

| 局限 | 来源 | 说明 |
|------|------|------|
| **尚无对照实验**证明 SDD 优于「裸 AI 辅助编码」 | 多来源交叉验证，包括 Thoughtworks 的 Birgitta Böckeler | SDD 的优势目前是「理论推导 + 案例证据」，不是受控试验 |
| **工具可用性问题** | Böckeler 独立评估 | Kiro 将一个小 Bug 变成 4 个 User Story、16 条验收标准；审查负担从代码转移到 Markdown 文件 |
| **Agent 不必然遵守 Spec** | 多来源 | 即使有了完整的 SDD 基础设施，AI Agent 有时仍然不遵循 Spec 中的所有指令——产生「虚假的控制感」 |
| **「苦教训」批判** | Rich Sutton 的「Bitter Lesson」外推 | 手工编写详细规则可能不随模型能力提升而持续有效——通用方法最终胜过领域特定的工程 |
| **MDD 重演风险** | 多来源，包括 Thoughtworks Radar | SDD 有重蹈 2000-2010 年 MDD（Model-Driven Development）失败的风险——LLM 移除了形式语言的障碍，但增加了非确定性 |

---

## 五、SDD 成熟度光谱

综合多篇论文和行业实践，可以构建一个六级的 SDD 成熟度模型：

| 级别 | 名称 | 描述 | 关键实践 |
|------|------|------|---------|
| **L0** | Raw Vibe Coding | 自然语言 prompt→代码，无规范，无审查 | 「全凭感觉」 |
| **L1** | Structured Prompt | 详细的 prompt 作为一次性规范 | 结构化自然语言 |
| **L2** | Spec-First | 编码前写 Spec，Spec 可能被丢弃 | Spec → 代码（单向） |
| **L3** | Spec-Anchored | Spec 与代码一起维护，自动化一致性检查 | Spec ↔ 代码（双向，有门禁） |
| **L4** | Constitutional SDD | 不可协商的原则嵌入规范层，CI/CD 强制执行 | 宪法 → Spec → 代码 → 验证 → 治理 |
| **L5** | Spec-as-Source | 人类只编辑 Spec，代码完全生成 | Spec → 代码（全自动），Spec 是唯一 source of truth |

**agent_go 当前**: L2（Spec-First，通过 `--spec`）→ 目标 L3（Spec-Anchored，通过 bench 验证 + Spec Gate）。

---

## 六、对 agent_go 的关键启示

### 6.1 验证了我们方向正确的

| SDD 共识 | agent_go 对应 | 学术支撑 |
|---------|-------------|---------|
| Spec-First 工作流 | Task Spec 7 章节 + `--spec` | P1/P8/P9 |
| 自动化门禁 | Spec Gate L1 硬门禁 + L2 软警告 | P1/P3 |
| 阶段间约束传递 | Spec → Plan prompt 注入（REQ-3） | P9（恢复完整 spec 即恢复 89% 上限） |
| 闭环反馈 | bench 数据 → 模型分级 → 路由决策 | P8（验证优先生命周期） |
| 人审查架构决策 | Plan 确认（Y/S/D/E/R/N） | P8（有责监督） |
| **git worktree 产物传递** | **tag merge 而非 prompt 传递** | **P9（共享决策的具体化是协调主机制）** |
| **Reviewer 不同源** | **cross_judge + judge != candidate** | **P10（同源审查是「回响」）** |

### 6.2 提示了需要加强的

| SDD 启示 | agent_go 需要做的 | 学术支撑 |
|---------|-----------------|---------|
| 「宪法」机制（Constitutional SDD） | Skill 系统是 agent_go 的「宪法」——domain/convention Skill = 不可协商原则。需加强其在 Plan/Worker 阶段的**强制执行**（非建议） | P3 |
| 规范不对齐问题（Specine） | REQ-1~4（prompt engineering）让 Planner 理解的与 Spec 作者意图一致 | P5/P9 |
| **AST 冲突检测器（无需 LLM，97% 精度）** | **Spec Gate 可加 AST 层：检测多子任务文件冲突，零 LLM 成本** | **P9** |
| 再生测试（AfterVibe） | bench 框架加「同一 Spec × 多模型重跑 → 一致性评分」作为 Spec 质量度量 | P4 |
| 没有框架在六个维度上都强 | 继续在 Execution/Validation 深度投入，不在 Specification 维度与 Spec-Kit/OpenSpec 竞争 | P7 |

### 6.3 警告了需要警惕的

| 警告 | agent_go 的应对 | 学术支撑 |
|------|---------------|---------|
| **SDD 尚无对照实验证明优于裸 AI**（最大局限） | S10 bench v2 全因子设计对比「有 Spec vs 无 Spec」的 pass_rate → **自己做对照实验**；**P9 已提供了一个对照实验范本（51 任务，L0-L3 spec 细节梯度）** | 多源/P9 |
| 「虚假的控制感」——Agent 可能不遵循 Spec | L2 软警告 + semantic evaluator 检查代码是否符合 Spec 约束 | 批判性审视 |
| 「苦教训」——手工规则可能不随模型升级持续有效 | Skill 系统的 bench 反馈闭环：每次 bench 重跑自动评估 Skill 有效性 → 自动标记过时 | Rich Sutton |
| **同源审查的循环论证** | semantic evaluator 的 judge 必须跨家族；可执行验证（shell）优先，LLM 审查只用于「结构残差」 | **P10** |
| SDD 可能重蹈 MDD 失败 | agent_go 不做「形式化规范语言」——Task Spec 是自然语言 Markdown，人工可读可写 |

---

## 七、推荐阅读顺序

**如果想快速建立知识框架（2-3 小时）**：

1. arXiv:2602.00180（SDD 综述，8 页）→ 建立 SDD 三级模型
2. OpenSpec vs Spec-Kit vs BMAD 框架对比（Dev.to / HackerNoon）→ 了解工具生态
3. arXiv:2607.09900（AfterVibe）→ 理解「规范恢复」和「再生测试」
4. arXiv:2602.02584（Constitutional SDD）→ 理解「宪法」机制

**如果想深入特定领域**：

- **安全**: arXiv:2602.02584（Constitutional SDD）
- **企业治理**: arXiv:2607.16680（SGRM）
- **需求对齐**: arXiv:2509.01313（Specine）
- **可审计性**: ACM AIWare 2026（Spec-Driven Contracts）
- **框架比较**: arXiv:2606.04967（Process Taxonomy）

---

*数据来源：arXiv, ACM Digital Library, GitHub, Dev.to, HackerNoon, Thoughtworks Technology Radar, 知乎/CSDN/掘金 中文技术社区。日期：2026-08-01。*
