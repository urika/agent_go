# jev 失败子任务复核排序试点需求（agent_go）

> 状态：**已冻结 v1.1；v1.2–v1.7 为登记更新（H3 回路／规则集管线／S-1／O-14 P0／S-1 P0 结论／仪器补齐与命名统一，2026-10-05）**——冻结测量面（题面／判据／阈值／规模／哈希）**未变**，不构成重开一轮。冻结后按 §17.5 推进。
> 日期：2026-10-05 ｜ 适用仓：agent_go ｜ v0.2＝PM 评审修订，v0.3＝增 §15 架构落位与降级，v0.4＝协议修订（靶根因口径／rank 预注册／规模口径），v0.5＝题面工程（英文结构化 criteria）＋阈值弃权通道，v0.6＝增 §16 M0 冻结清单与签署，v0.7＝增 §9.4 数据闭环与规则收敛登记，v0.8＝增 §7 预标注与辅助真值协议，v0.9＝增 §17 初步设计与推进计划＋ADR-013 提案，**v1.0＝M0 五项签署完成＋冻结（F10 数据纠正：批配置对齐 `delivery-20260820`）**（见文末变更记录）
> 上位依据（冲突时以其为准）：
> - swe-eval `docs/jev-paradigm-20260926.md`（用法权威 v0.3：三型契约/边界/纪律）
> - swe-eval `docs/jev-call-card-20260927.md`（操作卡：三情境/四条不可越）
> - swe-eval `docs/jev-efficacy-preregistration-20261004.md`（JEP-1 v0.10：测什么、判据、成本模型）
> - swe-eval `docs/jev-verification-requirements-20261004.md`（JVR-1 v0.1：R-J1…R-J6 验收门）
>
> 来源分级：`[实测]` 本仓/关联仓实测证据 ｜ `[分析]` 推演 ｜ `[需求]` 本件规定

## 0. 结论先行

- **选定的试点**＝**失败子任务的离线复核队列排序**：对一批冻结的失败子任务，用 jev 产出"人工复核优先级"排序＋诚实弃权集，对照现状默认序与一条粗规则，量出"是否省人审"。
- **形态**＝离线、**B 情境（人闸门，逐条组装/确认 state）**、**软排序**（禁 top-k 截断）；不进任何运行时决策路径、不进 verdict/AC。
- **与 agent_go 的关系**＝只读本地任务产物（`~/.agent_go/task-*`），在运行时**之外**新落一个 dev 工具与流程；不改 `pipeline/executor/subtask`、不改 `meta.json` 契约、不改 Web/看板。
- **两阶段投入（v0.2）**：先做 **M0.5 薄预试**（小批、手工/半自动，只验 4 件事），过了才投 **M1 正式仪器**——先花小钱买"该不该建"的答案。
- **消费面（v0.2）**：排序的落地物＝本地 `review-queue.md`（含 rank→task/subtask 映射，仅本地、不外发），操作者照单复核；**采纳＝操作者实际按队列复核并留痕**，这是 Go 判据可达成的前提。
- **成本口径（v0.2）**：全口径＝冻结批运行费＋人工标注/复核工时＋jev 调用费；M4 报告必须给出"每批节省人时"换算与不确定度，否则不构成引入依据。
- **落位与降级（v0.3）**：仪器留**包外 dev 工具**（不上收产品包，升格条件见 §15.2）；降级为单向阶梯 jev→粗规则序→默认序→纯人工，**禁止横向换 LLM judge**（§15.4）。
- **协议修订（v0.4）**：靶改**根因口径**（代码正确而验证器判错＝环境流程型）；**rank 构造与"真目标"预注册**（§6.1/§9.1）；规模与判据对齐——Go 需 **n≥30**（预算约 85 次执行），n=20–29 只出 Conditional；H6 增基率检查、κ 在小样本降为 sanity；增**时间盒**（§11.1）。
- **题面工程与弃权通道（v0.5，依据官方 cookbook）**：questions/criteria **英文结构化冻结**（`what`/`not_for`/`examples`）；弃权**双通道**（模型自选＋`P_max<τ` 低置信，τ 建议 0.60）；Go 后按"规则先行＋jev 补模糊区"**级联**落地。
- **M0 冻结包（v0.6）**：§16 汇总全部冻结项（F1–F14）与角色签署——**可定的建议值已定格，剩下只有四项需要人/组织决定**（suite 选型、owner 认领、消费方具名、ADR 立项）；签署即冻结、进 M0.5。
- **数据闭环与规则收敛登记（v0.7）**：闭环三件（回包 `model` 版本、复核 `outcome` 回写、跨轮 `labels.jsonl`）已定格为仪器要求（F14）；**从 jev 决策收敛到规则**（G1–G7 缺口清单）登记为 §9.4＋O-12，**不在本轮冻结范围**，Go/Conditional 后另立。
- **标注成本压缩（v0.8）**：三层协议＝**程序化探针（干净环境复跑验证命令）＋LLM 预标注（本地模型优先）＋人工终审**；**真值仍是人**，预标/探针只降单位成本、永不覆盖人工标签；人工 7–16 → **4.5–10 人时**（§7/§10.1）。
- **M0 材料已备（v0.9）**：suite 建议＝**decision**（29 个互异任务、fixture 全在、失败率/成本实测）；[ADR-013](adr/ADR-013-jev-offline-triage-egress.md)（Proposed）已起草；§17 含初步设计、工作量与"M0→M0.5 开跑手册"。
- **已冻结（v1.0，2026-10-05）**：M0 五项签署完成（owner／消费方＝jinsongwang；PM 确认 τ/H1/ROI/时间盒；suite＝decision × `delivery-20260820` 口径；ADR-013 → Accepted，见 §16.3）。**下一步**＝薄版工具实现 → 预试批（decision × repeat 1，29 次，≈$0.3）→ M0.5 四件事门。
- **题面重冻（v1.1）**：按官方 cookbook batching（同 state 多问题、答案互不影响、新题不重复发送 state），落地 Q3 `ranking_noul` 第二排序器（官方 rerank 配方形态）——**仅探索对照、不进判据**；Q1–Q3 同调用发送（需求评审 ①+③ 形态）。
- **规则集管线登记（v1.3）**：§18＋[概念设计](rule-set-pipeline-design-20261005.md)——把"从 jev 决策收敛到规则"落成可执行管线（生成→人审冻结→影子→生效），以**运行中的 swe-eval tdd 臂实测**（tdd 7/10 vs nudge 2/10 vs plain 1/10、配对零负）为范式背书；立项＝O-14（Go/Conditional 后与 O-12 同批）。
- **S-1 登记（v1.4）**：spec 覆盖扫描 → TDD 输入——覆盖/证据完整度前瞻可测、有效性事后回填、格位风险借留出表；缺口映射为 TDD 靶（J-only 必测/双缺不进开发/R-only 回归钉住）驱动 ADR-012 起草；立项＝O-15；概念设计 §10。
- **成功标准不是"证明 jev 好用"**，而是按预注册判据如实产出 **Go／Conditional／No-Go／不可判** 之一＋弃权集画像（No-Go 也是有效产出，参照 JEP-1 §16）。
- 上游实证决定了本试点的最可能形态：**证据充分时 jev ≡ 一条粗规则**（JEP-1 §15），其独立价值在"诚实弃权"；唯一出现正增量的靶是**内容型靶**（JEP-1 §17）。本试点的靶设计据此对齐（§7）。

**术语表（对 agent_go 读者的最小词典）**

| 术语 | 含义 |
|---|---|
| `effort@recall(r)` | 覆盖 r 比例**真目标**（＝人工标注的 content_fix，见 §9.1）所需的复核条数（越小越好；主指标） |
| H6（阳性对照） | 用"已知答案写在 state 里的问题"检验仪器能否读到 state；不过即整轮作废 |
| R-J1…R-J6 | JVR-1 的六级验收门：读数保真/复现可审/靶合规/效能/落点安全/成本 |
| `insufficient_evidence` | jev 的弃权输出（"按当前证据判不了"）；本试点将其作为一等观察对象 |
| B 情境 / CON-1 | 仅人组装并确认 state 的调用情境 / 使用半径硬约束：只作分流排序，不进 verdict |

---

## 1. 背景与依据

### 1.1 agent_go 现状（[实测]）

| 事实 | 证据 | 对本试点的含义 |
|---|---|---|
| 验证层误判是已知的一等问题族 | ISSUE-29（验证命令语法错误→正确代码判死）、ISSUE-31（沙箱环境不一致→155 个通过的测试被判失败）、ISSUE-40（0 子任务真空 DELIVERY_READY 假成功）、ISSUE-51（diff base 污染语义判定） | 复核"失败到底是不是真失败"有真实价值；该族**横跨内容型与环境流程型**，本试点只测内容型一侧（价值边界见 §1.3） |
| 现有复核入口是"清单/顺序"，没有"排序" | `agent_go review --task`（聚合裁决）、`agent_go inspect`（保留的 worktree）；机器信号 `verification_confidence`／`blind_spots`／`goal_adherence.needs_human_review` 逐条标记而非队列排序 | jev 的落点是**排序器**，不是判定器；默认序＝现状清单顺序 |
| 失败产物默认保留，但有清理风险 | failed/blocked 子任务 worktree 默认保留（`pipeline.py:938-952`＋`.preserved` 标记）；`agent_go clean` 会删任务目录；历史 bench 494 条 failed 记录中 task_dir 仅 **3 条**存活（2026-10-05 实测） | 池必须"现跑现冻＋证据快照"，不能依赖历史 run |
| 失败样本量足够但需现产 | 历史 bench 1699 条记录 / **494 条 failed（≈29%）**，每条失败记录平均 **1.18 条** failed 子任务；failure_class 分布：verification_failure 208、None 147、timeout 90、infra 22、system_error 14、model_failure 13 | 池规模可按此换算（§4）；批成本按历史单价换算（§10.1） |
| 密钥继承链存在敞口 | `executor._build_sandbox_env()` 对验证命令环境**已脱敏**（API_KEY/SECRET/TOKEN 等关键词剔除）；worker 子进程环境**未脱敏**（`executor.py` worker env 构造处 `env = os.environ.copy()` → `subtask.py` 的 `Popen(..., env=env)`；2026-10-05 实测位于 L2924，**行号会漂、以语义为准**） | 试点密钥若与 agent_go 批同 shell，子任务可继承并自调 jev ⇒ 撞 A 情境红线；须设密钥隔离前置门（§10.2 P0-1） |

### 1.2 jev 可迁移证据（JEP-1 四轮，[实测] 引用）

- 加证使弃权消失后，jev 输出与粗规则 **8/8 一致**（JEP-1 §15）——增量不在"判得更准"，在**诚实弃权**；
- **靶不对齐 ⇒ 负相关**（JEP-1 §16：ρ=−0.444，No-Go）；**靶对齐 ⇒ 首现正增量**（JEP-1 §17：precision@4 1.00／ρ 0.807／effort 5<7，优于粗规则 0.75／0.704／6）；
- JEP-1 §17 边界：n=20、单一靶源（ARS＝agent 产出、非独立人工）、**方向性，不构成验收**；消融（去 `agent_final_excerpt`）未做；
- 待补缺口 JO-3＝**独立靶**——本试点的人工盲标正是补这一格。

### 1.3 为什么选这个落点（[分析]）

1. agent_go 的误判族（#29/#31/#40/#51）横跨两类；JEP-1 §17 唯一出现正增量的靶是**内容型**一侧，本试点据此把"内容型优先"作为可测量命题；
2. 离线形态绕开四条硬约束：情境合规（B 可用）、CON-1（不进 verdict）、外发（逐次可审）、远程依赖（不阻断任何现有流程）；
3. 失败产物本地可得（worktree＋result.json＋日志/轨迹），靶可由人在本地裁。
4. **价值边界（v0.4）**：本队列的价值主张＝**挑出内容型问题优先深判**；**环境型假失败（ISSUE-29/31 型）不在本试点的测量收益内**（其处置属另一条线：复跑/误判检测）。M4 结论不得外推至环境型方向。

### 1.4 治理约束摘要（[需求]，全文适用）

- **使用半径（CON-1）**：jev 输出只作分流/排序/候选解释；不进 verdict、不进能力结论、不进任何 AC 分子分母。
- **三情境**：本轮只允许 **B**（人闸门）；**A** 批内 agent 禁调；**C** 无人值守**未立项**。
- **审计三项**：逐次外发内容类别自评＋落证＋记账（调用数/token/成本）。
- **软排序红线**：禁 top-k 截断，排序结果全量给人。
- **升级腿＝人**：低置信/弃权不得升级给云端大模型（JEP-1 §13）。
- **任务级计数挂起（OP-1 类比）**：任务级常量（如子任务总数）不外发。

---

## 2. 试点选择

### 2.1 候选与否决

