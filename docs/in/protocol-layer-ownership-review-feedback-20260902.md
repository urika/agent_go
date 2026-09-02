# 正式反馈：《设计 Review：Protocol Layer 归属与边界 v1.1》评审意见与双端任务分解

> **反馈方**：agent_go 项目 ｜ **日期**：2026-09-02 ｜ **版本**：v1.3（追加：§八 第一阶段 AG-1/2/3 已落地，含交付清单与验证结果）
> **评审对象**：llama-defender 仓库 `docs/02-architecture-design/protocol-layer-ownership-review-20260902.md`（v1.1）
> **评审方式**：文档逐条 + 双端代码实证核对（关键断言均经 file:line 级复核）
>
> **v1.1 修订（回应对方反评）**：对方对本文 v1.0 的反评全部接受，两处事实更正已核实并纳入——① characterization tests 基线为 **124**（原引 41 仅含 decompose+orchestrator 两文件，遗漏 escalate 21 / idempotency 14 / post_governance 31 / verification_chain 17；本端实测 `pytest` 六文件 124 passed 复现确认）；② AG-3 补软依赖 LD-1。另采纳对方两点深化意见：LD-4 投入口径拆分、C2 轮次级策略的类型边界。双方对四项定版条件已达成一致。

---

## 一、总体结论

**同意"有条件通过"，但需修正条件内容后方可定版。**

- 核心架构判断（任务工程归 agent_go、输入工程归代理）**正确**，C1 证据链经本端复核成立；
- **Phase 2"整包迁移 7 模块"方案不可接受**，应改为"主干不动、组件拆解吸收"（理由见 §2.2-1）；
- 前置悬案 B1 由本端直接关闭：**agent_go 为纯 Python 项目**（stdlib-only 运行时、64 模块、`pyproject.toml` + pytest，名称与语言无关——对方反评已独立复核确认），Phase 2 是迁移路径而非重写，§5.2 条件分支可收敛为"共享契约包"。

## 二、对 Review 文档的意见

### 2.1 确认无误、无需修改

- **C1（v1.1 更正版）**：已复核 `contract_registry.py:48`（Task producer=`application_layer`）与 `:56`（SubTask producer=`P1_decompose`），更正准确；"代理无 P2 执行器"的功能性论证成立，强于错位论证。
- **M1/M2/M6**：代码锚点全部核实（`decompose.py:63` 只装箱 `input_files`、`:171` 返回 `[]`、`test_decompose.py:20` 伪路径恰好伪造真实感）。
- **M5 三约束**：v1.1 最有价值的修订，预算强制 / OOM 豁免显式决策 / append-only 约束均必要，同意全部采纳。
- **B1-B5 自省机制**：方法论上值得肯定，B5 的诚实标注比 v1.0 的断言更准确。

### 2.2 需修正

1. **【关键】Phase 2 从"整包迁移"改为"组件拆解吸收"。** agent_go 已有完整任务工程栈：`generate_plan` + `plan_to_subtasks`（≈P1）、验证循环 + `evaluator.py`（≈P3）、`replan.py` + 失败重试（≈P5）、wave scheduler（≈编排闭环）。整包迁移会在 agent_go 内部复现 C1 批判的双脑问题，且 `protocol_orchestrator` 的 P2 执行器仅为 callable stub，迁移即执行面回退。正确处置：**编排器冻结为可执行规范（每吸收一组件带走对应 characterization tests，全部吸收后归档删除）；确定性组件按消费方拉力逐个嵌入 agent_go 既有扩展点**。验收标准从"7 模块出现在 agent_go"改为"验证循环具备机械前置层、replan 具备确定性决策表、双端共享 EscalationDecision 契约"。
   - **v1.1 采纳对方补充**：LD-4（decompose 补完）投入口径拆开——契约澄清与测试先行，ScopeCriterion 实现等 AG-6 拉力确认后再动，避免给待归档代码超前投资。
2. **B1 关闭**（见 §一），Phase 0 前置验证项与 §5.2 条件式相应收敛。
3. **C2 处置软化**：LRC-P3 不宜整体撤销，应改写为**轮次级独立策略**（解除与 `escalate.py` 的耦合）。
   - **v1.1 采纳对方深化**：解除耦合后该策略与既有 LRC-P1/P2（退火+触发升级+回滚）大幅收敛，真实增量仅在**决策表形式化 + 幂等闸 + 熔断**三件；落地时定义为自有信号（triggers/cooldown/max_run）的独立策略模块，**不共享 `EscalationDecision` 类型**（该契约保持任务级语义）。相应要求：`cognitive-gap-closure-design` §3.3 从"撤销"回改为"改写"，Wave 4 恢复条目。
