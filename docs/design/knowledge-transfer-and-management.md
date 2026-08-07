# 知识与需求传递：从业务知识到 agent_go 任务执行

> **版本**：v1.0
>
> **目的**：定义业务知识、领域规则、编程规范等「非代码知识」如何组织、维护、传递到 agent_go 的任务执行中。回答「agent_go 怎么知道这段代码应该这样写而不是那样写」。
>
> **日期**：2026-08-01

---

## 一、知识分类

agent_go 需要的知识分为四层。每层的创建者、更新频率、传递机制不同。

```
Layer 1: 仓库知识          代码库自带的，agent_go 自动收集
  ├─ 项目文件列表           git ls-files → 自动注入
  ├─ 目录结构               自动分析
  ├─ 运行时环境             Python 版本、依赖 → 自动检测
  ├─ CLAUDE.md             项目级 AI 指令 → 自动读取
  └─ .gitignore / CI 配置   自动识别

Layer 2: 领域知识          人（PM/工程师）维护的，通过 Skill 系统传递
  ├─ 业务规则              如「金额字段必须用 Decimal，禁止 float」
  ├─ 合规要求              如「用户数据加密存储，日志脱敏」
  ├─ 领域术语              如「Order 的状态机：pending→confirmed→shipped→delivered」
  └─ 外部系统约定          如「支付回调幂等性要求、重试间隔」

Layer 3: 项目规范          人（工程师）维护的，通过 Skill + CLAUDE.md 传递
  ├─ 代码风格              如「所有 public 函数必须有 type hints」
  ├─ 架构约束              如「Controller 不直接访问 DB，必须走 Service 层」
  ├─ 测试约定              如「pytest + fixture，所有外部调用必须 mock」
  └─ Git 规范              如「Conventional Commits，commit message 格式」

Layer 4: 任务知识          人（工程师）每次任务时编写的 Task Spec
  ├─ 本次目标               改什么、不改什么
  ├─ 本次约束               本次特有的约束（如「迁移可回滚」）
  ├─ 验收标准               怎么算做完
  └─ 参考资料               类似实现、设计文档链接
```

---

## 二、传递机制全景

```
┌─────────────────────────────────────────────────────────────┐
│                        知识来源                               │
│                                                             │
│  PM 写             工程师写            代码库自动             │
│  业务规则           架构约束            CLAUDE.md             │
│  合规要求           代码规范            文件列表               │
│  领域术语           测试约定            Git 信息               │
│                    外部接口约定          运行时环境              │
│                                                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    知识载体（文件系统）                          │
│                                                             │
│  ~/.agent_go/skills/       全局 Skill（跨项目复用）            │
│  <project>/.agent_go/skills/  项目 Skill（项目专属）           │
│  <project>/CLAUDE.md          项目指令（Claude Code 自动读）    │
│  <project>/docs/              设计文档（关联引用）              │
│  <project>/docs/tasks/        Task Spec（每次任务创建）         │
│                                                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    传递通道（agent_go 运行时）                   │
│                                                             │
│  通道 A: 自动注入（无需人指定）                                │
│    ├─ 文件列表 + Git 信息 → Plan prompt user content          │
│    ├─ CLAUDE.md → Claude Code 子系统自动读取                  │
│    └─ Role-Skill 规则引擎 → 自动匹配 Skill                     │
│                                                             │
│  通道 B: 显式指定（人在 CLI / Task Spec 中指定）               │
│    ├─ --spec → Task Spec 7 章节 → Plan prompt               │
│    ├─ --skill → 加载指定 Skill → Plan prompt                 │
│    └─ --docs → 参考文档 → Plan prompt                        │
│                                                             │
│  通道 C: Skill 全文注入（Worker 执行时）                        │
│    └─ render_skill_for_execution → TASK.md                    │
│                                                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                    agent_go 执行                              │
│                                                             │
│  Plan 阶段: Skill 摘要（500 字符）+ Task Spec + 自动上下文     │
│  Worker 阶段: Skill 全文 + TASK.md 窄化上下文                  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、Skill 作为知识管理单元

### 3.1 为什么用 Skill 而不是其他方式

agent_go 已有完整的 Skill 系统，支持：

| 能力 | 已实现 | 说明 |
|------|--------|------|
| 文件格式 | ✅ | YAML frontmatter + Markdown body |
| 加载路径 | ✅ | 全局（`~/.agent_go/skills/`）+ 项目（`.agent_go/skills/`） |
| Plan 注入 | ✅ | `render_skill_for_plan()` → 前 500 字符摘要入 Plan prompt |
| Worker 注入 | ✅ | `render_skill_for_execution()` → 全文入 TASK.md |
| 自动发现 | ✅ | `discover_skills()` 关键词匹配 task 描述 |
| 显式加载 | ✅ | `--skill` CLI 参数 |
| 规则匹配 | ✅ | Role-Skill 规则引擎（按关键词/文件模式/agent_type 匹配） |

**不需要新建机制**。需要的只是在现有 Skill 系统上约定知识分类和组织方式。

### 3.2 Skill 类型约定

在 Skill 的 YAML frontmatter 中用 `type` 字段标注知识类型，帮助 Rule 引擎精确匹配。

```yaml
---
name: banking-decimal-rules
type: domain           # domain | convention | tool | workflow
description: 金融领域精度规则 — 金额字段必须用 Decimal，禁止 float/double
applies_to:            # 自动匹配条件
  keywords: [金额, 支付, 转账, 结算, 余额, 费率, financial, payment, transaction]
  files: ["**/models/*payment*", "**/models/*transaction*", "**/finance/**"]
