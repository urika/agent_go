# Verification Agent + /Goal 混合模式设计 Spec

> 版本: v1.0  
> 状态: Draft  
> 关联 Issue: N/A  
> 设计目标: 在 agent_go pipeline 中引入"验证 Agent"循环机制，结合 Claude Code `/goal` 原生能力，实现可配置的自动修复循环（verify → fix → verify → ... until pass or max_retries），并将验证结果反写为下游依赖阻断信号。

---

## 1. 设计目标

### 1.1 现状问题

当前 `executor.py:_verify_changes()` 的验证机制：

```
Claude 执行完成
  → git commit + tag
  → 运行验证命令（shell exit code）
  → 失败？
      → headless: 注入 fix prompt → 重跑 claude → 再验证 1 次（硬编码）
      → 交互: 弹出 user prompt（Continue/Retry）
  → 无论验证是否通过，都继续 pipeline（不阻断下游）
```

关键缺陷：
- 最大 1 次重试，无循环
- 验证失败不阻断下游依赖，级联失败不可控
- 验证结果只有 exit code，无语义信息传递
- fix prompt 是事后拼接的，Claude 缺乏完整上下文（看不到验证命令的 stdout/stderr）

### 1.2 目标

1. **可配置验证循环**：验证失败后自动修复并重新验证，直至通过或达到上限
2. **外部验证 + LLM 评估双模**：既支持确定性命令验证（`npm test`），也支持语义目标评估
3. **依赖阻断**：验证未通过的 subtask 阻断下游依赖，防止级联失败
4. **Goal 注入**：在 worktree 内注入 Claude Code 的 `/goal` 机制，让原生循环与外部验证协同
5. **全量失败反馈**：每次重试时，将 stdout/stderr + git diff 注入 fix prompt，形成闭环
6. **可观测性**：每次验证-修复循环记录到 metrics，供 eval 系统分析

---

## 2. 总体架构

```
┌────────────────────────────────────────────────────────────┐
│                    agent_go Pipeline                       │
│                                                            │
│  ┌──────────┐    ┌──────────┐    ┌────────────────────┐   │
│  │ Wave 调度 │───▶│ executor │───▶│ Verification Agent │   │
│  │ (topo)   │    │ run_sub  │    │ (verify loop)      │   │
│  └──────────┘    │ task()   │    └────────┬───────────┘   │
│                  └──────────┘             │               │
│                           ┌───────────────▼──────────┐    │
│                           │   双模评估引擎             │    │
│                           │  Mode A: Shell exit code  │    │
│                           │  Mode B: LLM 语义评估      │    │
│                           └───────────────┬──────────┘    │
│                                           │               │
│                           ┌───────────────▼──────────┐    │
│                           │   修复 Agent (Repairer)   │    │
│                           │   - 注入失败上下文         │    │
│                           │   - 重新调用 Claude        │    │
│                           │   - 循环直到通过/上限      │    │
│                           └───────────────────────────┘    │
│                                                            │
│  Worktree 内部（可选 /goal 集成）：                         │
│    TASK.md + "/goal <condition>" 让 Claude 原生循环         │
│    外部 Verification Agent 兜底安全校验                      │
└────────────────────────────────────────────────────────────┘
```

### 2.1 核心组件

| 组件 | 职责 | 文件 |
|------|------|------|
| **Verification Agent** | 验证循环编排器：执行验证 → 判断 → 触发修复 → 再验证 | `verifier.py` (新增) |
| **评估引擎 (Evaluator)** | 双模评估：Mode A shell exit code / Mode B LLM 语义 | `verifier.py` |
| **修复 Agent (Repairer)** | 构建 fix prompt 并调用 Claude 修复 | `verifier.py` |
| **Goal 注入器** | 在 worktree 中注入 `/goal` + Stop Hook | `goal_injector.py` (新增) |
| **pipeline 改造** | 验证失败时阻断下游依赖，传播阻塞状态 | `pipeline.py` (修改) |
| **executor 改造** | 调用 Verification Agent 替代 `_verify_changes` | `executor.py` (修改) |
| **config 扩展** | 新增验证循环配置项 | `config.py` (修改) |

