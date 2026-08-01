# 架构设计如何传递给 agent_go

> **版本**：v1.0
>
> **目的**：回答「传统开发中的架构设计——划模块、定接口、分功能——在 AI 增强开发流程中怎么做，设计产物如何传递给 agent_go 执行」。
>
> **日期**：2026-08-01

---

## 一、传统架构设计 vs AI 增强流程

```
传统流程：
  需求 → 架构设计（人 1-3 天）→ 模块拆分 → 分配开发者 → 各自实现 → 集成

AI 增强流程：
  需求 → 架构设计（人 + Claude Code，0.5-1 天）→ 模块级 Task Spec → agent_go 执行 → 集成
```

**变化不是「AI 替代架构设计」，而是「架构设计的产出物格式变了」——从人脑中的心理模型 + 口头沟通，变成结构化的、agent_go 可消费的设计文档。**

---

## 二、架构设计的四层产出

一次架构设计产出四层信息，分别对应不同的传递通道：

```
Layer 1: 系统级（整个项目的约束）
  → 传递通道：CLAUDE.md + 项目 Skill
  → 加载方式：每次 agent_go run 自动加载
  → 内容：技术栈、模块边界规则、数据流方向、全局约束

Layer 2: 功能级（一个功能/Epic 的模块划分）
  → 传递通道：设计文档（docs/design/） + Task Spec §3（范围）
  → 加载方式：--spec 或 --docs
  → 内容：功能模块划分、模块职责、接口契约、数据模型

Layer 3: 任务级（单个 Task 的实现指令）
  → 传递通道：Task Spec §4（约束）+ Plan step agent_prompt
  → 加载方式：--spec
  → 内容：本次改什么、不改什么、具体约束

Layer 4: 模式级（可复用的实现模式）
  → 传递通道：Skill（convention / tool 类型）
  → 加载方式：Rule 引擎自动匹配 + --skill 显式指定
  → 内容：代码模板、API 使用约定、错误处理模式
```

---

## 三、架构设计过程（Phase 1 详细拆解）

### Step 1：理解现状（30 min，Claude Code 辅助）

```
工程师 + Claude Code 交互对话：

"帮我理一下当前项目的模块结构和依赖关系"

Claude Code 做的事：
1. 读 CLAUDE.md / architecture.md（如果有）
2. 遍历目录结构 → 识别模块边界
3. 分析 import 关系 → 输出模块依赖图
4. 识别关键接口（public API、数据模型）
5. 输出：当前架构概览

产出物（可选，写入 docs/architecture.md 或直接用于下一步）：
  - 模块清单（名称、职责、关键文件）
  - 模块依赖图（谁 import 了谁）
  - 关键接口列表（函数签名 + 被调用频率）
```

### Step 2：设计方案（1-2 h，人决策 + Claude Code 辅助）

```
工程师 + Claude Code 交互对话：

"我们要加一个转账功能。需要新增哪些模块？跟现有模块什么关系？
  给出两个方案的对比。"

Claude Code 做的事：
1. 读需求（Task Spec 草稿或 PRD 段落）
2. 基于 Step 1 的架构理解，生成方案
3. 每个方案列出：新增模块、改动模块、接口变更、数据流
4. 对比 trade-off（复杂度、耦合度、可测试性）

人做的事：
  → 评估 trade-off
  → 选择方案（或提出第三种方案）
  → 做出关键决策：模块边界、接口契约、数据模型

产出物（写入 docs/design/transfer-feature-design.md）：
  - 方案选择及理由
  - 模块清单（新增 + 改动）
  - 接口契约（函数签名、API 端点、数据模型）
  - 数据流图（从请求到响应的完整路径）
```

### Step 3：拆解为 Task Spec（30 min，人 + Claude Code 辅助 + agent_go scope）

```
工程师 + Claude Code：

"基于刚才选定的方案，拆成可独立执行的 Task Spec"

Claude Code 做的事：
1. 读取方案设计文档
2. 按模块边界拆解：每个模块的改动 = 一个 Task Spec
3. 标注依赖：B 依赖 A 的接口（不需要 A 的完整实现）
4. 为每个 Spec 生成 7 章节草稿

人做的事：
  → 确认拆分粒度（太大 → 单任务超时，太小 → 碎片化）
  → 确认依赖关系（A 的接口先定义，B 基于接口实现）
  → 确认验收标准（怎么验证模块 A 独立工作？）

产出物（写入 docs/tasks/）：
  docs/tasks/task-transfer-model.md         ← Spec 1: 数据模型 + 迁移
  docs/tasks/task-transfer-service.md       ← Spec 2: 业务逻辑（依赖 Spec 1）
  docs/tasks/task-transfer-api.md           ← Spec 3: API 端点（依赖 Spec 2）
  docs/tasks/task-transfer-integration.md   ← Spec 4: 集成测试（依赖 Spec 1-3）
```

