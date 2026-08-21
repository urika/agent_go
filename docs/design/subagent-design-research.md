# Subagent 设计调研与 agent_go 拆分算法改进

> 目的：回答「agent_go 什么情况下该拆分子任务、拆分的判据是什么、多 Agent 如何分工」。
> 方法：调研 opencode 与 Claude Code 的多 Agent / Subagent 设计，对照 agent_go 的
> worktree + 拓扑波次 + 角色化 subtask 机制，结合真实 bench 数据，给出拆分算法改进建议。

日期：2026-08-10
状态：调研完成 + 拆分算法改进已实施（G6 升级 / G7 / G8）

## 1. Subagent 的核心价值

**Subagent 本质上是一个上下文隔离机制。** 它不是让系统「更聪明」，而是解决一个具体
的工程问题：长会话中的上下文污染——grep/find/ls 的噪声输出、探索性的错误日志、试错
过程会填满上下文窗口，导致模型注意力退化、判断力下降。

OpenCode 和 Claude Code 的 subagent 设计围绕四个共同原则：

| 原则 | OpenCode | Claude Code |
|------|----------|-------------|
| 上下文隔离 | 子 agent 独立 session，只返回结果摘要 | 独立 context window，不继承父会话历史 |
| 工具限制 | tools allowlist（Read/Grep/Glob/Bash），内建 explore（只读）和 general（全能力） | 按 agent 类型配置工具集（reviewer 无 Edit/Bash，file-creator 无 Bash） |
| 权限控制 | 三级 permission（auto/ask/none）+ 细粒度 bash/edit/web 权限 | permissionMode + deny/ask/allow 规则 |
| 单层嵌套 | 子 agent 不可再 spawn 子 agent | 子 agent 不可再 spawn 子 agent |

## 2. 什么时候使用 Subagent

来自 Anthropic 官方指南和社区最佳实践：

1. **上下文隔离（最强信号）**：冗长输出任务（测试套件/日志分析/API 探索）、
   探索性搜索（跨目录 grep/find）、独立研究路径——产生大量 token 但只需返回结构化摘要。
2. **工具/权限边界（安全考量）**：只读审查者（安全审查/性能分析）、受限执行环境
   （无 Bash 的代码生成器）、外部网络隔离（WebFetch 有/无）。
3. **异构认知模式**：探索 vs 实现（Haiku 扫描 + Sonnet/Opus 实现）、计划 vs 执行
   （Plan 只读 + Worker 全能力）、审查 vs 构建（独立 reviewer 捕捉「实现者盲区」）。
4. **可独立产生证据（最关键的判断标准）**：一个子任务值得拆分，当且仅当它能独立产出
   可验证的证据——测试通过/失败、可复现的调用链、可审查的 diff——而不依赖其他部分
   未完成的判断。

## 3. 什么时候不应使用 Subagent

社区共识更强烈的一条：**默认应该是单 agent**，只在单 agent 出现具体瓶颈时才加 subagent。

| 反模式 | 原因 |
|--------|------|
| 小范围局部修改 | delegation 开销 > 收益，一次简单 prompt 就够了 |
| 串行依赖工作流 | 步骤 2 需步骤 1 全部输出——单 session 串行更干净 |
| 同文件编辑 | 两个 subagent 并行编辑同一文件必然冲突 |
| 紧耦合组件 | 需要持续交互/共享状态的组件必须在同一 agent 内 |
| 无法切割因果链的任务 | 若 subagent 仍需完整历史+全部文档+全部工具——无真正上下文边界，拆分无意义 |
| 「协调剧场」 | 更多中间产物 ≠ 更多真相。多个 agent 可能都基于同样的不完整事实 |

**核心判断公式**：使用多 agent 当且仅当
- 单 agent 上下文窗口不够大
- OR 有明显的并行收益（且子任务真正独立）
- OR 需要异构认知模式（只读审查 + 全能力实现）
- OR 错误/慢速的成本很高（值得用 token 换质量）

## 4. 主流设计模式

| 模式 | 描述 | 对应 |
|------|------|------|
| 可替换集群（Fungible Swarm） | 多个相同 agent 从共享任务板取任务，最推荐默认 | OpenCode general |
| 层级分派（Hierarchical） | Manager 分解 → Worker 并行 → Manager 聚合 | **agent_go 当前** |
| 编排委托（Orchestrator Delegation） | 主 conversation 链式调 subagent，subagent 间不通信 | Claude Code Workflow |
| 对等协作（Peer Collaboration） | 多 agent 迭代至共识 | 代码审查 |
| 两阶段审查（Two-Stage Review） | 先 spec 合规检查，再代码质量检查 | 高风险代码 |
| 合约先行（Contract-First） | 先冻结接口合约，再并行派发 builder；只有 coordinator 能改合约 | 最安全并行 |

## 5. 关键设计权衡