4. **新端点须定位为集成契约修正案**：signals / task-context / pin 实质是 R1-R12 契约的扩充（R17+ 性质），应纳入 `llama-defender-integration-requirements.md` 版本化管理，而非仅列于 §3.5 交互面。LD-8 依赖方向（LD-1/2/3 之后）双方确认合理。
5. **M5 约束①措辞**："pin 预算由存储层强制"概念越界（JSONL 存储只能记账）——改为"存储层记账 + pin 注入点（管线 stage）强制"。**双方确认连带修正**：`memory-storage-requirements-selection-20260829.md` §E 原文"上限由存储层强制"同样越界，一并改为"存储层记账 + 注入点（stage）强制"。
6. **§6 措辞**："实现质量无可挑剔"与 M1/M2 有张力，限定为"编排闭环（状态机/幂等/防御）合格，分解器能力待建"。

## 三、本项目（agent_go）任务项

| # | 任务 | 说明 | 依赖 |
|---|---|---|---|
| AG-1 | 共享契约包引入 ✅ **已落地（v1.3）** | 引入 `signal_types` / `protocol_types.EscalationDecision` 主副本 + `CONTRACT_VERSION` 漂移检测测试。交付：`agent_go/llama_contracts.py` + `tools/check_llama_contracts.py`（真实仓库实测 OK） | 无（最先启动） |
| AG-2 | **验证循环机械前置层** ✅ **已落地（v1.3）** | 吸收 verification_chain L1，注册为 `evaluator.py` `EvalStrategy` 前置策略：空/畸形 diff 在 LLM 语义评估前零成本拦截。交付：`agent_go/verify_chain.py`（MechanicalGate + ChainEvalStrategy），opt-in `evaluator.strategy="chain"` | AG-1 |
| AG-3 | **replan 确定性决策层** ✅ **已落地（v1.3）** | 吸收 escalate 决策表 + 幂等闸 + 熔断器接入 `replan.py`（`decide_escalation` + `TaskCircuitBreaker`），输出 `EscalationDecision` 并落入 `verify_results["replan"]["escalation_decision"]` 审计面。**触发信号口径已决策（§六）：agent 侧自有失败信号（verify verdict / 无进展），不依赖 LD-1** | AG-1（LD-1 软依赖已解除） |
| AG-4 | task-context 消费端 | 实现 `(task_descriptor, session_key) → context_bundle` 调用端，供 reload 重试路径使用 | LD-2 |
| AG-5 | pin 注入支持 | reload 重试请求携带 `X-Proxy-Pin-Context: <anchor>`，遵守对方预算/降级语义 | LD-3、AG-4 |
| AG-6 | decompose 判据吸收评估 | 补完后评估嵌入 `spec.py` 准入闸（LLM plan 体量预检/拆分建议） | LD-4（ScopeCriterion 部分） |
| AG-7 | post_governance 吸收评估 | EntryLayerCalibrator 与 difficulty 路由去重、PatternCompiler 与 skills/problems 体系对齐 | B5 论证完成 |
| AG-8 | 双端 E2E | 任务→分解→执行→验证失败→escalate(reload)→task-context→pin 重执行→通过；契约漂移检测 | AG-2/3/4/5 |

**明确不做**：不迁移 `protocol_orchestrator`（wave scheduler / executor / worktree 主干不动）；AG-6/AG-7 在依赖未满足前不启动。

## 四、建议对方项目（llama-defender）任务项