### Step 4：Spec 准入审查（5 min）

```
agent_go scope --verify docs/tasks/task-transfer-model.md
  → L1 硬门禁：必填字段、文件路径、白名单
  → L2 软警告：依赖遗漏检测

工程师逐 Spec 确认 → 通过 → 进入执行队列
```

---

## 四、设计产物如何传递给 agent_go

### 传递机制矩阵

| 设计信息 | 传递通道 | agent_go 哪个阶段用到 | 注入方式 |
|---------|---------|---------------------|---------|
| 技术栈、全局约束 | CLAUDE.md + 项目 Skill | Plan（Planner 决定技术方案兼容性） | 自动加载（Claude Code 子系统） |
| 模块边界规则 | architecture.md → `--docs` 或 `--context` | Plan（Planner 知道哪些模块可以动、哪些不能动） | user content 注入 |
| 接口契约 | 设计文档 + Task Spec §3-4 | Plan + Worker | Plan: Spec 注入；Worker: 上游 tag merge |
| 数据模型 | 设计文档 + Task Spec §4 约束 | Worker（代码生成时参考） | TASK.md 约束段 |
| 代码模板/模式 | Skill（convention/tool 类型） | Worker | Skill 全文注入 TASK.md |

### 关键设计决策：系统级信息如何传递给窄化上下文的 Worker

**核心矛盾**：架构设计是系统级的（需要看到全局），但 agent_go 的 Worker 是窄化的（只能看到当前子任务）。

**解决方案：通过 Plan 阶段的 Planner 做信息降维。**

```
系统级设计文档（全局）
        │
        ▼
  Plan 阶段（Planner 通读全局设计 + Task Spec）
        │
        │  Planner 做的事：
        │  1. 理解全局架构
        │  2. 识别当前子任务在全局中的位置
        │  3. 将「全局约束」转化为「局部指令」
        │
        ▼
  Plan step 的 agent_prompt（局部）
        │
        ▼
  TASK.md（Worker 只看到这一步需要的信息）
```

**示例**：架构设计规定「所有金额字段用 Decimal，API 返回时序列化为字符串（防 JS 精度丢失）」。

```
Planner 读到架构设计 → 生成 Plan：

Step 1: 新增 Transfer 模型
  agent_prompt: "创建 Transfer 模型。金额字段用 DecimalField(max_digits=18, decimal_places=2)。
               在 Serializer 中，金额字段序列化为字符串（防止 JS Number 精度丢失）。
               参考模式见 Skill: banking-decimal-rules。"

Step 2: 新增转账 API
  agent_prompt: "创建转账 API 端点。请求中的金额字段接收字符串，后端转换为 Decimal。
               响应中的金额字段返回字符串。遵循 Skill: api-conventions。"
```

**Planner 不需要把「为什么金额要序列化为字符串」的完整论证传给 Worker——它只需要把「怎么做」的指令传下去。**

---

## 五、模块间接口的传递：先定义接口，后各自实现

这是传统架构设计中最重要也最难传递的部分。解决方案：

### 设计阶段：定义接口契约

```markdown
# docs/design/transfer-feature-design.md

## 模块接口契约

### TransferService（新增）
```python
# src/services/transfer_service.py

class TransferService:
    """转账业务逻辑。依赖：Transfer 模型、User 模型。"""

    def transfer(
        self,
        from_user_id: str,
        to_user_id: str,
        amount: Decimal,
        idempotency_key: str
    ) -> TransferResult:
        """
        执行转账。
        
        前置条件：
        - from_user 存在且状态为 active
        - amount > 0
        - idempotency_key 未使用过（幂等性检查）
        
        返回：
        - TransferResult(status=COMPLETED|INSUFFICIENT_BALANCE|USER_NOT_FOUND)
        
        副作用：
        - 创建 Transfer 记录
        - 更新 from_user 和 to_user 的余额
        - 记录审计日志
        """
        ...

### TransferAPI（新增）
```python
# src/api/transfer.py

POST /api/v2/transfers
  Request:  {"from_user_id": "...", "to_user_id": "...", "amount": "100.00", "idempotency_key": "..."}
  Response: {"code": 0, "data": {"transfer_id": "...", "status": "COMPLETED"}}
  
  依赖：TransferService.transfer()
```