---

## 3. 详细设计

### 3.1 Verification Agent 循环

```
VerificationAgent.run(subtask, worktree, env):
  │
  ├── 1. 收集变更（git status --porcelain）
  ├── 2. 执行验证命令（Mode A: shell exit code）
  │
  ├── 结果判断：
  │   ├── 所有命令通过 (exit 0/127) → ✅ verify_ok = true
  │   │   └── 触发 Git commit + tag → 返回结果
  │   │
  │   └── 有命令失败 (exit != 0) →
  │       ├── 检查 retry_count >= max_verify_retries？
  │       │   ├── 是 → ❌ verify_ok = false，标记 FAILED_BLOCKING
  │       │   └── 否 →
  │       │       ├── 收集失败上下文：
  │       │       │   - 失败命令 + exit code
  │       │       │   - stdout / stderr
  │       │       │   - 当前 git diff
  │       │       │   - 历史重试摘要（如有）
  │       │       ├── 构建修复 prompt（含完整上下文）
  │       │       ├── 调 RepairAgent.fix() → claude -p
  │       │       ├── retry_count++
  │       │       └── → 回到步骤 2（再验证）
  │       │
  │       └── ⚡ 可选 Mode B 评估（当配置 enable_llm_eval=true）：
  │           ├── 将验证结果 + 对话摘要发送给评估 LLM
  │           ├── 评估 LLM 返回：pass/fail + reason
  │           └── 结果与 shell exit code 做 AND 逻辑
  │
  └── 3. 返回验证结果 dict
       ├── verify_ok: bool
       ├── retry_count: int
       ├── retry_history: [{attempt, vcmd, exit_code, stdout_preview, fix_prompt_hash}]
       ├── mode: "shell" | "llm" | "hybrid"
       └── failure_reason: str (when verify_ok=false)
```

### 3.2 双模评估引擎

#### Mode A: Shell Exit Code（默认，确定性验证）

```
评估逻辑：
  for each vcmd in verification_cmds:
    result = subprocess.run(vcmd, cwd=worktree)
    if result.returncode not in (0, 127):
      return FAIL + {stdout, stderr, exit_code}
  return PASS
```

安全校验沿用现有的 `_is_safe_verification_command()` + `_build_sandbox_env()`。

#### Mode B: LLM 语义评估（可选，配置启用）

```
评估逻辑：
  将以下内容发送给评估 LLM（默认 Haiku 级别模型）：
    - Goal condition（来自 subtask 的 goal_condition 字段）
    - 验证命令的 stdout/stderr
    - Claude 执行后的 git diff --stat
    - 对话摘要（如有）

  评估 LLM 返回：
    {"passed": true/false, "reason": "..."}
```

评估 LLM 使用 `config.verification.goal.provider` 配置的模型，默认与 `plan_api` 相同，但可用更轻量模型。

#### 混合模式

当两种模式都启用时，结果的 AND 逻辑：

```
final_verify = mode_a_pass AND mode_b_pass
```

---

### 3.3 修复 Agent (Repairer)

修复 prompt 的构建规范：

```
fix_prompt = f"""
{task_md_original}

========================================
【验证失败 - 第 {retry_count} 次重试】
========================================

失败命令:
  {vcmd}

退出码: {exit_code}

标准输出:
{stdout}

错误输出:
{stderr}

当前变更摘要（git diff --stat）:
{git_diff_stat}

{retry_history_block}

========================================
请修复上述问题，确保以下验证命令全部通过:
{verification_cmds_formatted}

要求:
1. 直接修改文件，不要询问
2. 修改后运行验证命令确认通过
3. 如遇到编译/语法错误，优先修复明显的问题
4. 只修改必要文件，不要引入无关变更
========================================
"""
```

`retry_history_block` 在 retry_count > 1 时包含前次尝试的摘要（避免重复同样的错误）。

### 3.4 /goal 注入器

在 worktree 创建后、Claude 执行前，在 worktree 内注入 `/goal` 配置：

```json
// worktree/.claude/settings.json
{
  "hooks": {
    "Stop": {
      "command": "scripts/verify-goal.sh",
      "type": "script"
    }
  }
}
```

