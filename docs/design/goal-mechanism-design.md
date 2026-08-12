# Goal 机制设计

> 状态：设计稿，按阶段落地
> 日期：2026-08-12
> 关联：[prd.md](../prd.md) · [roadmap.md](../roadmap.md) · [Plan 级反馈与受控重规划调研](plan-self-optimization-research.md)

## 1. 产品定位

Goal 是 agent_go 的 **Worker 持续执行策略**，不是任务最终交付判定，也不是全局 Pipeline 控制器。

```text
Goal Contract       任务最终要达到什么状态
Plan                要做哪些步骤
Plan Preflight      这个计划能否安全执行
Goal Policy         Worker 是否持续自主执行
Verification        代码结果是否正确
Pipeline            多个 Subtask 如何调度
Delivery            结果是否真正交付
Governance          需求、架构和交付是否可追踪
```

核心原则：

> **Goal Contract 默认存在；Goal Loop 按策略启用；用户可以覆盖策略；Goal 不能绕过 Verification、Commit、Delivery 和 Accepted Delivery。**

## 2. 设计目标与非目标

### 2.1 设计目标

- 减少 Worker 完成部分工作后提前退出。
- 支持长时间、长 horizon、需要多轮修复的 Subtask。
- 降低 headless/CI 场景中的人工续跑次数。
- 统一 Claude 原生 Goal、Stop Hook 和 agent_go watchdog 的生命周期。
- 让 Goal 的完成、阻塞、暂停、取消、超时和预算耗尽可查询、可恢复、可审计。
- 保持 agent_go 的独立确定性验证和 Accepted Delivery 判定。

### 2.2 非目标

- Goal 不负责生成或修改全局 Plan。
- Goal 不替代 Plan Preflight Repair。
- Goal 不替代 shell verification 或 semantic evaluator。
- Goal 不直接设置 `completed` 或 `ACCEPTED_DELIVERY`。
- Goal 不默认启用无限循环。
- Goal 不负责 PR 创建、mergeability 或目标分支合并。
- Goal 不把 Claude/Kimi 的私有 Goal 实现强行当成统一内部语义。

## 3. 四层模型

### 3.1 Goal Contract

Goal Contract 是任务的完成契约，默认由任务描述、Task Spec、requirement、acceptance criterion、Plan 和 verification 共同形成。

示例：

```json
{
  "goal_description": "完成 checkout 模块迁移并准备交付",
  "acceptance_criteria": [
    "checkout 相关测试通过",
    "新增异常场景测试",
    "不得修改 migrations/"
  ],
  "completion_evidence": [
    "pytest tests/checkout -q",
    "git diff --check"
  ],
  "constraints": [
    "只修改 checkout/ 和 tests/checkout/"
  ],
  "delivery_required": true
}
```

Goal Contract 默认生成，即使 Goal Loop 没有启用，也用于：

- Plan 生成和拆解。
- Plan Preflight Repair。
- Subtask verification 设计。
- requirement/acceptance traceability。
- 最终结果和交付报告。

### 3.2 Goal Recommendation

Planner 根据任务特征提出建议，但建议不是最终开关：

```json
{
  "goal_recommendation": {
    "mode": "auto",
    "reason_codes": [
      "medium_or_hard_task",
      "clear_verification",
      "headless_compatible",
      "long_running_candidate"
    ],
    "risk_codes": [],
    "estimated_turns": 8,
    "estimated_duration_sec": 900,
    "requires_human_approval": false
  }
}
```

Planner 可以建议 `auto` 或 `off`，但不能静默强制 `force`。系统还必须用确定性规则复核：是否有安全 verification、是否有预算、是否要求人工确认、是否支持目标 backend。

### 3.3 Goal Policy

Goal Policy 决定是否实际启动持续执行：

| mode | 含义 | 适用场景 |
|---|---|---|
| `off` | 不启用 Goal continuation，保留普通执行和验证 | 简单任务、高风险任务、预算严格任务 |
| `auto` | 根据 Plan 和系统策略自动选择 | 推荐默认策略 |
| `force` | 用户明确要求持续自主执行 | 长任务、测试修复、大型迁移 |
| `hook` | 启用 Goal continuation 及确定性 Stop Hook | 高约束实验场景 |

当前 CLI 兼容映射：

```text
--goal       → goal_mode=force
--no-goal    → goal_mode=off
--goal-hook  → goal_mode=hook 的 Stop Hook 覆盖项
```

后续推荐增加：

```bash
agent_go run <repo> <task> --goal-mode auto|off|force|hook
```

### 3.4 Goal Evidence

Goal Evidence 是完成判断的证据，而不是模型的一句总结：