| 候选 | 形态 | 判定 | 理由 |
|---|---|---|---|
| 运行时判决（验证裁决、看门狗 nudge/kill） | in-path | **否** | CON-1 硬禁（verdict）；A/C 情境不合规；闭源远程依赖须规则兜底 |
| C 通道批量（脚本直连、无人值守） | 批量 | **否（本轮）** | 未立项：机械审计＋预算登记＋仪器可信度前置未就绪 |
| 用 jev 输出训练/生成规则集 | 派生 | **否** | JEP-1 结论 4：那是它的观点、不是真值；正确用法＝特征/规则充分性探针 |
| 用现有 worker 模型 / LLM judge 直接排序 | 在线推理 | **否（本轮）** | ①不可复现：JEP-1 §4.1 实测 LLM judge 逐例方差 92–913×，"稳定地错比随机错更危险"；②被审对象同族/同源，撞本仓已登记的 `uncovered_perspectives.independent_reviewer` 缺位（judge==candidate 风险）；③欲做对照臂须先过独立审查面设计，成本不小于本试点 |
| **失败子任务离线复核排序** | 离线／B／软排序 | **选中** | 与 JEP-1 §17 正增量靶族同构；本地产物可得；风险最低 |

### 2.2 选定功能（试点内容）

新增**离线仪器** `tools/jev_triage.py`（dev script，仅标准库）＋一条人闸门流程：

1. `--build`：从冻结批的任务产物构造池与白名单 state（零外发）；
2. `--check`：机械门 R-J1a–d／R-J2a–c（禁入项、白名单、三型 lint、阳性对照声明、三类指纹）＋**粗规则选择性检查**（§6.3），**exit 1 即禁调用**；
3. `--packets`：输出人审阅包（含逐条外发类别自评模板）；
4. **B 会话调用**：默认在 ZCode 会话内用既有 `mcp__jev__jev_decide` 逐条调用（复用 llama.cpp `tools/jev_mcp_server.py` 的客户端级注册，不新写调用器）；可选串行 caller（显式人工确认触发，密钥仅从 env 读；实现须 spawn 既有 MCP server——本工具**零网络能力**，见 §15.4.3）；
5. `--label`：人工盲标靶（`labels.jsonl`，append-only）＋**记录单条耗时**（§7）；`--import-labels`：批量导入人工标签（跨轮合并/离线补录；整批校验，一处不合规即拒收）；
6. `--queue`：生成**本地复核队列** `review-queue.md`（rank→task/subtask/worktree 映射；仅本地，不外发）——这是排序的消费面，采纳率的计量载体；
7. `--analyze`：H6、双基线对比、四态结论、弃权集画像、人时换算（零外发）。

### 2.3 范围

- **做**：失败子任务排序试点；协议按 JVR-1 对齐（R-J1…R-J6）；复用既有 MCP 封装与额度探测件（llama.cpp `tools/jev_quota.py`／MCP `jev_usage`）；新增“本地队列文件”作为消费面（刻意不是 UI 改动）。
- **不做**：不改 `pipeline.py`／`executor.py`／`subtask.py`／`meta.json` 契约／Web 复核 UI／`review` 子命令行为；不写 `kanban.json`／`problems.jsonl`；不新增运行时依赖；**不让 agent_go 运行时任何路径调用 jev**；不引入 LLM judge；不做 C 通道。

---

## 3. 系统边界与数据流

```text
冻结批（bench / run；failed/blocked worktree 默认保留）
   └─ tools/jev_triage.py --build      # 零外发：读产物 → 池 + state + questions + manifest
        ├─ --check                     # 零外发：R-J1/R-J2 机械门 + 粗规则选择性（退出码即门槛）
        ├─ --packets                   # 人审阅 + 逐条外发类别自评
        ├─ B 会话人闸门 ── jev_decide ─► 外部 API（外发 state）
        ├─ --label                     # 人工盲标（先于查看 jev 结果）+ 单条耗时
        ├─ --analyze                   # 零外发：H6 + 指标 + 人时换算 + 四态
        └─ --queue                     # 本地 review-queue.md（消费面；含本地映射，不外发）
落盘：~/.agent_go/jev/<pilot-id>/      # pilot-id = jev-review-<冻结批号>
  pool.jsonl / state/*.json / questions.json / manifest.json
  packets.json|md / human-review.md / caller-audit.log
  results.json / labels.jsonl / analysis.json / review-queue.md / README.md（记账）
  labels.jsonl（本轮＝跨轮同文件，append-only；跨轮以 --import-labels 合并，v0.7）
  outcomes.jsonl（复核结果回写，v0.7）
  prelabels.jsonl（LLM 预标注初稿，v0.8）/ probe.jsonl（程序化探针结果，v0.8）
```

硬边界：以上全部在 agent_go 运行时**之外**；试点任何环节失败不影响任务执行；删除该目录不影响 agent_go 任何功能。`review-queue.md` 含真实 task_id/路径，属**本地**物料，禁止外发或上传。

---

## 4. 样本池（两阶段）

- **预试池（M0.5）**：**29 次任务执行**（decision × repeat 1，配置对齐 `delivery-20260820` 基线口径：`models=claude-sonnet-4-6` 经本地代理、`--with-delivery`）→ 预期 **~10 条 failed 子任务**（换算依据[实测]：该口径 **0.34 failed 子任务/次**、$0.0094/次）。用途：只验 4 件事（§11.1 M0.5），不判 Go。**跑前先以小批探测实际失败率再定正式量**；**禁止**用低失败臂（c4-kv-inj 2%／arm_cloud 0%——按它们需 500+ 次执行）。
- **正式池（M1）**：**目标 n≥30 ⇒ 约 87 次任务执行**（decision × repeat 3，同口径）≈$0.9；若不足 ⇒ 补批（每次补批单独登记 batch 边界，池合并须记录来源）或按 §9.2 降级口径只出 Conditional/探索性（**不判 Go**）。
- **冻结批命令骨架**：
  `agent_go eval bench --suite <suite> --repeat 1 --output eval_suite/results_<batch>.jsonl --source-batch <batch-id> --yes`
  （suite 选型＝开放事项 O-3；批自身成本见 §10.1。）
- **入池**：该批 `status=failed` 的子任务（含 retry 耗尽、无进展停止、`kill_reason ∈ {stuck, hard_timeout}` 等真失败）。
- **排除**：synthetic 结果（上游阻断／预算熔断／依赖循环／计量不可用）——无 worktree，且靶可机械判定。
- **外部效度（v0.2 新增）**：bench fixture 失败 ≠ 真实任务失败。要求：池内**真实任务失败样本 ≥20%（目标）**（来自试点窗口内的 dogfooding run；产物同样快照）；若不足，报告须显式标注"**bench 分布，结论不可迁移到真实任务**"，且 Go 只支持"低风险试点"（操作者可选使用队列），**不得**据此进入 review 流程或任何自动化。
- **快照**（冻结即做，防清理/防篡改）：每条失败子任务快照 `result.json`＋`meta.json` 对应条目＋`git diff <base>..HEAD` 补丁＋`execution.log`/`trajectory` 相关片段 → `evidence/<run_ref>/`；批运行后**不执行 `agent_go clean`**。
- **run_ref**：`sha256(f"{task_id}/{subtask_id}")[:16]`；task_id、路径、仓库名一律不出现在外发侧（本地队列文件除外，见 §3）。
- **manifest 指纹**：`sample_sha256`（run_ref 序列）、`state_keys_sha256`、`questions_sha256`（含 instructions 全文）——`--check` 复算，不匹配＝违规（改件），缺失＝软告警。

---

## 5. 外发白名单与禁入清单（[需求]）

### 5.1 允许字段（state 顶层键 ⊆ 本表；单条 state ≤8KB）

| 字段 | 来源 | 处理 |
|---|---|---|
| `run_ref` | 派生 | 哈希截断 |
| `status` / `exit_code` / `verify_ok` / `retry_count` | result | 原值 |
| `kill_reason`（归一化枚举）/ `degraded` / `loop_detected` / `crash_but_verified` | result | 枚举白名单，未知值→`other` |
| `duration_sec` / `timing.claude_execute_ms` / `timing.verification_ms` | result | 数值 |
| `change_stats`（files_changed / insertions / deletions / new_files / modified_files） | result | 仅计数，无路径 |
| `trajectory.steps` / `tool_calls` / `tool_errors` / `mutations` / `mutation_without_worktree_change` | trajectory_signals | 数值/布尔 |
| `trajectory.path_violation_n` / `repeated_edit_max` / `repeated_edit_files_n` | trajectory_signals | 由路径列表**聚合为计数**，路径本身不外发 |
| `verification_confidence`（level / anchoring / warning 摘要） | result | 截断 ≤200，脱敏 |
| `verification[].command` | verification_results | 截断 ≤300，路径/仓库名→占位符 |
| `verification[].exit_code` / `attempt` | verification_results | 原值 |
| `verification[].stdout_tail` / `stderr_tail` | verification_results | 各截断 ≤800，脱敏 |
| `failure_reason` | result | 截断 ≤300，脱敏 |
| `patch_excerpt`（可选实验变量） | git diff | 截断 ≤2000，脱敏；开/关即实验变量 |
| `agent_final_excerpt`（可选实验变量） | trajectory 末段 | 截断 ≤800，脱敏；对应 JEP-1 §17 消融项 |
| `control.change_nonempty`（阳性对照用） | change_stats | 布尔 |

### 5.2 禁入（`--check` 硬违规）

`task_id`／任务文本（task、spec、agent_prompt）／repo 名与 URL／绝对路径／用户名／worktree 路径／`commit_hash`／branch/tag／`base_commit`／模型名与 provider／gold 与隐藏测试名／任务级计数（子任务总数等，OP-1 类比）／`failure_class`（机器结论，防循环）。

### 5.3 脱敏规则（统一函数，测试钉住）

`/Users/<name>/...`、`/home/...`、`/tmp/...` → `<path>`；仓库名 → `<repo>`；40 位 hex → `<sha>`；邮箱与 URL → `<url>`；连续空白/换行归一。

**语言（v0.5）**：state 的枚举值与新增字段一律英文（与 §6.1 题面一致）；中文自由文本（`failure_reason`/验证输出片段等）保留原文——官方明示 CJK 准确率较低，属**已知局限**，报告须声明；不得为"翻译"引入不确定的逐次改写（如需归一化，只允许确定性映射表，并计入指纹）。

### 5.4 外发类别自评（逐次，人写）

模板：`本次外发＝规范化派生指标（补丁形态/验证输出片段/agent 末段），无人名、无绝对路径、无题面与 gold。` 每条 packet 附一行。

---

## 6. 测量协议与仪器规格（v0.2 分节）

> 6.1 是**协议**（不随实现变，冻结进预注册）；6.2/6.3 是**仪器规格**（实现可替换，判据不变）。
> **官方对照（v0.5）**：本节及 §9 的若干设计已被官方 cookbook 背书——单样本多问同调用（`cookbooks/parallel_questions`：答案逐位不变、无批量效应，13 问合 1 便宜 12.2×）、状态最小化（`model-jaggedness` #5 context rot）、choice 显式兜底（`primitives/choice`）、低置信→人工与"答案＝是什么、置信度＝是否行动"（`patterns/confidence-routing`）、降级到规则/人工（`how-to-build-with-system-one`"能用代码就用代码"）。

### 6.1 协议：问题包与阳性对照

**三型**（服务端硬约束：≤16 问／≤200KB／60s）。

**题面语言与写法（v0.5，依据官方 cookbook）**：questions/criteria **全文用英文冻结**（官方 `concepts/state`：Jev 主要训练语言是英语，CJK 准确率较低）；中文仅作本文档对照。易混选项用**结构化 criteria**（`what` / `not_for` / `examples`，官方 `primitives/choice` 推荐写法），`instructions` 用对象式 `{question, focus}`。**以下英文全文即冻结文本**（工具按此构造 JSON）：

**Q1 `review_class`（choice，歧义首选＋显式兜底）**

```json
{
  "type": "choice",
  "instructions": {
    "question": "What is the root cause of this failed subtask?",
    "focus": "Decide where the fix would have to be made: in the delivered content, or in the verification / harness / process around it."
  },
  "criteria": {
    "content_fix": {
      "what": "The root cause is in the delivered content itself: a wrong fix, a missed change, a wrong location, or a partial implementation.",
      "not_for": "Defects in the verifier, sandbox, timeouts, dependencies, harness, or process.",
      "examples": [
        "The implementation omitted an edge case required by the task.",
        "The change was made in the wrong file."
      ]
    },
    "infra_or_process": {
      "what": "The root cause is in the verifier, sandbox, timeout, tooling / network, budget, upstream dependency, harness, or process.",
      "not_for": "Errors in the delivered content itself.",
      "examples": [
        "The verification command had a syntax error while the code was correct.",
        "A sandbox environment mismatch failed correct code."
      ]
    },
    "insufficient_evidence": {
      "what": "The provided state does not carry enough evidence to assign either root cause.",
      "not_for": "A case that merely looks difficult; use this only when the evidence is absent."
    }
  }
}
```

**Q2 `control_change_nonempty`（noul，阳性对照）**