同时 TASK.md 末尾追加 `/goal` 命令：

```
/goal "以下验证命令全部退出码为0: npm test && ruff check"

之后 Claude Code 将在每个 turn 后自动执行 Stop Hook 中的验证脚本，
检查条件是否满足。满足后自动退出循环并提交代码。
```

**执行流程（带 /goal）：**

```
1. agent_go 创建 worktree
2. agent_go 写入 .claude/settings.json（Stop Hook）
3. agent_go 生成 verify-goal.sh（执行验证命令）
4. TASK.md 末尾追加 /goal condition
5. agent_go 执行: claude -p "TASK.md内容 + /goal"
6. Claude 进入 goal 循环：
   turn → 编码 → Stop Hook 执行 verify-goal.sh → exit 0? → 完成
   turn → 编码 → Stop Hook 执行 verify-goal.sh → exit 1? → 继续
7. Claude 退出后，agent_go Verification Agent 兜底再验证
8. 通过 → commit + tag → 继续 pipeline
```

**为什么不完全依赖 /goal？**

| 原因 | 说明 |
|------|------|
| 安全校验 | /goal 的 Stop Hook 脚本不经过 agent_go 的 4 级命令白名单 |
| 验证完备性 | 评估器不执行工具，只能读对话；agent_go 跑真实命令 |
| 阻断逻辑 | /goal 只控制 Claude 内部循环，不控制 pipeline 拓扑 |
| 跨任务可见性 | /goal 的结果不出 worktree，agent_go 需要将状态传递到下游 |

**结论：/goal 是 Claude 内部的加速循环，agent_go Verification Agent 是外部的安全兜底。**

---

## 4. 配置扩展

### 4.1 config.json 新增

```json
{
  "verification": {
    "max_retries": 3,
    "mode": "shell",
    "enable_goal": false,
    "block_on_failure": true,
    "goal": {
      "enable_goal_hook": false,
      "condition_template": "",
      "evaluator": {
        "provider": "anthropic",
        "model": "claude-haiku-4-20250514",
        "base_url": "",
        "api_key": ""
      }
    },
    "llm_eval": {
      "enabled": false,
      "provider": "anthropic",
      "model": "claude-haiku-4-20250514",
      "base_url": "",
      "api_key": ""
    }
  }
}
```

### 4.2 字段说明

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_retries` | int | 3 | 验证失败最大重试次数（0=不重试） |
| `mode` | str | `"shell"` | 验证模式: `shell` / `llm` / `hybrid` |
| `enable_goal` | bool | false | 是否在 worktree 内注入 /goal 机制 |
| `block_on_failure` | bool | true | 验证失败是否阻断下游依赖 |
| `goal.condition_template` | str | `""` | 自定义 /goal condition 模板，空则自动生成 |
| `goal.evaluator` | object | - | 评估模型配置（用于 Stop Hook 中的 prompt-based hook） |
| `llm_eval.*` | object | - | Mode B LLM 评估配置 |

### 4.3 subtask 扩展

plan_to_subtasks 阶段，每个 subtask 可以带验证相关的元数据：

```python
{
  "id": "sub-2",
  "title": "实现用户注册接口",
  "verification": "npm test tests/test_auth.py",
  "verification_mode": "shell",         # 覆盖全局设置
  "verification_max_retries": 5,         # 覆盖全局设置
  "goal_condition": "npm test 全部通过且 git status 干净",  # 自定义 goal
  "blocking": True,                      # 是否阻断下游（默认 True）
  "depends_on": ["sub-1"],
}
```

### 4.4 CLI 新参数

```bash
# run 命令新增
--verify-mode {shell|llm|hybrid}      # 验证模式
--verify-retries N                     # 验证最大重试次数
--verify-block                         # 验证失败阻断下游（默认开启）
--goal                                 # 启用 /goal 注入
```

```bash
# 使用示例
python3 agent_go.py run ~/repo "添加登录功能" \
  --verify-mode hybrid \
  --verify-retries 5 \
  --goal