---
# 金融精度规则

## 核心原则
所有涉及金额的字段必须使用 `Decimal` 类型，禁止使用 `float` 或 `double`。

## 数据库
- MySQL: `DECIMAL(18,2)` 或 `DECIMAL(18,6)`（根据精度需求）
- PostgreSQL: `NUMERIC(18,2)`
- 迁移文件中的金额字段必须显式声明精度

## Python
```python
from decimal import Decimal

# ✅ 正确
amount = Decimal("100.00")
price = models.DecimalField(max_digits=18, decimal_places=2)

# ❌ 错误
amount = 100.00  # float 精度丢失
price = models.FloatField()  # 禁止
```

## 常见陷阱
- `Decimal(1.1)` 仍有浮点误差，必须用 `Decimal("1.1")`
- 除法运算必须指定精度：`amount / Decimal("3")` 可能无限循环，用 `quantize()`
```

### 3.3 四种 Skill 类型

| 类型 | 内容 | 创建者 | 示例 |
|------|------|--------|------|
| **domain** | 业务规则、领域知识、合规要求 | PM + 工程师 | `banking-decimal-rules`, `order-state-machine`, `gdpr-data-handling` |
| **convention** | 代码规范、架构约束、测试约定 | 工程师 | `python-type-hints`, `controller-service-dao`, `pytest-mock-convention` |
| **tool** | 工具使用指南、外部系统接口约定 | 工程师 | `redis-cache-patterns`, `kafka-producer-conventions`, `openapi-client-gen` |
| **workflow** | 流程模板、最佳实践 | 工程师 | `database-migration-checklist`, `api-versioning-strategy` |

---

## 四、知识传递的四个关键时刻

### 时刻 1：Plan 阶段（生成执行计划）

**注入的知识**：自动上下文 + Skill 摘要 + Task Spec

```
Plan prompt 组成：

[system prompt]
  ├─ Skill 清单表（名称 + 描述，最多 10 个）
  ├─ Role-Skill 规则摘要（匹配条件 + 推荐 Skill）
  ├─ 匹配的 Skill 摘要（前 500 字符，受 10000 字符预算限制）
  └─ 项目运行时环境

[user content]
  ├─ 任务描述 ← Task Spec §1 目标
  ├─ 背景动机 ← Task Spec §2 动机
  ├─ 项目文件列表（自动）
  ├─ Git 信息（自动）
  ├─ 范围约束 ← Task Spec §3-4
  └─ 参考资料 ← Task Spec §6