```json
{
  "type": "noul",
  "instructions": "Did this subtask produce a non-empty code change?",
  "criteria": {
    "true": {"what": "At least one file was added or modified.", "examples": ["2 files changed, 40 insertions"]},
    "false": {"what": "No file content changed.", "examples": ["0 files changed"]}
  }
}
```

**Q3 `ranking_noul`（noul，第二排序器；v1.1 新增，官方 rerank 配方形态）**

```json
{
  "type": "noul",
  "instructions": {
    "question": "Is the root cause of this failed subtask in the delivered content itself?",
    "focus": "Answer yes only when the fix would have to change the delivered content (wrong fix, missed change, wrong location, partial implementation)."
  },
  "criteria": {
    "true": {"what": "The root cause is in the delivered content itself.", "examples": ["The implementation omitted an edge case required by the task."]},
    "false": {"what": "The root cause is outside the delivered content: verifier, sandbox, timeout, tooling, budget, dependency, harness, or process.", "examples": ["The verification command had a syntax error while the code was correct."]}
  }
}
```

- 中文对照：`content_fix`＝根因在交付内容本身（错改/漏改/定位错/部分实现）；`infra_or_process`＝根因在验证器、沙箱、超时、工具/网络、预算、上游/依赖、harness 或流程（**代码正确而验证器判错＝此类**，ISSUE-29/31 型）；混合按根因主导，无法裁决 ⇒ `insufficient_evidence`。
- **Q1–Q3 同调用发送（v1.1）**：同一 state 一次发送、多问题由服务端并行评估（官方 batching：答案互不影响；新题**不额外发送 state**）——即"同一 state × 多问题"形态的落地（需求评审 ①+③）。
- Q1+Q2+Q3 全文进 `questions_sha256`，冻结后不得改（改＝重开一轮）。
- **rank 构造（v0.5 预注册）**：
  1. 取 `choice` 回包中的 `P(content_fix)`（经 `probabilities` 键校验）；
  2. 按 P 降序排列；并列按 `run_ref` 升序破；全等分＝退化告警（见 §9.1 反指标）；
  3. **两条弃权通道（v0.5）**：①模型自选 `insufficient_evidence`；②`P_max < τ`（τ 定格建议 **0.60**，官方一致性实验默认；探索性预注册报告 τ∈{0.50,0.60,0.70} 的覆盖/翻转曲线，不进主判据）。两通道样本**均单列"探针清单"、均不计入 effort@recall 覆盖**（保守口径，见 §9.1）；
  4. 排序**只用 `P(content_fix)`，不得用 `confidence` 字段**（官方定义＝分布尖锐度、≠top-1；本地校准未做）；
  5. **第二排序器已落地（v1.1，O-11 改判）**：Q3 `ranking_noul` 同调用发送；其排序（`noul` 降序）**只作探索对照**（§9.1），**不进主判据、不影响四态结论**；
  6. `review-queue.md` 与本规则一致生成；规则随 `questions_sha256` 同批冻结。
- **H6**：Q2 命中率 ≥0.80（目标 1.00，参照 ex03 30/30）；**不过即整轮作废**。

### 6.2 仪器规格：lint 与指纹（R-J1/R-J2）

- **lint（v0.5 更新）**：三型契约（choice 非空 map＋兜底；score ≥2 档；noul 是非题且恰 true/false criteria）、**题面语言门（必须英文；混入中文＝违规）**、**结构化 criteria（易混选项须含 `not_for`）**、一题一判、双重否定、数学/计数/日期措辞。
- **对照基率检查（v0.4）**：Q2 对照字段的真值须**两类各 ≥3 条**（M0.5）／**少数类 ≥20%**（M2）；越界 ⇒ 对照退化，须补样或更换对照题（更换＝改件重冻）。`--check` 输出实测基率。（现存 4 条 failed/no_changes 样本 `files_changed` 全为 0、n 小不可外推——退化方向双向都可能。）
- **完整性门（v0.4）**：`--analyze` 前校验 `调用数 == 样本数`；缺调用（含 429 中断）⇒ **fail-closed 拒绝出判据**，续跑补全后重试。
- **回包版本记录（v0.7）**：`results.json` 每条记录回包中的 `model` 版本；**版本变化＝改件**（须重开一轮）——官方一致性实验要求"报出返回的每个版本号，让别名变更可见"；缺版本记录的轮次标"版本不可知"。
- **复核结果回写（v0.7）**：操作者复核每条后记 `outcome`（枚举：`content_confirmed`／`misjudged_rerun`／`no_action`／`other`＋备注），落 pilot 目录；这是数据闭环的标签回流入口（§9.4 G2），**不回写 runtime 任何存储**。
- **R-J2（v0.5 更新）**：重复调用保持 **state 逐字节一致**（**故意不加 uid/nonce**——与官方一致性实验的差异声明：我们测仪器自身噪声，而非缓存击穿）；报**双指标**＝多数标签复现率＋平均概率漂移（官方对照：TypeSafe 原始多数标签复现 90.8%，加 0.60 阈值后一致性 99.2%）；**阈值后翻转率 ≤5% 为主判据、原始翻转率作对照**（参照 ARS AC-8）；数值死区 ≥0.05；序反指标 |ρ|≤0.30 告警；无重跑样本＝"不可判"，不得读成 0 漂移。

### 6.3 粗规则选择性门（v0.2 新增）

`--check` 须报**粗规则命中率**（在全池上的"优先"比例）：

- 命中率 **>80% ⇒ 视为退化基线**（"全高"等于无排序），自动切换更 selective 的候选规则（默认备选：`loop_detected ∨ retry_count≥2`），切换＝改件，须重新冻结指纹并按新规则重算；
- 命中率 **<20% ⇒ 亦告警**（可能过度 selective，与默认序无差异），须人工确认后重冻。

---

## 7. 靶设计（R-J3，第一变量）

- **rubric**（人标，三值同 Q1；v0.4 改根因口径）：内容型／环境流程型／**不可判（单列，不计入主指标分子分母）**。主导类规则：**根因在交付内容本身（错改/漏改/定位错/部分实现）⇒ 内容型；根因在验证器/沙箱/超时/依赖/harness/流程 ⇒ 环境流程型；代码正确而验证器判错 ⇒ 一律环境流程型**。混合按根因主导裁决；裁不动 ⇒ 不可判。人标 rubric 与 §6.1 Q1 criteria **共用同一根因定义**（题面英文冻结、人标可中文操作）。
- **代理效度说明（v0.2）**：内容型是"值得人工深判"的**代理**而非等同——内容型不必然可行动。预试阶段用锚点＋小样显式检查该代理的效度（≥6/8 锚点与直觉一致才继续）。
- **盲标**：标注者先写 `labels.jsonl`，之后才可查看 `results.json`；流程上 `--label` 必须先于 `--analyze` 完成（仪器只提示不阻断，顺序由标注者自证）。
- **预标注与辅助真值协议（v0.8）**：**真值来源仍是人**——LLM 与探针只能降低人的单位成本，不能替代人的确权；人工对每一条**确认或推翻**后才写入 `labels.jsonl`。
  1. **程序化探针（优先）**：对保留 worktree 的失败样本，在**干净环境复跑记录的验证命令**（本地、零外发）。判读：原判失败而 probe 通过 ⇒ **环境/harness 型证据**；probe 也失败 ⇒ 按形态分——命令级错误（语法/未找到/被拒）⇒ 环境流程型；断言/测试失败 ⇒ 内容型；非确定性或超时 ⇒ 不可判。约束：执行前仍过 `utils._is_safe_verification_command` 安全前缀检查（**不绕过**）；probe 结果**不进 state**（不制造机械耦合）；可能产生构建副作用的命令记 README 或跳过。
  2. **LLM 预标注**：优先**本地模型**（零外部外发）；若用云端 ⇒ 按**第二条外发通道**登记（逐次自评＋记账，见 §10.3）。须与 jev **不同家族**；**不得查看 jev 结果**；只出初稿。
  3. **不覆盖原则**：预标注与探针结果分别落 `prelabels.jsonl` / `probe.jsonl`（含模型/命令与版本）；**永不覆盖**人工标签；`labels.jsonl`（跨轮）只收人工终审标签。**实现状态（2026-10-05）**：程序化探针 **已落地**——`--probe [--confirmed] [--probe-timeout] [--probe-force]` 复跑最近一轮验证命令：与 runtime 同源的沙箱 env（`_build_sandbox_env`）与资源上限、`shlex.split` 不经 shell、执行前**必须**过 `utils._is_safe_verification_command`（不绕过）、安装/推送/迁移类副作用模式默认跳过、结果只写 `probe.jsonl`（**不进 state、不写 results**）；逐条判读 `env_or_harness`（复跑通过或命令级错误）/`content`（断言类失败）/`undecidable`（超时/非确定性），汇总 `probe_summary.json` 含**探针-人一致率（探索性，不进判据）**。**LLM 预标注（`prelabels.jsonl`）未实现**——选型与边界＝O-13。
  4. **探索性指标**：LLM-人一致率、probe-人一致率（度量靶的主观性与探针覆盖，**不进判据**，见 §9.1）。
- **耗时记录（v0.2 新增，P0-2）**：标注每条时记录**耗时（分钟）**；该值作为"单条复核耗时"的**代理**（标注≠复核，报告须声明此局限），用于 §9 的人时换算。
- **锚点校准（gold 校准，先于实标）**：3–5 条已知真值锚点（例：验证命令语法错误而代码正确→环境流程〔ISSUE-29 型〕；沙箱误杀→环境流程〔ISSUE-31 型〕；错改→内容型；空交付假成功→内容型）；锚点全对才开标，否则先改 rubric。
- **双标与一致性门（v0.4 修订）**：≥20% 样本由第二人独立标，报一致率/κ 及其 CI；**M0.5 的 κ 仅作 sanity（不计门）**；主判据门 **κ≥0.6 在 M2 适用，且双标 ≥10 条**（n=6 时 κ 点估不稳）。不达 ⇒ 先修 rubric／剔除歧义样本（预留 +1 人时/10 条的 re-label 预算），重标后仍不达 ⇒ 靶"不可判"。
- **非机械耦合预检**：逐条检查是否任一单字段/平凡规则即可推出标签（如 `kill_reason=infra` ⇒ 环境型）；此类样本**分层**处理——主判据只在"非机械耦合子集"上计算，全池指标作辅证；若全池皆机械可判 ⇒ 判"靶无信息量（不可判）"，**不得耗调用**。
- **靶-框架对齐预检**：主问题即靶（**不设**"值得复核/repay"类锐目标——JEP-1 §16 教训：靶错位 ρ=−0.444）；对齐与否由 M2 锚点与预标注探测判定。
- **池退化处置**：若人标分布 ≥90% 落入同一类 ⇒ 方差不足，如实报"不可判"，停。
- **跨轮标签库（v0.7）**：`labels.jsonl` append-only，key=`run_ref`，每条携带 rubric 版本与 `questions_sha256`；后续轮次优先复用已标样本、增量标注——多轮累积是规则挖掘的样本前提（§9.4 G3/G5）。

---

## 8. 流程与命令

> 命令面为实现建议；§6.1/§9 的协议与判据不随实现变。

```bash
# 统一 pilot 目录（--out 为必填；下同）
PILOT=~/.agent_go/jev/jev-review-<batch-id>

# M0.5 薄预试（小批；零外发建池 + 手工/半自动 packet）
agent_go eval bench --suite <small-suite> --repeat 1 --output eval_suite/results_<pre-batch>.jsonl \
  --source-batch <pre-batch-id> --yes
python3 tools/jev_triage.py --build --out "$PILOT" --results eval_suite/results_<pre-batch>.jsonl \
  --limit 10 --stage pilot                     # --limit 0＝全部
python3 tools/jev_triage.py --check --out "$PILOT"     # exit 1 ⇒ 停（含粗规则选择性门）
python3 tools/jev_triage.py --packets --out "$PILOT"   # 人审阅 + 逐条外发类别自评
python3 tools/jev_triage.py --label --out "$PILOT"     # 盲标 + 单条耗时（先于查看 results）
# B 会话逐条调用（≤10 次；H6 不过即停，不进入 M1 仪器建设）
#   会话内 mcp__jev__jev_decide 逐条调用后，把每次回包落盘并回填：
#   python3 tools/jev_triage.py --out "$PILOT" --record <run_ref> --response <回包.json>
python3 tools/jev_triage.py --analyze --out "$PILOT" --stage pilot   # 只出 4 件事的结论（不判 Go）

# M1 正式轮（预试 4 件事全过后）
agent_go eval bench --suite <suite> --repeat 1 --output eval_suite/results_<batch>.jsonl \
  --source-batch <batch-id> --yes
python3 tools/jev_triage.py --build --out "$PILOT" --results eval_suite/results_<batch>.jsonl
python3 tools/jev_triage.py --check --out "$PILOT"     # R-J1/R-J2 + 粗规则选择性门
pytest tests/test_jev_triage.py -q

# M2（零外发）
python3 tools/jev_triage.py --packets --out "$PILOT"
python3 tools/jev_triage.py --label --out "$PILOT"     # 盲标（先于查看 results）+ 耗时
#   跨轮复用/离线补录（可选）：--import-labels <labels.jsonl>（整批校验，已标自动跳过）

# M3（B 情境，人闸门，串行）
# 默认：ZCode 会话内逐条 mcp__jev__jev_decide（state/questions 取自 state/*.json）→ --record 回填
# 可选串行 caller：python3 tools/jev_triage.py --call --out "$PILOT" \
#   --server <既有 MCP server 路径> --confirmed [--rpc-timeout 90]   # 密钥仅从 env 读；stderr 落 caller-server.err.log
# 额度探测（元数据 GET，不计次不计费）：复用 llama.cpp tools/jev_quota.py 或 MCP jev_usage（本薄版不内置 --usage）

# M4（零外发）
python3 tools/jev_triage.py --analyze --out "$PILOT"   # H6 + 双基线 + 人时换算 + 四态 + 弃权集画像
python3 tools/jev_triage.py --queue --out "$PILOT"     # 本地 review-queue.md（消费面）
```

