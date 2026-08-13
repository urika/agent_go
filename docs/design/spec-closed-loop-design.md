# SDD Spec 闭环设计

> **版本**：v1.0
> **日期**：2026-08-13
> **文档目的**：指导 SDD（Spec-Driven Development）的 spec 闭环落地。汇总 spec 的介入点、映射机制、复杂架构下的拆解、架构输入通道、一致性边界与落地路径，作为阶段 B/C 实施的依据。
> **关联文档**：
> - [`sdd-references-and-frameworks.md`](sdd-references-and-frameworks.md) — SDD 学术框架与 5 级模型
> - [`agent-go-input-spec.md`](agent-go-input-spec.md) — agent_go 输入契约（Task Spec 7 章节）
> - [`business-architecture.md`](business-architecture.md) — A/B 类决策登记（本设计的决策依据）
> - [`roadmap.md`](../roadmap.md) — 阶段 B/C 计划

---

## 一、目标与定位

### 1.1 spec 闭环的最终目标

把 spec 从「一次性门禁」升级为「贯穿执行全程的锚」，支撑 agent_go 从 SDD L2（Spec-First）推进到 L3（Spec-Anchored），最终服务「全自主交付（渐进自治）」的北极星。

```text
L2 Spec-First（当前）：spec 先于代码，用完即弃
L3 Spec-Anchored（目标）：spec 贯穿执行，锚定每个环节
L5 Spec-as-Source（不排期）：spec 即唯一真相源，仅安全域试点
```

北极星意义：全自主交付的核心瓶颈是「信任」。spec 把「交付对不对」从「人肉判断」变成「机器可验证的契约」（spec 每条验收达成 + 可追踪），从而把人工介入收敛到三个例外点（Plan 确认 / merge 决策 / 失败审查）。

### 1.2 spec 与需求的关系

```text
Phase 0 需求（模糊意图）
  → Phase 1 设计（方案 + 决策）
  → Phase 2 任务 Spec（可执行契约）  ← agent_go 的输入
  → Phase 3-4 执行 + 交付           ← agent_go 的地盘
```

- spec 是「需求的可执行形态」：需求（要什么）＋ 范围边界 + 约束 + 验收标准（怎么算做完）。
- agent_go 不产出「需求 → spec」的转化（Phase 0-2 是人的事），只消费 spec → 交付。
- 需求质量决定 spec 质量（P9：提高自治度的最高杠杆是 Spec 质量）。

---

## 二、spec 闭环现状盘点（8 环节）

| # | 环节 | 做什么 | 代码 | 状态 |
|---|---|---|---|---|
| ① | 编写/模板 | 生成 7 章节空白模板 | `render_spec_template`（`agent_go spec template`） | ✅ |
| ② | 解析 | 解析 Spec 成 TaskSpec 结构 | `parse_spec` / `TaskSpec` | ✅ |
| ③ | L1 门禁 | 准入审查（必填章节/文件路径/命令白名单） | `validate_spec_l1`（`agent_go spec validate`） | ✅ |
| ④ | 注入 Plan | spec 约束注入 planner prompt | `_build_spec_context` + `generate_plan(spec_context=...)` | ✅ |
| ⑤ | AST 冲突检测 | 符号级检测「两个 step 改同一函数」 | `detect_step_conflicts` | ✅ |
| ⑥ | 执行追踪 | requirement → subtask → verification → delivery | `governance.traceability_matrix` | ⚠️ 独立命令，run 不自动触发 |
| ⑦ | 偏差回流 | spec/architecture/acceptance 偏差记录 | `deviation.py` | ⚠️ 记录但需人工查 |
| ⑧ | goal 回溯 | completed 回看 goal/acceptance/overview | pipeline.py completed 判定 | 🔄 进行中（M4） |

**断裂位置**：spec 在「执行前」（①-⑤）密集介入，但「执行中/后」（⑥-⑧）断裂——执行、验证、交付不再回头看 spec。这正是缺口 4「spec 闭环断裂」。

**缺的两块**（B3 最小冒烟的增量）：
- spec 快照/持久化（A4 决策，未落地）—— 任务启动时拷贝 SPEC.md，保证任务可复现。
- §5 结构化（A2 决策，未落地）—— 验收标准写成 checkbox + 反引号命令，可勾选、可提取。

---

## 三、spec 介入点设计

### 3.1 前段介入（执行前，硬门禁，已实现）