```

**关键约束**：system prompt 有 10000 字符硬上限。Skill 全文不能全部注入 Plan 阶段——只注入摘要。如果 Skill 很多，按规则匹配优先级截断。

### 时刻 2：Worker 执行阶段（子任务写代码）

**注入的知识**：Task Spec 派生指令 + Skill 全文

```
TASK.md 组成：

  # 子任务: <title>
  ## 背景
  ← Task Spec §1-2 相关段落

  ## 执行指令
  ← Plan step 的 agent_prompt

  ## 验证要求
  ← Plan step 的 verification

  ## Skill 知识注入
  ← render_skill_for_execution() 全文
  ← 仅注入与此子任务匹配的 Skill（files + keywords 匹配）

  ## 约束
  ← Task Spec §3-4 中与此子任务相关的约束
```

**关键约束**：Worker 的 TASK.md 只包含与此子任务相关的知识，不注入全局知识。窄化上下文是 agent_go 的核心设计原则。

### 时刻 3：验证阶段（判断是否通过）

**注入的知识**：验收标准 + 业务规则

```
验证循环：

  shell 验证（确定性）
    ← Plan step verification 命令
    ← 从 Task Spec §5 验收标准派生的验证命令

  semantic 评估（LLM 判断）
    ← Task Spec §5 验收标准
    ← 匹配的 domain Skill（业务规则检查）
```

**示例**：如果一个任务的 Skill 包含「金额字段必须用 Decimal」，semantic evaluator 可以额外检查：diff 中新增的金额相关字段是否使用了 Decimal 而非 float。

### 时刻 4：审查阶段（人做最终判断）

**注入的知识**：不需要注入——人用自己的知识做判断。

agent_go 的 `review --task` 输出聚合 diff，人在自己的知识背景下判断「这段代码是否符合业务预期」。这个阶段的知识传递不是 agent_go 的职责。

---

## 五、知识生命周期

```
创建 ──→ 使用 ──→ 更新 ──→ 退役
  │        │        │        │
  │        │        │        └── 业务规则变更、技术栈升级 → 标记 deprecated
  │        │        │
  │        │        └── 新规则发现、旧规则修正 → 更新 Skill 文件
  │        │
  │        └── 通过 Rule 引擎匹配 → Plan/Worker 注入 → 影响代码生成
  │
  └── PM/工程师在 Claude Code 中编写 Skill → 存入 .agent_go/skills/
```

### 5.1 谁来创建

| Skill 类型 | 创建者 | 触发时机 |
|-----------|--------|---------|
| domain | PM + 工程师 | 新项目启动、新业务线接入、合规要求变更、bench 复盘发现重复性错误 |
| convention | 工程师 | 项目初始化、代码审查中发现重复性风格问题 |
| tool | 工程师 | 引入新的外部依赖、发现 API 使用模式 |
| workflow | 工程师 | 重复性操作超过 3 次 |

### 5.2 何时更新

| 触发条件 | 动作 |
|---------|------|
| bench 数据中同类型任务反复失败 | 检查是否缺少对应的 domain Skill → 创建 |
| Rule 引擎匹配了 Skill 但 Worker 仍然违反规则 | 检查 Skill 内容是否足够具体 → 补充代码示例 |
| 技术栈升级（如 Django 4→5） | 更新 convention/workflow Skill 中的 API 示例 |
| 业务规则变更（如合规要求更新） | 更新 domain Skill → 标注变更日期 |

### 5.3 何时退役

- 技术栈迁移（不再使用该框架）
- 业务线关闭
- 规则被更通用的规则替代
- 验证数据显示该 Skill 的匹配准确率 < 30%（误匹配太多）

退役方式：在 Skill frontmatter 中标注 `status: deprecated` + 迁移指引。

---

## 六、与 Task Spec 的关系

**Task Spec 和 Skill 是不同的东西，各自解决不同的问题：**

| 维度 | Skill | Task Spec |
|------|-------|-----------|
| **生命周期** | 长期，跨任务复用 | 一次性，执行后归档 |
| **维护者** | PM + 工程师共同维护 | 工程师每次任务编写 |
| **内容** | 「所有同类任务都应该遵守的规则」 | 「这一次任务要做什么」 |
| **粒度** | 领域/规范级别 | 单次任务级别 |
| **更新频率** | 低频（业务变更时） | 高频（每次任务） |
| **传递方式** | Rule 引擎自动匹配 + `--skill` 显式指定 | `--spec` 指定 |
| **存储位置** | `~/.agent_go/skills/` 或 `<project>/.agent_go/skills/` | `<project>/docs/tasks/` |

**互补关系**：

```
Task Spec: 这次要加支付功能，改 Payment 模型，加 create_payment API

  + Skill: banking-decimal-rules
    → 金额字段必须用 Decimal(18,2)

  + Skill: api-versioning-strategy
    → 新 API 路径用 /api/v2/，旧版本保持兼容

  + Skill: payment-idempotency
    → 支付接口必须幂等，用 idempotency_key 去重
