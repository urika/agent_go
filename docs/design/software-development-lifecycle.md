# agent_go 软件开发全流程

> **版本**：v1.0
>
> **目的**：描述从需求到交付的完整软件研发流程，明确 agent_go 在其中的位置、上下游接口契约、各阶段的角色与工具。
>
> **适用对象**：PM、工程师、以及任何需要理解「需求怎么变成代码」的人。
>
> **日期**：2026-08-01
>
> **当前基线（2026-08-08）**：实际流程和验收状态以 `functional-architecture.md`、`delivery-design.md`、`verification-design.md` 及 M0 清单为准；本文保留为上下游生命周期说明。

---

## 一、核心理念

```
最好的 AI 研发工具不是替代人做决策，而是让人的决策更高效地变成代码。

agent_go 的角色：不是「全自动写代码的 AI」，而是「把写好的 Spec 变成 PR 的执行引擎」。
```

三个原则贯穿全流程：

| 原则 | 含义 |
|------|------|
| **人在战略点决策，AI 在战术层执行** | PM 决定做什么、工程师决定怎么做、agent_go 负责做出来 |
| **文档是接口，Spec 是契约** | 阶段之间通过 Markdown 文档传递信息，不依赖口头沟通或隐式上下文 |
| **越早发现问题，修复成本越低** | Spec 审查（$0）< Plan 修正（$0.05）< 执行重试（$0.69）< 线上回滚（不可计量） |

---

## 二、整体流程（五阶段）

```
Phase 0           Phase 1           Phase 2            Phase 3          Phase 4
需求层            设计层             任务层              执行层            交付层
───────           ──────            ──────             ──────           ──────
PRD/Issue   →    技术方案     →     Task Spec    →    写代码+测试   →   Code Review
用户反馈         架构决策           任务拆分            验证+修复         PR/Merge
Bug 报告         影响分析           依赖排序            提交+推送         部署验证
                                        │
                                        │  ← agent_go 从这里接手
                                        │
                               agent_go Plan → Decompose → Execute → Verify → PR
```

---

## 三、各阶段详细定义

### Phase 0：需求层 — 「要做什么」

**角色**：PM（Product Manager）

**输入**：用户反馈、数据分析、竞品研究、业务目标

**产出物**：

| 产出 | 存放位置 | 格式 | 更新频率 |
|------|---------|------|---------|
| PRD | `docs/prd.md` | Markdown | 方向变更时 |
| Roadmap | `docs/roadmap.md` | Markdown | 每次迭代排期后 |
| 设计文档 | `docs/design/*.md` | Markdown | 功能设计时 |
| 分析报告 | `docs/bench-analysis-*.md` | Markdown | 数据驱动决策时 |

**工具**：Claude Code（交互式对话）——读数据、探查代码库、运行分析脚本、输出文档。PM 不写代码，但通过 Claude Code 辅助完成数据分析、方案对比、文档撰写。

**验收标准**：PRD 中的功能描述足够清晰，能回答「这个功能要达成什么效果？为什么现在做？」

**输出到 Phase 1 的接口**：PRD 段落 + Issue + 设计文档引用。

---

### Phase 1：设计层 — 「怎么做」

**角色**：工程师（Developer）

**输入**：PRD 段落、Issue、设计文档

**产出物**：

| 产出 | 存放位置 | 格式 |
|------|---------|------|
| 技术方案 | `docs/design/*.md` | Markdown |
| 受影响模块清单 | 方案文档内 | 文件路径列表 |
| 技术约束 | 方案文档内 | 迁移策略、兼容性要求、依赖变更 |

**工具**：Claude Code（交互式对话）——读代码库、定位受影响模块、评估方案可行性、输出技术方案文档。

**关键动作**：
1. 读 PRD/Issue，理解需求
2. 探查代码库，定位受影响模块和依赖关系
3. 评估技术方案（架构选择、trade-off 分析）
4. 输出技术方案文档（可选：简单需求可跳过，直接在 Phase 2 写 Task Spec）

