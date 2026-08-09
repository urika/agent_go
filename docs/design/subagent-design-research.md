# Subagent 设计调研与 agent_go 拆分算法改进

> 目的：回答「agent_go 什么情况下该拆分子任务、拆分的判据是什么」。
> 方法：调研 opencode 与 Claude Code 的多 Agent / Subagent 设计，对照 agent_go 的
> worktree + 拓扑波次 + 角色化 subtask 机制，结合真实 bench 数据，给出拆分算法改进建议。

日期：2026-08-10
状态：调研完成 + 拆分算法改进已实施

## 1. 调研背景

agent_go 核心执行模型是「Plan 拆分成多个 subtask → 每个 subtask 在独立 git worktree
中运行 → 拓扑波次并行 + artifact 传递」。拆分决策目前完全交给 LLM Planner，系统只做
三类**告警性**检查（不阻断）：

| 检查 | 判定 | 层级 |
|------|------|------|
| L1.5 AST 冲突检测 | 多 step 同文件/同符号 | 交互阻断（symbol）/提示（file） |
| G5 check_under_decomposition | hard 任务 < 3 子任务 | warning-only |
| G6 check_over_decomposition | ≤2 文件或全 easy 但 ≥3 子任务 | warning-only |

真实 bench 数据显示：子任务数从 1→4，成本近似线性增长（4-subtask 时 8.6×），而通过率
从 79.3% 跌到 11.1%。**拆分的收益（并行加速）与代价（上下文重复注入 + 交叉污染风险）**
需要更理性的平衡。本调研从 opencode / Claude Code 的多 Agent 设计中寻找判据。

## 2. opencode 的 Agent 体系

参考：https://opencode.ai/docs/agents/

### 2.1 Primary Agents

- **Build**：默认主 agent，全工具集（读写、执行、Web），执行任务。
- **Plan**：只读规划 agent，只用读工具，产出执行计划。

### 2.2 Subagents（子代理）

- **General**：通用子代理，可写，官方定位「run multiple units of work in parallel」。
- **Explore**：只读探索子代理，permission deny edit，用于代码库探索。
- **Scout**：外部研究子代理。

调用方式：主 agent 按 `description` 字段**自动**调用，或用户 `@name` 手动调用。

### 2.3 设计要点（对 agent_go 有借鉴意义）

1. **上下文隔离**：每个 subagent 有独立 context window，结果汇总回主 agent。
   - 主 agent 不会因 subagent 的大段探索内容膨胀上下文。
   - agent_go 反例：每个 subtask 把 TASK.md 固定执行要求**逐字重复注入**，
     导致 4-subtask 成本线性膨胀。
2. **权限最小化**：permission allow/ask/deny 每 agent 独立；Explore 直接 deny edit。
   - agent_go 反例：subtask 只有 agent_type，无工具级权限隔离。
3. **模型路由**：每 agent 可配独立模型。
   - agent_go 已有：difficulty → worker_models 路由。
4. **任务独立性是拆分判据**：官方把 General 定位为「并行独立工作单元」，
   Explore 定位为「只读探索」。拆分发生在**任务真正独立**时，
   而非「文件数量多」时。

## 3. Claude Code 的并行机制

Claude Code 有三层并行机制（按复杂度递增）：

| 机制 | 定位 | 通信 | 适用 |
|------|------|------|------|
| **Subagents** | 专注型子代理，只要结果 | 单向汇报 | 低复杂度、独立子任务 |
| **Agent Teams** | 需要讨论的协作代理组 | 多向通信 | 中复杂度、需协商 |
| **Git Worktree** | 多任务隔离代码环境 | 通过 git 传递 | 同仓库多分支并行 |

### 3.1 Subagents 最佳实践（官方）

- 保持专注（单一职责）
- 限制工具（最小权限）
- 模型分级（Haiku 简单 / Sonnet 复杂 / Opus 更深）
- **避免文件冲突**：分解工作使每个队友负责不同的文件集
- 团队规模 3-5 人、每队友 5-6 个任务

### 3.2 与 agent_go 的同构性

agent_go 的 worktree 隔离 + 拓扑波次 + 角色化 subtask，与 Claude Code 的
**Git Worktree** 机制几乎同构：

| agent_go | Claude Code 对应 |
|----------|------------------|
| git worktree add -b agent_go/{task}/{sub} | Git Worktree 多分支隔离 |
| 拓扑波次 + ThreadPoolExecutor | Agent Teams 并行 |
| upstream tag → git merge artifact 传递 | git 提交传递 |
| 角色化 agent_type + skills | Subagents 专注单一职责 |
| difficulty → worker_models | 模型分级 |

## 4. 核心结论：拆分的判据是「任务独立性」，不是文件数量

opencode 与 Claude Code 都把 subagent 视为「**能力分离 + 并行 + 只读探索**」机制，
**而不是把大任务切块的机制**。拆分判据是：

