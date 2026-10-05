# ADR-013: jev 离线复核排序试点的外发边界与本地数据面

## 状态

**Accepted（2026-10-05，M0 签署批准）**——本 ADR 裁决**边界**（外发面、本地数据面、前置门、降级），不裁决试点判据。判据与冻结清单见 [jev-review-triage-pilot-requirements-20261005](../jev-review-triage-pilot-requirements-20261005.md)（v1.0 已冻结，§16.3 签署表）。**边界已生效**：允许按需求文档 §17.5 推进——外发调用仍受本 ADR 决策 2/3 与密钥前置门约束。

## 背景

- **痛点**：ISSUE-29（验证命令语法错误误判正确代码）、ISSUE-31（沙箱环境不一致误杀正确代码）、ISSUE-40（0 子任务真空假成功）、ISSUE-51（diff base 污染语义判定）——验证层误判族，复核目前依赖人工清单顺序。
- **证据**：swe-eval JEP-1 四轮实证——证据充分时 jev ≡ 一条粗规则（§15，独立价值在诚实弃权）；靶不对齐 ⇒ 负相关（§16，No-Go）；内容型靶对齐 ⇒ 首现正增量（§17，方向性）。本试点据此把"内容型优先"作为可测量命题。
- **安全实测（2026-10-05）**：验证命令环境**已脱敏**（`executor._build_sandbox_env()` 剔除 API_KEY/SECRET/TOKEN 等关键词）；**worker 子进程环境未脱敏**（`executor.py` worker env 构造处 `env = os.environ.copy()` → `subtask.py` 的 `Popen(..., env=env)`）⇒ 若调用密钥与 agent_go 批同 shell，子任务可继承并自调 jev（撞批内 agent 禁调红线）。
- **对象性质**：jev 为闭源托管远程 API（`api.commandcode.ai`，不可自托管）；调用即把 `state` 明文发给第三方，ZDR=off（一律视为会留存）。任何使用都是一条新的**出境通道**——按本仓模块变更规则，边界变更须以 ADR 裁决。

## 决策

1. **使用半径（CON-1）**：仅离线、**B 情境**（人闸门，逐条组装/确认 state）、**软排序**（禁 top-k 截断）；**不进 verdict／能力结论／任何 AC 分子分母**；不进入 runtime 任何路径。
2. **外发面**：仅经**既有 MCP 封装**（llama.cpp `tools/jev_mcp_server.py`，客户端级注册）调用；**新增代码零网络能力**——可选 `--call` 只 spawn 该 server 并经 stdio JSON-RPC 交互，不实现 provider API、不读取密钥内容。逐次**审计三项**（外发内容类别自评／落证／记账）。
3. **第二条外发通道**：LLM 预标注（§7 三层协议）默认用**本地模型**（零外部外发）；若用云端，须按同一审计三项登记并在报告披露。
4. **本地数据面**：新目录 `~/.agent_go/jev/<pilot-id>/`（`pool.jsonl`／`state/*.json`／`questions.json`／`manifest.json`／`packets.json|md`／`results.json`／`labels.json(l)`／`outcomes.jsonl`／`prelabels.jsonl`／`probe.jsonl`／`analysis.json`／`review-queue.md`／`caller-audit.log`／`README.md`）；**仅本地**、不提交仓库、**不写 runtime 任何存储**（meta/kanban/problems 均不写）；保留期 ≥1 个季度（与结论保鲜期一致），清理前先归档分析件。
5. **密钥前置门**：pilot 调用终端与 agent_go 批运行终端**分离**；批运行环境验收 `env | grep CMD_API_KEY` 为空；结构性收口（worker env `pop("CMD_API_KEY")`，参照 swe-eval 先例）**单独立项**、批期禁改。
6. **降级**：单向阶梯 `jev → 粗规则序 → 默认序 → 纯人工清单`；**禁止横向换 LLM judge**（升级腿＝人）；jev 不可用时原流程不变（无新增依赖点）。仪器读不到 state（H6 不过）⇒ 整轮作废。
7. **不做**：不改 `pipeline/executor/subtask` 与 `meta.json` 契约、不改 Web／`review` 流程、不新增运行时依赖、不做 C 通道／无人值守、不自动调用、不写任何 verdict。
8. **升格条件**：若试点判 Go 且队列要并入 `review` 流程、或被任何自动化消费，须**另开 ADR** 并经信任指标/评审门——本 ADR 不预授权。

## 后果

- **正面**：新增一条可审计、可降级、与 runtime 解耦的复核效率通道；为 JEP-1 待补缺口 JO-3（独立靶）与后续规则收敛（需求文档 §9.4）积累样本。
- **代价**：新增一处出境面（受决策 2/3 约束）；运营纪律（密钥隔离、逐次自评）；批运行与标注人时（需求文档 §10.1：≈$4–8 ＋ 4.5–10 人时）。
- **边界**：零 runtime 行为变化；零新增依赖；Go/No-Go 判据与阈值冻结不在本 ADR（见需求文档 §9.2/§16）。

## 引用

- 需求与冻结清单：[jev-review-triage-pilot-requirements-20261005](../jev-review-triage-pilot-requirements-20261005.md)（v0.9；§16 签署表、§17 推进计划）
- 用法与治理（swe-eval）：`docs/jev-paradigm-20260926.md`、`docs/jev-call-card-20260927.md`、`docs/jev-efficacy-preregistration-20261004.md`、`docs/jev-verification-requirements-20261004.md`
- 调用件（llama.cpp）：`tools/jev_mcp_server.py`、`tools/jev_quota.py`
- 相关先例：swe-eval F3 egress 硬化（批界落地补丁）；ADR-006（bench 批次治理）、ADR-012（默认关 opt-in 的落地形态参照）