```

---

## 5. 数据流

### 5.1 验证状态传播

```
executor.run_subtask()
  │
  ├── _create_worktree()
  ├── _inject_goal()              ← 新增：注入 .claude/settings.json + verify-goal.sh
  ├── _run_claude()               ← Claude 内部可能进行 /goal 循环
  │
  └── 替代 _verify_changes() →
      VerificationAgent.run()
        │
        ├── 执行验证命令
        ├── 通过？ → commit + tag → status="completed"
        │              ├── context.md 写入 ✅ verify=passed
        │              └── downstream 可见此 tag
        │
        └── 失败？ → retry 循环
             ├── 未达上限 → RepairAgent.fix() → re-verify
             └── 达到上限 →
                  ├── status="failed"
                  ├── context.md 写入 ❌ verify=failed, reason=...
                  ├── meta.json blocking=true
                  └── pipeline 跳过依赖此 subtask 的所有下游
```

### 5.2 依赖阻断逻辑

`pipeline.py` 波浪调度时新增检查：

```python
def _is_blocked(subtask, results_map):
    """检查 subtask 是否被上游阻断。"""
    for dep_id in subtask.get("depends_on", []):
        dep_result = results_map.get(dep_id)
        if dep_result is None:
            return True  # 依赖未执行
        if dep_result.get("status") == "failed" and dep_result.get("blocking", True):
            return True  # 上游失败且标记为 blocking
    return False
```

波浪调度修改：

```python
wave = [st for st in remaining
        if all(dep in completed_ids for dep in st.get("depends_on", []))
        and not _is_blocked(st, results_map)]  # ← 新增阻断检查
```

被阻断的 subtask 标记为 `blocked` 状态：

```python
{
    "subtask_id": "sub-3",
    "status": "blocked",
    "blocked_by": ["sub-2"],
    "summary": "上游 sub-2 验证失败，阻断"
}
```

---

## 6. 新文件与修改清单

### 6.1 新增文件

| 文件 | 行数预估 | 职责 |
|------|---------|------|
| `agent_go/verifier.py` | ~400 | Verification Agent + 评估引擎 + Repairer |
| `agent_go/goal_injector.py` | ~150 | /goal 注入：生成 settings.json + verify-goal.sh |

### 6.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `agent_go/executor.py` | 调用 VerificationAgent 替代 `_verify_changes()`；新增 `_inject_goal()` 调用点 |
| `agent_go/pipeline.py` | wave 调度加入 `_is_blocked()` 检查；`blocked` 状态处理；cleanup 阶段处理 blocked 的 worktree |
| `agent_go/config.py` | `DEFAULT_CONFIG` 加入 `verification` 配置块 |
| `agent_go/cli.py` | `run` 子命令新增 `--verify-mode`, `--verify-retries`, `--goal` 参数 |
| `agent_go/ui.py` | `verify_subtask()` 交互新增更多选项（重试详情、查看失败日志） |
| `agent_go/metrics.py` | 新增验证循环指标采集 |
| `agent_go/eval.py` | 新增验证相关 KPI（平均重试次数、阻断率、修复成功率） |
| `agent_go/tui.py` | TUI 面板显示验证状态和重试进度 |

---

## 7. 关键实现细节

### 7.1 Verifier 核心接口

```python
# agent_go/verifier.py

@dataclass
class VerificationResult:
    verify_ok: bool
    retry_count: int
    max_retries: int
    mode: str  # "shell" | "llm" | "hybrid"
    failure_reason: str = ""
    retry_history: list[dict] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

class VerificationAgent:
    def __init__(self, config: dict, logger: logging.Logger):
        self.max_retries = config.get("verification", {}).get("max_retries", 3)
        self.mode = config.get("verification", {}).get("mode", "shell")
        self.block_on_failure = config.get("verification", {}).get("block_on_failure", True)
        self.llm_eval_config = config.get("verification", {}).get("llm_eval", {})
        self.logger = logger

    def verify(
        self,
        subtask: dict,
        worktree: Path,
        env: dict,
        task_md: str,
        task_id: str,
        sub_id: str,
        allowed_tools: Optional[list] = None,
        active_pids: Optional[set] = None,
        active_pids_lock: Optional[threading.Lock] = None,
    ) -> VerificationResult:
        """主入口：执行验证循环。"""
        ...

