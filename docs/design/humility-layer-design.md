# 谦逊层（Humility Layer）设计

> **版本**：v1.0
> **日期**：2026-08-14
> **文档目的**：把「共生软件架构」讨论中可借鉴的 4 个元原则（谦逊介入、层间可追溯、知识生命周期、未覆盖视角）落成 agent_go 的一个正交观测层——「谦逊层」。全部复用已实现能力，不新增机制。
> **关联文档**：
> - [`spec-closed-loop-design.md`](spec-closed-loop-design.md) — spec 闭环设计（本层的上游基础）
> - [`business-architecture.md`](business-architecture.md) — A/B 类决策（A1 正交观测、IV-2 不改变 status 语义）
> - 共生软件架构讨论（`~/Desktop/🎯 成长目标.md`）— 借鉴来源

---

## 一、定位

**谦逊层是正交于 status 的「观测层」**——它不决定成败（status 8 态状态机决定），只回答「这个交付的**已知不可信之处**在哪里」。

```text
                    status（8 态，决定成败）
                          ↑ 正交，互不影响
        ┌───────────────────────────────────────────┐
        │           谦逊层（Humility Layer）          │
        │  H1 blind_spots            盲区清单        │
        │  H2 layer_attribution      层间归因        │
        │  H3 problem lifecycle      知识生命周期     │
        │  H4 uncovered_perspectives 未覆盖视角      │
        └───────────────────────────────────────────┘
              ↓ 消费方
    review / replay / 交付门 / eval gate / KnowledgeStore
```

**为什么叫「谦逊层」**：agent_go 交付的「可信度」瓶颈不是「做得对不对」（status 已可信），而是「**用户不知道哪些地方不可信**」。这一层把「谦逊」从原则变成可查字段。

**铁律**（延续 A1/IV-2）：
1. 谦逊层是「观测 + 标记 + 可查」，不改变 status 语义，不硬阻断。
2. 全部复用已实现能力，只做聚合与统一，不新增数据采集（除 H3 两个字段）。
3. 无 spec 的任务行为完全不变（IV-3 向后兼容）。

---

## 二、四个组成

### H1：blind_spots 盲区清单（ROI 最高，~0.5 天）

**共生来源**：实体节点的「无知维」——known_unknowns 必须显式声明。

**设计**：pipeline 交付门聚合**已分散存在的 5 个盲区信号**为 `meta["blind_spots"]`：

```python
meta["blind_spots"] = {
    "uncovered_acceptance_ids": [...],    # traceability.missing_requirement_ids（已实现）
    "weakly_anchored_subtasks": [...],    # A2 verification_not_anchored（已实现）
    "unattributed_failures": [...],       # 失败但 readonly_review 无 root_cause（已实现采集）
    "baseline_dirty": bool,               # A3 baseline 状态（已实现）
    "inconclusive_evaluations": [...],    # semantic evaluator inconclusive（已实现采集）
}
```

**消费方**：`review` / `replay` / 交付门输出「已知盲区」段落。

**复用基础**：5 个信号全部已存在（traceability 自动触发、A2 锚定、readonly_review、A3 基线、语义评估），H1 只做聚合。

### H2：layer_attribution 层间归因（~1 天）

**共生来源**：「违规时必须向上回溯——是规则错？规范不合理？原则冲突？禁止只在功能层修补」。

**设计**：给失败加 `layer_attribution`，让「谁错了」可定位。六层映射：

```text
目标层 = spec §1 + 北极星三支柱
原则层 = A/B 类决策（business-architecture.md）
规范层 = spec §5 验收 + failure_class 8 类
规则层 = L1 门禁 + do-not-touch + 文件所有权（A1）+ AST 冲突检测
协议层 = 依赖 DAG + artifact tag 传递
功能层 = subtask 执行 / 模型能力
```

**落地**：deviation 的 `root_cause_category` 扩展为层级归因：

| layer_attribution | 含义 | 已有雏形 |
|---|---|---|
| `spec_too_broad` | 规范层：验收写太宽，验证无法锚定 | deviation_type=spec_deviation |
| `planner_out_of_scope` | 规则层：planner 违反 do-not-touch/所有权 | spec_do_not_touch_violation（已实现） |
| `contract_broken` | 协议层：上游 artifact 传递断裂 | merge conflict 路径 |
| `worker_capability` | 功能层：模型能力不足 | VERIFICATION_FAILED |
| `constraint_blocked` | 规则层：成本/预算阻断 | BLOCKED |