**验收标准**：技术方案中明确了受影响模块、技术选型理由、与现有架构的兼容性。

**输出到 Phase 2 的接口**：技术方案文档 + 设计文档引用。

---

### Phase 2：任务层 — 「做到什么程度算做完」

**角色**：工程师（可辅以 PM 确认范围）

**输入**：PRD 段落 / Issue / 技术方案

**产出物**：

| 产出 | 存放位置 | 格式 |
|------|---------|------|
| **Task Spec** | `docs/tasks/task-<name>.md` | Markdown（7 章节） |

**Task Spec 是 agent_go 的输入契约**。它用 7 个结构化章节把「怎么做」说清楚：

```markdown
# Task Spec: <任务名称>

## 1. 目标（做什么）
一段话描述最终效果。

## 2. 动机（为什么）
背景、关联 Issue/PRD。

## 3. 范围（动哪里，不动哪里）
### 需要改动的文件/模块
### 明确不动的区域

## 4. 约束
技术约束、设计约束、兼容性要求。

## 5. 验收标准（怎么算做完）
可自动化判定的验收条件。

## 6. 参考资料
设计文档链接、类似实现的 commit hash。

## 7. 已知风险
大表迁移、兼容性问题等。
```

**生成方式（三种）**：

| 方式 | 工具 | 适用场景 |
|------|------|---------|
| **A. 手动填写** | `agent_go spec template --repo ./my-repo` 生成模板 → 人工填写 | 工程师清楚要做什么 |
| **B. 交互式生成** | Claude Code 对话：读代码库 → 追问澄清 → 输出 Spec | 需求模糊，需要探索 |
| **C. 半自动生成** | `agent_go scope "需求" --output task.md`（待实施） | 需求明确，快速出 Spec |

**Spec 准入审查**（`agent_go run --spec` 自动触发）：

```
Spec 提交
  → L1 硬门禁（机器判，阻断）：必填章节完整？文件路径有效？验证命令在白名单？
  → L2 软警告（LLM 判，提示）：范围遗漏？约束矛盾？验收标准可自动化？
  → 通过 → Phase 3
```

**验收标准**：Spec 通过准入审查（L1 全绿 + L2 已确认）。Task Spec 文件提交到 `docs/tasks/`，纳入版本管理。

**输出到 Phase 3 的接口**：`docs/tasks/task-<name>.md` 文件。

---

### Phase 3：执行层 — 「做出来」

**角色**：agent_go（自动化执行引擎）

**输入**：Task Spec（`--spec`）+ 代码仓库

**内部流程**：

```
agent_go run ./repo --spec docs/tasks/task-xxx.md

  Phase 3a: Plan（规划）
    ├─ 读 Spec → 注入 Plan prompt（目标/动机/范围/约束/验收标准/风险）
    ├─ 自动收集上下文（文件列表、Git 信息、Skill 清单、运行时环境）
    ├─ LLM 生成 Plan（JSON：overview + steps[] + dependencies + estimated_effort）
    └─ 人确认（Y/S/D/E/R/N）或 --yes 自动确认

  Phase 3b: Decompose（分解）
    ├─ Plan step → subtask（注入 agent_prompt + verification + skills）
    ├─ Role-Skill 规则匹配（自动补充分配 skill）
    ├─ Difficulty 路由（easy→Haiku, medium→Haiku/Sonnet, hard→Opus）
    └─ 依赖排序（拓扑波次）

  Phase 3c: Execute（执行）
    ├─ git worktree 隔离（每个子任务独立分支 + worktree）
    ├─ Claude Code 无头执行（--model 按 difficulty 路由）
    ├─ 验证循环（shell 验证 + LLM 语义评估）
    ├─ 失败重试（注入 stdout/stderr/diff → 修复 → 再验证，max 3 次）
    ├─ 级联阻断（上游失败 → 下游 blocked，不扩散错误）
    └─ git commit + tag（命名空间隔离：{task_id}/{sub_id}）

  Phase 3d: Verify & Report（验证与报告）
    ├─ 聚合结果（pass_rate / 耗时 / 成本 / 变更统计）
    ├─ 质量仪表（通过率、验证率、合并就绪指示）
    └─ 多通道通知（desktop / webhook / command）
```