> 任务 A 与任务 B 能否互不依赖地独立完成？
> - 只读探索 → Explore（不拆进主工作流）
> - 需讨论协作 → Agent Teams（不是静默并行）
> - 真正独立、可并行 → Subagents / 并行波次

Claude Code 官方明确「避免文件冲突」的最佳实践：**分解工作使每个队友负责不同的文件集**。

### 4.1 agent_go 当前的偏差

1. **LLM 自由拆分**：Planner 按自己的理解拆分，无「文件互斥」硬约束。
   实证失败：task-20260809-123021-784-042c 的 implement-done-command 拆 2 个，
   sub-2 越界改 cli.py+storage.py+models.py，与 sub-1 的 storage.py 重叠 → 交叉污染
   → VERIFICATION_FAILED。
2. **小改动也拆**：fix-missing-default（easy 5 行改动）被拆 2-3 个 → 成本翻倍且 pass=False。
3. **G6 只告警不阻断**：过度分解（≤2 文件 / 全 easy 但 ≥3 子任务）只写日志，不阻止执行。
4. **上下文重复注入**：TASK.md 固定执行要求逐字重复进每个 subtask，成本线性膨胀。

### 4.2 拆与不拆的判据（建议）

```
拆分的充分条件（需同时满足）：
  ① 子任务之间文件作用域互斥（不重叠）——否则必合
  ② 任务确有多份可独立推进的工作单元（不是小改动的伪拆分）
  ③ 并行收益 > 上下文重复注入成本（≥2 文件 / 非全 easy）

禁止拆分的条件（任一命中）：
  A. 涉及文件 ≤2 且子任务 ≥3（小改动过度分解）
  B. 全 easy 难度但子任务 ≥3
  C. 无依赖关系的子任务共享同一文件（交叉污染高风险）
```

## 5. 拆分算法改进实施

在 `agent_go/planning.py` 的 `validate_plan_quality` 中新增两个确定性检查，
与既有的 scope_conflict / dependency_cycle / requirement_coverage 并列：

### 5.1 新增 G7：跨子任务文件重叠检测（file_overlap）

对每个子任务提取文件作用域（`files` 字段 ∪ `files_hint` 逗号拆分，`*` 忽略），
对每个被 ≥2 个子任务引用的文件：

- **存在一对子任务无依赖路径**（不构成顺序）→ **blocking**：`file_overlap_without_dependency`。
  并行修改同一文件必然交叉污染（bench 实证）。
- **所有共享文件的子任务都在一条依赖链上**（顺序执行，artifact 会 merge）→ warning：
  `file_overlap_with_dependency`。串行修改同一文件仍有集成风险，但不阻断。

依赖路径用既有 graph（subtask `depends_on`）做 BFS 传递闭包判断。

### 5.2 G6 过度分解升级为 blocking

当 `子任务数 ≥ 3` 且 `有效文件数 ≤ 2`（且文件作用域非空）→ **blocking**：
`over_decomposition`。直接对应 bench 实证的 fix-missing-default 场景
（5 行改动拆 3 个 → 成本翻倍且失败）。

> 说明：`全 easy 但 ≥3 子任务` 仍保持 warning（G6 原逻辑），
> 因为「多文件全 easy 任务并行拆分」可能是合法的（如多个独立 helper）。
> 仅「文件作用域 ≤2 的小改动」升级为阻断，证据最硬。

### 5.3 实现要点

- `validate_plan_quality` 的 `blocking_issues` 新增 `over_decomposition` 与
  `file_overlap_without_dependency` 两种 type。
- `plan_conflict_count` 统计范围扩展包含上述新 blocking issue。
- 新增独立函数 `check_subtask_file_overlap(subtasks)` 供测试与单独调用。
- 保持既有行为：files_hint 不参与 scope_conflict（tester 场景不误报），
  但参与跨子任务文件重叠检测。

## 6. 遗留问题（非本次范围）

1. **上下文重复注入**：TASK.md 固定执行要求逐字重复进每个 subtask。
   可探索「共享上下文 + 增量」注入，或上游验证命令输出传递。
   这是 4-subtask 成本线性膨胀的主因，但改动面大，单独排期。
2. **工具/权限最小化**：subtask 只有 agent_type，无工具级权限隔离
   （Explore deny edit 这类能力）。可纳入后续权限模型。
3. **G5 欠分解阈值**：hard 任务 < 3 子任务告警仍是 V1 硬编码，可从
   verify_state.json 历史学习（V2）。

## 7. 参考资料

- opencode 官方文档：https://opencode.ai/docs/agents/
- Claude Code 并行任务（Subagents / Agent Teams / Git Worktree）：
  菜鸟教程整合官方行为
- 项目内部：docs/design/plan-capability-phaseb-2026-08-09.md
- 项目内部：docs/design/bench-convergence-plan.md（阶段 C/D bench 数据）