| # | 介入方式 | 说明 |
|---|---|---|
| 1 | 模板生成 | `spec template` |
| 2 | L1 门禁 | `--force` 可跳过 |
| 3 | 任务描述覆盖 | §1 目标 → task |
| 4 | 预算覆盖 | `budget:` → L3 上限 |
| 5 | 类型路由 | `task_type:` → 难度/模型 |
| 6 | 参考资料注入 | §6 → reference_docs |
| 7 | Plan 注入 | §3/§4/§5/§7 → planner prompt |
| 8 | AST 冲突检测 | Plan 确认后 |

### 3.2 后段介入（执行中/后，观测为主）

**铁律（A1 + IV-2）**：后段 spec 介入是「观测 + 标记 + 可查」，不是「硬阻断」——执行前才是硬门禁，执行后 spec 只做正交观测，不改变 verification 决定 status 的语义。

| 环节 | 介入方式 | 强度 |
|---|---|---|
| 执行期（subtask） | TASK.md 追加「Spec 约束」段，按 files 范围过滤注入 §3 范围 + §5 验收 | 观测 |
| 验证期 | 用 `extract_verification_scopes` 提取 §5 命令，记录「本子任务覆盖哪些 AC」，缺失标记 uncovered | 观测 |
| 追踪（governance） | 交付门前自动跑 traceability，断链标记 `traceability_incomplete` | 观测 |
| 偏差（deviation） | 交付门汇总 deviation.jsonl，`requires_approval=True` 阻断/要求人工 | 硬信号（唯一） |
| goal 回溯（M4） | completed 附 spec 合规度分（正交维度，不改 status） | 观测 |
| 交付（PR/merge） | 交付门汇总合规信号（追踪+偏差+验收覆盖），警告可查不硬阻断 | 观测+汇总 |

**介入强度分层**：

```text
硬门禁（确定性，执行前阻断）：L1 门禁 / AST 冲突检测        ← 已有
观测+标记（正交，不阻断）：执行注入 / 验证覆盖 / 追踪        ← 要补
硬信号（偏差 requires_approval）：阻断交付                   ← 要补
观测+汇总（交付门汇总）：合规度 / 交付门                     ← 要补（合规度进行中）
```

**为什么后段「观测」而非「硬阻断」**（A1 理由）：「执行都过但漏验收」和「执行失败但失败恰不在 spec 范围」都是合法状态。后段硬阻断会把「合法的不完整 spec」误判为失败，并破坏「verification 决定 status」的核心语义。后段 spec 的职责是「让偏差可见、可查、可决策」，决策留给人或 eval gate。

---

## 四、spec → 后段的映射机制

### 4.1 映射骨架：稳定 ID 贯穿全程

```text
spec §1 目标 → REQ-001（需求 ID）
spec §5 验收 → AC-001（验收 ID）

planner 生成 step 时标注（api.py:231-232）：
  requirement_ids: ["REQ-001"]
  acceptance_criteria_ids: ["AC-001"]
  files / verification

→ 「AC-001 ↔ step ↔ 文件 ↔ 验证命令」串起来
→ 追踪期 build_traceability_matrix 拼出 requirement → subtask → verification → delivery
→ 回溯期（M4）检查每个 AC 是否覆盖 + 验证通过
```

ID 归一化：`governance._canonical_id` 支持 REQ-001 / AC-001 / req1 / ac2 等写法，编号补零到 3 位。

### 4.2 映射分两层

**软映射（现有，靠 planner LLM）**：`_build_spec_context` 把 spec 变文本注入 planner，planner 语义理解后落到 step 的 files/verification/requirement_ids。

- 问题：不可靠（漏标 ID / 错标 / 不标）。

**硬映射（要补，靠 §5 结构化）**：§5 验收写成 checkbox + 反引号命令，AC ↔ 验证命令 ↔ 文件的绑定变确定性：

```markdown
## 5. 验收标准
- [ ] AC-001 登录返回 JWT：`pytest tests/test_login.py::test_login`
- [ ] AC-002 密码哈希：`pytest tests/test_auth.py::test_password_hash`
```

正则提取反引号命令（A2 的 `extract_verification_scopes` 已在做）→ 命令锚定到所在 AC 行 → 「AC-001 → 命令」确定性成立，不依赖 planner 猜。

**结论**：映射骨架（ID）+ 软映射（planner）已在，缺硬映射（§5 结构化）。§5 结构化是「软映射不可靠」的唯一解，也是 goal 回溯（M4）能做「确定性覆盖检查」的前提。

---

## 五、复杂架构下的映射拆解

### 5.1 三个拆解维度

