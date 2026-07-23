# agent_go 设计评审 — 概念与机制问题

> 原始路径: docs/design/DESIGN-REVIEW-概念与机制问题.md
> 整理日期: 2026-07-24

> **评审日期**: 2026-05-26  
> **评审范围**: 全部 13 个源文件，聚焦概念模型与机制设计  
> **方法**: 不审查代码风格/质量，只审查"系统设计层面的假设是否成立"

---

## 问题总览

| 层级 | # | 问题 | 严重度 |
|------|---|------|--------|
| Tier 1 | D-1 | Plan 生成缺乏代码理解 | 🔴 Fundamental |
| Tier 1 | D-2 | Git-as-IPC 协调模型语义缺失 | 🔴 Fundamental |
| Tier 1 | D-3 | 无执行反馈闭环 | 🔴 Fundamental |
| Tier 2 | D-4 | Agent Type 定义与执行断裂 | 🟡 Abstraction Leak |
| Tier 2 | D-5 | Skill 是不可观测的 Prompt 片段 | 🟡 Abstraction Leak |
| Tier 2 | D-6 | 验证通过 ≠ 任务完成 | 🟡 Abstraction Leak |
| Tier 3 | D-7 | 固定粒度计划无法表达复杂工作流 | 🟠 Scaling |
| Tier 3 | D-8 | 上下文线性累积污染 | 🟠 Scaling |

---

## Tier 1 — 基础假设缺陷

### D-1: Plan 生成缺乏代码理解

**概念**: LLM 在没有源码上下文的情况下生成执行计划。

**现状机制**:
```
generate_plan() 的输入:
  ├── 任务描述 (用户原始文本)
  ├── analyze_project() → git ls-files / find 文件列表
  ├── get_git_info() → remote URL, branch, commit SHA
  ├── get_resource_map() → 目录结构、配置文件列表
  └── Skill 摘要 (可选)
```

**问题**: Plan prompt 中**没有任何源代码内容**。LLM 只知道"有哪些文件"，不知道"文件里写了什么"。生成的 plan 本质上是**基于文件路径和任务描述的推测**。

**具体表现**:

| 场景 | 预期行为 | 实际行为 |
|------|---------|---------|
| "给 User 模型加 email 字段" | 读取 models.py 确认字段名、类型约定 | 仅列出 models.py 为涉及文件，不了解现有字段 |
| "修复 auth 中间件的超时 bug" | 定位超时逻辑，理解错误处理链 | 仅标记 auth*.py，不了解实际实现 |
| "重构数据库查询层" | 理解 ORM 用法、query patterns | 仅列出 db/ 目录，不了解查询模式 |

**影响**: 
- 步骤划分依赖猜测，依赖关系可能遗漏
- `files` 字段可能指向错误文件
- `agent_prompt` 中的指令缺乏具体上下文，Claude Code 仍需自行探索

**可能的改进方向**:
1. 为关键文件注入代码摘要（如类签名、函数列表、import 依赖图）
2. 让 LLM 在 plan 前增加"探索阶段"，主动请求读取特定文件
3. 利用 LSP symbols 提取项目结构化信息

---

### D-2: Git-as-IPC 协调模型语义缺失

**概念**: 子任务之间通过 git merge + 非结构化文本文件传递工作成果。

**现状机制**:
```
子任务 A 完成 → git commit + tag → 子任务 B merge A 的 tag
                                      ↓
                              读取 SHARED_CONTEXT.md (纯文本)
                              读取 A 修改的文件 (代码 diff)
```

**问题**: Git 提供的是**字节级同步**（文件内容一致），不是**语义级协调**（理解变更意图）。

**具体表现**:

| 信息类型 | 传递方式 | 问题 |
|---------|---------|------|
| "我创建了 UserService 类" | SHARED_CONTEXT.md 纯文本 | B 不知道类签名、接口定义 |
| "API 返回格式改为 v2" | 代码 diff | B 可能仍按 v1 调用，直到运行时报错 |
| "第 3 步不需要了" | 无机制传递 | B 仍按原 plan 执行 |
| 冲突文件 | git merge conflict | 依赖 Claude Code 自行解决，无上下文 |