class RepairAgent:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def fix(
        self,
        task_md: str,
        worktree: Path,
        env: dict,
        failed_vcmd: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        git_diff: str,
        retry_count: int,
        sub_id: str,
        allowed_tools: Optional[list] = None,
        active_pids: Optional[set] = None,
        active_pids_lock: Optional[threading.Lock] = None,
    ) -> bool:
        """构建 fix prompt 并调用 Claude 修复。返回修复是否成功。"""
        ...

class LLEvaluator:
    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def evaluate(
        self,
        goal_condition: str,
        verification_output: str,
        git_diff: str,
    ) -> tuple[bool, str]:
        """调用评估 LLM 判断 goal condition 是否满足。"""
        ...
```

### 7.2 Goal 注入器

```python
# agent_go/goal_injector.py

class GoalInjector:
    """在 worktree 中注入 /goal 所需的配置文件和脚本。"""

    GOAL_HOOK_SCRIPT = "scripts/verify-goal.sh"

    @staticmethod
    def inject(
        worktree: Path,
        verification_cmds: list[str],
        condition: str = "",
        evaluator_config: Optional[dict] = None,
    ) -> None:
        """
        在 worktree 中创建：
          .claude/settings.json  ← Stop Hook 配置
          scripts/verify-goal.sh ← 验证脚本
        """

    @staticmethod
    def build_goal_condition(
        verification_cmds: list[str],
        custom_condition: str = "",
    ) -> str:
        """从验证命令自动生成 /goal condition 字符串。"""

    @staticmethod
    def cleanup(worktree: Path) -> None:
        """清理注入的文件（pipeline 结束时）。"""
```

### 7.3 日志与指标

每次验证循环的输出：

```json
{
  "event": "verify_retry",
  "sub_id": "sub-2",
  "attempt": 2,
  "max_retries": 5,
  "vcmd": "npm test tests/test_auth.py",
  "exit_code": 1,
  "failure_snippet": "...AssertionError: expected 200, got 403...",
  "fix_prompt_length": 1850,
  "duration_ms": 45000
}
```

eval 系统新增指标：

| 指标 | 说明 |
|------|------|
| `Q9_retry_success_rate` | 重试后通过的比例 |
| `Q10_blocking_rate` | 阻断下游的比例 |
| `P7_verify_loop_time` | 验证循环总耗时 |
| `P8_avg_retries` | 平均重试次数 |
| `M1_goal_mode_usage` | /goal 模式的使用率 |

---

## 8. 恢复（Resume）兼容性

### 8.1 中断恢复时的验证状态

```python
# resume 逻辑新增：

def _recover_verification_state(task_dir, sub_id):
    """从本地恢复验证循环状态。"""
    state_file = task_dir / sub_id / "verify_state.json"
    if state_file.exists():
        return json.loads(state_file.read_text())
    return None
```

每次进入 Verification Agent 时，如果存在 `verify_state.json`，从断点恢复而非重新开始。

恢复流程：

```
resume 检测到 subtask 标记为 "running" 且 verify_state.json 存在
  → 读取 retry_count、失败信息
  → 从上次失败的修复尝试继续
  → 注入"这是恢复运行，请继续修复以下问题：..." 到 fix prompt