```text
① 模块维度（横向）：auth / user / payment / ...      ← 功能模块
② 层级维度（纵向）：UI / API / service / data / ...   ← 技术层
③ 契约维度（接口）：模块间接口 = 边界               ← 模块怎么拼
```

### 5.2 结构化 ID 范式（升级扁平 REQ/AC）

```text
扁平（现状）：REQ-001, AC-001
多维（要升级）：
  REQ-{module}-{num}         REQ-auth-001
  AC-{module}-{layer}-{num}  AC-auth-api-001
  或树形：REQ-001 → REQ-001-1（API）/ REQ-001-2（服务）/ REQ-001-3（数据）
```

ID 里编码「哪个模块、哪一层」，追踪/回溯不再猜，ID 本身说了。

### 5.3 验证分层映射

| 验证层 | 验证什么 | AC 锚定到 |
|---|---|---|
| 单元测试 | 模块内部逻辑 | 单个模块（AC-auth-service-001） |
| 集成测试 | 模块间契约 | 契约（AC-contract-login-001） |
| 端到端 | 整体链路 | REQ 根 |

这是 A2（验证锚定）在复杂架构下的延伸——锚定粒度从「函数级」扩展为「模块 × 层级 × 契约」。

### 5.4 契约 = 复杂架构映射的核心

spec §4（或新增 §4a 接口契约）显式列出模块间接口：

```text
- auth 服务对外接口：POST /login → { jwt }
- 前端依赖：POST /login（auth 服务）
- 契约验收：pytest tests/test_contract_login.py
```

契约让「模块 A 改接口 → 模块 B 受影响」的依赖显式化，进而映射到集成测试、跨模块回归。

### 5.5 边界克制（IV-5）

```text
需求分解（REQ 树怎么拆）     → Phase 1 设计的事，agent_go 不做
模块契约怎么定               → Phase 1 设计的事，agent_go 不做
接受已结构化的 spec（多维 ID + 契约）→ agent_go 做
按结构映射 + 分层验证 + 追踪回溯      → agent_go 做
```

**agent_go 不发明需求分解树，但要能「消费」已经分解好的多维 spec**——否则滑向「需求管理工具」，违反 IV-5。当前 B3 最小冒烟先做「扁平 + §5 结构化」打地基，多维映射随复杂任务逐步长出。

---

## 六、架构输入的通道

架构输入是三种不同性质的东西，分三条通道，别混：

| 通道 | 位置 | 性质 | 代码 |
|---|---|---|---|
| spec §4 约束 | spec 内 | 人已提炼的**硬约束**（必须遵守） | `_build_spec_context` → planner |
| reference_docs | 上下文 | **原始架构文档**（供理解） | `--docs` + §6 → `read_reference_docs` |
| architecture_review | 后置 | 独立 agent 审查 plan 是否偏离架构 | cli.py:1003，默认 fail-open |

**约束 ≠ 文档**：约束（短、结构化、可门禁）走 spec §4；文档（长、自由形式、供理解）走 reference_docs。不能都塞 spec（会让 spec 失去结构性），也不能只藏在文档（不可校验）。

**架构硬约束的校验建议**：spec §4 硬约束做确定性校验（fail-close）；architecture_review 保持 fail-open（LLM 语义审查误杀率高）。硬约束不靠 LLM 猜，软审查不硬阻断。

---

## 七、架构一致性与 spec 驱动的边界

### 7.1 「一致」是五种

| 一致类型 | 含义 | 无显式架构约束时靠什么 |
|---|---|---|
| 接口一致 | 模块间契约不破 | 验证（typecheck / 集成测试） |
| 行为一致 | 功能不回归 | 测试门禁 |
| 语义一致 | 命名概念统一 | 现有代码模式 + planner 对齐 |
| 风格一致 | 代码约定 | lint |
| 架构一致 | 分层、依赖方向正确 | 最难 |

### 7.2 架构约束的两种存在形式

```text
显式架构约束：spec §4 / 接口契约 / 架构文档      ← 人写出来的
隐式架构约束：现有代码结构（架构的编码）+ 验证命令（架构的守护）
```

**关键洞察**：增量开发场景下，spec 驱动**不需要显式架构约束**——架构已「编码」在现有代码里，spec §3 范围（"动 src/auth.py，不动 src/payment.py"）就是对现有架构的引用；验证命令是架构的守护。spec 驱动 = 「在现有架构约束下做增量」+「用验证守护不破坏架构」。

### 7.3 一致性的上限

```text
增量开发（架构在代码里）   → 隐式架构够用 ✅
绿地/大重构（架构不在代码里）→ 无显式约束时靠 planner 常识 + 审查 + 验证反馈，
                              一致性靠运气，上限低 ❌
```