**已有基础**：`status.py` 的 `BLOCKED`（约束阻断=规则层）vs `VERIFICATION_FAILED`（能力失败=功能层）——这个分层判断已存在，H2 只是推广到全链路。

**价值**：复盘时直接回答「该修 spec、修 planner 还是换模型」——是 eval / 难度校准 / 模型路由的决策输入。

### H3：problem lifecycle 知识生命周期（挂 B4，~0.5 天增量）

**共生来源**：知识会休眠、死亡；死亡有「葬礼仪式」（记录为何曾重要、为何不再适用）。

**设计**：B4 的 Problem 实体（三态 + 复发重开，已设计）补两个字段：

```python
Problem:
    status: opened | analyzed | resolved
    stale_after_days: int      # 半衰期：默认 90 天未复发 → dormant（派生状态，不新增状态机节点）
    resolution_summary: str    # 葬礼：resolved 时记录「为何曾重要、如何被修、修法可复用性」
```

**价值**：`resolution_summary` 是 KnowledgeStore（B5=c）的**直接输入**——让「从失败中学习」从「记录失败」升级到「记录解法」。

**已具备**：`clean --older-than`（保留期）= 半衰期粗粒度版；B4 复发重开 = 休眠/唤醒的工程化。

### H4：uncovered_perspectives 未覆盖视角（~0.5 天）

**共生来源**：「谦逊 API」——输出附带未覆盖视角和已知分歧，而非假装全知。

**设计**：交付门聚合 `meta["uncovered_perspectives"]`：

```python
meta["uncovered_perspectives"] = [
    {"perspective": "independent_reviewer", "missing": True,
     "reason": "review_agent 未启用（judge==candidate 风险）"},
    {"perspective": "architecture_review", "missing": True,
     "reason": "architecture_review 未启用（fail-open）"},
    {"perspective": "semantic_verdict", "missing": True,
     "reason": "语义评估 inconclusive"},
]
```

**复用基础**：三个「视角」都已存在（reviewer 两阶段审查 / architecture_review / semantic evaluator），H4 只做显式汇总。

---

## 三、数据流

```text
执行期各环节（已实现）
  ├─ traceability 自动触发（pipeline 交付门）→ H1.uncovered_acceptance_ids
  ├─ A2 锚定（planning warning）             → H1.weakly_anchored_subtasks
  ├─ readonly_review（executor）             → H1.unattributed_failures + H3.root_cause
  ├─ A3 基线（cmd_run）                      → H1.baseline_dirty
  ├─ semantic evaluator（executor）          → H1.inconclusive + H4
  └─ deviation（executor）                   → H2.layer_attribution
                ↓ 聚合（pipeline 交付门，新增 ~30 行）
        meta["blind_spots"] / meta["uncovered_perspectives"] / layer_attribution
                ↓ 消费
    review / replay / 交付门输出 / eval gate / KnowledgeStore（B5=c）
```

---

## 四、落地顺序与成本

| 序 | 项 | 成本 | 依赖 |
|---|---|---|---|
| 1 | H1 盲区清单 | ~0.5 天 | 无（5 信号已存在） |
| 2 | H4 未覆盖视角 | ~0.5 天 | 无 |
| 3 | H2 层间归因 | ~1 天 | deviation 已有字段 |
| 4 | H3 知识生命周期 | ~0.5 天 | B4/M5 实施时挂上 |

**总成本 ~2.5 天，零新机制、零新采集（除 H3 两字段）**。

---

## 五、边界（刻意不做）

| 不借 | 理由 |
|---|---|
| 涌现/自组织架构 | agent_go 护城河是确定性（commit 边界 / recover 不碰孤儿 / worktree 隔离） |
| 实体节点 CRD/Sidecar | subtask + git worktree 是更对的抽象，轻一个数量级 |
| 社会/生态/认知外部性 | agent_go 是工具不是世界接口；外部性 = 成本 + 质量 + 介入时间（三支柱已覆盖） |
| 多主体复调协商 | 单一用户场景；三个例外点（Plan 确认 / merge 决策 / 失败审查）已是充分表达 |

---

## 六、一句话

> **agent_go 要借的不是共生架构的「复杂」，而是它的「谦逊」——把已散落的盲区信号、视角缺位、层间判断，收敛成一层可查的「谦逊层」。**

---

## 七、产品视角（PM 结论）

### 7.1 核心叙事升级

> **agent_go 卖的不是「自动化」，是「可信的自动化」。**

Claude Code 直接跑不需要 agent_go；用户之所以要 agent_go，是因为它承诺可靠性（可审计、可恢复、可验证交付）。共生架构的「谦逊介入」「无知图谱」「可质疑的翻译」本质是「可信」的产品化方法论。**谦逊层 H1-H4 的产品价值主张 = 系统主动交底，用户才敢放权。**