| # | 任务 | 说明 | 依赖 |
|---|---|---|---|
| LD-1 | `GET /api/session/<key>/signals` | IFC 四指标 + H_BE + 压缩统计快照，`SignalSnapshot` 为契约；兼作 HealthGate 观测出口 | 无 |
| LD-2 | `POST /api/task-context` | 任务描述→上下文证据包（manifest→lookup→orig 组合，预算裁剪）；留 SEM 语义卡扩展点 | 无 |
| LD-3 | `X-Proxy-Pin-Context` 头 | 压缩/截断/context_engine 跳过 pinned 锚点；落地三约束（预算记账+注入点强制、OOMSafetyFIFO 豁免显式决策、禁注历史区） | Phase 0 前置决策 |
| LD-4 | decompose 补完（**v1.1 拆口径**） | 先行：契约澄清 + M6 真实文件装箱测试；**缓行**：ScopeCriterion + 真实 `_infer_dependencies`，待 AG-6 拉力确认后启动 | 无 |
| LD-5 | LRC-P3 改写（**v1.1 更新**） | 解除 escalate 耦合，改为轮次级独立策略模块：自有信号（triggers/cooldown/max_run）、不共享 `EscalationDecision` 类型；增量限决策表形式化+幂等闸+熔断；HealthGate/LRC-P1/P2 不动；`cognitive-gap-closure-design` §3.3 回改"改写"、Wave 4 恢复条目 | 无 |
| LD-6 | 编排器冻结（**v1.1 数字更正**） | `protocol_orchestrator` 标注"参考实现/可执行规范"；整理 **124 个** characterization tests 行为基线（decompose 20 / orchestrator 21 / escalate 21 / idempotency 14 / post_governance 31 / verification_chain 17，本端实测复现），作为吸收方案的验收资产 | 无 |
| LD-7 | 文档状态同步 | AGENTS.md / CLAUDE.md / three-layer-spec：Protocol Layer 定位改为"契约基准+参考实现"，删除"Phase 2 接 pipeline"表述；连带修正 `memory-storage-requirements-selection` §E 措辞（见 §2.2-5） | 无 |
| LD-8 | 集成契约版本化 | 三个新端点写入 `llama-defender-integration-requirements.md`（R17+ 性质，含版本号与降级语义） | LD-1/2/3 |

## 五、接口与契约变更清单

### 5.1 新增接口（对方提供，本项目消费）——变更性质：纯增量

| 接口 | 方向 | 契约 |
|---|---|---|
| `GET /api/session/<key>/signals` | 代理 → agent_go | `SignalSnapshot`（`signal_types.py`） |
| `POST /api/task-context` | agent_go → 代理 | 请求：task descriptor；响应：context bundle（预算裁剪） |
| `X-Proxy-Pin-Context: <anchor>` 请求头 | agent_go → 代理 | pinned 锚点跳过压缩/截断；oom_danger 档降级语义待 LD-3 决策 |

### 5.2 契约变更

- **新增共享契约**：`SignalSnapshot`（代理生产 / agent_go 消费）、`EscalationDecision`（双端共享，**任务级语义**——轮次级策略不复用此类型，见 LD-5）；以 `CONTRACT_VERSION` 对齐 + 漂移检测。
- **复用既有标准**：`unit_model.msg_hash` 作为双端共享指纹，只读复用，无变更。
- **元数据变更（非运行时）**：`contract_registry` 血缘标注——`P1_decompose` / `P5_escalate` 标记"agent_go 外部生产"。
- **语义降级**：`recall` / `manifest` / `orig` 端点降为 admin/debug 面。经核对本项目 `diag.py` 仅消费 ledger/metrics/archive/ctx_config/props（R13-R16 面），**不消费上述端点，对本项目无影响**（对方反评已独立复核确认）。

### 5.3 明确无变更

既有 R1-R16 全部接口（控制面、路由/模型面、`X-Proxy-Route-*` / `X-Proxy-Diag-*` 归因头、诊断数据面）**零破坏、零语义变更**；新增三项为纯增量，可按 R17-R19 编号纳入集成契约。

---

## 六、执行安排（v1.1 补，2026-09-02）

不另行约定跨端阶段计划——依赖图已自然切分阶段，按"零耦合先行"执行：

**第一阶段（✅ 已于 2026-09-02 完成，agent_go 单方，与对方跑批零冲突——交付记录见 §八）**

- **AG-1 → AG-2 → AG-3**：均为只读拷贝对方类型/组件 + 本仓库内改造，不触碰对方运行时与 bench 数据口径。
- **AG-3 触发信号口径决策**：采用 **agent 侧自有失败信号**（verify verdict / 无进展）重定义触发条件，消除对 LD-1 的软依赖，本阶段即可闭环；LD-1 落地后再评估是否增补 `reread_pressure` 等 IFC 信号作为决策表输入。

**第二阶段（对方跑批结束后，接口耦合部分）**

- AG-4 / AG-5 待 LD-2 / LD-3 落地后启动；AG-8 双端 E2E 最后执行。
- 跑批期间不向对方提出任何影响运行时的改动请求（LD 系列全部后置）。

**contract-first 约定（已双端确认，条款见 §七）。** 对方实施 LD-1 / LD-2 前，先将端点契约草案写入 `llama-defender-integration-requirements.md`，本端评审冻结后双端各自动工——AG-4 对着冻结契约开发，不等端点实现、不返工。

---

**定版条件汇总**：① §2.2-1（Phase 2 改为拆解吸收）写入文档；② B1 标记已验证并收敛条件分支；③ C2 处置改为"改写轮次级独立策略"（含 v1.1 类型边界约束）；④ 新端点纳入集成契约版本化（LD-8）。**对方反评已确认四项条件全部接受，双方达成一致**；v1.1 的两处事实更正（124 测试基线、AG-3 软依赖 LD-1）不影响定版条件，仅作执行口径修正。