```

Task Spec 说「做什么」，Skill 说「怎么做才对」。

---

## 七、实施建议

### 7.1 当前立即可用

| 能力 | 使用方式 |
|------|---------|
| 创建 domain Skill | 在 `<project>/.agent_go/skills/` 下创建 Markdown 文件，写好 YAML frontmatter 中的 keywords |
| Rule 引擎自动匹配 | 配置 `~/.agent_go/role_skill_map.json`，添加 keyword→skill 规则 |
| Task Spec 引用 Skill | 在 Spec §4「约束」中写「遵循 banking-decimal-rules」 |
| 显式加载 Skill | `agent_go run --skill banking-decimal-rules` |

### 7.2 短期改进（S11-P1 之后）

| 改进 | 说明 |
|------|------|
| Skill `type` 字段标准化 | 在 Skill frontmatter 中增加 `type: domain|convention|tool|workflow`，Rule 引擎按类型匹配 |
| Task Spec 自动关联 Skill | `agent_go scope` 在生成 Spec 草稿时，根据 Spec 内容自动推荐相关 Skill |
| Skill 有效性度量 | 在 metering 中记录匹配的 Skill，bench 数据关联「Skill 匹配的任务」的 pass_rate |

### 7.3 远期（KnowledgeStore 落地后）

| 改进 | 说明 |
|------|------|
| Skill 自动蒸馏 | 从成功执行轨迹中自动提取重复模式，生成 Skill 草稿 → 人审核 → 入库 |
| Skill 冲突检测 | 两个 Skill 的约束互相矛盾时（如一个要求 Decimal、另一个说可以用 float），L2 准入审查告警 |
| 跨项目 Skill 复用 | 全局 Skill 库（`~/.agent_go/skills/`）的项目间共享和同步 |

---

## 八、示例：一个完整的知识传递链路

以一个金融项目的「新增转账功能」为例：

```
项目已有知识：
  ~/.agent_go/skills/banking-decimal-rules/SKILL.md    ← domain: 金额 Decimal 规则
  <project>/.agent_go/skills/api-conventions/SKILL.md   ← convention: API 响应格式约定
  <project>/.agent_go/skills/db-migration-guide/SKILL.md ← workflow: 迁移必须可回滚
  <project>/CLAUDE.md                                    ← 项目级指令

工程师创建 Task Spec: docs/tasks/task-transfer-feature.md
  §1 目标: 实现用户间转账功能
  §3 范围: src/models/transfer.py, src/api/transfer.py, tests/
  §4 约束:
    - 遵循 banking-decimal-rules（金额精度）
    - 遵循 api-conventions（响应格式）
    - 迁移文件遵循 db-migration-guide（可回滚）
  §5 验收标准: pytest tests/test_transfer.py -v, 余额校验不通过时返回 402

