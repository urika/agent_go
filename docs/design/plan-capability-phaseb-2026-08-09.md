# Task Plan 能力分析与阶段 B 收敛记录

> 更新日期：2026-08-09
> 关联：[ADR-009](adr/ADR-009-bench-convergence.md) Bench 收敛、[bench-convergence-plan.md](bench-convergence-plan.md) 收敛计划、[ISSUES.md](../ISSUES.md)

本文档记录两个产出：
1. **Task Plan 能力现状评估与改进方向**（用户问题）
2. **阶段 B 收敛执行记录**（用 agent_go 自身 dogfooding 完成）

---

## 1. Task Plan 能力现状

### 1.1 已覆盖的预检能力（`planning.py` + `utils.py`）

| 能力 | 位置 | 类型 |
|---|---|---|
| 验证命令白名单（结构化正则） | `utils.py _CMD_ARG_RULES` | 安全 |
| 验证命令 shell 注入扫描 | `utils.py _is_safe_verification_command` | 安全 |
| python -c 单行结构预检（装饰器/with/换行） | `utils.py Stage 2.5` | 语法 |
| **python -c compile 语法预检（新增）** | `utils.py Stage 2.5` | 语法 |
| 依赖循环检测 | `planning.py validate_plan_quality` | 结构 |
| requirement/acceptance coverage | `planning.py` | 追踪 |
| scope_conflict（files ∩ do_not_touch） | `planning.py` | 结构 |
| 过度/欠分解检测（G5/G6） | `planning.py` | 质量 |
| 难度交叉核对（G4） | `planning.py difficulty_hint` | 质量 |
| agent_prompt 函数引用检查（P2） | `planning.py check_agent_prompt_functions` | 质量 |

### 1.2 阶段 B 修复的问题

**B-1 scope_conflict 误报（已通过 agent_go 修复）**
- 问题：`files_hint`（涉及/引用文件）被并入 `files`（修改文件）集合，验证子任务引用 do_not_touch 源码文件被误判冲突 → BLOCK 任务（非确定性失败）
- 修复：scope_conflict 只检查 `files` ∩ `do_not_touch`，`files_hint` 不计入
- 位置：`planning.py:58-63`
- 测试：新增 `test_validate_plan_quality_files_hint_no_scope_conflict`

**B-2 ISSUE-29 验证命令语法预检（已通过 agent_go sub-1 修复）**
- 问题：LLM 生成的 `python -c` 单行命令含 try/except 单行拼接 → SyntaxError → 正确代码误判 failed
- 修复：`_is_safe_verification_command` 增加 `compile()` 预检，编译失败即拒绝（`python -c 语法错误`）
- 位置：`utils.py Stage 2.5`
- 测试：5 项新增（try/except 拒绝、if/else 拒绝、合法 import/print 通过、多语句通过、python3 同样处理）

### 1.3 评估后未采用的方向

**rejected 验证命令跳过 retry（agent_go sub-2 提出）**
- 提议：验证命令被拒绝时不消耗 retry 预算，直接判失败
- **未采用原因**：
  1. 拒绝的验证命令（如 `python -c` 语法错误）现在已被 compile 预检在计划阶段拦截，执行阶段极少出现"不可修复的 rejected 命令"
  2. 对真正的不安全命令（如 `rm -rf`），让 Claude 重试修复成安全命令是有价值的——跳过 retry 会阻止这种修复
  3. sub-2 改动范围过大（含 `AGENT_GO_METERING_PATH`/`AGENT_GO_CLAUDE_MODEL` 环境透传清理等超范围改动），验证时因 E2E 测试在沙箱环境失败被误判

---

## 2. 用 agent_go 完成阶段 B（dogfooding 记录）

### 2.1 方法

在 agent_go 自身仓库上运行 `agent_go run`，让 agent_go 的 Claude 子任务完成阶段 B 的代码修复。

### 2.2 任务 1：scope_conflict 误报修复

- **结果**：✅ agent_go 完整完成
  - 1 个子任务，Claude 自动读取代码 → 修改 → 运行测试 → 语义评估通过（confidence=0.95）
  - delivery branch 创建，cmd_merge 合并到 main
  - 全量 1953 测试通过

### 2.3 任务 2：验证命令语法预检 + rejected retry

- **sub-1**（compile 预检）：✅ 完成，语义评估 confidence=0.95，cherry-pick 到 main
- **sub-2**（rejected 不消耗 retry）：⚠️ agent_go 验证失败（test_executor E2E 在沙箱环境失败），评估后**未采用**该改动

### 2.4 观察到的 Plan 能力问题

用 agent_go 执行过程中观察到的 agent_go 自身 Plan 能力局限：