```json
{
  "goal_status": "COMPLETED",
  "evidence": [
    {"command": "pytest tests/checkout -q", "exit_code": 0},
    {"command": "git diff --check", "exit_code": 0}
  ],
  "scope_check": "passed",
  "commit_check": "pending",
  "delivery_check": "pending"
}
```

Goal 完成不能单独产生 Accepted Delivery。最终判定仍然是：

```text
Goal Evidence
  AND Verification
  AND Scope Check
  AND Commit Boundary
  AND Delivery Contract
```

## 4. 决策优先级

Goal Policy 采用以下优先级：

```text
用户明确覆盖
    > 配置明确策略
    > 系统确定性策略
    > Planner recommendation
    > 默认策略
```

### 4.1 自动策略建议

`auto` 模式下，以下条件同时满足时才建议启用 Goal：

- headless 或明确允许无人值守。
- Subtask 难度为 medium/hard，或预计执行时间较长。
- 存在至少一个安全、可执行的 verification command。
- 有明确的 max turns、timeout 和 budget。
- 不要求中途人工审批。
- Worker backend 支持 Goal 或 agent_go 内部 watchdog。

以下条件建议关闭 Goal：

- 没有可测量的完成条件。
- verification 不稳定或无法安全执行。
- 任务很简单，单轮即可完成。
- 涉及高风险生产操作。
- 需要频繁人工确认。
- Goal backend 与当前 Worker 不兼容。

### 4.2 用户覆盖

用户选择的意义不是“是否存在任务目标”，而是控制 Worker 的自主执行程度：

- `off`：用户希望一轮一停或严格控制成本。
- `force`：用户明确允许长时间自主推进。
- `hook`：用户要求使用确定性 Stop Hook。

Goal Policy 不得绕过安全白名单、成本控制、超时、G8、verification 或 delivery 门禁。

## 5. 功能架构

```text
Task / Task Spec
      ↓
Goal Contract
      ↓
Plan + Goal Recommendation
      ↓
Plan Preflight Repair
      ↓
Subtask Completion Contract
      ↓
Goal Policy Resolver
  ├─ native_cli
  ├─ native_sdk
  ├─ hook_only
  ├─ internal_watchdog
  └─ unsupported
      ↓
Worker Session
      ↓
Goal State + Evidence
      ↓
agent_go Verification
      ↓
Commit / Pipeline / Delivery
```

### 5.1 当前模块映射

| 模块 | 当前职责 | Goal 设计中的职责 |
|---|---|---|
| `cli.py` | `--goal`、`--no-goal`、`--goal-hook` 参数 | 解析用户覆盖，计算最终 policy |
| `config.py` | `goal.enabled/max_turns/timeout` | Goal Policy、预算和兼容配置 |
| `ui.py` / `planning.py` | Plan、Subtask 和 verification | 生成并校验 Goal Contract |
| `executor.py` | TASK.md、Goal 文本、验证和 retry | 构造 Subtask Goal、接收 Goal 结果 |
| `goal_injector.py` | Stop Hook 和验证脚本 | `hook_only` / `native_cli` 适配 |
| `subtask.py` | `claude -p`、watchdog、进程组回收 | Worker session、turn/timeout/kill 状态 |
| `utils.py` | verification 安全白名单 | Goal Evidence 命令安全边界 |
| `evaluator.py` | 语义评估 | 高风险 Goal 的独立完成判断 |
| `pipeline.py` | wave、依赖和结果汇总 | Goal 不得绕过 pipeline 状态机 |
| `delivery.py` | delivery branch、Accepted Delivery | Goal 不直接影响交付判定 |
| `metrics.py` / `metering` | 成本、耗时和结果指标 | Goal turn、cost、timeout、false-success 统计 |

### 5.2 Provider Adapter

Goal 应被抽象为 Worker backend 能力，而不是写死在 Claude 文本中：

| backend | 适配方式 |
|---|---|
| Claude CLI | 原生 `/goal`、Stop Hook、agent_go watchdog |
| Claude Agent SDK | slash command、hooks、`max_turns`、`max_budget_usd`、sessions |
| Kimi Code | native `/goal`、pause/resume/cancel、exit code 映射 |
| 不支持 Goal 的 Worker | 只使用 agent_go internal watchdog 和 verification |

当前 agent_go 使用 `claude -p` subprocess，优先保留 CLI adapter。Claude Agent SDK 没有独立 Goal API，SDK 主要提供 agent loop、hooks、sessions、turn 和 budget 控制；不因 Goal 单独引入 SDK 依赖。

## 6. 状态和约束

### 6.1 Goal 状态

统一状态：