agent_go run --spec docs/tasks/task-transfer-feature.md

  Plan 阶段:
    ├─ system prompt 注入 Skill 清单（banking-decimal-rules, api-conventions, db-migration-guide）
    ├─ Role-Skill 规则匹配：task 含 "转账" → 自动关联 banking-decimal-rules
    ├─ Plan step 1: 新增 Transfer 模型 + 迁移
    │     skills: [banking-decimal-rules, db-migration-guide]
    │     difficulty: medium（涉及 Decimal 精度 + 迁移可回滚）
    ├─ Plan step 2: 新增转账 API
    │     skills: [banking-decimal-rules, api-conventions]
    │     difficulty: medium
    └─ Plan step 3: 编写测试

  Worker 执行 step 1:
    ├─ TASK.md 注入 banking-decimal-rules 全文 → 金额字段用 Decimal(18,2)
    ├─ TASK.md 注入 db-migration-guide 全文 → 写 upgrade() 和 downgrade()
    └─ Claude Code 生成代码 → 遵循约束

  Worker 执行 step 2:
    ├─ TASK.md 注入 banking-decimal-rules 全文
    ├─ TASK.md 注入 api-conventions 全文 → 响应格式遵循约定
    └─ Claude Code 生成 API 代码

  验证:
    ├─ pytest → 全绿
    └─ semantic evaluator: 检查 diff 中金额字段是否用 Decimal（基于 banking-decimal-rules）

  → 完成，PR 生成
```

**这个链路中，没有人在执行时手动告诉 agent_go「金额要用 Decimal」——这个知识已经通过 Skill 系统传递了。**

---

## 九、agent_go 内部的 Agent 信息管理

### 9.1 信息流架构

```
                        ┌─────────────────────────────────┐
                        │        agent_go 编排器            │
                        │                                 │
  Task Spec ────────────→  Plan 阶段                       │
  --spec                  ├─ Skill 匹配 + 摘要注入          │
  --skill                 ├─ 自动上下文收集                 │
  --docs                  └─ Plan JSON 生成                │
  --context                       │                       │
                           Plan JSON (steps + dependencies)│
                                  │                       │
                                  ▼                       │
                        ┌─────────────────────────────────┐
                        │   Per-Subtask Worker            │
                        │   (git worktree 隔离)            │
                        │                                 │
  Plan JSON ────────────→  TASK.md 生成                    │
  Skill 全文              ├─ 目标 + 指令                   │
  Task Spec 约束          ├─ Skill 全文注入                │
  Git 上下文              ├─ 约束 + 验收标准               │
                          └─ 上游产物（git merge tag）     │
                                  │                       │
                                  ▼                       │
                        ┌─────────────────────────────────┐
                        │   Claude Code (headless)         │
                        │                                 │
  TASK.md ──────────────→  读写 worktree 文件              │
                          ← stdout/stderr/diff            │
                                  │                       │
                                  ▼                       │
                        ┌─────────────────────────────────┐
                        │   验证循环                        │
                        │                                 │
                          ← shell exit code               │
                          ← LLM semantic evaluation        │
                                  │                       │
                          ┌───────┴───────┐               │
                          │ pass          │ fail          │
                          │ → next step   │ → RepairAgent │
                          │               │   注入失败上下文│
                          │               │   max 3 次    │
                          └───────────────┘               │
                        └─────────────────────────────────┘
```

### 9.2 信息的四个生存周期

| 周期 | 信息 | 存储位置 | 生命周期 |
|------|------|---------|---------|
| **任务级** | Task Spec、Plan JSON、最终结果 | `~/.agent_go/{task_id}/meta.json` | 任务完成 → 持久保留 |
| **子任务级** | TASK.md、Skill 全文、上游产物、验证命令 | worktree 目录 + git branch | 子任务完成 → worktree 清理（失败保留） |
| **请求级** | API 调用的 token/成本/延迟 | `~/.agent_go/{task_id}/metering.jsonl` | 每次 LLM 调用追加一行 |
| **会话级** | Skill 匹配决策、Rule 触发记录 | logger DEBUG 输出 | 进程退出 → 丢失（远期进 KnowledgeStore） |

### 9.3 信息如何在子任务间传递

agent_go 用 **git tag + merge** 做子任务间产物传递，不经过 agent_go 的内存。

```
子任务 A 完成
  → git commit + tag: {task_id}/sub-1
  → worktree 保留文件变更