**产出物**：

| 产出 | 位置 | 说明 |
|------|------|------|
| Git commits | repo worktree branches | 每个子任务独立 commit |
| Git tags | `{task_id}/{sub_id}` | 跨 worktree 产物传递 |
| meta.json | `~/.agent_go/{task_id}/` | 完整执行记录（plan/subtasks/results） |
| metering.jsonl | `~/.agent_go/{task_id}/` | 每 API 请求的 token/成本/延迟 |
| 保留 worktree | `~/.agent_go/{task_id}/worktrees/` | 失败子任务保留现场，供 inspect |

**验收标准**：所有子任务 `all_verify_ok=true`。失败任务通过 `agent_go inspect` 可查看现场。

**输出到 Phase 4 的接口**：Git 分支 + PR（通过 `agent_go pr --push`）+ 聚合审查摘要。

---

### Phase 4：交付层 — 「审查与合并」

**角色**：工程师（Reviewer）

**输入**：agent_go 产出的分支 + PR

**流程**：

```
agent_go review --task <task-id>
  → 聚合 diff（按文件分组，展示各子任务变更摘要）
  → 人审查决策：
      [approve]           → PR 合并
      [reject]            → 打回，说明原因
      [changes-requested] → 要求修改后重新提交
      agent_go review --deep → 独立模型逐子任务审查（额外质量层）

agent_go pr <task-id> --push
  → 推送 worktree 分支到远程
  → 通过 gh CLI 创建 PR
  → 关联 Issue（如果 Task Spec 中标注了）

人工最终审查：
  → GitHub PR Review（读 diff、跑 CI、验证测试）
  → Merge
  → 部署（CI/CD 自动或手动触发）
```

**产出物**：

| 产出 | 说明 |
|------|------|
| Merged PR | 代码合入主分支 |
| Closed Issue | Issue 状态更新 |
| 部署验证 | CI 全绿 → 上线 |

**反馈闭环**：合并后，Task Spec 及关联的 Issue 更新状态。如果线上出问题，回写 Issue 或新建 Bug Report → 重新走 Phase 0-4。

---

## 四、角色与工具矩阵

```
┌──────────────────────────────────────────────────────────────────┐
│                        角色分工                                   │
│                                                                  │
│  PM               工程师             agent_go           CI/CD    │
│  ───              ─────             ────────           ─────    │
│  Phase 0           Phase 1           Phase 3            Phase 4  │
│  需求分析          技术方案           执行引擎            部署验证  │
│  方向决策          Task Spec          Plan→Execute      测试套件  │
│  优先级            代码审查           Verify→Report      上线     │
│                                                                  │
│  工具：             工具：             工具：              工具：    │
│  Claude Code       Claude Code       CLI + LLM API      GitHub   │
│  (交互式对话)       (交互式对话)       (headless)         Actions  │
│                    agent_go scope                                │
│                                                                  │
│  产出：             产出：             产出：              产出：    │
│  PRD/Roadmap       Task Spec         Git commits        部署      │
│  设计文档           技术方案           PR                 测试报告  │
│  分析报告           代码审查意见        meta.json                   │
│                                      metering.jsonl              │
└──────────────────────────────────────────────────────────────────┘
```