### User 模型（改动）
```python
# src/models/user.py  — 新增字段
balance: DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
# 不改动现有字段，不改变现有 API 签名
```
```

### 执行阶段：按依赖顺序执行

```
Task 1: 定义接口 + 数据模型（最先执行，无依赖）
  agent_go run --spec task-transfer-model.md
  产出：Transfer 模型 + User.balance 字段 + TransferService 接口骨架

Task 2: 实现 TransferService（依赖 Task 1 的接口定义）
  agent_go run --spec task-transfer-service.md
  上游产物（Task 1 的 git tag）自动 merge 到 worktree
  → Worker 看到的代码库已包含 Transfer 模型和接口骨架
  → Worker 只需要填充 TransferService.transfer() 的实现

Task 3: 实现 TransferAPI（依赖 Task 2 的 Service 实现）
  agent_go run --spec task-transfer-api.md
  上游产物（Task 1 + 2 的 git tag）自动 merge
  → Worker 看到的代码库已包含完整的数据模型和业务逻辑
  → Worker 只需要写 API 端点，调用 TransferService

Task 4: 集成测试（依赖全部）
  agent_go run --spec task-transfer-integration.md
```

**接口在代码中以骨架形式先行存在。** 子任务 B 的 Worker 看到的不是「一个还没实现的接口文档」，而是「已经定义好签名和 docstring 的 TransferService 类，只需要填充实现」。

---

## 六、哪些设计产物应该持久化

| 产物 | 持久化位置 | 格式 | 生命周期 | 是否传给 agent_go |
|------|-----------|------|---------|-----------------|
| 架构概览 | `docs/architecture.md` | Markdown | 长期维护 | ✅ `--docs` 或自动 |
| CLAUDE.md | `<project>/CLAUDE.md` | Markdown | 长期维护 | ✅ 自动（Claude Code） |
| 模块依赖图 | `docs/architecture.md` 内 | Mermaid/ASCII | 重大重构时更新 | 参考（非直接注入，避免 token 浪费） |
| 功能设计文档 | `docs/design/<feature>-design.md` | Markdown | 功能完成后归档 | ✅ `--docs` |
| 接口契约 | 设计文档内 + 代码骨架 | Markdown + Python stub | 功能完成后归档 | ✅ Spec §4 + Plan step agent_prompt |
| 数据模型 | 设计文档内 + 代码 | Markdown + Django models | 随代码演进 | ✅ Spec §3 范围 + §4 约束 |
| Task Spec | `docs/tasks/` | Markdown（7 章节） | 执行后归档 | ✅ `--spec` |

---

## 七、什么不应该传给 agent_go

| 信息 | 不传的原因 |
|------|-----------|
| 完整的设计讨论过程（方案对比、trade-off 分析） | Planner 只需要决策结论，不需要知道被否决的方案。token 浪费 |
| 完整的模块依赖图 | 对具体的 Worker 任务，只需要知道「当前模块依赖谁」，不需要全局图 |
| 未选中的替代方案 | 干扰 Planner 决策——可能让 Planner 尝试走被否决的路线 |
| 人的决策心路历程 | 「最终选了方案 B 因为 A 太复杂」→ Worker 不需要知道，只需要知道按 B 做 |

**原则**：传决策结果，不传决策过程。传怎么做，不传为什么这么做（除非「为什么」直接决定了「怎么做」的正确性）。

---

## 八、与现有 agent_go 能力的对应

| 设计活动 | agent_go 已有的支持 | 差距 |
|---------|-------------------|------|
| 探索代码库结构 | 自动（analyze_project + get_resource_map） | ✅ 足够 |
| 生成架构方案 | ❌ agent_go 不做（是 Claude Code 交互的事） | 不需要——这是工程师 + Claude Code 的领域 |
| 接口骨架先行 | 支持：Task 1 定义接口 → commit → Task 2 merge tag 后基于骨架实现 | ✅ 足够（git worktree + tag 产物传递） |
| 模块边界约束 | Plan prompt 可注入模块约束（通过 Spec §3） | ⚠️ 需要 Spec §3 被高效解析和注入 |
| 跨 Task 架构一致性 | Planner 负责通读全局设计，降维为局部指令 | ⚠️ 依赖 Planner 质量——如果 Planner 不理解架构，可能生成不一致的局部指令。bench 待验证 |

---

*关联文档：*
- [agent-go-input-spec.md](agent-go-input-spec.md) — Task Spec 规范
- [knowledge-transfer-and-management.md](knowledge-transfer-and-management.md) — 知识传递机制
- [software-development-lifecycle.md](software-development-lifecycle.md) — 软件开发全流程
- [../architecture.md](../architecture.md) — agent_go 自身架构设计