### 7.2 四个产品动作

**① 交付报告改版：从「结果报告」到「交底报告」**

交付完成时主动告知「已知盲区」（哪些验收未覆盖、哪些验证弱锚定、哪个失败无根因），把用户的审查从「找问题」变成「读交底」——直接压低 Human Intervention Minutes（用户审查时间大头在「找」不在「判」）。

**② 「拒绝权」从隐式机制升级为显式产品承诺**

三个例外点（Plan 确认 / merge 决策 / 失败审查）已经是「拒绝权」的工程化，但需变成显式承诺：写进 CLI 交互、产品文档、Web 操作台三处——「你可以随时说不，这是你的权利」。用户放权的勇气来自「随时能收回」的确信。

**③ 「学习可感知」：让用户看见产品「越用越聪明」**

失败时告知历史（「该模式第 N 次出现 + 上次根因 + 上次解法 + 是否复发」），把智能闭环从「论文指标」变成「每次失败时的可见记忆」。

**④ 补充「信任指标」到产品指标体系**

| 指标 | 定义 | 衡量什么 |
|---|---|---|
| 审查后修改率 | 用户审查后动手改交付物的比例 | 交付的「初始可信度」 |
| 盲区命中率 | 交底报告标盲区的项，最终真出问题的比例 | 盲区标注准确度（防「狼来了」） |
| 复发可见率 | 失败时能关联到历史 Problem 的比例 | 学习闭环覆盖率 |

信任指标是「渐进自治」的放行依据（阶段 D）——交底可信，才敢自动化升级。

### 7.3 产品边界（定位即边界）

| 不借 | PM 理由 |
|---|---|
| 社会/生态/认知外部性 | 用户是工程师、场景是开发任务；硬加伦理维度是定位错乱 |
| 「涌现」叙事 | 用户要确定性；「涌现演化」的叙事会摧毁委托信任 |
| 多主体复调 | 单人委托工具，不是协作平台；单用户的「复调」= 三个例外点 |

### 7.4 产品叙事：agent_go 的成长故事

```text
第一阶段（现在）：可靠地执行，诚实交付结果 + 成本
第二阶段（谦逊层）：主动交底——告诉你哪里不可信，把审查从「找问题」变「读交底」
第三阶段（学习感知）：每次失败带着记忆，你看见它越来越聪明
第四阶段（渐进自治）：信任指标达标，才把环节交给它
```

**核心**：每一次「自动化升级」都以谦逊为前提——先证明交底可信，再放权。这正是北极星「渐进自治」的定义本身。

### 7.5 产品价值场景（5 个）

**场景 1：睡前委托，早起看结果（盲区清单 + 交底报告）**
委托跨文件重构后醒来看到「✅ 全部完成」。无谦逊层时用户必须自己挖坑（验收真测了吗？验证是整仓还是锚定的？未提交改动算基线了吗？）——审查时间大头在「找」不在「判」。有谦逊层时最终报告直接交底（未覆盖 AC / 弱锚定子任务 / 基线 dirty）。价值：**Human Intervention Minutes → 0 的核心杠杆**。

**场景 2：失败复盘的定位（层间归因）**
失败后要决定「换模型 / 修 plan / 修 spec」——三种动作对应三种完全不同的修复路径，猜错就是浪费一轮跑。层间归因直接定位（`spec_too_broad` = 该修 spec；`planner_out_of_scope` = 该修 plan）。价值：**每次失败从「成本」变成「精确定位的改进信号」**。

**场景 3：新人接手失败现场（失败历史关联）**
新人看到 shell_fail 正准备从零排查，review 直接显示「该模式第 4 次出现 + 历史根因 + 历史解法（上次修复未生效，已重开）」。价值：知识跨任务复用 + 用户**亲眼看见产品在记忆**——「越用越聪明」从 benchmark 数字变成可见事实。

**场景 4：要不要更自动？数据说话（信任指标）**
团队犹豫「它交底可信吗？」——审查后修改率下降 + 复发可见率上升直接回答。价值：**自治升级从「赌」变「有据」**（阶段 D 放行门）。

**场景 5：失败模式的代谢（半衰期 + 葬礼）**
resolved 时记录葬礼（为何曾重要、如何修、可复用性）= KnowledgeStore 输入；90 天未复发 → dormant 自然淡出。价值：**记忆会代谢——有用的沉淀、过时的淡出**（防历史知识污染）。

---

## 八、存储与召回机制