**人闸门清单**（每次调用前人工勾选）：①packet 已审（禁入项 0 命中）②外发类别自评已写 ③额度已探（可调）④串行、无并发。

---

## 9. 判据与指标（预注册；冻结后不可改）

### 9.1 指标

| 维度 | 指标 | 说明 |
|---|---|---|
| 有效性（主） | `effort@recall(0.5)` | 条数口径；**真目标＝`labels.jsonl` 中 content_fix（=1），infra=0，undecidable 剔除，分母＝内容型条数**；按任务聚类 bootstrap 报 CI（v0.4） |
| 有效性（辅） | `precision@4`、Spearman（**均值秩**，按任务聚类 bootstrap，CI 不含 0） | — |
| **人时（v0.2 新增，探索性辅证）** | **`人时@recall(0.5)`** | 用标注阶段记录的单条耗时加权换算（代理，含局限声明）；Go 报告必须给出条数→人时换算与不确定度 |
| 辅助真值（探索性，v0.8） | LLM 预标注-人一致率、probe-人一致率 | **不进判据**；用于度量靶的主观性与探针覆盖（§7） |
| 第二排序器（探索性，v1.1） | Q3 `ranking_noul` 排序：effort@recall／precision@4／Spearman(noul vs 标签) | **不进判据**；与主排序同覆盖口径，对照官方 rerank 配方（§6.1） |
| 稳定性 | **阈值后**重跑翻转率 ≤5%（主口径）＋原始翻转率/多数标签复现率/平均概率漂移（对照双指标）；数值漂移落 ≥0.05 死区 | v0.5；官方原始复现 90.8%、阈值后 99.2% |
| 弃权（辅证） | **弃权集质量**：**两通道**（模型弃权＋`P_max<τ` 低置信）样本上，粗规则/默认序的错误率或 undecidable 占比显著更高 | 方向性；两通道样本均单列"探针清单"、**均不计入 effort@recall 覆盖**（保守口径，v0.5） |
| 反指标 | 序反（评分 vs run_ref 序）\|ρ\|≤0.30；全等分＝退化告警 | 同 JVR-1 JR-2 |
| 采纳 | **采纳＝操作者按 `review-queue.md` 实际复核并留痕（≥1 条、记录 rank 序列）**；未使用队列＝采纳 0；**另报采纳质量**（复核事件的 rank 分布/中位 rank，v0.4） | Go 前提 |

### 9.2 判据（v0.2 拍建议值，M0 确认后冻结）

| 编号 | 判据 | 门槛 |
|---|---|---|
| H6 | 阳性对照命中率 | ≥0.80（目标 1.00）；不过⇒整轮作废 |
| H1 | `effort@recall(0.5)`：jev **同时优于**默认序与粗规则 | **Go 口径：n≥30 且降幅 ≥2 条、聚类 bootstrap CI 下界 >0**；**只优于默认序不判 Go**（3 行规则也能胜默认序）；**n=20–29 ⇒ 只出 Conditional**（降幅 ≥1 条且 H2 正）或不可判；**n<20 ⇒ 不可判**（v0.4 对齐规模口径） |
| H2 | `precision@4` 与 Spearman vs 默认序 | ρ>0 且 CI 不跨 0 |
| H3 | **阈值后**重跑翻转率 ≤5%（主判据）；原始翻转率＋多数标签复现率＋概率漂移作对照报告 | v0.5；官方原始复现 90.8% ⇒ 原始口径不宜直接套 5% |
| 低置信阈值 | τ（对 `P_max` 的门） | **0.60**（官方一致性实验默认）；已定格建议（§16 F2），M0 签署 |
| 一致性 | 双标 κ（M2，双标 ≥10 条，报 CI） | ≥0.6（不达先修 rubric，见 §7） |
| 基线有效性 | 粗规则命中率 | 20%–80%；越界按 §6.3 处置 |
| 止损（M0.5） | 四件事（H6／靶方差／对齐／粗规则选择性）任一不过 | 停，归档负结果，不投 M1 |

- **双基线（R-J4）**：①默认序＝现状复核清单顺序（计划序，即"无排序"基线）；②粗规则：`loop_detected ∨ retry_count≥2 ∨ kill_reason∈{stuck,hard_timeout,infra,system_error} ∨ files_changed=0 ⇒ 优先`（若选择性门不过则换 §6.3 备选并重新冻结）；③可选取值：现有机器信号序（`verification_confidence.warning`／`blind_spots`）。
- **操作者工作流假设（v0.4 预注册）**：按队列顺序复核至 recall 达成点（剩余项经人工确认同类后可批量处置/停止）；停止点与剩余项处置记入 README；`人时@recall` 按此口径计算——没有该假设，"省人时"无来源。
- **规模声明**：n≥30 才可判 Go；n=20–29 只出 Conditional/不可判（v0.4 对齐 §4 与 H1）；报告须带 CI；结论不得作能力结论、不得进 AC。M0.5（n≈8–10）只出"四件事"结论，**不判 Go/Conditional**。

### 9.3 四态与后果表（v0.2 新增，防报告孤儿化）

| 结论 | 触发 | 承诺的下一步 |
|---|---|---|
| **Go** | H6∧H3∧H1∧采纳>0∧全程软排序（∧真实样本占比达 §4 目标） | 进入"低风险落地"：**级联落地（v0.5）——机械可判样本由代码直接分流，仅非机械耦合子集进 jev 队列**（官方"能用代码就用代码"）；队列并入操作者复核流程（本地 md，人可选使用）；再用真实任务池跑第二轮独立靶后，才讨论任何自动化；**不改变 CON-1** |
| **Conditional** | H1 未达但 H2 正、效率有方向性收益 | 限低风险试点：仅保留队列生成，不推广；补靶/扩样后再判 |
| **No-Go** | **仪器失效**：H6 不过、ρ<0、或靶审计中属仪器失效者 | **停止 jev 在 agent_go 的落地线**；负结果归档（写入 `problems`/经验档）；"弃权探针"作为可选用途须另立项 |
| **不可判** | **协议/数据失效**：κ 不达、池不足、n<20、CI 跨 0、靶退化 | 写明原因（CI/n/池/靶/规模）与复发条件；是否再跑由 PM 按剩余预算与时效决定（jev 为 early access，结论有保鲜期） |

**边界（v0.4）**：仪器失效 ⇒ No-Go；协议/数据失效 ⇒ 不可判。二者不得混用（κ 不达属后者，不是 No-Go）。

### 9.4 数据闭环与规则收敛（v0.7 新增；第二阶段，非本试点判据）

> 本试点只测"jev 排序是否有增量"；**从 jev 决策收敛到规则是另一轮的事**，登记为 O-12（Go/Conditional 后议）。本节固定缺口清单与合法路径，防止后续误推。
> **边界**：该回路**不能自动闭合**——数据积累可自动（本地落盘），但 jev 调用受 B 情境人闸门、输出受 CON-1 约束（§1.4）；第二阶段同样不得产出 verdict/AC；且 jev 闭源不可训，闭环的合法对象只有题面/状态面/规则/阈值。

**现状能支持的**：弃权探针清单（两通道）＋非机械耦合子集（§7）＋双基线（§9.2）＝"规则覆盖不到哪里"的边界测量；白名单 state 提供可枚举的特征面；`labels.jsonl`（v0.7）提供人工真值。

**缺口清单（v0.7 登记）**

| # | 缺口 | 现状 | 补齐动作 |
|---|---|---|---|
| G1 | 回包 `model` 版本未记录 | 托管模型别名会漂，中途换版不可察觉 | `results.json` 记录版本；版本变化＝改件重开轮（§6.2） |
| G2 | 人审结果未结构化回写 | 只记"按队列复核"（采纳），不记每条结论 | `outcome` 枚举回写（§6.2） |
| G3 | 跨轮标签库未设计 | 每轮标签文件独立，下一轮需重标 | `labels.jsonl` append-only（key=`run_ref`，带 rubric/框架版本，§7） |
| G4 | 无特征/规则挖掘步骤 | `--analyze` 只出判据与画像 | 第二阶段：单特征 CV／规则候选枚举＋留出验证（复用 swe-eval 通道核验思路，参照 §15.5 V4） |
| G5 | 样本量差一个量级 | n=20–30 只出方向性 | 多轮累积至 **≥100 条带标签**再开挖掘轮 |
| G6 | 合法流程未写清 | 只有禁令（§2.1：不得用 jev 输出训规则） | 挖掘**只许人工标签**输入；jev 仅作"规则不够用"的指针；新规则须留出验证后再生效 |
| G7 | 落地出口未定义 | 粗规则是测量基线、非产品功能；§2.3 不改 review 流程 | 新规则进 review/信号面＝边界变更，须单开 ADR |

**合法路径（四步；第二阶段立项时按此预注册）**：①"规则收敛建议"（**非判据**）——在非机械耦合子集上描述弃权样本的特征分布 vs 已判样本，只出候选特征清单（本轮 M4 可作为**探索性附件**预置，不计入判据与工时承诺）；②补齐 G1–G3，多轮累积样本至 ≥100 条；③开挖掘轮（人工标签输入、留出验证、报增益与误报）；④落地出口 ADR 评审。

**终局预期**：规则每扩一次，jev 的增量区就缩一分——与 JEP-1 §15"证据充分时 jev ≡ 规则"一致；"弃权探针"若作长期用途须另立项（§9.3 No-Go 行）。

**与 H3 自进化的关系（v1.2 登记）**：本节回路＝`roadmap`/`prd` 中 **H3 自进化**的第一条具体回路（**规则迭代回路**）——H3 的启动前置"可信历史数据"正由本试点的数据面补足（人工真值标签＋弃权探针＋冻结指纹可复算）；KnowledgeStore 仍为 H3 的独立前置。产物是**规则候选**（不是成品规则），四道闸缺一不可：

1. **标签源闸**：只许人工真值/程序化真值；**禁止用 jev 输出训练或标注**（它是观点不是真值，JEP-1 结论 4）——jev 只作"哪里规则不够"的指针（弃权集）；
2. **验证闸**：规则候选须在**留出集**（≥100 条带标签、跨批）上验证，且旧样本不得回退（回归测试钉住）；
3. **落地闸**：规则以**代码＋测试**形式进库（可解释、可审计、可回滚），部署走信任指标门＋边界 ADR；不得引入不可解释模型；
4. **反哺计量闸**：每次规则扩张后按**同一预注册判据**重度量（jev 增量区应收缩）——让自进化本身可计量。

**不做**：自动改规则（提出候选→人审→落地）；把"jev 与规则一致"当作规则正确的证据；在无标签样本上迭代。

**可执行出口（v1.3）**：本回路的落地实现＝**§18 规则集管线**（概念设计已出），`--analyze` 由此获得 `rule_candidates.jsonl` 产出面。

---

## 10. 成本、安全与合规

### 10.1 全口径成本与 ROI（v0.2 新增，P0-3）

| 项 | 预试（M0.5） | 正式轮（M1–M4） | 依据 |
|---|---|---|---|
| 冻结批模型成本 | **29 次 ≈ $0.3**（decision × repeat 1，delivery-20260820 口径） | **~87 次 ≈ $0.9**（repeat 3，n≥30） | [实测] `delivery-20260820` 基线：41% 记录失败、**0.34 failed 子任务/次**、$0.0094/次；**排除低失败臂**（c4-kv-inj 2%／arm_cloud 0%，需 500+ 次） |
| jev 调用 | ≤10 次 ≈ ≤0.02 credits | ≤35 次（30 条×1＋重跑 5）≈ **≤0.06 credits** | [实测] 0.0000366–0.0016 credit/次；5h 窗 cap 16 credits ⇒ <0.4% |
| 人工：盲标（v0.8 预标注/探针辅助） | 8–10 条 × 4–8 分钟 ≈ **0.5–1.5 人时** | 20–30 条 ≈ **1.5–4 人时** | [分析] 人工只做确认/推翻（§7 三层协议） |
| 人工：双标/锚点/报告 | ≈ **2–4 人时** | ≈ **3–6 人时** | [分析] κ 复核＋锚点校准＋M4 报告 |
| **合计** | **≈$0.3 ＋ 2.5–5.5 人时** | **≈$1 ＋ 4.5–10 人时**（n≥30 口径，v1.0） | 人时是本试点的真正大头 |