**结论**：spec 驱动的系统一致性 = 架构信息被「显式化或编码化」的程度。spec 不「生成」架构，它「引用并守住」架构——架构要么在代码（隐式）、要么在 spec 约束（显式）、要么靠审查（后置）；三者皆无，一致性无从谈起。这正是 L2 → L3 的逻辑：L3 把架构约束显式化，让一致性从「靠运气」变「可门禁」。

---

## 八、架构方案/决策/约束的传递

三种架构知识，三种载体：

```text
架构方案（solution）  = 整体设计，分层/模块怎么拼     → 代码结构（隐式）+ 设计文档（reference_docs）
架构决策（decision）  = 为什么这么选，权衡了什么       → ADR（决策记录，含 rationale）
架构约束（constraint）= 必须遵守的规则（方案/决策的落地）→ spec §4
```

- spec 约束能传递「约束」（执行 agent 需要的全部），不能也不需传递「方案全貌」和「决策 rationale」——方案已在代码里，rationale 对执行是噪音。
- **真正风险**：方案/决策没被「翻译成约束」。翻译（方案/决策 → 约束）是 Phase 1 设计者的责任，缺了它 spec 驱动的架构一致性就断。
- ADR 与约束配套：ADR 记录「决策 + rationale」（给人看的「为什么」），spec §4 承载「翻译成的约束」（给 agent 看的「怎么做」）。只有 ADR 没约束 → agent 无从遵守；只有约束没 ADR → 将来改架构不知「当时为什么」。

---

## 九、落地路径（B3）

### 9.1 路径：补齐 → 接 M4 → 冒烟校准 → 迭代加码

```text
第 1 步 补齐闭环物理链路（<1 天，确定性）：
  ① spec 快照（A4）② §5 结构化（A2）③ goal 回溯（M4，接并发进程）
第 2 步 5 任务冒烟（1 天，实验性）：
  跑完整链路，测 4 指标，校准投入力度
第 3 步 按冒烟结果迭代（持续）：
  C1 填写成本高 → 优化模板；R2 追踪低 → 优化 requirement 提取
第 4 步 推进 L2 → L3（长期）：
  spec 成为非平凡任务默认输入，支撑交付可机器验证
```

三个原则：复用不重建（6/8 已落地，只补缺口）；先确定性后实验性（快照+§5 无风险先做，冒烟有不确定后做）；用数据校准投入（肯定做，冒烟作用是「决定投多少」而非「决定做不做」）。

### 9.2 ROI 门禁（冒烟判据）

对照组：M3 的 12 任务（无 spec，ADR 91.7%，追踪完整率≈0）作基线；实验组 5 任务填 spec。

| 指标 | 测量 | 阈值 |
|---|---|---|
| C1 填写成本 | 空模板到 validate 通过耗时 | 中位数 ≤ 15 分钟 |
| R1 交付成功率 | 5 任务 ADR vs 基线 91.7% | ≥ 基线（不劣化） |
| R2 追踪完整率 | requirement 可追踪到测试+交付比例 | ≥ 90%（基线≈0） |
| R3 Plan 编辑次数 | 每任务确认时编辑/重生成次数 | ≤ 基线 |

四选一结果：

| 结果 | 判据 | 动作 |
|---|---|---|
| 强正 ROI | 4 项全过 | 做 M2/M4 全套，spec 进常规 |
| 弱正 ROI | R2 达标、R1/R3 持平 | 留门禁+追踪轻量版，不重投 |
| 中性 | 无显著差异 | 冻结，留 --spec 入口，等真实需求 |
| 负 ROI | C1 超标或 R1 劣化 | 砍增量投入，资源转 B5=c |

原则：先定判据再跑实验；底线「不劣化」；R2 是 spec 独特价值核心指标；弱正 ROI 是诚实结论。

---

## 十、决策索引（对应 business-architecture.md）

| 决策 | 内容 | 状态 |
|---|---|---|
| A1 | goal 达成语义纯观测（converge 不改 status） | ✅ |
| A2 | §5 结构化 = checkbox + 反引号命令 | ✅（未落地） |
| A3 | converge 判定确定性优先（AST） | ✅ |
| A4 | spec 快照（任务启动拷贝） | ✅（未落地） |
| A5 | 问题实体存全局 problems.jsonl | ✅ |
| A6 | issue 自动创建默认关 | ✅ |
| B1 | merge 策略（未分叉 ff / 分叉 merge commit / 冲突人工） | ✅ |
| B2 | 定位：交付工具为主 | ✅ |
| B3 | spec ROI：肯定做，走补齐→接M4→冒烟校准→迭代加码 | 本设计，待登记 |
| B4 | 问题跟踪：聚合优先 + 最小状态机 | ✅ |
| B5 | 循环智能 b（最小止血+收口） | ✅ |

