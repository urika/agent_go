# Workflow 确定性编排 vs 动态 Subagent：agent_go 架构改进设计

> 目的：将 Workflow（确定性脚本编排）与动态 Subagent（模型自主委派）的设计差异，映射到 agent_go 的三层架构改造方案。
> 前提：已阅读 [subagent-design-research.md](subagent-design-research.md)（拆分算法 G6/G7/G8 已落地）。
> 状态：设计草案
> 日期：2026-08-10

## 1. 核心概念：两个维度，而非对立

Workflow 和动态 Subagent **不是二选一，而是两个正交的维度**：

```
                   确定性（开发者编码）
                        │
        ┌───────────────┼───────────────┐
        │  Workflow      │  Hybrid       │
        │  Script        │  (agent_go    │
        │  (CI Pipeline) │   target)     │
        │               │               │
 静态 ──┼───────────────┼───────────────┼── 动态
 结构   │               │               │    结构
        │  Config File   │  Dynamic      │
        │  (Rules)       │  Subagent     │
        │               │  (Claude Code  │
        │               │   spawn)       │
        └───────────────┼───────────────┘
                        │
                   模型自主（LLM 判断）
```

- **Workflow**：开发者编码「怎么收敛」（`while dry < 2`、`votes >= 2 refute → kill`）
- **动态 Subagent**：模型判断「什么时候分」（运行时根据任务特征 spawn）
- **Hybrid（agent_go 目标位）**：Workflow 提供确定性骨架 + LLM Plan 提供灵活性 + Execute 提供自主性

关键洞察：**Workflow 对抗不确定性，动态 Subagent 对抗复杂性。** agent_go 需要两层都强。

## 2. agent_go 当前架构映射

```
agent_go 当前已经是三层，但层间有模糊地带：

Layer 1: Pipeline（确定性骨架）✅
  - 拓扑 waves 调度（ThreadPoolExecutor + --parallel N）
  - 上游 failed/blocked → 下游 skip
  - gc.auto 禁用/恢复
  - max_retries 硬上限
  缺失：收敛条件、Barrier vs Pipeline 选择、合约冻结

Layer 2: Plan（LLM 生成）✅ 基础已有，G6/G7/G8 已加固
  - 任务分解 + 依赖关系
  - 验证命令白名单
  - 文件作用域互斥校验
  缺失：认知模式推断（explore/implement/review）、接口合约识别

Layer 3: Execute（动态委派）✅
  - Claude Code 自主探索/实现
  - 验证循环（shell + semantic eval + auto-fix）
  - worker_models 模型路由
  缺失：独立审查 agent（同一进程自修复的盲区）、上下文去重注入
```

## 3. 改进总览

| # | 改进项 | 层 | 优先级 | 解决什么问题 |
|---|--------|-----|--------|-------------|
| A | 三层架构显式化 | 架构 | P0 | 当前层间职责模糊，改进缺乏锚点 |
| B | 收敛条件细化 | L1 | P1 | 当前 max_retries 粗粒度，「打地鼠」循环未被检测 |
| C | 合约先行并行 | L1 | P1 | 并行 waves 修改重叠接口时无保护 |
| D | 上下文基座+增量注入 | L2→L3 | P1 | TASK.md 逐字重复导致成本线性膨胀 + Telephone Game |
| E | 认知模式三级路由 | L3 | P2 | 当前仅 difficulty→model 一维路由 |
| F | Pipeline/Barrier 自适应 | L1 | P2 | 当前全是 Barrier waves，串行依赖时浪费壁钟 |

## 4. 改进 A：三层架构显式化（P0）

### 4.1 当前问题

`pipeline.py` 中的 `_run_pipeline()` 混合了所有三层职责：
- L1 调度逻辑（拓扑 waves、线程池、gc 控制）
- L2 产物传递（git merge upstream tag——属于「合约」概念）
- L3 执行细节（MCP client 启停、Stop Hook 注入——属于 subagent 配置）

层间没有显式接口，改一层容易踩到另一层。

### 4.2 设计