> 预标注（本地模型优先）与程序化探针的一次性工程 ≈0.5 人日，计入 M1 仪器工作量；若用云端做预标注，按第二条外发通道登记（§10.3）。

- **ROI 框架（M4 必须给出）**：`每批节省人时 = 每批 failed 子任务数 × 单条复核耗时 × 人时降幅`；对照引入后的运维成本（调用费可忽略＋每批审计/操作 ≈0.5–1 人时＋规则维护）。
- **建议产品门槛（M0 确认）**：若 M4 点估 **人时降幅 <25%** 或 **<1 人时/批**，产品结论记为"不引入，保留规则"——条数好看但人时不省不算成功。
- 批成本按既有 bench／成本治理审批；本件登记估算值，试点结束后回填实际值。

### 10.2 前置门（不过不得进入 M3）

- **P0-1 密钥隔离**（[实测] 依据：worker 环境构造处 `env = os.environ.copy()` 未脱敏（`executor.py`，2026-10-05 于 L2924，以语义为准）＋ `subtask.py` 的 `Popen(..., env=env)`；`executor._build_sandbox_env()` 对验证命令环境已脱敏）：
  ① 运营隔离——pilot 调用终端与 agent_go 批运行终端**分离**；批运行环境验收＝`env | grep CMD_API_KEY` 为空；
  ② 建议结构性收口（worker env `pop("CMD_API_KEY")`，参照 swe-eval 先例 e3af494）列为**衍生加固需求**，单独评审、批期禁改。
- **P0-2 批配置不改**：不因试点改闭网/开网策略；agent 一律禁调；不放宽任何 MCP 收口。
- **P0-3 本地落盘即含外发内容/本地映射**：`~/.agent_go/jev/`（含 `review-queue.md`）按"会留存的第三方内容＋本地敏感映射"对待，不外传、不提交仓库。**保留期（v0.4）**：保留至四态结论进 roadmap 归档后 ≥1 个季度（与结论保鲜期一致）；到期清理前须先归档分析件。

### 10.3 审计与额度

- 审计三项（逐次自评／落证／记账）＋**ZDR=off 披露**（一律视为发给会留存的第三方）。
- 429：先读 `resetAt` 再等；**被拒 ≠ 未发送**，仍记审计；串行、不并发；调前用 `--usage` 探测。
- **第二条外发通道（v0.8，仅当用云端预标注时）**：同一份 state/证据会发给预标注模型——须按同一审计三项登记（内容类别自评／落证／记账）并在报告披露；**本地模型预标注不产生外部外发**（首选，§7）。

---

## 11. 交付物与里程碑

### 11.1 里程碑（v0.2 增 M0.5）

| 里程碑 | 内容 | 门槛（不过不进下一步） |
|---|---|---|
| M0 冻结 | 本件评审；suite/n/阈值/粗规则/κ 裁决；**题面冻结（英文结构化）＋τ/O-11 决策**；**owner 认领＋消费方具名**；ADR 立项确认（§15.5 V3） | 未冻结不得开跑 |
| **M0.5 薄预试** | 小批（25–30 次执行→8–10 条 failed）；手工/半自动 packet；≤10 次 B 会话调用 | **4 件事任一不过即停**：H6≥0.80／靶方差<90% 同类／对齐预检通过／粗规则命中率 20–80% |
| M1 池与仪器 | 正式批运行；证据快照；`tools/jev_triage.py`＋`tests/test_jev_triage.py`；`--build/--check` exit 0 | R-J1/R-J2 机械门全过；池按 §4 口径（**Go 需 n≥30**；n 20–29 只出 Conditional）；真实样本占比达 §4 目标，否则报告声明受限 |
| M2 靶 | rubric＋锚点校准；盲标＋耗时；双标一致性 | 锚点全对；**κ≥0.6（双标 ≥10 条）**；分布非退化（<90% 同类） |
| M3 调用 | 人闸门逐条调用；packets/results/audit 落证 | **H6 过**（否则整轮作废） |
| M4 判定 | `--analyze`＋`--queue`；报告＋四态结论＋弃权集画像＋**人时换算** | 判据预注册不可改；结论按 §9.3 后果表承诺 |

**时间盒（v0.4）**：M0.5 ≤1 周；M0.5→M4 全程 ≤4 周；超时未完成 ⇒ 按"不可判"归档（不出半成品结论）。结论保鲜期＝报告日期起 1 个季度（jev 为 early access）。

### 11.2 治理绑定（v0.2 新增，P1-4）

- **Owner**：M0 认领（accountable）；执行/复核分离——标注者 ≠ 分析者，若同一人须在报告披露。
- **消费方**：操作者（复核人）必须在 M0 **具名**，否则 M3 的采纳项无法测。
- **决策论坛**：M4 报告提交下一次 roadmap／PM 评审（30 分钟）；四态结论与后续承诺进 roadmap 或本件附录。
- **交付物**：仪器＋测试、池与证据、预注册记录、调用落证、分析报告（建议落 `docs/design/jev-review-triage-pilot-report-<date>.md`）、**"非作者可复跑"README（含额度探测/429 处置/异常恢复）**、本地复核队列。

---

## 12. 风险与反指标

| 风险 | 反指标（出现即停/降级） |
|---|---|
| over-trust（软排序被当硬结论） | 队列被截断使用；无人工复核记录 |
| 稳定地错（比随机错更危险） | H2 负相关；一致率低于粗规则 |
| 靶污染（单标注者/失盲） | 标注在查看 results 之后完成；κ<0.6 |
| 机械耦合靶（测了平凡规则） | 非机械耦合预检全池命中 ⇒ 不可判 |
| **条数降幅不折成人时降幅（v0.2）** | 人时@recall 反升或降幅 <25% ⇒ 产品结论"不引入" |
| **外部效度不足（v0.2）** | 真实样本 <20% ⇒ 报告声明不可迁移；Go 降级为低风险试点 |
| 数值漂移被当证据 | 单次数值进报告；死区 <0.05 |
| 外发事故 | 禁入项非 0 命中；未写自评即调用；`review-queue.md` 被外发 |
| 密钥泄漏 | 批运行环境含 CMD_API_KEY；子任务可触 `api.commandcode.ai` |
| 供应商依赖 | 闭源不可自托管——排序不可用时原流程不受影响（兜底恒在） |

---

## 13. 开放事项

| ID | 事项 | 归属 | v0.2 状态 |
|---|---|---|---|
| O-1 | H1 的 X | PM | **建议值已填（v0.4），汇总至 §16 F4**：Go 需 n≥30、降幅 ≥2 条且 CI 下界 >0、同时优于粗规则（§9.2）；M0 签署 |
| O-2 | 粗规则冻结 | 域负责人 | **草案＋选择性门已定（§6.3/§9.2），汇总至 §16 F5**；M0 签署 |
| O-3 | 冻结批 suite 选型与批量 | 执行 owner | **已定格（v1.0）**：decision × `delivery-20260820` 口径；预试 29 次／正式 ~87 次（§16 F10） |
| O-4 | 单仪器 vs 双仪器（并入 swe-eval `--pool`） | PM／两仓 owner | 待定；不影响本件协议 |
| O-5 | 是否升格 EXP/ADR；owner 认领；roadmap 登记 | PM | 待定（M0 必答） |
| O-6 | 双标比例、锚点样例集 | 域负责人 | **κ≥0.6（M2，双标 ≥10 条）已定（v0.4）**；比例 ≥20% 已定；样例集待选 |
| O-7 | `patch_excerpt`／`agent_final_excerpt` 实验变量开关与消融计划 | 执行 owner | 待定（对应 JEP-1 §17 后续①） |
| O-8 | 结构性密钥收口（worker env pop CMD_API_KEY）是否单独立项 | PM | 待定 |
| O-9 | ROI 产品门槛（人时降幅 25%／1 人时/批）确认 | PM | **建议值已填（§10.1），汇总至 §16 F6**；M0 签署 |
| O-10 | 架构侧待验证 V1–V7（含 ADR 立项、升格条件、tools 质量门） | PM／架构 | 见 §15.5；M0 冻结时确认 |
| O-11 | 低置信阈值 τ 与 noul 第二排序器取舍（§6.1 rank 规则 #3/#5） | PM／域负责人 | **已落地（v1.1）**：τ=**0.60**＋探索曲线不变；noul 第二排序器**已加为 Q3**（同调用、仅探索对照、不进判据）——题面重冻 v1.1；见 §16 F1/F3 |
| O-12 | 数据闭环与规则收敛第二阶段（缺口 G1–G7，§9.4）：挖掘工具、留出验证、落地出口 ADR；**＝H3 自进化的规则迭代回路**（v1.2 登记，四道闸见 §9.4） | PM／域负责人 | **不在本轮冻结**（§16）；Go/Conditional 后议 |
| O-13 | 预标注模型选型（本地优先）与 probe 适用边界（非确定性用例、无 worktree 样本） | 域负责人／执行 owner | **部分已落地（2026-10-05）**：probe 侧边界已实现并固化（安全门/副作用跳过/无 worktree→`worktree_absent`/超时→`undecidable`）；**LLM 预标注选型未定**（`prelabels.jsonl` 未实现）——M1 前定；见 §7 |
| O-14 | **规则集管线立项**（概念设计已出：受限 DSL／清单／影子→生效两态／四闸／P0–P2）＋新 ADR（规则执行面＋全局规则数据面） | PM／架构 | **P0 已落地（2026-10-05，离线、零 runtime 接入）**：`agent_go/rule_set.py`＋41 例测试（含 `holdout_sha`/`promote --report` 补强）；P1（影子）仍待立项＋新 ADR——Go/Conditional 后与 O-12 同批；见 §18 |
| O-15 | **S-1：spec 覆盖扫描 → TDD 输入**（覆盖/证据前瞻可测＋格位风险回溯借用；缺口映射为 TDD 靶驱动 ADR-012 起草；A/B 自证）＋新 ADR（spec 全文外发面） | PM／架构 | **P0 已完成（2026-10-05，见 [findings](s1-coverage-audit-findings-20261005.md)）**：缺口效应经同批同模型对照**不成立**（29% vs 29%，OR=0.99，p=0.87；缺口＝仪表缺口）⇒ **靶改用 `warning`（规则不确定）群体**（同批同模型 +12~37pp）；仪器＝`tools/s1_coverage_audit.py`／`tools/s1_spec_scan.py`；**下一步＝S-1 A/B**（预注册＝概念设计 §10.1；唯一硬阻塞＝环境占用）；见 §18 |

---

## 14. 来源与引用

- swe-eval：`docs/jev-paradigm-20260926.md`、`docs/jev-call-card-20260927.md`、`docs/jev-efficacy-preregistration-20261004.md`、`docs/jev-verification-requirements-20261004.md`、`results/jev/{ex03,ex04,mvp-efficacy-ars-20261004}/`、`scripts/jev_mvp_efficacy.py`、`scripts/jev_mvp_caller.py`
- llama.cpp：`tools/jev_mcp_server.py`、`tools/jev_quota.py`、`docs/06-reference-metrics/{jev-mcp-tool,jev-quota}.md`
- 官方 cookbook（2026-10-05 通读本地镜像 `~/study/jev_datawhale/jev-cookbook`＝datawhalechina.github.io/jev-cookbook）：`concepts/state`（语言支持）、`concepts/how-to-build-with-system-one`（能用代码就用代码／综合运用）、`primitives/choice`（结构化 criteria、同调用多问）、`cookbooks/{rerank_typesafe,parallel_questions,consistency_choice}`（逐候选打分、批量不变性、阈值弃权）、`confidence`、`patterns/confidence-routing`、`model-jaggedness/jev-1.13`（九条失败模式）
- agent_go：[ISSUES.md](../ISSUES.md) #29/#31/#40/#51、[result-schema.md](result-schema.md)、[m0-failure-class.md](m0-failure-class.md)、[m0-bench-schema.md](m0-bench-schema.md)、[humility-layer-design.md](humility-layer-design.md)、[verification-design.md](verification-design.md)、`agent_go/{executor,subtask,pipeline}.py`
- 架构核验（2026-10-05）：`pyproject.toml`（打包面 `include = ["agent_go*"]`）、`.github/workflows/test.yml`（ruff/mypy 仅 `agent_go/`，pytest 全量）、[module-catalog.md](module-catalog.md) §模块变更规则、`tools/*.py` 先例（导入方向 3 处、输出方式 9/9 用 `print`）、`agent_go/{trajectory_signals,evidence,cross_judge,assessment,config,utils}.py` 复用面实测
- 规则集管线范式（[实测]，2026-10-05 读取）：swe-eval `results/experiments/exp-20260930-splash-efficacy-r2/batch.json`（运行中；3 臂 × 12 实例 × 2 重复，已判定 30 runs：**tdd 7/10 vs nudge 2/10 vs plain 1/10**，配对 tdd 对 plain 5胜4平0负、对 nudge 5胜5平0负）；机制模板＝[ADR-012](adr/ADR-012-spec-to-test-pipeline.md)
- 实测数据（2026-10-05 扫描 `eval_suite/` 111 个结果文件）：1699 条记录 / 494 条 failed / 每条失败记录 1.18 条 failed 子任务 / 1432 条成本记录 mean $0.0513、median $0.0200、p90 $0.0944 / 历史 task_dir 存活 3/494