**核心矛盾**: 
子任务在独立 worktree 中执行，彼此是**隔离的 Agent**。它们之间的"通信协议"是 git（字节同步）+ 自由文本（SHARED_CONTEXT.md），缺乏：
- **结构化契约**：API 变更、接口定义、数据格式
- **意图传递**：为什么改、改了什么、下游应该怎么适配
- **冲突预防**：基于语义的潜在冲突检测

**可能的改进方向**:
1. 定义结构化的 `ARTIFACT.md` 格式（接口变更、新增类型、API contract）
2. 在 merge 后增加"上下文注入"步骤，自动提取上游关键变更摘要
3. 为下游子任务生成 diff-aware 的 agent_prompt

---

### D-3: 无执行反馈闭环

**概念**: Plan 确认后，执行是单向 fire-and-forget 流水线。

**现状机制**:
```
Plan 确认 → _run_pipeline() → wave1 → wave2 → ... → 完成
                                        ↓
                              某步失败 → auto-retry (最多 N 次)
                                        ↓
                              仍失败 → 标记失败，继续后续步骤
```

**问题**: Plan 被视为**静态契约**，执行结果不影响后续步骤的执行方式。

**缺失的反馈循环**:

```
理想流程:
  Step 1 执行 → 结果分析 → Step 2 是否需要调整？
                          ↓
                  ├─ 方案 A: 原计划有效 → 继续
                  ├─ 方案 B: 需要微调 → 修改 Step 2 agent_prompt
                  └─ 方案 C: 方案失效 → 重新规划 Step 2+

当前流程:
  Step 1 执行 → exit code 0 → Step 2 按原计划执行（不管 Step 1 实际做了什么）
```

**影响**:
- 前序步骤的偏差会**线性传播**，越往后偏差越大
- 没有机制从中间结果学习并修正
- 失败重试是"再跑一遍同样的指令"，而非"分析失败原因后调整"

**可能的改进方向**:
1. 每步完成后增加"结果评估"环节，与 plan 预期对比
2. 支持 plan 动态修订（delta-plan）：后续步骤可基于已完成步骤的实际输出生成
3. 引入"检查点"概念：关键节点暂停，等待用户确认方向

---

## Tier 2 — 抽象泄漏

### D-4: Agent Type 定义与执行断裂

**概念**: Agent Type 定义了角色和权限，但执行时不被完整执行。

**现状机制**:

```
AgentType 定义:
  ├── permission_mode: "default" | "bypassPermissions" | "acceptEdits"
  ├── allowed_tools: ["Read", "Write", "Edit", "Bash"]  ← 仅声明
  └── preload_skills: ["security-review"]                ← 从未消费

实际执行:
  ├── permission_mode → 映射为 --permission-mode CLI 参数 ✅
  ├── allowed_tools → 未传递给 Claude Code ❌
  ├── preload_skills → 未在 get_claude_command() 中使用 ❌
  └── Headless 模式 → 强制 bypassPermissions，无视 Agent 配置 ❌
```

**问题**: 用户配置了一个 `architect` Agent（只读），期望 Claude Code 在执行时**真的只读**。但实际上 `allowed_tools` 只存在于 JSON 定义中，从未转化为任何运行时约束。

**虚假的安全感**: 系统展示了 4 种 Agent 类型和它们的权限差异，但差异只在 `permission_mode` 层面（且 headless 模式连这个都绕过了）。`allowed_tools` 字段是一个**纯装饰性声明**。

**可能的改进方向**:
1. 通过 Claude Code 的 `--allowedTools` 参数实际限制工具集
2. Headless 模式应尊重 Agent 配置，而非硬编码
3. 消费 `preload_skills`，在命令构建时注入 skill 内容

---

### D-5: Skill 是不可观测的 Prompt 片段

**概念**: Skill 被定义为领域知识注入机制，但实际上只是 Markdown 文本拼接到 TASK.md 中。