```

### 8.2 /goal 恢复

Claude Code 的 `/goal` 支持 session resume 后自动恢复未完成的 goal（轮数/token 计数重置）。
agent_go 在 resume 时检查 worktree 内是否有 `.claude/settings.json`，有则保留。

---

## 9. 边界条件处理

| 场景 | 处理方式 |
|------|---------|
| **验证命令全部跳过**（无 verification 字段） | 跳过验证，直接 commit + tag |
| **无文件变更** | 跳过验证（无内容可验证），status="no_changes" |
| **验证命令被安全门禁拒绝** | 记录 audit，标记 verify_ok=false，阻断下游 |
| **修复 Agent 超时** | 超时（600s）后 kill 进程，验证循环继续（不消耗重试次数） |
| **修复引入新问题** | 新的验证命令有更高优先级，修复 Agent 在后续重试中修正 |
| **达到 max_retries 仍失败** | 阻断下游，保留 worktree 供人工审查（`agent_go inspect <task-id>` 查看路径） |
| **/goal 循环不终止** | 外部 watchdog（`max_goal_turns` 或全局超时）强制 kill Claude 进程 |
| **同时启用 hybrid 模式** | shell 先过（快速过滤），llm 评估再跑（语义判断）；shell 不过则不触发 llm |
| **Restore 时 worktree 已损坏** | 清理后重新创建 worktree，从上游 merge 恢复 |

---

## 10. 实施计划

### Phase 1: 基础验证循环 + 现场保留（预估 3-4 天）

**验证循环核心：**
- 实现 `VerificationAgent` 基础类（Mode A only）
- 实现 `RepairAgent`（fix prompt 构建 + claude 调用）
- 改造 `executor.py` 替换 `_verify_changes`
- 改造 `pipeline.py` 依赖阻断（blocked 状态 + 波浪调度跳过）
- 配置新增 + CLI 参数
- 单元测试

**保留现场供人工审查：**

清理逻辑改造（`pipeline.py`）：
```python
# 清理时跳过失败/阻断的 worktree
for st in confirmed:
    r = results_map.get(st["id"])
    if r and r.get("status") in ("failed", "blocked"):
        logger.info(f"[worktree] 保留 {st['id']} 供人工审查: {wt_path}")
        continue  # ← 跳过删除
    wt_path = task_dir / st["id"] / "work"
    if wt_path.exists():
        _worktree_remove(repo, wt_path)
```

新增 `agent_go inspect` 命令：
```bash
# 列出失败/阻断的 worktree 路径
agent_go inspect <task-id>
# 输出示例：
# ❌ sub-2 (验证失败: npm test exit=1)
#    📁 ~/.agent_go/task-xxx/sub-2/work
#    🔗 git branch: agent_go/task-xxx/sub-2
#    📝 TASK.md | result.json | verify_state.json
#
# 🔗 sub-3 (上游 sub-2 阻断)
#    (未创建 worktree，无现场)
```

CLI 参数：
```bash
--preserve-worktrees   # 保留所有 worktree（默认仅保留 failed/blocked）
--no-preserve          # 强制清理所有 worktree（覆盖默认行为）
```

新增清单：
| 交付物 | 文件 | 说明 |
|-------|------|------|
| worktree 清理跳过逻辑 | `pipeline.py` | failed/blocked 的 worktree 不删除 |
| `agent_go inspect` 命令 | `cli.py` | 列出保留的 worktree 路径 + 状态摘要 |
| `--preserve-worktrees` | `cli.py` | 全局保留 flag（默认仅保留失败） |
| 文档提示 | — | terminal 中提示用户如何查看现场 |

### Phase 2: /goal 集成（预估 1-2 天）

- 实现 `GoalInjector`
- TASK.md 注入 /goal 命令
- worktree 内 Stop Hook 配置
- watchdog 超时保护

### Phase 3: LLM 评估 & 混合模式（预估 2 天）

- 实现 `LLMEvaluator`
- hybrid 模式编排
- 评估模型配置

### Phase 4: Resume 兼容 & Metrics（预估 1 天）

- verify_state.json 持久化
- recover 逻辑
- eval 新指标

---

## 11. 附录

### 11.1 与现有系统的集成点

```
executor.run_subtask() (line 376)
  ├── 在 _create_worktree 之后, _run_claude 之前
  │   └── 新增: GoalInjector.inject()
  │
  ├── 替换 _verify_changes() (line 224-347)
  │   └── 新增: VerificationAgent.verify()
  │
  └── 在 _generate_context() 中
      └── 新增: 验证状态写入 context.md

pipeline._run_pipeline() (line 48)
  ├── wave 调度 (line 107)
  │   └── 新增: _is_blocked() 过滤
  │
  └── 清理阶段 (line 208)
      └── 新增: GoalInjector.cleanup()