- **上下文隔离 vs 上下文丢失（Telephone Game）**：subagent 不继承父历史 → 干净但可能
  丢关键信息，多个 agent 基于「同样不完整事实」做不一致决策。缓解：system prompt 注入
  关键约束和上下文摘要。
- **Token 成本**：单 agent 1x → +1-2 subagent ~4x → 多 agent 工作流可达 15x。按角色拆分
  （planner/implementer/tester/reviewer）时，协调 token 消耗可能超过实际工作。
- **并行数量**：推荐 2-4 个 subagent，每 agent 处理 5-8 个 item，超过收益递减。
- **模型选择**：Opus=安全审计/复杂调试；Sonnet=审查/测试编写；Haiku=文档/快速探索。
  跨模型多样性（builder 与 reviewer 用不同厂商）可消除同家族偏差。
- **关键约束**：subagent 不能 spawn sub-subagent（一层上限）；foreground 阻塞等权限，
  background 并发但 auto-deny 未预批准操作；重试循环必须硬上限（如 3 轮）。

## 6. opencode 的 Agent 体系

参考：https://opencode.ai/docs/agents/

### 6.1 Primary Agents

- **Build**：默认主 agent，全工具集（读写、执行、Web），执行任务。
- **Plan**：只读规划 agent，只用读工具，产出执行计划。

### 6.2 Subagents（子代理）

- **General**：通用子代理，可写，官方定位「run multiple units of work in parallel」。
- **Explore**：只读探索子代理，permission deny edit，用于代码库探索。
- **Scout**：外部研究子代理。

调用方式：主 agent 按 `description` 字段自动调用，或用户 `@name` 手动调用。

## 7. Claude Code 的并行机制

| 机制 | 定位 | 通信 | 适用 |
|------|------|------|------|
| **Subagents** | 专注型子代理，只要结果 | 单向汇报 | 低复杂度、独立子任务 |
| **Agent Teams** | 需要讨论的协作代理组 | 多向通信 | 中复杂度、需协商 |
| **Git Worktree** | 多任务隔离代码环境 | 通过 git 传递 | 同仓库多分支并行 |

### 7.1 Subagents 最佳实践（官方）

- 保持专注、限制工具、模型分级（Haiku/Sonnet/Opus）、避免文件冲突（不同文件集）、
  团队 3-5 人每队友 5-6 任务。

### 7.2 与 agent_go 的同构性

| agent_go | Claude Code 对应 |
|----------|------------------|
| git worktree add -b agent_go/{task}/{sub} | Git Worktree 多分支隔离 |
| 拓扑波次 + ThreadPoolExecutor | Agent Teams 并行 |
| upstream tag → git merge artifact 传递 | git 提交传递 |
| 角色化 agent_type + skills | Subagents 专注单一职责 |
| difficulty → worker_models | 模型分级 |

## 8. 核心结论：拆分的判据是「任务独立性」，不是文件数量

opencode 与 Claude Code 都把 subagent 视为「能力分离 + 并行 + 只读探索」机制，
而不是把大任务切块的机制。拆分判据是：**任务 A 与任务 B 能否互不依赖地独立完成？**
只读探索 → Explore；需讨论 → Teams；真正独立可并行 → Subagents / 并行波次。
Claude Code 官方明确「避免文件冲突」：**分解工作使每个队友负责不同的文件集**。

### 8.1 agent_go 当前的偏差（已修复 + 遗留）

已修复：
1. **LLM 自由拆分无硬约束** → G6/G7/G8 确定性校验（见 §9）。
2. **小改动也拆** → G6 过度分解 blocking。
3. **G6 只告警不阻断** → 升级为 blocking。
4. **verification 与改动不匹配** → G8 扩展 verification_file_mismatch。

遗留：
5. **上下文重复注入**：TASK.md 固定执行要求逐字重复进每个 subtask，成本线性膨胀。
6. **工具/权限最小化缺失**：subtask 只有 agent_type，无工具级权限隔离（Explore deny edit）。
7. **只读审查 subagent 缺失**：验证循环是同一 Claude Code 进程自修复，无独立只读审查。
8. **重试收敛判断缺失**：max_retries 无「连续不同缺陷提前终止」逻辑。

### 8.2 拆与不拆的判据（agent_go 已实施）

```
拆分的充分条件（需同时满足）：
  ① 子任务之间文件作用域互斥（不重叠）——否则必合（G7 blocking）
  ② 任务确有多份可独立验证的工作单元（无验证且被依赖 → G8 blocking）
  ③ 并行收益 > 上下文重复注入成本（≥2 文件 / 非全 easy）

禁止拆分的条件（任一命中）：
  A. 涉及文件 ≤2 且子任务 ≥3（G6 过度分解 blocking）
  B. 全 easy 难度但子任务 ≥3（G6 warning）
  C. 无依赖关系的子任务共享同一文件（G7 file_overlap blocking）
  D. 被依赖子任务无验证命令（G8 unverifiable_upstream blocking）
```

