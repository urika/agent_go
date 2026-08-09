# agent_go 执行能力评估

> 评估日期：2026-08-09
> 评估任务：`task-20260809-093426-382-fe30`
> 场景：Bench 收敛阶段 B：验证命令和 Plan 收敛

## 一、执行结果

本次通过 agent_go 在独立 worktree 中执行 Stage B，结果为：

```text
sub-1 completed
sub-2 verification_failed
sub-3 blocked
sub-4 blocked
task VERIFICATION_FAILED
```

失败 worktree 被保留，未合并到主工作树。主工作树的既有 P0/P1 改动没有被本次 agent_go 任务覆盖。

## 二、已验证能力

本次执行证明 agent_go 已具备：

- 能从自然语言任务生成多步骤 Plan。
- 能建立 DAG 依赖并按波次执行。
- 能为子任务创建隔离 worktree。
- 能在上游失败后阻断下游，避免继续执行不满足依赖的任务。
- 能保留失败 worktree，供后续 review。
- 能在子任务级别执行验证、修复重试和语义评估。
- 能在 subtask 失败后将任务级状态设为 `VERIFICATION_FAILED`。
- 能通过 commit/tag 记录子任务完成边界。

这些能力说明 agent_go 可以作为“Plan -> Execute -> Verify”的执行编排底座。

## 三、发现的问题

### 1. 自动拆解的文件所有权不清晰

本次 Plan 中：

- sub-1 修改 `planning.py`、`pipeline.py`、`failure.py`、`bench_schema.py` 等多个核心文件。
- sub-2 又修改 `planning.py` 和 `subtask.py`。

多个 subtask 修改同一个核心文件，导致实现互相重写、语义验收难以对应最终 diff，也提高了 merge conflict 风险。

结论：

```text
功能边界合理 != 文件所有权合理
```

### 2. Worker 重复实现已有安全能力

sub-1 没有稳定复用已有的：

```python
agent_go.utils._is_safe_verification_command
```

而是重新实现 verification command 白名单和检查逻辑，造成规则漂移风险。安全命令解析只能有一个权威实现，不能由每个任务重复实现。

### 3. Plan 没有形成函数级验收契约

sub-2 的任务要求实现：

```text
check_step_consistency
detect_dependency_cycle
```

但最终实现与任务契约、实际调用链和语义审查预期不一致。局部测试通过不代表功能已经接入真实 Plan 执行路径。

### 4. 未提交改动不会自动进入 agent_go worktree

执行时主工作树存在未提交的 P0/P1 改动。agent_go 的隔离 worktree 从 Git `HEAD` 创建，因此 Worker 看到的是提交基线，不是主工作树中的未提交版本。

结果可能是：

- Worker 重复实现已有能力。
- Worker 不知道最新状态和接口变更。
- 新旧逻辑发生冲突。

结论：使用 agent_go 前必须准备干净、可追踪的基线 commit。

### 5. 下游 blocked 是正常结果，不是独立根因

sub-3、sub-4 因 sub-2 失败而 blocked。这符合 DAG 阻断逻辑，不应把它们重复统计为独立模型失败。真正根因是 sub-2 的 Plan consistency 实现未通过验收。

## 四、能力边界

| 能力 | 当前评价 |
|---|---|
| Plan 生成 | 可用，但复杂增量任务的拆分质量不稳定 |
| DAG 调度 | 可用，失败阻断逻辑有效 |
| Worktree 隔离 | 可用 |
| 验证和重试 | 可用，但验证命令和语义验收需要更强约束 |
| 失败现场保留 | 可用 |
| 已有代码理解 | 中等，容易重复实现已有工具 |
| 多 subtask 核心文件协作 | 风险较高 |
| 直接合并 Worker 产物 | 不建议，必须经过独立 review |
| 处理未提交基线 | 不支持自动继承 |

当前适合的定位：

```text
受约束的执行编排器
```

不应直接定位为：

```text
无需人工审查的自主软件交付系统
```

## 五、后续使用规范

### 基线要求

- 执行前提交或明确记录干净基线 commit。
- 不让 agent_go worktree 依赖主工作树的未提交改动。
- 每个 subtask 明确生产文件所有权。

### Plan 要求

- 同一个核心文件只能由一个 subtask 负责实现。
- 现有公共函数必须明确标记为“复用”，禁止重复实现。
- 每个 subtask 提供函数级或行为级验收条件。
- 验证命令使用单一、可白名单解析的命令。

### Review 要求

- subtask `completed` 不等于 Stage accepted。
- 必须检查最终 diff、实际调用链和测试覆盖。
- 下游 blocked 只作为上游失败的派生结果统计。
- 失败 worktree 不得未经 review 直接合入主分支。

### 推荐拆解

对于 Stage B，推荐使用：

```text
sub-1: planning.py 核心检查
  -> sub-2: cli/pipeline 接入
  -> sub-3: 字段透传和 metadata
  -> sub-4: 集成测试和全量回归
```

每个阶段完成后独立 review，再进入下一阶段。

## 六、结论

本次失败不是 agent_go 完全不能完成 Stage B，而是暴露了其在“已有复杂代码上的增量治理任务”中的边界：

- 执行、隔离、验证、阻断能力已经可用。
- 自动拆解的文件所有权和既有代码复用能力不足。
- 复杂治理任务不适合一次性自动拆成多个交叉修改核心文件的 subtask。
- 需要干净基线、单文件所有权、阶段 review 和显式交接协议。

后续阶段 B 应拆成更小的串行任务，并优先复用现有安全命令解析和状态基础设施。