| 观察 | 问题 | 建议 |
|---|---|---|
| Skill 误匹配 | Python 修复任务自动匹配 `frontend-react` skill | skill 匹配需要更强的任务类型推断 |
| 超范围改动 | sub-2 Claude 顺手修改 env 透传逻辑（超出任务描述） | 需要更严格的 scope 约束 |
| 验证环境局限 | test_executor E2E（真实 spawn claude）在沙箱验证环境失败，手动 shell 通过 | 验证命令需要区分"沙箱可跑"与"需外部进程"的测试 |
| Plan 分解 | 任务2被拆成 3 个子任务（含依赖），sub-3 级联阻断 | 依赖链失败时下游阻断合理 |

---

## 3. 阶段 B 收敛状态

| 门禁 | 状态 |
|---|---|
| verification command rejected 可单独统计且不计模型能力失败 | ✅ failure.py:82-83 已归类 `infrastructure_failure` |
| scope_conflict 误报修复 | ✅ 已通过 agent_go 修复 |
| ISSUE-29 语法预检 | ✅ 已通过 agent_go 修复 |
| 验证命令执行前阻断 | ⚠️ compile 预检已在计划阶段拦截语法错误命令；真正的"命令被白名单拒绝"仍允许 Claude 重试修复（设计取舍） |

### 阶段 B 剩余项

- [ ] Golden Tasks（阶段 C）：6 任务 × 1 模型 × repeat 3，验证系统逻辑和可重复性
- [ ] 阶段 D：代表性实验（Plan/Verifier 改动前后对比）
- [ ] 阶段 E：正式 decision baseline

---

## 4. 风险隔离与验证评估（dogfooding 视角）

### 4.1 agent_go 的风险隔离：实际表现

| 隔离层 | 机制 | dogfooding 实际表现 |
|---|---|---|
| 工作区隔离 | 每子任务独立 `git worktree` + 独立分支 | ✅ 子任务全程在 `~/.agent_go/task-*/sub-*/work`，主仓库 main 未被污染，交付通过 delivery branch + cmd_merge 合入 |
| 变更边界 | commit 是唯一完成边界，recover 永不 commit 孤儿改动 | ✅ 失败子任务改动隔离在 worktree，未混入主分支 |
| 级联阻断 | 上游 failed → 下游 blocked | ✅ sub-2 失败后 sub-3 正确级联阻断 |
| 失败保留 | failed/blocked worktree 保留供审查 | ✅ sub-1/sub-2 worktree 保留，可直接查看 diff |
| 交付隔离 | delivery branch 独立，PR head/base 显式 | ✅ 交付前主仓库完全不受影响 |

### 4.2 验证机制：三层防御

1. **验证命令白名单**（`_CMD_ARG_RULES`）：拒绝 shell 注入/危险命令/路径穿越
2. **验证命令执行**：白名单内命令 + LLM 语义评估
3. **验证重试循环**：失败 → 注入失败上下文让 Claude 修复 → 重验

### 4.3 对比：agent_go vs 直接 agent 执行

| 维度 | agent_go | 直接 agent（Claude Code） |
|---|---|---|
| 工作区安全 | worktree 隔离，失败不污染主分支 | 直接改当前工作区，失败留半成品 |
| 验证门禁 | 自动验证命令 + 语义评估 + retry | 靠人审，无自动验证 |
| 失败恢复 | worktree 保留 + recover/resume | 手工 git 回滚 |
| 并发安全 | wave 调度 + task lock + gc.auto 管理 | 单线程顺序 |
| 上下文连续性 | **受限**——子任务只看 TASK.md + 相关文件 | **连续**——看整个仓库 + 对话历史 |
| 验证真实性 | 沙箱环境可能**失真**（E2E 测试跑不了，见 ISSUE-31） | 真实环境执行，结果可信 |
| 超范围控制 | 弱——Claude 顺手改无关代码（见 ISSUE-32） | 同样弱，但人在场可即时阻止 |
| 审计 | meta/metering/result 全记录 | 无结构化审计 |

### 4.4 核心权衡结论

- **agent_go 赢在**：隔离性、自动验证、可恢复、可审计——适合"无人值守的批量任务"。
- **直接 agent 赢在**：上下文连续性、验证环境真实性、人工即时干预——适合"需全局判断的复杂修复"。
- **核心权衡**：agent_go 用**上下文隔离**换取**安全隔离**，但隔离也让子任务"看不见全局"，且验证在沙箱中可能失真（ISSUE-31 是最大风险）。
- **一句话**：agent_go 的隔离是扎实的核心护城河；但验证环节的"环境真实性"是目前最大风险——需让验证环境与真实执行环境对齐，否则严格的验证门禁反而成为误判源。