```

### 11.2 关键决策记录

| 决策 | 选项 | 选择理由 |
|------|------|---------|
| 外部验证 vs 仅 /goal | 混合 | 外部验证兜底安全，/goal 加速 Claude 内部循环 |
| 阻断 vs 非阻断 | 默认阻断 | 避免级联失败，可用 `--no-verify-block` 关闭 |
| 评估模型选择 | 配置化 | 允许用户根据成本/质量权衡选择 |
| fix prompt 是否包含完整 TASK.md | 包含 | 保留原始上下文，避免修复偏离目标 |
| 重试次数计数方式 | 连续失败才计数 | 部分通过不重置计数，避免无限循环 |

### 11.3 验证 Agent 市场 — Skill 生态落地方式

验证 Agent 市场的概念从远期（独立市场）到近期（Skill 复用）分三阶段演进。

#### Phase 1: Skill 体系复用（当前可做）

验证规则包不引入新目录/新体系，直接利用现有的 Skill 机制落地：

```
~/.agent_go/skills/
  verify-security-scan/          ← 安全扫描验证包
    SKILL.md
    ── frontmatter:
    │   name: verify-security-scan
    │   type: verification        ← 新增字段，区别于普通 Skill
    │   description: SQL注入/XSS/CSRF安全检查
    │   match: [".*api/.*", ".*auth/.*"]
    └── body:
        ## 验证命令
        - 命令: scripts/scan.sh --sql-injection
          描述: SQL 注入检测
        - 命令: scripts/scan.sh --xss
          描述: XSS 漏洞检测

  verify-api-compatibility/      ← API 兼容性验证包
    SKILL.md
    ── frontmatter:
    │   name: verify-api-compatibility
    │   type: verification
    │   description: API 变更的向后兼容性检查
    │   match: [".*openapi.*", ".*swagger.*"]
    └── body:
        ## 验证命令
        - 命令: python scripts/check_breaking_changes.py --old refs/heads/main --new .
          描述: 检查破坏性 API 变更
```

**与普通 Skill 的区分逻辑**：

| | 普通 Skill | 验证 Skill（type: verification） |
|--|-----------|--------------------------------|
| 作用阶段 | Plan/Execute，指导 Claude 怎么做 | Verify，检查 Claude 做得对不对 |
| 渲染目标 | TASK.md / Agent Prompt | Verification Agent 的验证命令列表 |
| 触发方式 | LLM 自动匹配 / `--skill` 显式指定 | 同 Skill 匹配机制 + Verification Agent 自动加载 |

**核心复用点**（零新增基础设施）：

| Skill 机制 | 复用到验证 Agent |
|-----------|----------------|
| `~/.agent_go/skills/<name>/SKILL.md` 目录结构 | 同上，新增 `type: verification` 区分 |
| `list_skills()` 发现 | 过滤 `type == "verification"` |
| `discover_skills()` 关键字匹配 | 同一匹配引擎，匹配 subtask 的 `title`/`description` |
| `load_skill()` 加载 | 同上 |
| `render_skill_for_execution()` 渲染 | 新增 `render_verification_commands()` 提取验证命令 |
| `--skill` CLI 参数 | 同一参数，Verification Agent 自动识别 verification 类型 |

**实现改动**（极小）：

```python
# skills.py 新增
def render_verification_commands(skill: Skill) -> list[dict]:
    """从验证 Skill 中提取验证命令列表。"""
    commands = []
    # 从 body 中解析 ## 验证命令 段落
    ... 
    return commands

def is_verification_skill(skill: Skill) -> bool:
    """判断是否为验证类 Skill。"""
    return skill.frontmatter.get("type") == "verification"
```

```python
# verifier.py VerificationAgent 初始化时
def _load_verification_skills(self, subtask: dict, repo: Path):
    """加载匹配的验证 Skill。"""
    from .skills import list_skills, load_skill, render_verification_commands, is_verification_skill
    
    all_skills = list_skills(repo)
    verify_skills = [s for s in all_skills if is_verification_skill(s)]
    
    # 1. subtask 显式指定的 --skill
    explicit = subtask.get("skills", [])
    # 2. 关键字自动匹配
    matched = discover_skills(subtask.get("title", ""), repo, max_skills=3)
    
    selected = [s for s in verify_skills if s.name in explicit or s in matched]
    for s in selected:
        skill = load_skill(s["name"], repo)
        if skill:
            cmds = render_verification_commands(skill)
            self.verification_cmds.extend(cmds)