---

## 七、补充约定确认（2026-09-02 双端确认）：contract-first

> §六中唯一需要实施前约定的项已获对方确认，条款如下。其余按本反馈 §三/§四分工执行，无需额外协商。

- LD-1/LD-2 实施前，llama-defender 先将端点契约草案写入 `llama-defender-integration-requirements.md`（R17 `GET /api/session/<key>/signals` 的 `SignalSnapshot` 字段表、R18 `POST /api/task-context` 请求/响应结构、R19 pin 头语义，标注"草案态"），**agent_go 评审确认冻结后方可动工**。
- 冻结后 AG-4/AG-5 直接对着冻结契约并行开发，不等端点实现、不返工；实现期若需字段变更，走 `CONTRACT_VERSION` 递增 + 双端漂移检测测试同步，不做静默变更。

---

## 八、第一阶段落地记录（2026-09-02，agent_go 侧）

§六第一阶段（AG-1 → AG-2 → AG-3）已于当日完成落地，与对方跑批零冲突（全部为本仓库内改动 + 只读引用对方类型文件）。

### 8.1 交付清单

| 任务 | 新增/改动 | 测试 |
|---|---|---|
| AG-1 | `agent_go/llama_contracts.py`（SignalSnapshot + EscalationDecision，CONTRACT_VERSION=1，叶子模块）；`tools/check_llama_contracts.py`（AST 级版本+签名漂移检测，对本机真实仓库实测 `OK: 契约一致`） | `tests/test_llama_contracts.py` 10 用例（含 hermetic 假仓库漂移用例 + 真实仓库 skipif 用例） |
| AG-2 | `agent_go/verify_chain.py`（MechanicalGate 空/畸形 diff 零成本短路 + ChainEvalStrategy 委托默认 LLM 策略）；`evaluator.py` 注册 `chain` 策略（opt-in `evaluator.strategy="chain"`） | `tests/test_verify_chain.py` 11 用例（含"拦截时不得发起 LLM 调用"、`_pre_work_head` diff base 优先） |
| AG-3 | `replan.py` 新增 `TaskCircuitBreaker` + `decide_escalation`（决策表与上游同构：熔断→human / 幂等闸→human / 无进展→split / 默认 retry）；`executor.py` 接线（决策层先行于拆分修复，`escalation_decision` 日志事件 + 落 `verify_results["replan"]["escalation_decision"]` 审计面） | `tests/test_replan_escalation.py` 15 用例 + `test_replan.py` 新增 2 个接线级用例（decision 落记录 / 非 split 拦截 `skipped_human`） |

### 8.2 验证结果

- 全量测试 **2863 passed / 115 文件**（非集成 2817 + 集成 38 + 契约/链式/决策层新增），零回归；
- ruff（CI 口径 E/F/W）与 mypy（py39）：改动文件全部干净；
- AGENTS.md 已同步（模块表新增 `verify_chain.py` / `llama_contracts.py` 行、replan/evaluator 行更新、69 模块 / 2863 测试 / tools 清单）。

### 8.3 适配取舍与行为变化（如实记录）

- **AG-2 适配差异**：上游 citation_existence 不吸收（agent_go 无引用锚概念）；L3 台账验证待 R17 signals 端点落地后评估；"编译错/测试红"不重复实现（agent_go 的 shell 验证本就在语义评估前执行，已覆盖）——本层补的是 shell 验证空转通过（heuristic/manual 验证）时的确定性兜底；topic_relevance 降级为 advisory（中文描述按 `\W+` 分词易误判，只记日志不拦截）。
- **AG-3 行为变化（边界）**：幂等闸 `attempt >= max_retries → human` 意味着无进展信号若在**最后一次重试**才出现，replan 将被拦截（`skipped_human`），不再生成拆分建议——符合决策表终态语义，既有测试未覆盖该边界且不受影响。
- **AG-3 动作词汇**：当前子集 `retry / split / human`；`reload` 待 AG-4/AG-5（task-context + pin）落地后启用；`route_cloud` 为代理层概念，agent_go 的模型降档走 router/degrade 既有通道。

### 8.4 当前待办

- 等待对方：R17-R19 契约草案（§七 contract-first）→ 本端评审冻结后并行开发 AG-4/AG-5；
- AG-6/AG-7 维持门控（分别待 LD-4 ScopeCriterion 与 B5 论证）；
- AG-3 增补 IFC 信号输入（`reread_pressure` 等）的评估，待 LD-1 落地后进行。