核心原则：**任务级存原始，全局级去重聚合，召回靠「写入时建指针 + 查询时全量载入」**——确定性 stdlib 机制，零向量库、零索引服务。

### 8.1 三层存储

```text
任务级原始（task_dir/）：deviation.jsonl（偏差事件）、verify_state.json（根因/解法，schema_version=2 稳定契约：failure_analysis/effective_strategy/reflexion_triggered 前向兼容 KnowledgeStore）、
  metering.jsonl（计量）、meta.json（结果 + 谦逊层聚合 + results[].problem_id 指针）、
  review.json（审查决策）、spec_snapshot.md（A4 快照）
全局聚合（~/.agent_go/problems.jsonl）：Problem 实体（跨任务去重 + 生命周期 + 复发计数）
```

分层理由（A5）：任务级保留完整原始证据（可审计），全局只存聚合后的 Problem（跨任务可查复发/趋势）。

### 8.2 三种键（召回核心，无向量检索）

| 键 | 生成 | 用途 |
|---|---|---|
| `failure_pattern` → `problem_id` | `sha256(pattern)[:12]` → `p-xxx` | 跨任务去重 |
| `result.problem_id`（任务→全局指针） | executor 录制时写回子任务结果 | review 时 O(1) 跳到全局 Problem |
| REQ/AC ID（spec→执行指针） | 模板引导 + planner 标注 | 追踪链 spec→plan→验证→交付 |

### 8.3 两种召回策略

- **写入时召回（upsert）**：`record()` 同 pattern → 计数 +1 + 复发重开（resolved→opened，保留 resolution_summary 为历史证据）。
- **查询时召回（全量载入）**：`{p.id: p for p in load(problems.jsonl)}` 内存 dict O(1) 查——problems.jsonl 是低基数小文件（去重失败模式数），全量载入成本可忽略。

### 8.4 派生 vs 持久化

| 数据 | 机制 |
|---|---|
| `dormant`（半衰期休眠） | 派生：查询时算，不写库 |
| `resolution_summary`（葬礼） | 持久：复发重开时保留为历史证据 |
| `blind_spots`/`layer_attribution` | 写入时聚合：交付门算好存 meta，review 直接读 |

原则：可推导的不存储，有叙事价值的不覆盖，高频消费的先算好。

### 8.5 容错（知识层绝不阻塞主流程）

坏行跳过、写失败静默、召回失败降级——知识层失败，主流程照跑。

---

## 九、知识库/RAG 引入判断

**结论：短期不需要（且刻意不需要）；未来 B5=c 需要的是「受控的确定性经验注入」，不是通用 RAG。**

### 9.1 为什么现在不需要

| 理由 | 说明 |
|---|---|
| 知识形态 | agent_go 知识主体是**类别标签**（failure_pattern），不是自然语言语料。类别检索的正确答案是哈希精确匹配（O(1) 零误召）；语义检索是杀鸡用牛刀，还会召回「相似但不相同」的错误经验 |
| 量级 | RAG 价值在几千~几万条非结构化文档；problems.jsonl = 去重失败模式数（几十~几百类别），全量载入内存低于任何向量库维护成本 |
| 污染风险 | roadmap 风险「历史知识污染」——知识错误直接导致交付失败。精确匹配召回「同模式」（相关性有保证）；语义检索召回「相似」（相关性是概率性的）。**宁可少召回，不要错召回** |

### 9.2 触发条件（重新评估的信号）

| 触发条件 | 含义 |
|---|---|
| problems.jsonl 跨过量级阈值 | 失败模式从几十变几万类别 |
| 知识形态质变 | 积累大量自然语言经验文本，需要语义相似召回 |
| 「解法复用率」低于「复发可见率」 | 用户看到了历史（#50 已落地）但历史解法帮不上忙——精确键匹配不够 |

### 9.3 未来形态（B5=c 议题）

不是通用 RAG，是「受控的确定性经验注入」：失败 → failure_pattern 精确匹配 → 注入 resolution_summary + effective_strategy 到 repair prompt（本质是查表不是检索）。护栏：只注入 analyzed/resolved + top 3 + 来源可溯源 + A/B 验证 ADR 不劣化。

### 9.4 过渡建议（零成本验证「知识有没有用」）

现状（#50 已落地）知识召回**给人看**——观察「用户看到历史解法后，人工修复时间是否下降」。这就是 KnowledgeStore 的「无注入 A/B 基线」：人先消费验证解法质量，数据好 → B5=c 做自动注入（查表）；数据差 → 连人都不受益，自动注入更无 ROI。