子任务 B 启动（依赖 A）
  → git merge tag {task_id}/sub-1
  → B 的 worktree 自动包含 A 的所有变更
  → TASK.md 中注入：「上游 sub-1 已完成，变更已合并到当前 worktree」

信息传递不经过 prompt，经过文件系统。
```

**设计意图**：子任务 B 的 Claude Code 看到的是「已经包含了 A 的变更的完整代码库」，它不需要知道 A 做了什么，只需要基于当前代码库继续工作。这避免了在 prompt 中传递大量 diff 信息。

### 9.4 Worker 收到的窄化上下文（TASK.md 结构）

```markdown
# 子任务: 新增 Transfer 模型

## 背景
项目 task-mgr 需要支持用户间转账功能。
这是 3 步计划中的第 1 步：定义数据模型。

## 执行指令
在 src/models/transfer.py 中创建 Transfer 模型，包含以下字段：
- id: UUID 主键
- from_user: ForeignKey → User
- to_user: ForeignKey → User
- amount: Decimal（精确金额）
- status: CharField（pending/completed/failed）
- created_at: DateTime
- completed_at: DateTime（可空）

## 验证要求
python -m pytest tests/test_transfer_model.py -v

## Skill 知识注入

### banking-decimal-rules
所有金额字段必须使用 Decimal。详见：[Skill 全文]

### db-migration-guide
迁移文件必须包含 upgrade() 和 downgrade()。详见：[Skill 全文]

## 约束
- Transfer 模型不包含业务逻辑（业务逻辑在 Service 层）
- 迁移必须可回滚
- 不修改 User 模型

## 项目结构
当前仓库关键目录：
  src/models/     ← 在这里新增 transfer.py
  src/services/   ← 下一步在这里新增 TransferService
  tests/          ← 在这里新增 test_transfer_model.py
```

**这个 TASK.md 被喂给 Claude Code `-p` 作为 prompt。** Worker 只看到这一份窄化上下文，看不到全局 plan、看不到其他子任务的细节、看不到项目的完整架构文档。这就是「上下文隔离」的具体实现。

### 9.5 验证失败时的信息回流

```
Worker 执行 → 验证失败
  →
  RepairAgent prompt:
    "上一次执行失败。以下是失败信息：
     
     ## 验证命令
     pytest tests/test_transfer_model.py -v
     
     ## 退出码
     1
     
     ## stderr（尾部）
     FAILED tests/test_transfer_model.py::test_amount_precision - AssertionError: amount is float, expected Decimal
     
     ## git diff（变更摘要）
     src/models/transfer.py | 25 ++++++++++++++++++++++++
     
     请分析失败原因并修复。"
```

**RepairAgent 收到的信息**：验证命令 + 退出码 + stderr + diff 摘要。不包含原始 TASK.md（它已经在 worktree 里了，Claude Code 可以自己读）。

---

## 十、项目管理工具中的领域知识管理与供给

### 10.1 知识管理全景

```
┌──────────────────────────────────────────────────────────────┐
│                 项目管理工具（知识管理层）                       │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐ │
│  │ Skill 库    │  │ 设计文档库    │  │ 知识图谱              │ │
│  │             │  │             │  │                      │ │
│  │ domain/     │  │ docs/design/│  │ Skill → 代码模块      │ │
│  │ convention/ │  │ docs/       │  │ Skill → Task Spec     │ │
│  │ tool/       │  │             │  │ Skill → bench 数据    │ │
│  │ workflow/   │  │             │  │ （哪个 Skill 提升了   │ │
│  │             │  │             │  │   哪个 Skill 被忽略）  │ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬───────────┘ │
│         │                │                     │             │
│         │    ┌───────────┴──────────┐          │             │
│         │    │                      │          │             │
│         ▼    ▼                      ▼          ▼             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Task Spec 工作台（Scoping）                │   │
│  │                                                      │   │
│  │  工程师写 Spec ──→ AI 推荐相关 Skill ──→ 人确认关联     │   │
│  │                 ──→ AI 推荐相关设计文档 ──→ 人确认引用   │   │
│  │                 ──→ AI 推荐历史相似 Spec ──→ 人确认复用   │   │
│  └──────────────────────┬───────────────────────────────┘   │
│                         │                                    │
└─────────────────────────┼────────────────────────────────────┘
                          │
                    Task Spec + 关联 Skill
                          │
                          ▼
                   agent_go 执行