---

## 15. 模块落位与降级设计（v0.3 新增）

> 本节回答三件事：功能模块归属与落位、替代方案对比、失效时的降级设计。结论供 M0 冻结确认（待验证项见 §15.5）。

### 15.1 功能模块拆解与归属

| 功能单元 | 现有可复用件（实测） | 归属 |
|---|---|---|
| F1 池构建（bench 结果 × task 产物 join） | `bench_schema.validate_record`（入池校验）；meta.json 读取无 canonical loader（各模块内联读） | 自研（薄） |
| F2 证据快照（result.json/diff/日志） | `evidence.py materialize_evidence`：概念同源（不可变＋哈希）、形态不同（批次级证据包 vs 逐子任务产物）⇒ 不直接复用，哈希命名对齐其惯例 | 自研 |
| F3 轨迹信号提取 | **`trajectory_signals.collect_subtask_signals`**（只读纯函数、fail-open、不改判定） | **仓内复用** |
| F4 state 构建与脱敏 | 无现成路径脱敏件；`utils._safe_append_to_file`（审计落盘） | 自研（纯函数＋测试钉住） |
| F5 合规门（禁入/白名单/lint/指纹） | 仓库 `lint.py` 是代码 AST lint，不同域，不并入 | 自研（纯函数，fail-closed） |
| F6 问题包与人闸门 | 无 | 自研 |
| F7 调用编排 | llama.cpp `tools/jev_mcp_server.py`（stdio JSON-RPC）；swe-eval `jev_mvp_caller.py` 已验证"spawn 既有 server、不重实现 provider API、不处理密钥"模式 | **跨仓进程级复用** |
| F8 靶与标注（盲标/耗时/双标 κ） | `cross_judge.calibrate_judge`＋`_read_human_csv`（人工校准惯例，格式对齐、不直接复用代码）；`assessment.py` 对象不同 | 自研 |
| F9 分析器（H6/effort/precision/Spearman/聚类 bootstrap/人时/弃权） | 全仓无现成实现（实测零命中）；stdlib 满足 | 自研（纯函数＋固定种子） |
| F10 队列与记账落盘 | `config.AGENT_GO_DIR`、`utils._safe_append_to_file`；"复核队列"概念全仓不存在（无重复冲突） | 自研（只读输出） |

依赖方向实测：`tools/ → agent_go` 有先例（analyze_knowledge_ab / recompute_bench_results / spec_smoke 三处），**反向为零**（`agent_go/` 无 import `tools`）⇒"包外工具、按需导入包内纯函数"合法，无需复制代码。

### 15.2 落位判定：包外 dev 工具（不上收产品包）

试点仪器落 `tools/jev_triage.py`（dev-only），**不**上收 `agent_go/` 包。实测依据（2026-10-05）：

| # | 依据 | 结论 |
|---|---|---|
| 1 | `pyproject.toml` `include = ["agent_go*"]` | 包外工具不随 wheel 分发 |
| 2 | CI `ruff/mypy` 只查 `agent_go/`；`pytest tests/` 全跑（tests 可导入 tools，先例 `test_markdown_links.py`） | 留包外＝有测试、无 lint/类型门（**代价项**，见 V2） |
| 3 | 依赖方向单向（见 §15.1） | 可直接复用包内纯函数 |
| 4 | 模块变更规则（module-catalog）：新增核心模块/公共接口/边界变更才触发义务 | 留包外免 catalog/spec 义务；**外发边界一项仍应补 ADR**（V3） |

**升格触发条件**（满足任一即评估上收包内，并同步 catalog/spec/ADR 与 Console）：①M4 判 Go 且队列并入 `review` 流程被 runtime 消费；②需要公共接口（CLI 子命令或被其他模块 import）；③需进 ruff/mypy 门以支撑长期维护。现 tools 脚本输出先例全用 `print`（实测 9/9），上收时须改用 `Console`。

### 15.3 对比

**（a）落位四方案**

| 判据 | A 包内核心模块＋CLI | **B 包外 dev 工具（选定）** | C 跨仓复用 swe-eval 仪器 | D 服务化 |
|---|---|---|---|---|
| 运行时隔离 | 弱 | **强** | 强 | 强 |
| 分发面 | 随包发布 | **不发** | 不发 | 不发 |
| CI 质量门 | 全覆盖 | 仅 pytest | 仅 pytest | 仅 pytest |
| 文档变更成本 | catalog＋spec | 仅 ADR | 跨仓双方 | catalog＋ADR |
| 跨仓耦合 | 无 | **无** | 高（改其白名单＋回归） | 无 |
| 结论 | 升格后可选 | **本轮选定** | 备选（O-4） | 否决（无消费方） |

**（b）机制对比与"能否作降级腿"**

| 机制 | 可复现性 | 可用性 | 外发 | 可否作 jev 降级腿 |
|---|---|---|---|---|
| jev | 标签稳、数值漂 0.01–0.04 | 远程＋额度＋闭源 | 是（逐次审计） | 主路 |
| 粗规则 | 完全 | 本地零成本 | 无 | ✅ 一级 |
| 默认序 | 完全 | 本地 | 无 | ✅ 二级 |
| LLM judge | 低（方差 92–913×，JEP-1 §4.1） | 现有 worker | 视部署 | ❌ **禁止**（升级腿＝人；judge==candidate 缺位） |
| 纯人工清单 | 完全 | 人时最高 | 无 | ✅ 终态（流程不变） |

### 15.4 降级设计

**15.4.1 服务级阶梯（单向、不可横向换腿）**

`jev 排序 → 粗规则序 → 默认序 → 纯人工清单`。每一跳不改 verdict、不改流程、不新增外发；**止于规则/人工**。

**15.4.2 组件级降级矩阵**

| # | 失效模式 | 降级行为 | 影响面 | 恢复 |
|---|---|---|---|---|
| 1 | jev 429/额度耗尽 | 暂停调用、读 `resetAt` 改期；**不得用半数样本硬出结论** | 仅延期 | 窗口重置后续跑（阶段幂等） |
| 2 | jev 不可用/变更/下线 | 终止本轮；粗规则序仍可本地生成；结论标"保鲜期" | 排序收益 0，复核回默认序 | 重新立项评估 |
| 3 | H6 不过 | 整轮作废（预注册内建），归档仪器负结果 | 无 | 重出样 |
| 4 | 靶退化/κ<0.6 | 降级为描述性报告，不出 Go/Conditional | 决策延后 | 修 rubric/补样重标 |
| 5 | 池不足/产物被清 | 快照兜底→补批→否则"不可判" | 决策延后 | 补批 |
| 6 | 合规门命中 | **fail-closed**：停调用、修包重冻，不许豁免 | 延期 | 修包 |
| 7 | 密钥混入批运行环境 | 前置门拦截（env 验收不过即停） | 延期 | 环境分离后重跑 |
| 8 | MCP 注册漂移（实测：`~/.claude` 副本为 9/26 旧版） | 改用 ZCode launcher 指仓内 server；再退＝只跑零外发三段＋会话内手工调用 | 仅调用期 | 统一锚点（V7） |
| 9 | 无法双标 | 单标披露＋降级为探索性（**不进 Go 判据**） | 结论强度降级 | 补第二标注者 |
| 10 | 工具崩溃/磁盘异常 | 阶段幂等可重跑；只读不改任务数据 | 延期 | 重跑 |

**15.4.3 三条架构不变量（保证降级可行）**

1. **阶段幂等、状态全在磁盘**：build/check/label/analyze 各自可重跑，无内存状态；
2. **单点网络能力**：新代码不含 provider API 实现，网络只发生在既有 MCP server 进程内；可选 caller 仅 spawn 该 server 并转发 env（不读密钥内容）；
3. **fail-closed 合规、fail-open 读取**：出境侧检查失败必停（§2.2 `--check` 退出码），读取侧失败不阻断（与 `trajectory_signals` 语义一致）。

### 15.5 待验证清单（M0 冻结时确认）

| ID | 事项 | 归属/时点 |
|---|---|---|
| V1 | 升格条件仲裁（§15.2 三条触发条件是否成立） | PM/架构，M0 |
| V2 | tools/ 质量门：接受"无 ruff/mypy"，或上收纯函数，或给 CI 加 tools/ 检查 | 架构，M1 前 |
| V3 | **补 ADR**：新外发边界＋新数据面（模块变更规则要求） | 架构，M0 |
| V4 | 复用面复核：`cross_judge` 人工校准格式、`assessment` 事件层可否同源 | 执行 owner，M1 |
| V5 | 快照/指纹与 `evidence_hash` 命名与校验惯例对齐 | 执行 owner，M1 |
| V6 | 输出规范：留包外可用 `print`；若上收必须改 `Console` | 随 V1 |
| V7 | 跨仓锚点：llama.cpp server 的固定文件锚点（已出现双副本漂移） | 执行 owner，M1 |

---

## 16. M0 冻结清单与签署（v0.6 新增）

> M0 的目标是把"逐项拍板"变成"一次签署"。下表汇总全部冻结项：**F1–F10 已填建议值**（依据见对应章节；F10 的 worker 模型待定），**F11–F12 待具名、F13 提案已备待批准**。签署即冻结，进入 M0.5。

### 16.1 冻结项

| # | 冻结项 | 建议值／状态 | 依据 |
|---|---|---|---|
| F1 | 题面（Q1/Q2/Q3 英文结构化全文） | **已定格（v1.1 重冻）**：Q3 `ranking_noul` 第二排序器已加（同调用；仅探索对照，§6.1/§9.1） | 官方 cookbook（语言/结构化 criteria/rerank 配方/同 state 多问题） |
| F2 | 低置信阈值 τ | **0.60**（对 `P_max` 的门）；探索性预注册 τ∈{0.50,0.60,0.70} 曲线（不进主判据） | 官方一致性实验默认 |
| F3 | noul 第二排序器 | **本轮不加**；列为 Conditional/Go 之后的另轮候选（届时重新预注册） | 最小化冻结面；choice 侧有本仓正增量先例；noul↔choice 不可互换（1.13 #8） |
| F4 | H1 | n≥30、降幅 ≥2 条、聚类 bootstrap CI 下界 >0、同时优于粗规则 | §9.2 |
| F5 | 粗规则＋选择性门 | §9.2 表达式；命中率 20–80%（越界按 §6.3 处置） | §6.3/§9.2 |
| F6 | ROI 产品门槛 | 人时降幅 <25% 或 <1 人时/批 ⇒ 判"不引入" | §10.1 |
| F7 | 一致性门 κ | M2 双标 ≥10 条、κ≥0.6（报 CI） | §7/§9.2 |
| F8 | 规模与预算 | M0.5：25–30 次执行；M1：约 85 次执行（n≥30）；jev 调用 ≤0.06 credits（5h 窗 <0.4%） | §4/§10.1 |
| F9 | 时间盒 | M0.5 ≤1 周；全程 ≤4 周；超时按"不可判"归档；保鲜期 1 个季度 | §11.1 |
| F10 | 冻结批 suite 选型（含 worker 模型） | **已定格（v1.0，含数据纠正）**：`decision` 套件 29 个互异任务（fixture 已核）＋**配置对齐 `delivery-20260820` 基线口径**（`models=claude-sonnet-4-6`、经本地代理路由、`--with-delivery`）——该批实测 41% 记录失败、**0.34 failed 子任务/次**、$0.0094/次。预试＝**29 次（repeat 1）**（预期 ~10 条 failed，≈$0.3）；正式＝**~87 次（repeat 3）**（n≥30，≈$0.9）。**明确排除**：最近批 c4-kv-inj（2% 失败）与 arm_cloud（0%）——按它们需 500+ 次，不可行；跑前先以小批探测实际失败率再定正式量 | O-3；§17.5 |
| F11 | Owner（accountable） | **已认领（2026-10-05）**：`jinsongwang`（可与执行同人，报告披露） | §11.2 |
| F12 | 消费方（操作者/复核人） | **已具名（2026-10-05）**：`jinsongwang`（本仓唯一人类操作者；owner≠分析者的分离要求见 §16.2） | §11.2/§9.2 |
| F13 | ADR 立项（外发边界＋新数据面） | **已批准（2026-10-05）**：[ADR-013](adr/ADR-013-jev-offline-triage-egress.md) → Accepted | §15.5 V3 |
| F14 | 数据闭环三件（v0.7 新增） | **已定格为仪器要求**：回包 `model` 版本记录、复核 `outcome` 回写、`labels.jsonl` 跨轮累积；预标注/探针记录含模型/命令与版本（v0.8） | §6.2/§7/§9.4 |