## 9. 拆分算法改进实施（已提交）

### 9.1 G7：跨子任务文件重叠检测（check_subtask_file_overlap）

提取每个子任务文件作用域（files ∪ files_hint 逗号拆分，`*` 忽略），对每个被 ≥2 个
子任务引用的文件：存在一对子任务无依赖路径 → blocking `file_overlap_without_dependency`；
都在同一依赖链 → warning `file_overlap_with_dependency`。依赖路径用 graph 做 BFS 闭包。

### 9.2 G6 过度分解升级为 blocking

`子任务数 ≥ 3` 且 `有效文件数 ≤ 2`（文件作用域非空）→ blocking `over_decomposition`。
对应 bench 实证的 fix-missing-default（5 行改动拆 3 个 → 成本翻倍且失败）。
`全 easy 但 ≥3 子任务` 保持 warning（多文件全 easy 并行拆分可能合法）。

### 9.3 G8：独立可验证性 + verification 匹配校验

- `unverifiable_upstream`（blocking）：被依赖的子任务无验证命令 → 上游产物未经验证
  即被下游消费。
- `verification_file_mismatch`（warning）：verification 引用其他 step 专属文件 →
  提示补充 depends_on 或改用本步骤文件；验证既有回归测试（非任何 step 专属）不误报。

### 9.4 planner prompt 拆分三原则（api.py 注入）

1. **文件互斥**：不同步骤不修改同一文件，必须时用 dependencies 串行。
2. **独立可验证**：verification 必须能独立运行，强耦合应合并。
3. **小改动不拆**：≤2 文件优先 1 步骤，禁止为小改动制造不必要的拆分。
4. **微小改动必合**：每文件 <15 行的微小改动即使跨 2-3 文件也应合并。
   rationale 必填：说明为什么拆/合并，及为什么不是 N±1 个。

### 9.5 验证（Split Design Benchmark）

6 任务 × claude/opencode × deepseek-v4-flash 同模型对比：12/12 命中期望。≤2 文件任务
两侧均判 1 子任务（连 implement-done-command 也判 1——而 agent_go 旧版拆 2 导致交叉
污染失败）；hard 任务均正确拆分且文件互斥。独立验证了 G6/G7/G8 判据方向正确。

## 10. 对 agent_go 的改进方向评估（基于调研 §1-5）

| # | 改进方向 | 现状 | 优先级 | 说明 |
|---|----------|------|--------|------|
| 1 | 异构模型路由 | difficulty → worker_models 已有 | P1 | ✅ 已落地：`worker_models_by_cognitive` 按认知模式（explore/implement/review）路由，覆盖 task_type/difficulty。模式来源：subtask.cognitive_mode 或按 agent_type 推断（architect→explore, reviewer→review）。探索便宜模型/实现强模型/审查独立模型 |
| 2 | 只读审查 subagent | 验证循环是同一进程自修复 | P1 | ✅ 已落地：`review_agent.py` 独立只读审查（`verification.readonly_review.enabled`），验证失败时黑盒分析失败根因，意见注入修复 prompt（对应调研 §2-3/§4 两阶段审查）。默认关闭（成本可控），fail-open 不阻断验证循环。支持 `readonly_review.skill` 加载领域审查维度（如 `~/.agent_go/skills/readonly-review/SKILL.md`），空则回退内置通用模板 |
| 3 | 上下文显式注入 | context.md 已注入直接上游摘要 | P2 | ⏳ 待落地：强化注入关键因果链/约束条件，减少 Telephone Game（调研 §5） |
| 4 | 重试收敛提前终止 | max_retries 硬上限 | P2 | ✅ 已落地：`diverge_similarity_threshold` 打地鼠检测——连续两次语义评估指出不同缺陷时提前终止（`_defect_fingerprint`/`_defect_similarity`） |
| 5 | 工具/权限最小化 | 无工具级隔离 | P2 | ✅ 已落地：subtask 支持 `allowed_tools`/`permission_mode` 字段覆盖 agent 默认（Explore deny edit 对应能力，调研 §1 四原则）。`--allowedTools` 白名单透传 claude -p |
| 6 | 上下文去重注入 | TASK.md 逐字重复 | P2 | ⏳ 待落地：共享基座 + 增量，打破成本线性膨胀 |

## 11. 参考资料

- OpenCode 核心设计：主 Agent 与子 Agent 的分层架构
- How and when to use subagents in Claude Code（Anthropic）
- Claude Code Subagents: A 2026 Practical Guide – Tembo
- Best practices for Claude Code subagents – PubNub
- Agent Patterns Skill – oakoss/agent-skills
- AI Coding Agent 工程化：从上下文污染到多 Agent 分工
- 单 Agent 与多 Agent 的架构取舍
- Agentic Autonomy Levels – Addy Osmani
- 项目内部：docs/archive/design/bench-convergence-plan.md、docs/design/plan-capability-phaseb-2026-08-09.md