---

## 十一、执行不变量（红线）

1. 后段 spec 介入是「观测 + 标记 + 可查」，不硬阻断状态（A1/IV-2）。
2. 硬门禁只在前段（L1 门禁 / AST 冲突检测）和后段偏差 requires_approval。
3. agent_go 不产出「需求 → spec」转化，不发明需求分解树（IV-5）。
4. 架构约束走 spec §4（硬），架构文档走 reference_docs（软），合规审查走 architecture_review（后置）。
5. 所有新字段可空、新实体可选、新流程可跳过——无 spec 的任务行为完全不变（IV-3）。

---

## 十二、现状 Review（2026-08-13）

对照本设计逐项核对实现，发现「输入侧」扎实、「闭环侧」三处硬断裂，按严重度分级。

### 总体结论

spec 的输入侧（解析/门禁/AST 冲突检测）扎实，但闭环侧有硬断裂，最关键的是 **REQ/AC ID 链条在源头就断了**——这让追踪/回溯机制（R2 追踪完整率）实际「无米下锅」。

### P0（硬断裂，直接导致闭环失效）

**1. REQ/AC ID 链条在源头断裂**

- plan prompt 要求 planner「*when the Spec provides stable IDs*」输出 `requirement_ids`/`acceptance_criteria_ids`（api.py:258）。
- 但 spec 模板 §5 无 REQ-xxx / AC-xxx 引导（spec.py:488-492）。
- `_build_spec_context`（cli.py:394-408）只传 scope/constraint/acceptance/risk 纯文本，不提取不传 ID。
- 结果：spec 从不「provide stable IDs」→ planner 几乎不输出 ID → traceability_matrix 无米下锅 → R2≈0 是结构性的。

修复（最便宜、最高杠杆）：① 模板 §1/§5 加 ID 引导；② `_build_spec_context` 用 `extract_spec_requirements` 提取 ID 注入 planner；③ `validate_spec_l1` 加「§5 每条验收带 AC ID」检查。

**2. `extract_verification_scopes` 是孤儿 API**

A2 在 spec.py 加的 `extract_verification_scopes`（has_anchored/has_function_level）零调用点。真正被消费的 A2 能力是 planning.py `verification_not_anchored` + executor `anchoring` 字段（直接调 `classify_verification_scope`），但 spec 侧函数未接线：`validate_spec_l1` 没用它做 §5 锚定检查。修复：把 `has_anchored` 接进 L1 门禁（warning 不阻断）。

### P1（缺口，阻断闭环后半段）

**3. `validate_spec_l1` 缺两项检查**：当前只查 required/length/path/whitelist，缺 §5 锚定检查（A2 未接入）与 §5 结构化检查（A2 未落地）。

**4. spec 快照（A4）未实现**：`grep spec_snapshot/SPEC.md 拷贝` 零命中，任务不可复现。

**5. 后段介入全空**（对照 §3.2）：执行期 `_build_task_md` 无 spec 约束段；验证期不记录 AC 覆盖；追踪是独立命令 run 不自动触发；交付门不汇总合规信号。

**6. goal 回溯（M4）进行中**：pipeline.py:898 仍是否定式 `"failed" if has_failed else "completed"`。

### P2（质量/健壮性）

**7. 模板错别字「醇收标准」**：spec.py:488 错别字，靠 render_spec_template 的 `.replace`（spec.py:544）打补丁，脆弱。

**8. 架构硬约束无确定性校验**：§4 硬约束靠 planner 自觉 + architecture_review（默认 fail-open）后置兜底，缺确定性校验（「明确不动的文件」被改动应确定性命中）。

### 建议实施顺序（对齐 §9）

```text
第 1 步（P0，最高杠杆，~1 天）：ID 链条接通 + extract_verification_scopes 接进 L1
第 2 步（P1，补齐物理链路，~1 天）：spec 快照 + §5 结构化 + 后段注入/追踪/交付门
第 3 步（P1，接 M4）：goal 回溯收口
第 4 步（P2，顺手）：模板错别字 + §4 确定性校验
```

**关键判断**：P0 两项是「闭环失效根因」，且便宜、确定性；P1 是「补齐物理链路」；P2 是清理。**必须 ID 链条先通，否则后面做再多追踪/回溯都是空转**。