> §9.4 的规则收敛（O-12）**不在本轮冻结范围**，Go/Conditional 后另立。

### 16.2 角色与分离（认领时按此具名）

| 角色 | 职责 | 分离要求 |
|---|---|---|
| Owner（accountable） | 对本件冻结与最终结论负责；裁决 κ/靶争议与是否再跑 | 可与执行同人，但须在报告披露 |
| 执行（仪器与流程） | build/check/queue 与全部落证、记账 | — |
| 主标注者 | 盲标 `labels.jsonl`＋记录单条耗时 | **不得先看 `results.json`** |
| 第二标注者 | 双标 ≥10 条（M2） | 独立于主标注者 |
| 分析者 | `--analyze` 与 M4 报告 | **≠ 主标注者**；若同人必须在报告披露 |
| 消费方（操作者） | 按 `review-queue.md` 实际复核并留痕（采纳与采纳质量计量） | 必须具名 |

### 16.3 签署

| 项 | 名字 | 日期 |
|---|---|---|
| Owner 认领（F11） | jinsongwang | 2026-10-05 |
| 消费方具名（F12） | jinsongwang | 2026-10-05 |
| PM 确认（F2/F4/F6/F9） | jinsongwang（按建议值：τ=0.60；H1＝n≥30＋≥2 条＋CI>0＋胜粗规则；ROI 25%／1 人时；时间盒 ≤4 周） | 2026-10-05 |
| suite 选型（F10） | jinsongwang（decision × `delivery-20260820` 口径） | 2026-10-05 |
| ADR 立项（F13） | jinsongwang（ADR-013 → Accepted） | 2026-10-05 |

---

## 17. 初步设计与推进计划（v0.9 新增）

> 面向实现。落位与降级见 §15，冻结项见 §16——本节只写"怎么建、怎么开跑"。

### 17.1 组件与落位

`tools/jev_triage.py` 单文件（stdlib、零网络、包外 dev 工具，§15.2）；测试 `tests/test_jev_triage.py`（CI 可跑、零外发）。分层：

```
池层    build_pool() / snapshot_evidence() / manifest()
状态层  whitelist_map → sanitize → truncate → state_bytes（字节级确定）
题面层  FROZEN_QUESTIONS（§6.1 英文常量）→ questions.json + sha
门禁层  forbidden_scan / whitelist_check / lint / control_base_rate /
        coarse_selectivity / verify_fingerprints      # fail-closed，exit 1
排序层  rank_key（P(content_fix)↓, run_ref↑）/ abstain_marks（双通道）
标注层  probe → prelabel → label_session（人工终审）＋耗时
分析层  h6 / effort@recall / precision@k / spearman_mean_rank /
        cluster_bootstrap / human_hours / abstain_profile / flip_metrics / decide
队列层  render_queue_md（本地映射，不外发）
调用层  call（可选）：spawn 既有 MCP server（JSON-RPC stdio；无网络实现）
审计层  append_jsonl / 自评模板 / 记账
```

### 17.2 数据契约（与 §3 落盘一致）

| 文件 | 内容 | 关键不变式 |
|---|---|---|
| `pool.jsonl` | 每行一个 failed 子任务（run_ref/状态/证据路径） | run_ref 稳定；重出样逐位一致 |
| `state/<run_ref>.json` | 外发 state（白名单键） | **字节级可复算**（固定键序/缩进/UTF-8） |
| `questions.json` | 英文题面全文 | sha 与 manifest 一致；测试钉死 |
| `manifest.json` | 三个指纹＋批次/池组成 | 篡改必被 `--check` 拒 |
| `results.json` | 每调用：run_ref／model（回包版本）／choice／probabilities／usage／ts | 完整性＝调用数==样本数 |
| `labels.jsonl` | 人工终审标签（append-only，本轮＝跨轮同文件；每条带 `questions_sha256`） | 只收人工（`--label`／`--import-labels`）；预标/探针不写入；`--analyze` 拒收 sha 不一致的历史标签 |
| `prelabels.jsonl`／`probe.jsonl` | 预标注初稿／探针结果 | 含模型/命令版本；永不覆盖 |
| `review-queue.md` | rank→task/subtask 映射 | 本地物料，禁止外发 |
| `caller-audit.log`／`README.md` | 审计三项与记账 | 逐次自评 |

### 17.3 实现要点

- **字节级复算**：排序键固定、`ensure_ascii=False`、UTF-8——R-J2d"逐字节一致"依赖它；
- **统计**：Spearman 用**均值秩**；bootstrap **按 task 聚类**、固定 seed；n 不足返回"不可判"而非崩溃；
- **门禁语义**：出境 fail-closed（`--check` exit 1 禁调用）；读取 fail-open（与 `trajectory_signals` 一致）；每阶段 tmp+`os.replace`；429 中断 → `--analyze` 拒绝出判据（完整性门）；
- **边界**：只写 pilot 目录；一条 mock 测试断言"无 runtime 路径写入"；探针执行前过 `_is_safe_verification_command`。

### 17.4 工作量

| 组件 | 人日 |
|---|---|
| 池＋快照 | 0.5 |
| state＋脱敏 | 0.5 |
| 门禁＋lint＋指纹 | 1.0 |
| 统计＋四态 | 1.0 |
| 标注（probe/预标/终审）＋队列＋审计 | 0.5–1.0 |
| 测试（40–60 例＋合成池端到端） | 1.0–1.5 |
| **合计** | **4.5–6.0 人日** |

### 17.5 推进计划（M0 → M0.5）

**序列**：M0 签署（§16.3 五项）→ 实现薄版工具（0.5–1 人日，零外发；不含 queue/outcome 完整面）→ 跑预试批 → `--build/--check` → `--packets`＋人审＋逐条自评 → ≤10 次 B 会话调用 → `--analyze --stage pilot` → **四件事门**（H6／靶方差／对齐／粗规则选择性）→ 决定是否投 M1。

**预试批命令（decision × repeat 1，29 次执行；配置对齐 `delivery-20260820` 基线）**：

```bash
# 批终端（须与调用终端分离；env 无 CMD_API_KEY；本地代理已启动）
agent_go eval bench --tasks eval_suite --suite decision --repeat 1 \
  --candidate-models claude-sonnet-4-6 --with-delivery \
  --output eval_suite/results_<pre-batch>.jsonl \
  --source-batch <pre-batch-id> --bench-parallel 2 --yes
# 批后：不执行 clean；立即快照（工具 --build 即做）

# 正式批（M1，预试四件事全过后）：--repeat 3（约 87 次），其余同上
```

**开跑前置检查清单**：
- [ ] §16.3 五项签署完成（suite＋模型／owner／消费方／ADR-013／PM 确认 F2/F4/F6/F9）
- [ ] 调用终端与批终端分离；`env | grep CMD_API_KEY` 在批终端为空
- [ ] suite fixture 齐备（decision 29 任务 repo 全在——2026-10-05 已核）；预算登记（预试 ≈$0.3／正式 ≈$0.9；jev ≤0.02 credits）
- [ ] 结果文件与 `--source-batch` 命名登记；跑后禁 `clean`

**已备材料（2026-10-05）**：suite 建议（decision，含 fixture/失败率/成本实测）；[ADR-013](adr/ADR-013-jev-offline-triage-egress.md) 提案；本节命令与清单。

---

## 18. 规则集管线（第二阶段实现；v1.3 登记）

> 概念设计全文：[rule-set-pipeline-design-20261005.md](rule-set-pipeline-design-20261005.md)。本节只留登记要点，避免两处漂移。