```

### 10.2 项目管理工具的知识供给流程

**场景：工程师在项目管理工具中为「新增转账功能」写 Task Spec。**

```
工程师打开 Task Spec 编辑器
  │
  ├─ Step 1: 填写目标
  │   "实现用户间转账功能，包括模型、API、测试"
  │
  ├─ Step 2: AI 自动推荐（无需人操作）
  │   ├─ 扫描 Spec 内容关键词："转账"、"用户间"、"API"
  │   ├─ 匹配 Skill 库：
  │   │   banking-decimal-rules (domain)         — 匹配关键词 "转账"
  │   │   api-conventions (convention)           — 匹配关键词 "API"
  │   │   transfer-state-machine (domain)        — 匹配关键词 "转账"
  │   │   db-migration-guide (workflow)          — 匹配文件模式 "models/*"
  │   ├─ 匹配设计文档库：
  │   │   docs/design/payment-architecture.md    — 匹配 "转账" "支付"
  │   └─ 匹配历史 Spec：
  │       docs/tasks/task-payment-gateway.md     — 相似度 78%
  │       docs/tasks/task-order-model.md         — 相似度 65%
  │
  ├─ Step 3: 推荐面板（侧边栏展示）
  │   ┌─────────────────────────────────────────┐
  │   │ 📚 推荐的知识                               │
  │   │                                           │
  │   │ 🔧 领域规则 (2)                             │
  │   │ ☑ banking-decimal-rules                   │
  │   │ ☑ transfer-state-machine                  │
  │   │                                           │
  │   │ 📐 规范约定 (1)                             │
  │   │ ☑ api-conventions                         │
  │   │                                           │
  │   │ 🔀 工作流 (1)                               │
  │   │ ☑ db-migration-guide                      │
  │   │                                           │
  │   │ 📖 设计文档 (1)                             │
  │   │ ☐ payment-architecture.md                 │
  │   │                                           │
  │   │ 📋 历史相似 Spec (2)                        │
  │   │ ☐ task-payment-gateway.md (78% 相似)      │
  │   │ ☐ task-order-model.md (65% 相似)          │
  │   │                                           │
  │   │ [确认关联]  [手动添加]                       │
  │   └─────────────────────────────────────────┘
  │
  ├─ Step 4: 人确认
  │   工程师勾选相关 Skill → 写入 Spec §4「约束」和 §6「参考资料」
  │
  └─ Step 5: 准入审查（L2 软警告）
      检查：是否有关键约束遗漏？
      → 「检测到 Spec 涉及金额字段，但未关联 banking-decimal-rules。建议添加。」