```text
┌─────────────────────────────────────────────┐
│  L1: Orchestration Shell（确定性骨架）        │
│  - 拓扑 waves 调度 + 并行度控制               │
│  - 合约识别与冻结（contract.py）              │
│  - 收敛条件引擎（convergence.py）             │
│  - Barrier vs Pipeline 自动选择              │
│  - 预算监控 + 硬上限                         │
│  输出→L2: {subtask DAG, 合约文件列表, 收敛策略} │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│  L2: Plan Engine（LLM 生成 + 确定性校验）     │
│  - LLM 任务分解 + 依赖推断                    │
│  - G6/G7/G8 校验门（已有）                    │
│  - 认知模式推断（explore/implement/review）    │
│  - 接口合约识别（跨子任务共享接口标记）         │
│  - 上下文基座生成（去重注入）                  │
│  输出→L3: {subtask context bundle, 合约约束}  │
└─────────────────────────────────────────────┘
                    │
┌─────────────────────────────────────────────┐
│  L3: Execution Runtime（动态委派）            │
│  - Claude Code 自主实现                      │
│  - 验证循环（shell + semantic + readonly review）│
│  - 模型路由（difficulty + cognitive_mode）    │
│  - MCP 工具生命周期                          │
│  - artifact 传递（git merge tag）            │
│  输出→L1: {verification result, diff summary} │
└─────────────────────────────────────────────┘
```

### 4.3 层间接口

```python
# L1 → L2
@dataclass
class OrchestrationConfig:
    parallel: int
    convergence: ConvergencePolicy  # 见 §5
    contract_mode: Literal["freeze", "warn", "off"]
    budget: BudgetLimit

# L2 → L3
@dataclass  
class SubtaskContext:
    task_md: str              # 去重后的 TASK.md
    upstream_summary: str     # 上游关键因果链 + 约束
    contract_files: list[str] # 本 step 不可修改的文件
    cognitive_mode: str       # explore / implement / review
    allowed_tools: list[str]  # 工具白名单
    permission_mode: str      # acceptEdits / bypassPermissions / default

# L3 → L1
@dataclass
class SubtaskOutcome:
    verify_ok: bool
    diff_summary: str
    defect_fingerprints: list[str]  # 用于收敛判断
    contract_violations: list[str]   # 合约违规
    metering: MeteringRecord
```

## 5. 改进 B：收敛条件细化（P1）

### 5.1 当前问题

当前 `max_retries=3` 是粗粒度硬上限。5bc1/8259 的实际表现是：

```
retry 1: 语义评估指出「未实现 category list 缓存」
retry 2: 语义评估指出「未实现 trending posts 缓存」（不同缺陷！）
retry 3: 语义评估指出「未实现 category_stats 缓存」（又不同！）
→ 耗尽 max_retries，但实际上每次都在修不同东西，从未真正收敛
```

已有的 `diverge_similarity_threshold`（subagent-design-research §10-#4）检测「连续两次指出不同缺陷」，但只处理了「打地鼠」的检测，没有处理「在哪个维度上不收敛」。

### 5.2 设计：ConvergencePolicy

```python
@dataclass
class ConvergencePolicy:
    """收敛策略——Workflow 式的确定性收敛条件"""
    
    # 基础
    max_retries: int = 3
    
    # 发散检测（已有）
    diverge_threshold: float = 0.3  # 缺陷指纹相似度低于此值 → 发散
    
    # 新增：按缺陷类别追踪
    defect_categories: set[str] = field(default_factory=lambda: {
        "correctness",     # 逻辑正确性
        "completeness",    # 完整性（漏了功能）
        "compliance",      # 规范合规（API 签名、文件结构）
        "quality",         # 代码质量（lint、test 覆盖）
        "integration",     # 集成一致性（上下游接口匹配）
    })
    
    # 新增：收敛判据
    convergence_window: int = 2   
    # 连续 N 次重试中，缺陷指纹相似度均 > diverge_threshold → 真收敛
    # 反之，若每次指向不同 category → 提前终止（能力上限，非重试能解决）
    
    # 新增：进步检测
    progress_decay: float = 0.5
    # 即使缺陷类别不同，若 diff 规模递减（每次修改行数减少），
    # 说明 agent 在逐步逼近——允许额外重试（最多 max_retries * 1.5）
    
    # 回退检测
    revert_threshold: int = 2
    # 连续 N 次重试后 diff 回到之前见过的状态 → 循环振荡，立即终止
```

### 5.3 收敛判断流程