| 角色 | 阶段 | 工具 | 关键动作 | 产出 |
|------|------|------|---------|------|
| **PM** | Phase 0 | Claude Code（交互式） | 需求分析、方案对比、数据分析、写 PRD | PRD / Roadmap / 设计文档 / 分析报告 |
| **工程师** | Phase 1-2 | Claude Code（交互式）+ `agent_go scope` | 技术方案、代码库探查、写 Task Spec | 技术方案 / Task Spec |
| **agent_go** | Phase 3 | CLI + LLM API（headless） | Plan → Decompose → Execute → Verify | Git commits / PR |
| **工程师** | Phase 4 | GitHub + `agent_go review` | 审查 diff、Merge PR | Merged PR |
| **CI/CD** | Phase 4 | GitHub Actions | 跑测试、部署 | 测试报告、部署 |

---

## 五、接口契约

### 5.1 Phase 0 → Phase 1-2 接口：PRD + Issue

```
PRD 段落  ──→  工程师理解需求
Issue     ──→  工程师判断技术可行性
设计文档   ──→  工程师参考架构约束
```

**契约要求**：PRD 中的功能描述足够明确，工程师无需反复追问「为什么做」和「成功标准是什么」。

### 5.2 Phase 1-2 → Phase 3 接口：Task Spec

```
Task Spec（docs/tasks/task-<name>.md）  ──→  agent_go run --spec
```

这是**唯一的执行接口契约**。Task Spec 的结构化章节直接映射到 agent_go 的 Plan prompt 注入位置：

| Spec 章节 | Plan prompt 注入 | Planner 行为 |
|-----------|-----------------|-------------|
| §1 目标 | user content：「任务：{spec.目标}」 | 明确要达成的效果 |
| §2 动机 | user content：「背景：{spec.动机}」 | 理解决策上下文 |
| §3 范围-动什么 | system prompt：「必须涉及的模块：...」→ `steps[].files` 约束 | 不遗漏关键文件 |
| §3 范围-不动什么 | system prompt：「禁止修改的模块：...」→ `steps[].files` 排除 | 不误改禁止区域 |
| §4 约束 | system prompt：「设计约束：...」→ 分解策略受约束 | 遵守技术约束 |
| §5 验收标准 | system prompt → `steps[].verification` 自动派生 | 知道做到什么程度算完 |
| §6 参考资料 | user content：「参考：{spec.参考资料}」 | 参考类似实现模式 |
| §7 已知风险 | system prompt → `steps[].risks` + `difficulty` 标记 | 高风险步骤走强模型 |

**契约要求**：§1/§2/§3/§5 必须存在且非空。Spec 通过准入审查（L1+L2）。完整规范见 [agent-go-input-spec.md](agent-go-input-spec.md)。

### 5.3 Phase 3 → Phase 4 接口：Git 分支 + PR + 审查摘要

```
agent_go 产出  ──→  工程师审查
  ├─ Git 分支（worktree branch）：代码变更
  ├─ PR（通过 gh CLI 创建）：变更描述 + 关联 Issue
  ├─ meta.json：完整执行记录（plan/subtasks/results）
  ├─ metering.jsonl：成本/性能数据
  └─ agent_go review --task <id>：聚合 diff 摘要 + 子任务明细
```

**契约要求**：所有子任务 `all_verify_ok=true`。失败任务被阻断，不进入审查环节。

---

## 六、文档仓库结构

整个流程的文档（Phase 0-2 的产出物）存放在项目仓库的 `docs/` 目录下：

```
docs/
├── prd.md                              # Phase 0: 产品定位、KPI、设计原则
├── roadmap.md                          # Phase 0: 迭代排期、依赖关系
├── architecture.md                     # Phase 1: 核心架构、设计决策
├── spec.md                             # Phase 1: 模块接口速查
├── ISSUES.md                           # Phase 0: 已知 bug 和改进项
├── bench-analysis-*.md                 # Phase 0: 数据分析支撑决策
├── design/                             # Phase 0-1: 功能设计和架构方案
│   ├── agent-go-input-spec.md          #   ↑ 输入准则（本文档的接口规范）
│   ├── bench-v2-data-requirements.md   #   ↑ Bench 数据需求
│   ├── model-evaluation-and-tiering.md #   ↑ 模型分级设计
│   └── ...                             #   ↑ 其他设计文档
├── tasks/                              # Phase 2: Task Spec
│   ├── task-email-verification.md      #   ↑ 「实现邮箱验证」的 Spec
│   ├── task-db-migration.md            #   ↑ 「数据库迁移」的 Spec
│   └── ...                             #   ↑ 其他 Task Spec
└── archive/                            # 历史文档
```

