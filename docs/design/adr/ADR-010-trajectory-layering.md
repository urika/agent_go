# ADR-010: 轨迹平台化三层切分——代理层不做 LLM 会话管理

## 状态

Accepted

## 背景

阶段十三（B1-B7）落地多 backend 架构后，面临两个新的架构问题：

1. DeepSeek Harness（dsh）调研显示其「Session 事件日志即唯一真源」的事件溯源
   架构（turn/step/tool 边界事件、无损失败 attempt、fork/resume 原语）有很高
   借鉴价值，需要决定这些能力落在哪一层。
2. 代理层（llama.cpp 系，localhost:4000）已承担压缩/注入/diag/路由，且已有
   session 键控信号（`prefix_breaks_by_session`、R17 session signals）——
   是否应把 LLM 会话/轨迹管理进一步下沉到代理层。

## 决策

三层切分，各层拥有明确的真源与职责：

```text
backend 层（claude/pi/opencode/zcode/dsh）
  拥有：LLM 会话真源（backend 私有契约）
  对外：只读轨迹采集口（harvest_trajectory）

agent_go 平台层
  拥有：任务编排事件日志（平台唯一真源）+ 全部能力 seam
  （轨迹采集/验证链/知识注入/路由策略皆为可插拔 seam）

代理层（llama.cpp 系）
  拥有：协议级横切——路由/配额/failover/压缩/注入/diag/流量留痕
  不拥有：会话状态、轨迹、任务语义
```

**代理层明确不落地 LLM 会话/轨迹管理**，保持协议层定位。

## 原因

代理层管会话有三条硬约束：

1. **覆盖不全**：CLI 类 backend 可绕过代理直连（zcode 走自身 config、opencode
   走 auth.json 直连 z.ai），代理层会话管理只对 agent_loop 路径有效，破坏四臂
   统一性。
2. **观测边界错误**：代理在协议层只能看到 request/response 对；轨迹最有价值的
   一半（工具执行、worktree diff、验证结果、重试决策）对代理不可见。dsh 的事件
   溯源成立恰恰因为 Session 在 harness 内部，tool 事件与 assistant 消息同写一份
   日志。
3. **与 backend 内部会话管理冲突**：dsh 自带 compaction seam 与会话持久化，
   claude/pi/opencode 各自管理上下文。代理持会话状态 = 双重管理（代理压缩 +
   backend 压缩 → 上下文发散、prefix cache 互踢）。

例外：agent_loop 路径（agent_go 直接驱动 API）下代理的压缩/注入即该路径的
会话管理，现状合理、保持不动；但它天然无法推广到 CLI backend 路径。

### 补充（2026-09-05）：代理层上下文管理按路由分流

调研确认 `claude -p` **没有指定上下文长度的旗标**（窗口由模型决定；仅有
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` 输出上限与 auto-compact 开关
`DISABLE_AUTOCOMPACT=1` / 阈值 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`——后者写在
settings.json `env` 块会被静默忽略，须真实环境变量注入）。由此推导代理层
上下文管理的分流原则：

- **云端路由（claude 直连 / z.ai glm 等）→ 代理透传，不压缩不截断。**
  Claude Code 自身按其认定的窗口做 auto-compact 与会话簿记；代理再截断即
  双重管理——backend 以为还在的消息被截掉，上下文发散、prefix cache 互踢。
  且 `-p` 是每子任务一次的短会话，打满窗口概率低，代理压缩弊大于利。
- **本地模型路由（Ornith 35B 等经 :4000）→ 代理管理是唯一防线，必须保留。**
  Claude Code 以为自己有 200K 窗口、auto-compact 阈值 ~160K，永远赶不上本地
  模型的真实窗口；溢出防护完全靠代理层。配套动作：本地路由时给 worker 注入
  `DISABLE_AUTOCOMPACT=1`，避免 backend 在错误时点再压一轮。

落地形态：代理按路由元数据分流（如 `context_managed_by: proxy | client`，
云端默认 client 透传、本地默认 proxy 管理）；agent_go 侧无需改动。

## dsh 概念映射

| dsh 概念 | 落到哪层 | 对应物 |
|---|---|---|
| provider 适配器 / compaction seam / injection | 代理层 | 已有：路由/配额/压缩/注入/diag |
| SessionEvent 日志 / persistence seam | agent_go 层（改造为任务编排日志） | 新建 `events.jsonl`（每 task） |
| fork/resume 原语 | agent_go 层 | 已有 resume/recover；新增 fork-retry |
| 审批服务（fail-closed） | agent_go 层 | 已有：confirm_plan/confirm_subtasks/greywall |
| hook 桥接（审计） | agent_go 层 | 已有：谦逊层（problems/deviation/attribution） |
| 轨迹视图 | agent_go 层 | replay/checkpoint/plan-history 升级为事件驱动 |
| OTel 计量 | 两层各自 | 代理记流量、agent_go 记 metering.jsonl（现状正确） |

## 目标（可检验终态）

1. **单一真源**：每 task 一份 append-only `events.jsonl`；`meta.json` 降级为其
   投影（如 dsh 消息历史派生自日志）；resume/recover/replay 从日志重建。
2. **三层可追溯**：任一失败 subtask 一次查全——代理流量（模型级）+ 平台事件
   （编排级）+ backend 轨迹（执行级），经 session_key/worktree 路径关联。
3. **能力 seam 化**：新 backend/新能力不改核心循环。
4. **可量化收益**：失败归因事件级定位；fork-retry 省全量重跑 token（预估
   30-50%）；零双重压缩事故。

## 演进路径

- **阶段 0（本 ADR）**：边界固化，防未来漂移。
- **阶段 1（随 B8 dsh 落地）**：`BaseBackend.harvest_trajectory()` 可选钩子
  （fail-open）；dsh harvester + opencode 事件流落盘 →
  `<task_dir>/trajectory/{sub_id}.jsonl`；只采集不消费，用真实失败案例验证价值。
- **阶段 2（价值验证后）**：定义 TaskEvent 最小词汇
  （plan/decompose/subtask_start/model_attempt/verify/commit/retry/subtask_end）；
  executor/pipeline 发射骨架事件；meta.json 双写一个版本周期后切为投影；
  replay/checkpoint/recover 迁移为日志重建。
- **阶段 3（按需）**：轨迹驱动失败归因；fork-retry（dsh fork / opencode session
  resume）；代理流量留痕与平台事件 session_key 关联；KnowledgeStore 从轨迹取料。

**明确不做**：代理层不建会话状态；不复制 dsh 完整事件溯源（agent_go 不管模型
上下文，无需从日志重建 LLM 历史）；不自建轨迹重放执行（fork-retry 已覆盖主要
价值）。

## 风险与对策

- **meta.json 切换兼容性**（bench/assessment 依赖）：先改消费端读投影，确认一致
  后停写旧路径。
- **dsh 格式漂移**（developer preview）：adapter 边界翻译成平台格式（防腐层），
  平台消费端永不直接读 dsh 格式；采集失败降级不阻塞任务。
- **过度工程**：阶段 2 必须由阶段 1 的价值验证背书才启动。
