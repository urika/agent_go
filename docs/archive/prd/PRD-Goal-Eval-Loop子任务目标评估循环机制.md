# PRD: 子任务目标-评估-循环机制 (Goal-Eval-Loop)

> 版本: v1.1  
> 日期: 2026-05-26  
> 作者: Product  
> 状态: Draft  
> 变更: v1.1 新增 F-3.5 AgentRunner 抽象层，解除与 Claude Code 的硬耦合  
> 关联: [../design/requirements.md](../design/requirements.md) | [../design/review/design-review.md](../design/review/design-review.md) | [PRD-智能Agent角色与Skill分配.md](PRD-智能Agent角色与Skill分配.md)

---

## 一、背景与问题

### 1.1 现状

agent_go v2.0 的子任务执行流程是**单次 fire-and-forget** 模型：

```
Plan → Subtask → Execute (Claude Code, 1~2次) → Verify (单条 shell 命令 exit code) → Pass/Fail
```

具体机制（[executor.py#L229-L284](../../agent_go/executor.py)）：

| 环节 | 实现 | 局限 |
|------|------|------|
| 目标定义 | TASK.md 文本描述 | 不可机器判读，无法量化判定 |
| 执行 | Claude Code 单次执行 | headless 模式最多 2 次固定尝试 |
| 验证 | 单条 `verification` shell 命令的 exit code | 只能检查"命令是否报错"，不能检查"任务是否完成" |
| 失败处理 | 注入"请修复"通用指令重试 1 次 | 无具体失败原因，修复盲目 |
| 结果判定 | `exit_code == 0 && verify_ok` | exit code 0 ≠ 任务正确完成 |

### 1.2 核心问题

在设计评审 [design-review.md](../design/review/design-review.md) 中识别出三个关联问题：

| 编号 | 问题 | 严重性 | 说明 |
|------|------|--------|------|
| D-3 | 无执行反馈回路 | Tier 1 | Plan 确认后 pipeline 不可调整，不根据中间结果重新规划 |
| D-6 | 验证 ≠ 任务完成 | Tier 2 | shell exit code 0 不代表任务目标达成 |
| D-5 | Skill 是不可观测的 prompt 片段 | Tier 2 | 无法评估 Skill 知识是否被正确遵循 |

**根本矛盾**：系统缺少**目标的结构化表达**和**目标达成度的可判定评估**，导致执行结果无法保证质量。

### 1.3 用户影响

- **不可靠的交付**: 验证通过但功能未实现的"假阳性"，下游子任务基于错误假设继续
- **浪费的迭代**: 验证失败但只给通用修复指令，Claude 缺乏具体失败信息，反复盲目重试
- **不可观测**: 用户无法知道子任务离目标还差多远，只能看到 pass/fail 二元结果

### 1.4 Agent 产品耦合问题

当前执行层（[subtask.py](../../agent_go/subtask.py)）硬编码 Claude Code CLI：

| 耦合点 | 位置 | 说明 |
|--------|------|------|
| 命令构建 | `subtask.py#L68-L75` | `claude -p` + `--output-format stream-json` 等 Claude 专属参数 |
| 输出解析 | `subtask.py#L103-L173` | 解析 Claude `stream-json` 事件格式 |
| Agent 配置 | `agents.py#L136-L175` | `get_claude_command()` 构建 Claude CLI 参数 |
| 修复重试 | `executor.py#L264-L271` | 直接调用 `_run_headless()` |

GEL 的评估层（F-1/F-2/F-4）与具体 Agent 产品完全无关（仅依赖 shell exit code + git diff），但 **F-3 循环执行的迭代重试** 需要调用 Agent 执行代码。若不引入抽象层，GEL 将被锁定在 Claude Code 上，无法支持 opencode、aider 等替代 Agent。

---

## 二、目标与范围

### 2.1 产品目标

引入 **Goal-Eval-Loop (GEL) 机制**，为子任务提供：

1. **结构化目标定义**：可机器判读、可量化、可多维度验证
2. **多层次评估**：超越 shell exit code，覆盖功能正确性、约束满足、语义完整性
3. **循环迭代**：基于评估反馈的渐进式修复，直至目标达成或达到预算上限
4. **Agent 产品无关**：执行层抽象化，支持 Claude Code、opencode 等不同 Agent 后端

### 2.2 非目标

- 不解决 D-3（全局 pipeline 层面的 re-planning），GEL 作用域是**单个子任务内部**
- 不引入人工审批环节，保持自动化流程
- 不改变 Plan 生成阶段的逻辑（那是 D-1 的范畴）

### 2.3 成功指标

| 指标 | 当前基线 | 目标 |
|------|---------|------|
| 子任务一次性通过率 | ~60%（估算） | ≥ 80% |
| 验证"假阳性"率 | 未知（无法检测） | ≤ 5% |
| 平均迭代次数 | N/A（无迭代） | 1.5~2.5 次 |
| 修复成功率（第 2 次起） | ~30%（通用修复指令） | ≥ 60% |

---

## 三、功能需求

### F-1: 结构化目标定义 (Goal Schema)

**描述**：扩展 Plan JSON 中每个 step 的验证结构，从单条 `verification` 命令升级为结构化 `goal` 对象。

**Goal Schema**：

```json
{
  "goal": {
    "accept_criteria": [
      {
        "type": "test",
        "command": "pytest tests/test_email_verification.py -x",
        "description": "邮箱验证测试全部通过"
      },
      {
        "type": "check",
        "command": "python -c \"from models import User; assert hasattr(User, 'email_verified')\"",
        "description": "User 模型包含 email_verified 字段"
      },
      {
        "type": "lint",
        "command": "ruff check models.py",
        "description": "代码风格检查通过"
      }
    ],
    "constraints": [
      "不修改现有 User 迁移文件",
      "新增字段必须有默认值"
    ],
    "max_iterations": 3,
    "timeout_per_iteration_sec": 300
  }
}
```

**字段说明**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `accept_criteria` | array | 是 | 验收条件列表，全部通过才算达标 |
| `accept_criteria[].type` | string | 是 | 检查类型：`test` / `check` / `lint` |
| `accept_criteria[].command` | string | 是 | 可执行的 shell 命令 |
| `accept_criteria[].description` | string | 是 | 人类可读的通过条件描述（注入修复 prompt） |
| `constraints` | string[] | 否 | 不可违反的约束（文本形式，用于 Layer 2 检查） |
| `max_iterations` | int | 否 | 最大迭代次数，默认 3 |
| `timeout_per_iteration_sec` | int | 否 | 单次迭代超时（秒），默认 300 |

**向后兼容**：

```
if step has "goal"       → GEL 循环
elif step has "verification" → 现有单次验证逻辑（不改动）
else                     → 仅检查 Claude Code exit code
```

**涉及文件**：
- `api.py`：Plan prompt 增加 `goal` 结构说明和示例
- `ui.py`：`plan_to_subtasks()` 透传 `goal` 字段
- `config.py`：新增 `gel` 配置项（默认值）

**验收标准**：
- [ ] Plan prompt 包含 `goal` 结构说明和完整示例
- [ ] LLM 输出的 plan JSON 可包含 `goal` 字段
- [ ] `plan_to_subtasks()` 正确透传 `goal` 到 subtask dict
- [ ] 无 `goal` 字段的 step 走现有逻辑，行为不变

---

### F-2: 多层次评估器 (Evaluate Module)

**描述**：新增 `evaluate.py` 模块，实现三层评估架构。

**Layer 1 — 自动化检查**（必须，零额外成本）：

```python
def evaluate_criteria(goal: dict, worktree: Path, logger) -> EvalResult:
    """执行所有 accept_criteria 命令，收集通过/失败结果。"""
    results = []
    for criterion in goal.get("accept_criteria", []):
        rc, stdout, stderr = run_command(criterion["command"], worktree, timeout=...)
        results.append(CriterionResult(
            type=criterion["type"],
            description=criterion["description"],
            command=criterion["command"],
            passed=(rc == 0),
            stderr=stderr[-500:],
        ))
    return EvalResult(
        all_passed=all(r.passed for r in results),
        results=results,
        failures=[r for r in results if not r.passed],
    )
```

**Layer 2 — 约束检查**（必须，低成本）：

```python
def check_constraints(goal: dict, worktree: Path, logger) -> ConstraintResult:
    """检查约束是否被违反。
    
    策略：将 constraints 转化为可检查的规则。
    - 包含 "不修改"/"不要" → git diff --name-only 检查
    - 包含 "必须" → 对应的检查命令
    - 其他 → 作为文本注入 Layer 3 语义评估
    """
    violations = []
    for constraint in goal.get("constraints", []):
        if _is_file_constraint(constraint):
            # 解析出文件模式，检查 git diff
            violated = _check_file_constraint(constraint, worktree)
            if violated:
                violations.append(constraint)
        # 其他类型约束由 Layer 3 处理
    return ConstraintResult(
        all_passed=len(violations) == 0,
        violations=violations,
    )
```

**Layer 3 — 语义评估**（可选，高成本，默认关闭）：

```python
def semantic_evaluate(goal: dict, worktree: Path, diff: str, config: dict, logger) -> SemanticResult:
    """调用 LLM 对比目标描述 vs 实际变更，判断语义完整性。
    
    仅在以下条件触发：
    - config["gel"]["semantic_eval"]["enabled"] == True
    - Layer 1+2 全部通过，但需要确认语义正确性
    - 或达到 max_iterations - 1 仍未通过
    """
```

**评估结果数据结构**：

```python
@dataclass
class EvalResult:
    all_passed: bool
    results: list[CriterionResult]
    failures: list[CriterionResult]
    
@dataclass  
class CriterionResult:
    type: str           # test / check / lint
    description: str
    command: str
    passed: bool
    stderr: str         # 截断到 500 字符
    
@dataclass
class ConstraintResult:
    all_passed: bool
    violations: list[str]
    
@dataclass
class GELStatus:
    iteration: int
    eval_result: EvalResult
    constraint_result: ConstraintResult
    overall_passed: bool
```

**涉及文件**：
- 新增 `agent_go/evaluate.py`（~150 行）
- `config.py`：新增 `gel` 默认配置

**验收标准**：
- [ ] `evaluate_criteria()` 正确执行所有 accept_criteria 并收集结果
- [ ] `check_constraints()` 检测文件修改约束违反
- [ ] Layer 3 默认关闭，通过 config 开启
- [ ] 命令执行超时受 `timeout_per_iteration_sec` 控制
- [ ] 不安全的 shell 命令被拒绝（复用 `_is_safe_verification_command()`）

---

### F-3: Goal-Eval-Loop 循环执行

**描述**：改造 `run_subtask()` 中的验证逻辑，从单次验证升级为 GEL 循环。

**执行流程**：

```
run_subtask()
  ├── 构建 TASK.md (不变)
  ├── runner = get_runner(config)          [F-3.5]
  ├── runner.run(prompt, worktree, ...)    [F-3.5, 替代原 Claude Code 直接调用]
  │
  ├── [NEW] GEL 循环入口
  │   │
  │   ├── 有 goal 字段?
  │   │   ├── YES → 进入 GEL 循环
  │   │   └── NO  → 走现有 verification 逻辑 (不变)
  │   │
  │   └── GEL 循环:
  │       for iteration in range(max_iterations):
  │         │
  │         ├── Layer 1: evaluate_criteria()
  │         │   └── 全部通过? → Layer 2
  │         │
  │         ├── Layer 2: check_constraints()
  │         │   └── 全部通过? → Layer 3 (if enabled) 或 PASS
  │         │
  │         ├── Layer 3: semantic_evaluate() (可选)
  │         │   └── 通过? → PASS
  │         │
  │         ├── FAIL:
  │         │   ├── build_fix_prompt() (基于失败详情)
  │         │   ├── runner.run(fix_prompt, ...)     [F-3.5, Agent 无关]
  │         │   ├── git commit + tag
  │         │   └── continue (下一轮迭代)
  │         │
  │         └── PASS → break
  │
  ├── 生成共享上下文 (扩展: 包含 GEL 迭代摘要)
  └── 状态判定
```

**涉及文件**：
- `executor.py`：核心改造，`run_subtask()` 增加循环分支
- `runner.py`：新增 AgentRunner 抽象层（F-3.5）

**验收标准**：
- [ ] `run_subtask()` 正确区分 goal 模式和 verification 模式
- [ ] GEL 循环在 `max_iterations` 内迭代
- [ ] 每次迭代的评估结果记入日志（`log_event`）
- [ ] 循环中间状态的 git commit 正确（迭代提交 + 最终 tag）
- [ ] 非交互模式（非 headless）不自动循环，仅报告评估结果
- [ ] 共享上下文包含 GEL 迭代摘要（迭代次数、最终状态）

---

### F-3.5: AgentRunner 抽象层

**描述**：引入 Agent 执行抽象层，将 `_run_headless()` 中硬编码的 Claude Code 调用解耦为可切换的 Runner 实现，使 GEL 循环与具体 Agent 产品无关。

**抽象接口**：

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

@dataclass
class AgentResult:
    """Agent 执行的统一返回结果。"""
    exit_code: int
    stdout: str
    stderr: str
    interaction_detected: bool = False  # 检测到 Agent 等待用户输入

class AgentRunner(ABC):
    """Agent 执行器的抽象基类。"""
    
    @abstractmethod
    def run(
        self,
        prompt: str,
        worktree: Path,
        env: dict,
        logger,
        sub_id: str,
        config: dict,
    ) -> AgentResult:
        """在指定 worktree 中执行 Agent。
        
        Args:
            prompt:   注入给 Agent 的任务描述
            worktree: git worktree 路径
            env:      环境变量（含 AGENT_GO_* 变量）
            logger:   日志记录器
            sub_id:   子任务 ID
            config:   全局配置
            
        Returns:
            AgentResult 统一结果
        """
        ...
```

**内置实现**：

| Runner | 说明 | 状态 |
|--------|------|------|
| `ClaudeRunner` | 迁移现有 `_run_headless()` 逻辑，解析 `stream-json` 输出 | Phase 2 实现 |
| `OpenCodeRunner` | 适配 opencode CLI 的执行和输出格式 | 未来实现 |
| `GenericRunner` | 通用 shell 命令执行，仅检查 exit code | Phase 2 实现 |

**ClaudeRunner 迁移策略**：

```
现有 subtask.py:
  _run_headless(task_md, worktree, env, logger, sub_id)
    ├── subprocess.Popen(["claude", "-p", "", "--output-format", "stream-json", ...])
    ├── 解析 stream-json 事件
    └── 返回 (exit_code, stdout, stderr)

迁移后 runner.py:
  ClaudeRunner.run(prompt, worktree, env, logger, sub_id, config) -> AgentResult
    ├── 从 config["agent_runner"] 读取 Claude 专属参数
    ├── subprocess.Popen(["claude", "-p", prompt, ...])
    ├── 解析 stream-json 事件（保留现有逻辑）
    └── 返回 AgentResult(exit_code, stdout, stderr, interaction_detected)
```

**Runner 选择机制**：

```python
def get_runner(config: dict) -> AgentRunner:
    """根据配置返回对应的 Runner 实例。"""
    runner_type = config.get("agent_runner", {}).get("type", "claude")
    if runner_type == "claude":
        return ClaudeRunner()
    elif runner_type == "opencode":
        return OpenCodeRunner()
    else:
        return GenericRunner()
```

**涉及文件**：
- 新增 `agent_go/runner.py`（~120 行）
- `executor.py`：调用 `runner.run()` 替代 `_run_headless()`
- `subtask.py`：`_run_headless()` 迁移至 `ClaudeRunner`，保留函数签名做兼容过渡
- `config.py`：新增 `agent_runner` 配置段

**验收标准**：
- [ ] `AgentRunner` ABC 接口定义完整
- [ ] `ClaudeRunner` 正确迁移 `_run_headless()` 所有逻辑（包括 stream-json 解析）
- [ ] `get_runner()` 根据配置返回正确实例
- [ ] `executor.py` 通过 `runner.run()` 调用，不直接引用 `_run_headless()`
- [ ] 配置 `agent_runner.type = "claude"` 时行为与现有完全一致
- [ ] `GenericRunner` 可执行任意 shell 命令并返回 AgentResult

---

### F-4: 渐进式修复 Prompt

**描述**：根据迭代次数和失败详情，生成从具体到策略调整的渐进式修复指令。

**修复 Prompt 策略**：

| 迭代次数 | 策略 | 注入内容 | 目的 |
|---------|------|---------|------|
| 第 1 次 | 原始执行 | 无修改 | 首次尝试 |
| 第 2 次 | 精准诊断 | 失败的 criterion 详情 + stderr | 定向修复 |
| 第 3 次 | 全量诊断 | 所有失败 + diff 摘要 + 约束提醒 | 全面修复 |
| 第 4 次+ | 策略调整 | "请换一种实现方案" + 完整历史 | 避免死循环 |

**Prompt 模板**：

```
【第 N 次迭代 — 目标未达成】

以下验收条件未通过：
❌ {criterion.description}
   命令: `{criterion.command}`
   错误输出: {criterion.stderr}

❌ {criterion.description}
   命令: `{criterion.command}`
   错误输出: {criterion.stderr}

{如果 iteration >= 3}
⚠️ 已尝试多次未成功，请考虑更换实现方案而非继续修复。
{如果 constraint 被违反}
⚠️ 约束违反: {violation}
{结束}

请直接修复上述问题，确保所有验收条件通过。不要询问，直接执行。
```

**涉及文件**：
- `evaluate.py`：新增 `build_fix_prompt()` 函数

**验收标准**：
- [ ] 第 1 次执行使用原始 TASK.md
- [ ] 第 2 次起注入具体失败信息
- [ ] 第 4 次起建议换方案
- [ ] 约束违反信息始终包含在修复 prompt 中
- [ ] 修复 prompt 包含完整上下文（原始 task + 失败详情）

---

### F-5: 可观测性与配置

**描述**：GEL 过程对用户可见，关键指标可配置。

**5.1 运行时输出**：

```
🚀 sub-1: 实现用户邮箱验证
   [GEL] 迭代 1/3 — Layer 1: ❌ 2/3 条件通过
   [GEL] 失败: "邮箱验证测试全部通过" (exit code 1)
   [GEL] 失败: "代码风格检查通过" (ruff: 2 errors)
   [GEL] 注入修复指令，重试...
   [GEL] 迭代 2/3 — Layer 1: ✅ 3/3 条件通过
   [GEL] Layer 2: ✅ 约束检查通过
   ✅ sub-1 完成 (GEL: 2 次迭代)
```

**5.2 配置项**：

```json
{
  "gel": {
    "enabled": true,
    "default_max_iterations": 3,
    "default_timeout_per_iteration_sec": 300,
    "semantic_eval": {
      "enabled": false,
      "model": "claude-haiku-4-5",
      "trigger": "on_layer1_pass_layer2_fail"
    }
  },
  "agent_runner": {
    "type": "claude",
    "claude": {
      "output_format": "stream-json",
      "permission_mode": "bypassPermissions",
      "extra_args": []
    },
    "opencode": {
      "output_format": "stream-json",
      "extra_args": []
    }
  }
}
```

**5.3 命令行参数**：

```bash
# 全局覆盖 max_iterations
python3 agent_go.py run <repo> '<task>' --gel-max-iterations 5

# 禁用 GEL（强制走 verification 模式）
python3 agent_go.py run <repo> '<task>' --no-gel

# 开启语义评估
python3 agent_go.py run <repo> '<task>' --gel-semantic-eval

# 指定 Agent Runner（覆盖配置）
python3 agent_go.py run <repo> '<task>' --runner opencode
```

**5.4 `cmd_show()` 展示扩展**：

```
[sub-1] 实现用户邮箱验证
  状态: completed
  GEL: 2 次迭代 (max=3)
  验收条件:
    ✅ pytest tests/test_email_verification.py -x
    ✅ python -c "from models import User; assert ..."
    ✅ ruff check models.py
  约束: ✅ 无违反
```

**涉及文件**：
- `config.py`：新增 `gel` + `agent_runner` 配置段
- `cli.py`：新增 `--gel-max-iterations` / `--no-gel` / `--gel-semantic-eval` / `--runner` 参数
- `cli.py`：`cmd_show()` 展示 GEL 详情 + Runner 类型

**验收标准**：
- [ ] `config.json` 中 `gel` 和 `agent_runner` 配置项生效
- [ ] `--no-gel` 禁用 GEL，走现有逻辑
- [ ] `--gel-max-iterations` 覆盖 plan 中每 step 的 max_iterations
- [ ] `--runner` 覆盖 `agent_runner.type` 配置
- [ ] 运行时实时输出 GEL 迭代状态
- [ ] `cmd_show()` 展示每个 subtask 的 GEL 迭代详情 + Runner 类型

---

## 四、技术设计

### 4.1 新增模块

```
agent_go/
  evaluate.py    ← 新增 (~150 行)
    ├── evaluate_criteria()     Layer 1 评估
    ├── check_constraints()     Layer 2 约束检查
    ├── semantic_evaluate()     Layer 3 语义评估
    ├── build_fix_prompt()      渐进式修复 prompt 生成
    └── 数据类: EvalResult, CriterionResult, ConstraintResult, GELStatus

  runner.py      ← 新增 (~120 行)
    ├── AgentRunner (ABC)       Agent 执行器抽象基类
    │   └── run() -> AgentResult
    ├── ClaudeRunner            Claude Code 实现（迁移自 subtask._run_headless）
    ├── GenericRunner           通用 shell 执行实现
    ├── get_runner(config)      根据配置返回 Runner 实例
    └── 数据类: AgentResult     统一返回结构 (exit_code, stdout, stderr, interaction_detected)
```

### 4.2 改动范围

| 文件 | 改动类型 | 行数估算 | 说明 |
|------|---------|---------|------|
| `evaluate.py` | 新增 | ~150 | 评估器核心模块 |
| `runner.py` | 新增 | ~120 | AgentRunner 抽象层 + ClaudeRunner 实现 |
| `executor.py` | 改造 | ~100 行变动 | `run_subtask()` 增加 GEL 分支 + 调用 `runner.run()` |
| `subtask.py` | 修改 | ~30 行 | `_run_headless()` 迁移至 `ClaudeRunner`，保留兼容过渡 |
| `agents.py` | 修改 | ~10 行 | `get_claude_command()` 适配 Runner 接口 |
| `api.py` | 修改 | ~20 行 | Plan prompt 增加 goal 结构说明 |
| `ui.py` | 修改 | ~5 行 | 透传 goal 字段 |
| `config.py` | 修改 | ~20 行 | 新增 gel + agent_runner 配置项 |
| `cli.py` | 修改 | ~35 行 | 新增参数 + cmd_show 展示 |
| `tests/test_evaluate.py` | 新增 | ~200 | GEL 评估器测试 |
| `tests/test_runner.py` | 新增 | ~120 | AgentRunner 抽象层测试 |

### 4.3 数据流

```
用户命令
  │
  ├── --gel-max-iterations / --no-gel / --gel-semantic-eval
  │
  ↓
generate_plan()  ←── Plan prompt 包含 goal 结构说明
  │                  (附带示例和字段要求)
  ↓
LLM 输出 plan JSON
  │  steps[].goal = { accept_criteria, constraints, max_iterations }
  │
  ↓
plan_to_subtasks() 透传 goal → subtask["goal"]
  │
  ↓
run_subtask()
  │
  ├── get_runner(config)                     [F-3.5]
  │   └── 返回 ClaudeRunner / OpenCodeRunner / GenericRunner
  │
  ├── subtask.get("goal") 存在?
  │   ├── YES → GEL 循环:
  │   │   for iteration in range(max_iterations):
  │   │     evaluate_criteria() → Layer 1
  │   │     check_constraints() → Layer 2
  │   │     semantic_evaluate() → Layer 3 (可选)
  │   │     if passed: break
  │   │     else: build_fix_prompt() → runner.run() → continue  [F-3.5]
  │   │
  │   └── NO → 现有 verification 逻辑 (不改动)
  │
  ↓
共享上下文: 包含 GEL 迭代摘要
  │
  ↓
cmd_show(): 展示 GEL 详情
```

### 4.4 向后兼容矩阵

| Plan 版本 | 有 goal | 有 verification | 都没有 | 行为 |
|-----------|---------|----------------|--------|------|
| v2.0 旧 | ✗ | ✓ | — | 现有逻辑，不改动 |
| v2.0 旧 | ✗ | ✗ | ✓ | 仅检查 exit code |
| v2.1 新 | ✓ | — | — | GEL 循环 |
| v2.1 新 | ✓ | ✓ | — | GEL 循环（goal 优先） |

---

## 五、测试策略

### 5.1 单元测试

| 测试文件 | 覆盖内容 | 用例数 |
|---------|---------|--------|
| `tests/test_evaluate.py` | `evaluate_criteria()`, `check_constraints()`, `build_fix_prompt()` | ~15 |
| `tests/test_evaluate.py` | `semantic_evaluate()` mock | ~5 |
| `tests/test_runner.py` | `AgentRunner` 接口, `ClaudeRunner` 迁移验证, `get_runner()` 分发 | ~10 |
| `tests/test_gel_integration.py` | GEL 循环端到端（mock Runner） | ~8 |

### 5.2 集成测试场景

| 场景 | 预期 |
|------|------|
| Plan 有 goal，第 1 次迭代全部通过 | 直接完成，无重试 |
| Plan 有 goal，第 1 次失败，第 2 次通过 | 迭代 2 次，状态 completed |
| Plan 有 goal，达到 max_iterations 仍失败 | 状态 failed，日志含完整迭代历史 |
| Plan 无 goal 有 verification | 走现有逻辑，行为不变 |
| `--no-gel` 强制禁用 | 即使有 goal 也走 verification 逻辑 |
| 约束违反检测 | `check_constraints()` 正确识别文件修改约束 |
| Runner 切换 (`--runner opencode`) | `get_runner()` 返回正确实例，GEL 循环无感知 |
| ClaudeRunner 迁移 | 行为与原 `_run_headless()` 完全一致 |

---

## 六、风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| LLM 生成的 `accept_criteria` 命令不可靠 | 高 | 中 | 第一版 LLM 仅建议，用户可在确认阶段编辑；后续可验证命令可行性 |
| 循环次数过多导致 token/时间爆炸 | 中 | 高 | `max_iterations` 硬上限（默认 3）+ 每迭代累计 timeout |
| Layer 3 语义评估成本过高 | 中 | 中 | 默认关闭，config 手动开启；使用低成本模型 |
| 修复 prompt 越来越长超上下文窗口 | 低 | 高 | 第 3 次起压缩早期迭代为摘要（仅保留失败原因） |
| 非交互模式下 GEL 无法触发 Agent 修复 | 确定 | 中 | 非交互模式仅执行评估并报告结果，不自动循环 |
| 不同 Agent 产品输出格式差异导致 Runner 解析失败 | 中 | 高 | 每个 Runner 独立解析逻辑；GenericRunner 仅用 exit code 兜底 |

---

## 七、实施计划

| 阶段 | 内容 | 预估工作量 | 依赖 |
|------|------|-----------|------|
| Phase 1 | F-1 + F-2: Goal Schema + Evaluate Module | 2 天 | 无 |
| Phase 2a | F-3.5: AgentRunner 抽象层 + ClaudeRunner 迁移 | 1.5 天 | 无 |
| Phase 2b | F-3 + F-4: GEL 循环 + 渐进式修复（基于 Runner 接口） | 2 天 | Phase 1 + Phase 2a |
| Phase 3 | F-5: 可观测性 + 配置 + 测试 | 1 天 | Phase 2b |
| 验收 | 全量回归 + 集成测试 | 0.5 天 | Phase 3 |

**总预估**: 7 人天

---

## 八、开放问题

| 编号 | 问题 | 状态 | 备注 |
|------|------|------|------|
| Q-1 | LLM 生成的 `accept_criteria` 如何验证命令可行性？ | Open | 可在 plan 确认阶段预执行一次 |
| Q-2 | Layer 3 语义评估的 prompt 模板？ | Open | 需实验确定最佳 prompt |
| Q-3 | GEL 迭代中的 git commit 策略（每次迭代都 commit？） | Open | 建议: 每次迭代 commit，最终 tag 覆盖 |
| Q-4 | 是否支持 subtask 粒度的 `max_iterations` 覆盖？ | Open | 已在 goal schema 中预留，CLI 参数待设计 |
| Q-5 | OpenCodeRunner 的输出格式和错误检测机制？ | Open | 需调研 opencode CLI 的 stream 输出格式 |
| Q-6 | AgentRunner 是否需要支持同步/异步两种执行模式？ | Open | 当前 GEL 循环是同步的，未来并行 subtask 可能需要异步 |