- **实证前提（[实测]，2026-10-05；未跑完声明）**：swe-eval `exp-20260930-splash-efficacy-r2`（3 臂 × 12 实例 × 2 重复 = 72 runs，已判定 30 runs / 11 实例）——**arm_tdd 7/10** vs arm_nudge 2/10 vs arm_plain 1/10；配对：tdd 对 plain **5 胜 4 平 0 负**、对 nudge **5 胜 5 平 0 负**。机制（可迁移部分）＝ADR-012 四护栏：**生成 → 人审冻结 → 只读 → 重放**。
- **管线四步**：①生成（证据驱动：人工标签＋程序化探针；**禁读 jev 输出当标签**，jev 仅作缺口指针）→ ②人审（复用既有 CLI/web/MCP 确认渠道）→ ③冻结（`rules.jsonl`＋`frozen_sha256`，执行前校验）→ ④重放（**影子：只记不动** → 验证后 opt-in 生效）；迭代＝新标签复验、旧样本不得回退、扩张后按同一预注册判据重度量。
- **与试点的接口**：`tools/jev_triage.py --analyze` 增出 `rule_candidates.jsonl`（feature/op/threshold/cover_n/precision/recall/evidence_refs）→ 人审 → `agent_go rules promote`；本件 §9.4 的回路由此获得**可执行出口**。
- **数据面与两态**：`~/.agent_go/rules/rules.jsonl`（清单）＋`<task_dir>/rule_decisions.jsonl`（执行记录）；影子期不改变任何既有行为；项目级覆盖全局（合并语义须在 ADR 固化）。
- **四道闸与 §9.4 一一对应**：标签源（只许真值）／验证（留出 ≥100 条跨批＋回归不得回退）／落地（代码＋测试＋信任门＋ADR）／反哺计量（jev 增量区应收缩）。
- **阶段**：P0 离线（~1–1.5 人日，纯本地文件、不触边界）／P1 影子（~1–2 人日，接 runtime 后置点，**ADR 草案先行**）／P2 生效（独立 ADR＋信任门；规则可进 verdict——这是与 jev 的本质区别，jev 永不进）。
- **不做**：不用 jev 输出当标签；不自动改规则/自动生效；不引入外部规则引擎依赖（自研最小 DSL，stdlib）；不替换现有硬编码规则（影子先行）；不在无标签样本上迭代。
- **立项**：**O-14**（Go/Conditional 后与 O-12 同批）；实现物＝`agent_go/rule_set.py`＋`agent_go rules` CLI＋测试。
- **延伸落点（v1.4 登记，S-1）**：把覆盖/风险度量**前移到开发前**——对新 spec／修复做"规则+jev"覆盖扫描，缺口直接映射为 TDD 靶驱动 ADR-012 起草。**两分法**：覆盖/证据完整度＝前瞻可测；有效性＝事后才可测（等 `outcomes.jsonl` 回填）；格位风险＝留出表（≥100 标签）就绪后可用。**映射**：J-only⇒必须显式可执行测试；双缺⇒补证据/人工裁决、**不进入开发**；R+J 低风险⇒沿用；R-only⇒回归测试钉住。**自证**＝A/B（覆盖驱动 vs 均匀生成，同测试预算比 resolved/首过/返工/缺陷逃逸，配对＋McNemar）。**边界**：不混入本试点（spec/plan 全文外发面更大→B 情境＋逐次自评、闭网批禁用）；jev 仍建议性（CON-1）；case 结果必须回填否则闭环断裂。详见概念设计 §10；立项＝**O-15**。**P0 已完成（2026-10-05，v0.3 修正）**：仪器 `tools/s1_coverage_audit.py`（13 例）＋[findings](s1-coverage-audit-findings-20261005.md)——**缺口效应经同批同模型对照后不成立**（29% vs 29%，OR=0.99，p=0.87；缺口 98% 来自旧批缺 telemetry ⇒ 读作**仪表缺口**）；**成立的是 warning vs passed 的区分度**（同批同模型 +12~37pp）；两伪迹定案、验收覆盖字段从未产出。**S-1 的靶改用 warning 群体**；方法本身仍待 A/B。**P0 仪器已落地（2026-10-05）**：`tools/s1_spec_scan.py`（缺口清单扫描，7 例）＋`plan_acceptance_coverage` 结构性回退口径（`plan_coverage_basis` 字段，`planning.py`）；**A/B 预注册**见概念设计 §10.1（含 kill 判据；唯一硬阻塞＝环境占用）。

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.8 | 2026-10-05 | **薄版仪器补齐（不触碰冻结测量面）**：①**程序化探针落地**（`--probe`）——干净环境复跑最近一轮验证命令 → `probe.jsonl`；同源沙箱 env/资源上限、`shlex.split` 不经 shell、执行前过 `_is_safe_verification_command`（不绕过）、副作用模式默认跳过（`--probe-force` 才执行）、`--confirmed` 才执行、结果不进 state；判读 env_or_harness／content／undecidable＋`probe_summary.json`（含探针-人一致率＝探索性）；②§7 标注三层实现状态与 O-13（预标注选型）登记；③`--label`/`--import-labels` 与 `--call` IPC 健壮性、完整性门集合比对见 v1.7（本次一并声明测试 36 例）；④**未实现项**：`prelabels.jsonl`（LLM 预标注）、`outcomes.jsonl` 回写（v0.7 登记）。 |
| v1.7 | 2026-10-05 | **薄版仪器补齐实现（不触碰冻结测量面）**：①`--label`（盲标＋单条耗时）与 `--import-labels`（批量导入人工标签：整批校验、一处不合规即拒收、已标自动跳过）落地，每条标签带 `questions_sha256`，`--analyze` 拒收跨 rubric 标签；②落盘命名统一为 `labels.jsonl`（本轮＝跨轮同文件，跨轮以 `--import-labels` 合并）；③`--call` IPC 健壮性修复——单次 JSON-RPC 真超时（后台读线程）、server stderr 落 `caller-server.err.log`（不再用 PIPE 顶死 server）、`terminate→wait→kill` 回收、超时条目带 `error` 落盘且重跑只补未完成项；④完整性门改按 `run_ref` **集合**比对并拦截错误/空回包，H6 分母不再因缺 `control` 问项而静默缩小；⑤v0.8 预标注/探针（`prelabels.jsonl`/`probe.jsonl`）与本薄版不内置的 `--usage`（复用 llama.cpp `tools/jev_quota.py`／MCP `jev_usage`）**仍未实现**，登记为剩余缺口；⑥§17.5 开跑手册的命令同步到实现形态（`--out` 必填、`--results/--limit`、`--record` 回填、`--import-labels`、`--server/--rpc-timeout`）。 |
| v1.6 | 2026-10-05 | **状态同步（不触碰冻结测量面）**：①O-15 更新为 **S-1 P0 已完成**——缺口效应经同批同模型对照不成立（29% vs 29%，OR=0.99）、靶改用 `warning` 群体、下一步＝A/B（预注册 §10.1）；②O-14 的 P1 门 **ADR-014 已起草（Proposed）**（`docs/design/adr/ADR-014-rule-set-execution-plane.md`）；③删除与 `s1-coverage-audit-findings` 重复的临时分析件（本会话去重）。 |
| v1.5 | 2026-10-05 | **状态更新（不触碰冻结测量面）**：O-14 标注 **P0 已落地**（`agent_go/rule_set.py`＋37 例测试；CLI `python3 -m agent_go.rule_set`；零 runtime 接入）；P1 影子仍待立项＋新 ADR；概念设计升 v0.3、roadmap §H3 同步。 |
| v1.4 | 2026-10-05 | **登记更新（不触碰冻结测量面）**：§18 增延伸落点 **S-1：spec 覆盖扫描 → TDD 输入**（两分法：覆盖/证据前瞻可测、有效性事后回填、格位风险借留出表；缺口→TDD 靶映射；A/B 自证；四条边界）；新增 **O-15** 立项；概念设计升 v0.2（§10 详版＋R-6）。 |
| v1.3 | 2026-10-05 | **登记更新（不触碰冻结测量面）**：新增 **§18 规则集管线**（第二阶段实现；概念设计 [rule-set-pipeline-design-20261005.md](rule-set-pipeline-design-20261005.md)）——以运行中的 swe-eval tdd 臂实测（`exp-20260930-splash-efficacy-r2`：tdd 7/10 vs nudge 2/10 vs plain 1/10、配对零负；未跑完声明）为范式，登记管线四步、受限 DSL、数据面/两态、四闸映射与 P0–P2；§9.4 增"可执行出口"指针；新增 **O-14** 立项；§14 增 tdd 臂证据来源。 |
| v1.2 | 2026-10-05 | **登记更新（不触碰冻结测量面）**：§9.4 增"与 H3 自进化的关系"——规则收敛回路＝H3 自进化的第一条具体回路（规则迭代），并写入四道闸（标签源只许真值／留出验证＋回归／代码＋测试＋信任门／扩张后按同一判据重度量）与三条"不做"；O-12 标注为 H3 规则迭代回路。冻结面（题面/判据/阈值/哈希）逐位不变。 |
| v1.1 | 2026-10-05 | **题面重冻（Q3 noul 第二排序器落地）**：§6.1 增 Q3 `ranking_noul`（英文结构化、true/false criteria；官方 rerank 配方形态），Q1–Q3 **同调用发送**（同 state 一次发送、服务端并行评估——①+③ 形态落地）；§9.1 增"第二排序器（探索性）"指标（**不进判据**）；O-11 改判为"已落地"；§16 F1 更新为 v1.1；⚠️ 因 v1.0 无任何 jev 调用发生（预试批中止于基础设施侧），本次重冻**不废弃任何既有数据**。仪器同步：`tools/jev_triage.py` 题面常量＋Q3 解析＋探索指标；`tests/test_jev_triage.py` 重钉 hash（21 例全绿）。 |
| v1.0 | 2026-10-05 | **M0 签署完成并冻结**：§16.3 签署表填写（owner／消费方＝jinsongwang；PM 确认 F2/F4/F6/F9；suite＋模型；ADR-013）；F11/F12/F13 状态更新；**F10 数据纠正**——批配置对齐 `delivery-20260820` 基线口径（41% 记录失败、0.34 failed 子任务/次、$0.0094/次），预试＝decision × repeat 1（29 次 ≈$0.3）、正式＝repeat 3（~87 次 ≈$0.9），并**明确排除低失败臂**（c4-kv-inj 2%／arm_cloud 0%，需 500+ 次）；§4/§10.1 成本口径与 §17.5 命令同步；O-3 状态定格；[ADR-013](adr/ADR-013-jev-offline-triage-egress.md) → Accepted。 |
| v0.9 | 2026-10-05 | 推进材料三件套：**新增 §17 初步设计与推进计划**（组件分层、数据契约表、实现要点、4.5–6.0 人日工作量、M0→M0.5 开跑手册与前置检查清单）；**suite 建议定格**——F10 填 `decision`（29 个互异任务、fixture 已核、失败率 21%、$0.027/次；M0.5 跑 35–40 次≈$1.0–1.1，M1 约 120 次≈$3.2；worker 模型待定）；**ADR 提案**——新增 [ADR-013](adr/ADR-013-jev-offline-triage-egress.md)（Proposed：外发边界/本地数据面/密钥前置门/降级/升格条件），F13 指向之，并登记进 `adr/README.md`；§0 增材料已备要点。 |
| v0.8 | 2026-10-05 | 增 **§7 预标注与辅助真值协议**（三层：程序化探针＋LLM 预标注＋人工终审）——**真值仍是人**；探针＝干净环境复跑验证命令（仍过 `_is_safe_verification_command`、结果不进 state、失败形态判读），LLM 预标注＝本地模型优先（云端则按第二条外发通道登记 §10.3）、不同家族、不看 jev 结果；**不覆盖原则**与 `prelabels.jsonl`/`probe.jsonl` 落盘（§3）；§9.1 增"辅助真值一致率"探索指标（不进判据）；§10.1 人工工时 7–16 → **4.5–10 人时**（附 0.5 人日一次性工程）；新增 **O-13**（预标注选型与 probe 边界）；§16 F14 增记录版本要求。 |
| v0.7 | 2026-10-05 | 增 **§9.4 数据闭环与规则收敛（第二阶段，非本试点判据）**：登记缺口 G1–G7（回包 model 版本、复核结果回写、跨轮标签库、无挖掘步骤、样本量差一个量级、合法流程、落地出口）与四步合法路径（只许人工标签输入、≥100 条后开轮、留出验证、落地出口 ADR）；**闭环三件定格为仪器要求**——§6.2 增"回包版本记录（版本变化＝改件）"与"复核 `outcome` 回写"、§7 增"跨轮 `labels.jsonl`"、§3 落盘增 `labels.jsonl`/`outcomes.jsonl`，汇总为 §16 **F14**；§13 增 **O-12**（第二阶段不在本轮冻结）；§0 增登记要点；roadmap 立即可做队列增第 8 项（试点提案＋第二阶段缺口）。 |
| v0.6 | 2026-10-05 | 收敛 M0：新增 **§16 M0 冻结清单与签署**（F1–F13 冻结项＋角色分离表＋签署表）——F1–F9 建议值定格（题面、τ=0.60＋探索曲线、noul 不加、H1、粗规则、ROI 门槛、κ、规模预算、时间盒），F10–F13（suite/owner/消费方/ADR）待人与组织决定；**O-11 决议**：τ=0.60、noul 第二排序器本轮不加（另轮候选）；O-1/O-2/O-9 状态改"汇总至 §16，M0 签署"；§6.1 rank 规则增 τ 探索曲线；§0 增冻结包要点。 |
| v0.5 | 2026-10-05 | 按官方 cookbook 对照修订：**题面工程**——§6.1 questions/criteria 改**英文结构化冻结文本**（`what`/`not_for`/`examples`，依据 `primitives/choice` 与 `model-jaggedness` #1），并声明语言依据（`concepts/state`：CJK 准确率较低）；**弃权双通道**——新增 `P_max<τ`（建议 0.60）低置信通道，与模型弃权同入探针清单、均不计入 effort@recall 覆盖，排序禁用 `confidence` 字段（§6.1 rank 规则）；**H3 改双口径**（阈值后翻转率为主判据，原始翻转率/多数标签复现率/概率漂移为对照；官方对照 90.8%→99.2%，§6.2/§9.1/§9.2）；**R-J2** 增"state 逐字节一致、不加 nonce"差异声明与双指标；**Go 下一步改级联落地**（规则先行、jev 补模糊区，§9.3）；**lint** 增题面语言门与结构化 criteria 检查（§6.2）；**人标 rubric 与 Q1 criteria 共用根因定义**（§7）；新增 O-11（τ 与 noul 第二排序器取舍，§13）；M0 门槛增题面冻结与 τ/O-11 决策（§11.1）；§6 增官方背书对照说明、§14 增 cookbook 来源。 |
| v0.4 | 2026-10-05 | 按 PM 评审（第二轮）修订：**P0-1** 靶改根因口径——§6.1 Q1 选项与 §7 rubric 同步改写（代码正确而验证器判错⇒环境流程型，锚点补 ISSUE-29 型），消除"修好是否带来内容改进"的判据自相矛盾；**P0-2** 预注册 rank 构造（P(content_fix) 降序、run_ref 破并列、弃权单列探针清单且不计入覆盖）＋"真目标＝content_fix"落地定义（§0 术语表/§6.1/§9.1）；**P0-3** 规模与判据口径对齐——Go 需 n≥30、预算 85 次执行，n=20–29 只出 Conditional（§4/§9.1/§9.2/§10.1/§11.1）；**P1** H6 增对照基率检查、analyze 增完整性门（§6.2）、操作者工作流假设（§9.2）、No-Go 与不可判边界（§9.3）、主指标配聚类 bootstrap CI、双标 κ 在小样本降为 sanity 且 M2 双标 ≥10 条（§7/§9.2）、时间盒（§11.1）、价值边界声明（§1.3）；**P2** 采纳质量指标（§9.1）、成本表补 85 次档（§10.1）、保留期（§10.2）、行号引用改语义锚（§1.1/§10.2） |
| v0.3 | 2026-10-05 | 架构分析收敛为 §15：功能模块拆解与归属（F1–F10，含实测复用面）、落位判定（包外 dev 工具＋升格触发条件）、落位四方案与机制对比（含"LLM judge 不得作降级腿"）、降级设计（单向阶梯＋10 条组件矩阵＋三条架构不变量）、待验证清单 V1–V7；同步：§0 增落位/降级要点、§2.2 调用项增"零网络能力"不变量、§11.1 M0 门槛增 ADR 确认、§13 增 O-10、§14 增架构核验来源；另将全文对 JEP-1 §15/§16/§17 的裸引用统一加"JEP-1"前缀，消除与新增本册 §15 的章节号歧义 |
| v0.2 | 2026-10-05 | 按 PM 评审修订：**P0-1** 定义消费面（`--queue` 本地队列）与可计量的采纳定义（§2.2/§3/§9.1），解除 Go 判据结构性不可达；**P0-2** 增"单条耗时记录"与 `人时@recall` 辅证＋人时反向风险行（§7/§9.1/§12）；**P0-3** 增全口径成本表、ROI 框架与产品门槛、批成本实测换算（§10.1）；**P0-4** 增 M0.5 薄预试（含 25–30 次执行换算与止损门，§4/§9.2/§11.1）；**P1-1** 拍阈值建议值（H1 降幅 ≥2 条且须胜粗规则、κ≥0.6，§7/§9.2）；**P1-2** 增粗规则选择性门（20–80%，§6.3）；**P1-3** 增外部效度要求与真实样本 ≥20% 目标（§4/§12）；**P1-4** 增治理绑定与四态后果表（§11.2/§9.3）；**P2** 增 LLM-judge 替代方案否决行（§2.1）、协议/仪器规格分节（§6）、术语表（§0）、交接 README 要求（§11.2）。 |
| v0.1 | 2026-10-05 | 初稿。 |