**现状机制**:
```
Skill 加载 → render_skill_for_execution() → 拼接到 TASK.md 正文
                                              ↓
                                    Claude Code 读取 TASK.md
                                              ↓
                                    可能遵循，可能忽略
                                              ↓
                                    无任何合规性检查
```

**问题链**:
1. **注入不等于遵守**: Skill 内容是建议性的，Claude Code 没有义务遵循
2. **无合规度量**: 无法知道 Skill 中哪些指导被采纳、哪些被忽略
3. **无效果学习**: 系统不知道某个 Skill 是否真的提升了输出质量
4. **无版本追踪**: Skill 内容变更后，无法追溯哪些历史任务用了哪个版本

**影响**: Skill 系统给用户一种"领域知识被系统化应用"的印象，但实际效果完全取决于 Claude Code 对 prompt 文本的理解和遵循程度——这是不可控的。

**可能的改进方向**:
1. 将 Skill 关键指令转化为 `--instructions` 参数（Claude Code 原生支持）
2. 执行后对比 Skill 推荐与实际行为，生成合规性报告
3. 引入 Skill 效果追踪（基于验证结果关联分析）

---

### D-6: 验证通过 ≠ 任务完成

**概念**: 系统用 shell 命令的退出码判断子任务是否成功，但退出码 0 只表示"命令没报错"。

**现状机制**:
```python
# executor.py 中的验证逻辑
verification = subtask.get("verification", "")
if verification and _is_safe_verification_command(verification):
    result = subprocess.run(verification, shell=True, cwd=str(worktree))
    if result.returncode != 0:
        # 标记失败，可重试
```

**问题**: 验证命令是 LLM 生成的**单一 shell 命令**，覆盖面有限：

| 验证命令 | 实际验证了什么 | 没验证什么 |
|---------|-------------|-----------|
| `go build ./...` | 编译通过 | 功能正确性、边界情况 |
| `pytest tests/` | 测试通过 | 测试覆盖率、是否遗漏测试 |
| `python -c "import mod"` | 模块可导入 | 模块行为正确 |

**更深层的矛盾**: 
- 验证命令由 LLM 在 Plan 阶段生成（D-1 问题：LLM 不理解代码）
- 即使验证命令本身设计合理，它也只验证了**系统状态**，不验证**任务语义**
- "给 User 模型加 email 字段" → `python -c "from models import User; hasattr(User, 'email')"` → ✅ 通过，但可能缺少 migration、validation、tests

**可能的改进方向**:
1. 支持多级验证：编译 → 测试 → 语义检查（基于任务描述生成断言）
2. 验证命令与任务描述交叉验证：不仅检查"做了什么"，还检查"没遗漏什么"
3. 引入 LLM 验证环节：执行后让 LLM 对比任务描述与实际 diff

---

## Tier 3 — 扩展性瓶颈

### D-7: 固定粒度计划无法表达复杂工作流

**概念**: Plan 被约束为 2-5 个扁平步骤的 DAG，无法表达迭代、条件、嵌套。

**现状约束**:
```
Plan prompt 中明确要求:
  "步骤 2-5 个，可独立执行"
  
Plan 结构:
  steps: [step1, step2, step3, ...]  ← 扁平列表
  dependencies: {"2": [1], "3": [1]} ← 单层 DAG
```

**无法表达的工作流模式**:

| 模式 | 示例 | 当前能力 |
|------|------|---------|
| 迭代循环 | "反复优化性能直到 P99 < 100ms" | ❌ 只能展开为固定步数 |
| 条件分支 | "如果用了 Redis 则...否则..." | ❌ Plan 是静态的 |
| 嵌套分解 | "重构模块 A" → A 有 5 个子任务 | ❌ 只能展开为一层 |
| 探索-执行 | "先调研方案，再决定实现路径" | ⚠️ 需人工拆分 |
| 并行-聚合 | "同时测试 3 种方案，选最优" | ❌ 无聚合/决策机制 |

**影响**: 对于复杂任务，用户被迫在"粒度太粗（每步太大，Claude Code 容易跑偏）"和"粒度太细（5 步上限不够）"之间妥协。