```text
ACTIVE
COMPLETED
BLOCKED
PAUSED
CANCELLED
TIMED_OUT
BUDGET_EXCEEDED
```

必须记录：

- `goal_id`
- `goal_description`
- `goal_mode`
- `goal_backend`
- `goal_status`
- `goal_turn_count`
- `goal_timeout_seconds`
- `goal_budget_usd`
- `goal_stop_reason`
- `goal_evidence`
- `goal_evaluator_model`

### 6.2 循环边界

Goal 同时受到以下边界限制：

- Goal `max_turns`。
- Goal `timeout_seconds`。
- Worker `run_timeout` 和 `retry_timeout`。
- `max_retries`。
- L1/L2/L3 cost control。
- G8 rejected-command short circuit。
- revert/divergence detection。
- 进程组整体回收。

必须明确哪个边界先触发，并将最终 `stop_reason` 归入统一 failure class。Goal 不得产生无限 retry，也不得与 executor retry 形成双重无界循环。

### 6.3 完成条件

Goal evaluator 可以提供辅助判断，但不能单独完成任务。推荐完成条件：

```text
goal_evaluator = completed
  AND deterministic verification = passed
  AND scope = passed
  AND commit = exists
```

高风险任务可以增加：

```text
AND independent semantic evaluator = passed
```

## 7. 分阶段落地

### 阶段一：Goal Contract 标准化

目标：让任务目标默认存在，但不改变当前 Goal Loop 默认关闭策略。

- 从 Task/Spec/acceptance/verification 生成 Goal Contract。
- 写入 Plan、Subtask 和 `meta.goal_contract`。
- 在 governance/status/review/replay 中可见。
- Plan preflight 校验 Goal Evidence 是否完整、安全、可执行。
- 保持 `--goal`、`--no-goal` 兼容。

验收：

- 每个有明确验收标准的任务都有 Goal Contract。
- Goal Contract 与 requirement/acceptance 可追踪。
- Goal Contract 缺失不影响普通执行，但必须被报告。

### 阶段二：Goal Policy Auto

目标：由系统根据 Plan 特征提出并执行 `auto` 策略。

- 新增 `goal_mode=auto|off|force|hook`。
- Planner recommendation 只作为建议。
- 用户覆盖优先于系统策略。
- 记录最终 policy、决策原因和风险码。
- 默认只在明确 verification、medium/hard、headless 场景灰度开启。

验收：

- 简单任务不会产生不必要 Goal 成本。
- 长任务可以减少人工续跑。
- Accepted Delivery 不下降。
- Goal 额外成本和 timeout 可测量。

### 阶段三：Provider Goal Adapter

目标：统一 Claude CLI、Claude SDK、Kimi Code 和 internal watchdog 的 Goal 生命周期。

- 启动时发现 backend 能力。
- 映射不同 provider 的完成、暂停、阻塞、取消和预算状态。
- 解析 Claude/Kimi 的真实 Goal 结果，而不是只依赖进程退出码。
- 校验 Stop Hook 配置版本兼容性。

### 阶段四：真实任务 A/B 验证

使用 10-20 个长任务比较：

```text
A 组：Goal off
B 组：Goal auto/force
```

指标：

- Accepted Delivery Rate
- First-pass Rate
- Cost per Accepted Delivery
- Time to Accepted Delivery
- Human Intervention Minutes
- goal false-success rate
- goal timeout rate
- average goal turns
- verification duplicate execution count

只有在 Accepted Delivery 不下降、成本可接受、人工介入下降、false-success 接近 0 时，才扩大默认范围。

### 阶段五：局部重规划（后续）

Goal 只负责 Worker 持续执行；如果仍然失败，局部一次性重规划应单独实现：

- 只影响失败 Subtask 和未执行下游。
- 不重跑已接受 commit。
- 继承原预算、权限和 requirement/acceptance。
- 默认人工确认。
- 最多一次，不递归扩大任务图。

全局动态 DAG、无限 Goal Loop 和 Meta-Harness 暂不进入关键路径。

## 8. 参考

- [Claude Code Goal 官方文档](https://code.claude.com/docs/en/goal)
- [Claude Agent SDK 概览](https://code.claude.com/docs/en/agent-sdk/overview)
- [Claude Agent SDK Agent Loop](https://code.claude.com/docs/en/agent-sdk/agent-loop)
- [Claude Agent SDK Slash Commands](https://code.claude.com/docs/en/agent-sdk/slash-commands)
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks-guide)
- [Kimi Code 斜杠命令与 Goal 模式](https://moonshotai.github.io/kimi-code/zh/reference/slash-commands.html)
- [Kimi Code Changelog](https://moonshotai.github.io/kimi-code/en/release-notes/changelog.html)