```

#### Phase 2: Claude Code Plugin 分发（中期，生态成熟后）

当 Claude Code Plugin Marketplace 生态成熟（2025 Q4 已发布 Beta），将验证规则包打包为 Plugin：

```
my-verifiers-plugin/
  plugin.json           ← 插件清单
  skills/
    verify-security-scan/
      SKILL.md
    verify-api-compatibility/
      SKILL.md
  scripts/              ← 验证脚本
    scan.sh
    check_breaking_changes.py
```

通过 Plugin Marketplace 分发：

```bash
/plugin marketplace add my-org/verifiers
/plugin install verify-security-scan
```

#### Phase 3: 独立市场抽象（远期）

如果验证规则包的生态需求足够大，再抽象为独立体系：

```
~/.agent_go/verifiers/          ← 独立于 skills/ 的目录
  security-scan/
    VERIFIER.md                 ← 独立元数据格式
    rules/
      sql-injection.yaml
    scripts/
      scan.sh
```

何时触发 Phase 3：
- 验证规则包数量超过 Skill 的 50%
- 社区贡献者明确要求独立文档格式和发布渠道
- 验证规则包的匹配/组合逻辑与 Skill 出现显著分歧

```python
# eval.py 新增

def analyze_verification(results: list[dict]) -> dict:
    """分析验证循环效率。"""
    items = []
    for r in results:
        if r.get("verify_ok") is not None:
            items.append({
                "retry_count": r.get("retry_count", 0),
                "verify_ok": r.get("verify_ok", False),
                "mode": r.get("verification_mode", "shell"),
                "blocking": r.get("blocking", True),
            })

    if not items:
        return {"verification_score": 100}

    first_pass = sum(1 for i in items if i["retry_count"] == 0 and i["verify_ok"])
    retry_pass = sum(1 for i in items if i["retry_count"] > 0 and i["verify_ok"])
    blocked = sum(1 for i in items if not i["verify_ok"] and i["blocking"])
    total_retries = sum(i["retry_count"] for i in items)

    return {
        "first_pass_rate": round(first_pass / len(items) * 100),
        "retry_success_rate": round(retry_pass / max(len(items) - first_pass, 1) * 100),
        "blocking_rate": round(blocked / len(items) * 100),
        "avg_retries_per_subtask": round(total_retries / len(items), 2),
        "verification_score": round(
            (first_pass * 100 + retry_pass * 60) / len(items)
        ),
    }
```

### 11.4 实施偏差记录（2026-07-25 全链路验收后补记）

| 设计 | 实际实现 | 说明 |
|------|---------|------|
| `verifier.py` / `goal_injector.py` 新文件 | 验证循环在 `executor.py:_verify_changes()`，无独立 verifier.py | 功能等价，未做文件拆分 |
| Stop Hook（.claude/settings.json + verify-goal.sh） | **未实现**，仅有 TASK.md 文本注入 + subtask.py watchdog | Phase 2 剩余项 |
| `verification.llm_eval.*` 配置键 | 顶层 `evaluator.*` | 键名不同，功能等价 |
| `mode: shell/llm/hybrid` 选择器 | 无；shell 必跑，LLM 评估为可选叠加（shell 不过则不触发，等价 AND） | 纯 llm 模式不存在 |
| `goal.enabled` 默认 false | 默认 true | 与设计相反，产品决策待确认 |
| `blocked_by` 字段、Q3 口径（retry==0 AND verify_ok）、P8 平均重试（Q10） | 验收中补齐 | 见 tests/test_s2_acceptance.py |
| CLI 覆盖（--max-retries/--no-goal/--semantic-eval） | 验收中修复：经 run_subtask config 参数 + env 贯通，此前 load_config() 读磁盘导致全部断线 | — |
| 关键 bug：wave 调度未排除 blocked 子任务 | 验收中修复（阻断此前形同虚设） | — |