**关键约定**：
- **PRD/Roadmap/设计文档** 说「为什么」和「是什么」——长期维护、版本管理
- **Task Spec** 说「怎么做」——一次性、执行后归档、同类任务的 Spec 可被后续 Spec 参考
- **所有文档都是 Markdown**——人可读、AI 可解析、Git 可 diff

---

## 七、信息流全景图

```
用户反馈 ──┐
数据分析 ──┤
竞品研究 ──┼──→ Phase 0: PM ──→ PRD / Roadmap / 设计文档 ──┐
业务目标 ──┘                                                │
                                                            ↓
                                      Phase 1: 工程师 ──→ 技术方案（可选）
                                                            │
                                      Phase 2: 工程师 ──→ Task Spec
                                                            │
                                              ┌─ L1 硬门禁 ─┤
                                              │              │
                                              └─ L2 软警告 ─┘
                                                            │
                                                            ↓
                                      Phase 3: agent_go ──→ Git commits + PR
                                                            │
                                                            ↓
                                      Phase 4: 工程师 ──→ Review → Merge → 部署
                                                            │
                                                            ↓
                                      线上问题 ──→ Issue/Bug Report ──→ 回到 Phase 0
```

---

## 八、关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| agent_go 从 Phase 2 后半段介入 | Task Spec 是 agent_go 的输入起点 | agent_go 不做需求分析、不做技术方案。它只做「Spec → PR」 |
| Spec 是 Markdown 而非 JSON/YAML | 人可读可写、AI 可解析 | PM 和工程师不需要学新格式 |
| 准入审查不自动修复 Spec | 打回后由人修正 | 范围/约束/验收标准是人的决策 |
| PRD/Roadmap 不作为 agent_go 的直接输入 | 通过 `--context` 按需注入相关段落 | 全量注入超上下文窗口；PRD 是给人类看的，不是给 LLM 的 prompt |
| Phase 0-2 用 Claude Code 交互式，Phase 3 用 agent_go headless | 探索 vs 执行 | 交互式适合理解-探查-决策；headless 适合已知目标的执行 |
| 文档即接口 | 阶段之间通过 Markdown 文档传递信息 | Git 可版本管理、可 diff、可 review |

---

## 九、agent_go 的边界

```
agent_go 做什么：
  ✅ 读 Task Spec → 生成 Plan → 分解子任务 → 隔离执行 → 验证修复 → 提交代码 → 生成 PR

agent_go 不做什么：
  ❌ 需求分析、竞品研究、数据分析报告                ← PM + Claude Code
  ❌ 技术方案设计、架构决策                            ← 工程师 + Claude Code
  ❌ 交互式代码库探查、方案对比、追问澄清              ← 工程师 + Claude Code
  ❌ 部署、发布、线上监控                              ← CI/CD
  ❌ 需求管理（Issue 创建、优先级排序、状态流转）      ← GitHub Issues / Jira / Linear
  ❌ 代码审查的最终决策（approve/reject 是人）        ← 工程师
```

---

*关联文档：*
- [agent-go-input-spec.md](agent-go-input-spec.md) — agent_go 输入准则（Task Spec 规范 + Spec Gate 设计）
- [bench-v2-data-requirements.md](../archive/design/bench-v2-data-requirements.md) — Bench 数据需求规格
- [bench-analysis-2026-08-01.md](../archive/reference/bench-analysis-2026-08-01.md) — Bench v1 数据分析报告（历史参考）
- [prd.md](../prd.md) — 产品定位、KPI、设计原则
- [roadmap.md](../roadmap.md) — 迭代排期