```text
每次 LLM 语义评估失败后：

1. 提取缺陷指纹（已有 _defect_fingerprint）
2. 对缺陷分类（新增 _defect_categorize）：
   - 解析语义评估的 reason 文本
   - 匹配到 correctness/completeness/compliance/quality/integration
3. 与历史重试比较：
   
   if 连续 convergence_window 次指纹相似且同类:
       → "真收敛失败"：agent 在同一点反复挣扎
       → 标记 no_progress，终止
   
   elif 每次指向不同 category:
       → "打地鼠"（已有 diverge 检测）
       → 但如果 diff 规模递减：允许额外 retry（进步中）
       → 如果 diff 规模不递减：立即终止（能力上限）
   
   elif 指纹相同但 category 相同:
       → agent 在修同一个问题但没修好
       → 在修复 prompt 中明确标注「这是第 N 次尝试修复同一缺陷」
   
4. 回退检测：
   if diff_stat_hash 与之前某次重试相同:
       → "循环振荡"，终止
```

### 5.4 实现位置

`agent_go/pipeline.py` 中的 `run_subtask()` 验证循环部分，新增 `ConvergenceTracker` 类：

```python
class ConvergenceTracker:
    """追踪验证重试的收敛状态（无状态，每次重试传入历史）"""
    
    def assess(self, history: list[RetryRecord]) -> ConvergenceVerdict:
        """返回: CONTINUE | DIVERGED | OSCILLATING | CONVERGENCE_FAILED"""
```

不影响当前 `max_retries` 的语义——它仍是硬上限。`ConvergenceTracker` 在达到 `max_retries` 之前就可能提前终止。

## 6. 改进 C：合约先行并行执行（P1）

### 6.1 当前问题

当前 pipeline 的并行执行没有「接口合约」概念。两个并行的 worktree 可以自由修改同一文件（依赖 G7 做了文件互斥校验，但那只是静态分析）。

但更隐蔽的问题是：**跨 worktree 的逻辑接口不一致**。例如：
- sub-2 修改了 `get_post_list()` 的返回格式
- sub-3 的验证命令调用 `get_post_list()` 但基于旧格式断言
- 两个 worktree 各自验证通过（在自己的 worktree 里格式一致），合并后冲突

### 6.2 设计：合约文件标记

在 Plan 阶段，LLM 识别「跨子任务的共享接口」，标记为合约文件：

```json
{
  "steps": [
    {
      "id": "sub-2",
      "files": ["src/blog/views.py"],
      "contracts_provided": ["src/blog/views.py::get_post_list"],
      "contracts_consumed": []
    },
    {
      "id": "sub-3", 
      "files": ["tests/test_api.py"],
      "contracts_provided": [],
      "contracts_consumed": ["src/blog/views.py::get_post_list"]
    }
  ]
}
```

### 6.3 执行策略

```
L1 执行时：

1. 识别所有 contracts_provided 的子任务
2. 将这些子任务调度为「合约 wave」——先于消费者执行
3. 合约 wave 完成后：
   - 提取合约文件的当前状态（freeze）
   - 后续 wave 的 worktree 中，合约文件设为只读
   - git merge 合约 tag 而非整个上游 worktree
4. 验证时：消费者子任务的验证命令也必须通过合约文件的「冻结版本」
```

### 6.4 降级策略

合约识别依赖 LLM Plan 质量。当 LLM 未标记合约时：

```
Mode 1: freeze（严格）
  - Plan 中已标记 contracts_provided/consumed
  - 违反合约 = blocking

Mode 2: warn（宽松）
  - Plan 未标记合约
  - G7 做了文件互斥检查
  - 如果两个子任务修改同名函数/类 → warning（不阻断，但记录）

Mode 3: off
  - 完全依赖 worktree 隔离 + 最终合并
```

## 7. 改进 D：上下文基座 + 增量注入（P1）

### 7.1 当前问题

每个子任务的 TASK.md 包含：
- 项目概述（重复 N 次）
- 共享资源清单（重复 N 次）
- 角色执行要求（重复 N 次）
- 本步骤的具体指令（唯一不重复的部分）

对于 5 个子任务的任务，固定部分重复 5 次，token 线性膨胀。

此外，subtask 之间没有上游摘要传递（Telephone Game 效应）。

### 7.2 设计