```

### 10.3 知识供给的推荐算法

**不依赖复杂 ML，用规则 + 关键词权重即可：**

```python
# 伪代码：Skill 推荐引擎
def recommend_skills(spec_text: str, repo_path: Path) -> list[SkillMatch]:
    matches = []
    
    for skill in load_all_skills(repo_path):
        score = 0
        
        # 1. 关键词匹配（权重 50%）
        for keyword in skill.keywords:
            if keyword in spec_text.lower():
                score += 1
        
        # 2. 文件模式匹配（权重 30%）
        for pattern in skill.file_patterns:
            if glob_matches(spec_files, pattern):
                score += 1
        
        # 3. 历史关联（权重 20%）
        # 过去有 N 个 Spec 在关联此 Skill 后 pass_rate 提升了
        historical_boost = get_skill_effectiveness(skill.name)
        score += historical_boost * 0.2
        
        if score >= THRESHOLD:
            matches.append(SkillMatch(skill=skill, score=score, 
                           reason=generate_reason(skill, spec_text)))
    
    return sorted(matches, key=lambda m: m.score, reverse=True)
```

### 10.4 知识反馈闭环

```
Task Spec 关联 Skill → agent_go 执行 → bench 数据

  bench 数据分析:
    ├─ 关联了 banking-decimal-rules 的任务
    │   pass_rate: 92%（vs 未关联的同类任务: 78%）
    │   → Skill 有效性: ✅ 正面
    │
    ├─ 关联了 db-migration-guide 的任务
    │   pass_rate: 91%（vs 未关联: 85%）
    │   → Skill 有效性: ✅ 正面
    │
    └─ 关联了 some-outdated-skill 的任务
        pass_rate: 45%（vs 未关联: 67%）
        → Skill 有效性: 🔴 负面，可能已过时

  → 反馈到工具侧:
    ├─ 高有效性 Skill → 推荐权重提升
    ├─ 低有效性 Skill → 标记「可能已过时」，推荐权重降低
    └─ 缺失 Skill 的失败任务 → 聚类分析 → 自动建议创建新 Skill
```

### 10.5 工具侧的数据模型

```python
# 伪代码：知识管理的数据模型

class Skill:
    """领域知识单元"""
    name: str
    type: Literal["domain", "convention", "tool", "workflow"]
    description: str
    keywords: list[str]          # 自动匹配关键词
    file_patterns: list[str]     # 关联文件模式 (glob)
    body: str                    # Markdown 正文
    version: int
    status: Literal["active", "deprecated"]
    created_at: datetime
    updated_at: datetime
    effectiveness_score: float   # bench 数据反馈的有效性评分


class TaskSpec:
    """任务规格"""
    slug: str
    title: str
    sections: dict               # 7 章节内容
    linked_skills: list[str]     # 关联的 Skill name
    linked_docs: list[str]       # 关联的设计文档路径
    linked_issues: list[str]     # 关联的 Issue 编号
    parent_specs: list[str]      # 引用/复用的历史 Spec slug
    status: SpecStatus
    created_at: datetime


class KnowledgeGraph:
    """知识关联图"""
    skill_to_modules: dict       # Skill → 代码模块映射
    skill_to_specs: dict         # Skill → 历史 Spec 关联
    spec_to_spec: dict           # Spec 间相似度
    skill_effectiveness: dict    # Skill → bench pass_rate 提升效果
```

### 10.6 与 agent_go 的交互

项目管理工具管理的知识最终通过两个通道进入 agent_go：

| 通道 | 方式 | 内容 |
|------|------|------|
| **Task Spec 文件** | 工具写入 `docs/tasks/` → agent_go `--spec` 读取 | Spec §4「约束」中列出关联 Skill 名称，agent_go 通过 Rule 引擎加载 Skill 全文 |
| **Skill 文件** | 工具写入 `~/.agent_go/skills/` 或 `<project>/.agent_go/skills/` → agent_go Skill 系统自动加载 | 工具侧编辑好的 Skill Markdown 文件，同步到 agent_go 的 Skill 加载路径 |

**不需要新的 agent_go 接口**。工具侧负责知识的管理和推荐，agent_go 负责知识的消费和执行。

---

*关联文档：*
- [agent-go-input-spec.md](agent-go-input-spec.md) — Task Spec 规范和 agent_go 输入准则
- [software-development-lifecycle.md](software-development-lifecycle.md) — 软件开发全流程
- [project-management-tool-interaction.md](project-management-tool-interaction.md) — 项目管理工具与 agent_go 交互