**可能的改进方向**:
1. 支持 Hierarchical Task Network (HTN)：允许步骤内嵌子计划
2. 引入"计划模式"：线性 / 迭代 / 条件 / 探索
3. 移除步数上限，改为总 token/时间预算控制

---

### D-8: 上下文线性累积污染

**概念**: 上游子任务的上下文通过 SHARED_CONTEXT.md 线性传递给所有下游子任务，缺乏过滤和优先级。

**现状机制**:
```
Subtask 1 完成 → 写入 SHARED_CONTEXT.md
                ↓
Subtask 2 merge → 读取 SHARED_CONTEXT.md (只有 S1 的内容)
                ↓ 写入自己的上下文
Subtask 3 merge → 读取 SHARED_CONTEXT.md (S1 + S2 的内容)
                ↓ 写入自己的上下文
Subtask 4 merge → 读取 SHARED_CONTEXT.md (S1 + S2 + S3 的内容)
```

**问题**: 
- **信号衰减**: SHARED_CONTEXT.md 是 append-only 的自由文本。随着步骤增加，有用信息的信噪比持续下降。
- **无关上下文**: 子任务 4 可能只依赖子任务 2 的输出，但被迫处理 1 和 3 的全部上下文。
- **token 浪费**: 每个 Claude Code 实例都要读取并处理不断增长的上下文文件。

**具体场景**:
```
3 个并行子任务 A, B, C → 汇聚到 D

D 需要的信息:
  - A 的 API 接口定义 ✅ 有用
  - B 的数据库 schema 变更 ✅ 有用
  - A 的调试日志 ❌ 噪声
  - B 的中间尝试记录 ❌ 噪声
  - C 的全部上下文（D 不依赖 C） ❌ 完全无关

实际行为: D 被迫处理 A+B+C 的全部文本
```

**可能的改进方向**:
1. 结构化上下文：区分"接口契约"（必须传）、"实现细节"（可选）、"调试日志"（不传）
2. 基于依赖图的上下文过滤：只传递直接上游的上下文
3. LLM 摘要压缩：每步完成后生成结构化摘要替代原始文本

---

## 问题关联图

```
D-1 (Plan 无代码理解)
 │
 ├──→ D-6 (验证命令设计不可靠，因 LLM 不理解代码)
 │
 ├──→ D-3 (无法基于中间结果修正，因初始 Plan 本身就是推测)
 │
 └──→ D-7 (固定粒度限制了 Plan 表达力)

D-2 (Git-as-IPC 语义缺失)
 │
 ├──→ D-8 (上下文累积因缺乏结构化传递机制)
 │
 └──→ D-3 (反馈闭环需要语义理解，但只有字节同步)

D-4 (Agent Type 断裂) ──独立── 可单独修复
D-5 (Skill 不可观测) ──独立── 可单独修复
```

---

## 修复优先级建议

| 优先级 | 问题 | 投入产出比 | 理由 |
|--------|------|-----------|------|
| **P0** | D-4 Agent Type 断裂 | ⭐⭐⭐⭐⭐ | 改动小，效果直接，修复虚假安全感 |
| **P0** | D-5 Skill 不可观测 (部分) | ⭐⭐⭐⭐ | 改用 `--instructions` 参数，改动小 |
| **P1** | D-6 验证增强 | ⭐⭐⭐⭐ | 支持多级验证，减少"假成功" |
| **P1** | D-8 上下文过滤 | ⭐⭐⭐ | 结构化上下文格式，中等改动 |
| **P2** | D-1 Plan 代码理解 | ⭐⭐⭐ | 收益大但改动大，需引入代码摘要机制 |
| **P2** | D-2 语义协调 | ⭐⭐ | 核心架构变更，影响面广 |
| **P3** | D-3 反馈闭环 | ⭐⭐ | 需要 Plan 动态修订能力 |
| **P3** | D-7 工作流表达 | ⭐⭐ | 需要重新设计 Plan 数据模型 |