```text
上下文基座（TASK_BASE.md）—— 所有子任务共享，只注入一次
┌────────────────────────────────────────────┐
│ 1. 项目概述 + 技术栈                        │
│ 2. 关键目录 + 配置文件                       │
│ 3. 角色执行要求 + 通用约束                    │
│ 4. 全局合约文件列表（如有）                   │
└────────────────────────────────────────────┘

增量上下文（TASK_STEP_{N}.md）—— 每个子任务独有
┌────────────────────────────────────────────┐
│ 1. 本步骤指令                               │
│ 2. 上游摘要（自动生成）                      │
│    - 上游 sub-X 修改了哪些文件/函数/类        │
│    - 上游 sub-X 的关键约束（API 签名、配置项） │
│    - 上游 sub-X 的已知风险                    │
│ 3. 本步骤文件作用域（非只读）                 │
│ 4. 合约文件（本步骤不可修改）                 │
└────────────────────────────────────────────┘
```

### 7.3 执行时注入

```python
def build_subtask_context(subtask, upstream_results, base_context):
    """构建去重的子任务上下文"""
    
    # 上游摘要（结构化，不是全文）
    upstream_summary = []
    for dep_id in subtask["depends_on"]:
        r = upstream_results[dep_id]
        upstream_summary.append({
            "id": dep_id,
            "description": r["description"][:200],
            "files_changed": r["files_changed"],
            "key_constraints": r.get("key_constraints", []),
            "known_risks": r.get("known_risks", []),
            "contract_functions": r.get("contracts_provided", []),
        })
    
    # 只注入真正需要的部分
    return {
        "base": base_context,           # TASK_BASE.md（共享）
        "step": subtask["description"], # 本步指令
        "upstream": upstream_summary,   # 结构化上游摘要
        "files": subtask["files"],      # 文件作用域
        "readonly": subtask.get("contracts_consumed", []),  # 只读合约
        "verification": subtask.get("verification", ""),
    }
```

### 7.4 上游摘要的自动生成

subtask 完成后，从执行结果中提取关键信息：

```python
def extract_upstream_summary(subtask_result):
    """从子任务执行结果中提取上游摘要"""
    return {
        "files_changed": parse_diff_files(subtask_result["diff_summary"]),
        "key_constraints": extract_constraints(subtask_result["summary"]),
        "known_risks": subtask_result.get("risks_materialized", []),
        "contract_functions": subtask_result.get("contracts_provided", []),
    }
```

`extract_constraints` 用轻量规则匹配（正则 + AST 模式）而非 LLM：
- API 签名变更：`def (\w+)\(` → 函数签名变化
- 配置项新增：`settings\.\w+\s*=` → Django 配置变动
- 模型字段：`models\.\w+Field` → 数据库 schema 变化

这些规则匹配到的信息作为上游摘要注入下游 TASK.md，成本极低（不消耗 LLM token）。

## 8. 改进 E：认知模式三级路由（P2）

### 8.1 当前状态

`worker_models_by_cognitive` 已落地（subagent-design-research §10-#1），按 `task_type` / `difficulty` 路由。

### 8.2 增强：Plan 阶段自动推断 cognitive_mode

当前 `cognitive_mode` 需要 subtask 显式携带。增强为：Plan 生成后，从子任务特征自动推断：

```python
COGNITIVE_MODE_PATTERNS = {
    "explore": {
        "agent_types": ["architect"],
        "description_patterns": [r"分析|调查|探索|检查|审查.*代码库|阅读.*代码"],
        "files_pattern": None,  # 不修改文件
        "verification_patterns": [r"^echo|^ls|^cat|^grep"],  # 只读验证
    },
    "implement": {
        "agent_types": ["developer"],
        "description_patterns": [r"实现|创建|添加|修改|编写|重构"],
        "files_pattern": r".+",  # 修改文件
        "verification_patterns": [r"pytest|python -m|npm test"],
    },
    "review": {
        "agent_types": ["reviewer", "tester"],
        "description_patterns": [r"审查|验证.*合规|检查.*规范|编写.*测试"],
        "files_pattern": r"tests?/.*",  # 只改测试文件
        "verification_patterns": [r"pytest|python -m|npm test|lint"],
    },
}
```

推断逻辑在 `plan_to_subtasks()` 中执行，不依赖 LLM 输出。

### 8.3 模型路由矩阵

| cognitive_mode | difficulty | 模型 | 原因 |
|---------------|-----------|------|------|
| explore | easy | haiku | 快速扫描，只需结构化摘要 |
| explore | medium/hard | sonnet | 复杂代码库分析需要推理 |
| implement | easy | sonnet | 性价比平衡 |
| implement | medium | sonnet | 标准实现 |
| implement | hard | opus | 复杂多文件实现 |
| review | easy | haiku | 简单审查（lint/格式） |
| review | medium/hard | opus | 深度安全/架构审查 |

## 9. 改进 F：Pipeline/Barrier 自适应选择（P2）

### 9.1 当前状态

agent_go 使用拓扑 waves（相当于 Barrier 模式）——每个 wave 等所有任务完成后才进入下一 wave。

### 9.2 问题

对于线性依赖拓扑（A → B → C → D），Barrier 等价于 Pipeline（没有并行度差异）。但对于 Y 形拓扑：

```
   A
  ↙ ↘
 B   C
  ↘ ↙
   D
```

Barrier 模式：B 和 C 必须都完成后 D 才启动。Pipeline 模式：B 完成后 D 就可以开始（如果 D 只依赖 B）。

### 9.3 自适应选择

```python
def select_scheduling_strategy(dag: DAG, wave: list[Subtask]) -> SchedulingMode:
    """自适应选择 Pipeline vs Barrier"""
    
    # 如果当前 wave 只有一个子任务 → 无所谓
    if len(wave) <= 1:
        return SchedulingMode.DIRECT
    
    # 如果下游依赖所有上游 → Barrier
    downstreams = get_downstreams(dag, wave)
    if all(len(dag.in_edges(d)) == len(wave) for d in downstreams):
        return SchedulingMode.BARRIER
    
    # 如果下游只依赖部分上游 → Pipeline
    # （B 完成 → D 可开始，不等 C）
    return SchedulingMode.PIPELINE
```

Pipeline 模式下，下游子任务在上游**任何一个**依赖完成时就启动（而非等全部），但需要额外处理：
- 上游 tag merge 变为增量 merge（每次只 merge 新完成的依赖）
- 合约文件在首次 merge 时冻结，后续 merge 不可覆盖

### 9.4 适用范围

对于 agent_go 的典型工作负载（Plan 生成的 DAG 大多是线性的），Pipeline 模式收益有限。真正的收益出现在：
- 大量独立探索型子任务（多个 explore subagent 并行扫描不同目录）
- 有局部依赖的复杂 DAG（如 M1 的多模块任务）

当前默认 Barrier 是正确的。Pipeline 作为可按需启用的优化。

## 10. 实施优先级与分期

### M2（当前迭代）—— 架构基础
- **A**: 三层接口定义（`OrchestrationConfig` / `SubtaskContext` / `SubtaskOutcome`）
- **B-基础**: `ConvergenceTracker` 类 + 发散检测增强

### M3 —— 执行质量
- **B-完整**: 缺陷分类 + 进步检测 + 回退检测
- **D**: 上下文基座 + 增量注入 + 上游摘要自动生成
- **C-tests**: 合约文件标记的数据结构 + 测试

### M4 —— 优化
- **E**: 认知模式自动推断
- **F**: Pipeline/Barrier 自适应
- **C-完整**: 合约冻结 + 只读执行

## 11. 不做的方向（有意识的取舍）

| 方向 | 原因 |
|------|------|
| 子 agent 递归嵌套（agent_go → Claude Code → subagent → sub-subagent） | 违反一层上限原则。Claude Code subagent 已不可再 spawn。agent_go 应 focus 在自己的编排层做好 |
| 动态 Workflow 生成（LLM 写 workflow 脚本） | Workflow 脚本的正确性是安全关键（预算上限、收敛条件）。LLM 生成的脚本不可靠 |
| Agent Teams（多向通信的 subagent 群） | 复杂度收益比不划算。当前 worktree 隔离 + tag merge 已经解决了协调问题 |
| 完全放弃 Plan（纯 Workflow 驱动） | agent_go 的核心价值是 LLM 理解任务并分解。纯确定性 Workflow 退化为 CI pipeline |

## 12. 参考资料

- [subagent-design-research.md](subagent-design-research.md) — 拆分算法 G6/G7/G8 + 改进方向评估
- [verification-design.md](verification-design.md) — 验证层次与重试状态
- [functional-architecture.md](functional-architecture.md) — 阶段职责与角色边界
- [architecture.md](../architecture.md) — 核心数据流与状态流转
- Claude Code Workflow 工具文档（pipeline/parallel/barrier 语义）
- OpenCode Agent Teams 设计提案（Issue #12711）
- Anthropic 官方：How and when to use subagents in Claude Code
- Addy Osmani: Agentic Autonomy Levels
