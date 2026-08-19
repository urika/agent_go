# R13-R16 诊断数据面 agent_go 消费侧需求文档

> 状态：需求稿 v1（2026-08-19）
> 关联：[llama-defender-integration-requirements.md](llama-defender-integration-requirements.md)（服务方契约，R13-R16 已于 2026-08-19 全部交付） · [llama-defender-context-engineering-design.md](llama-defender-context-engineering-design.md)（上下文工程本体） · [llama-defender-integration-test-plan.md](llama-defender-integration-test-plan.md)（R1-R7 验收先例）
> 视角：本文档是**消费方（agent_go）需求**，描述如何用代理已交付的诊断数据面支撑产品能力；服务方接口行为以集成需求文档 §3.2 为准，不在此重复定义。

## 1. 背景与问题

llama-defender 已交付 R13-R16 上下文工程诊断数据面：

| 需求 | 代理侧能力 | 数据形态 |
|---|---|---|
| R13 | 诊断归因：prefill 实算数、feedback 注入计量、request_id 关联键 | 非流式 HTTP 头（`X-Proxy-Prompt-Processed-N` / `X-Proxy-Epoch-Count` / `X-Proxy-Feedback-Injected` / `X-Proxy-Diag-Request-Id`）；流式 SSE 尾注 `: x-proxy-diag {...}` |
| R14 | 会话台账：dup / last_dup_turn / 材料清单 + 会话发现 | `GET /api/sessions`、`GET /api/session/<key>/ledger` |
| R15 | sent_view 档案：模型实际所见的最终 payload（含注入标注） | `GET /api/session/<key>/archive?view=sent` |
| R16 | per-turn 深度落盘 + session 聚合时序 + 口径段 + 后端反代 | `logs/diag/sessions.jsonl`、`GET /api/session/<key>/metrics`、`GET /metrics/history?session=`、`/api/status` `ctx_config` 段、`/api/backend/props\|slots` |

**agent_go 侧现状（2026-08-19 探查结论）：零消费。**

- `api.py:156-166` 只解析 R8 四个路由归因头；R13 四个头无任何代码读取。
- `call_api()`（`api.py:101`）为 urllib 非流式调用，SSE 尾注通道暂不可达（但见 C1 边界说明）。
- 全链路**不发 `X-Claude-Code-Session-Id`**——R14 会话 key 回退 `md5(ip:ua:date)`，批跑流量按天合并，台账/档案/会话指标全部被污染。
- metering.jsonl 无 session / diag request_id / hit_ratio 字段；`eval.py` 聚合层无相关维度。
- bench manifest（`batch_governance.py:30-65`）无代理口径快照，跨批次 A/B 无法区分诊断面/上下文工程配置是否一致。

## 2. 产品价值与目标

诊断数据面回答三个此前无数据源的产品问题：

1. **「本地模型的 KV cache 到底有没有在吃？」** → 缓存命中率成为一等指标（质量/性能/成本分析的公共维度）。
2. **「worker 是在推进还是在原地打转？」** → 轮级看门狗用 dup/last_dup_turn 判定无效轮，比超时一刀切更早更准。
3. **「压缩/注入之后模型实际看到了什么？」** → 复盘以代理 sent_view 为准，消除「客户端转录 ≠ 模型所见」的视角错位。

**总目标**： metering 采集 → eval 聚合 → 看门狗/复盘/manifest 消费，全链路闭环，全部 fail-open（旧版代理或字段缺失不阻断任务）。

## 3. 需求总览

| # | 需求 | 优先级 | 产品能力 | 主要落点 |
|---|---|---|---|---|
| C1 | 会话标识注入（`X-Claude-Code-Session-Id`） | **P0** | R14/R15/R16 全部会话维度的前置契约 | `api.py`、bench/worker harness |
| C2 | R13 诊断头采集入 metering | **P1** | 缓存命中率、注入计量、request_id 互查 | `api.py`、`executor.py`/`subtask.py` |
| C3 | eval 聚合：hit_ratio / route / 注入维度 | **P1** | `eval bench` 报告与 gate 出数 | `eval.py`、`metrics.py` |
| C4 | 轮级看门狗消费会话台账 | P2 | 无效轮检测、提前干预 | `executor.py`（或独立 watchdog 模块） |
| C5 | 批次口径快照（ctx_config 入 manifest） | P2 | 跨批次 A/B 可追溯 | `batch_governance.py`、`bench.py` |
| C6 | 失败复盘入口（archive sent_view + session metrics） | P2 | `inspect`/`review` 诊断增强 | `cli.py`、`review_agent.py`（只读） |
| C7 | 健康检查增强（ctx_config / backend props） | P3 | 配置中心展示压缩/注入/上下文规格 | `profiles.py`、`web_server.py` |

依赖关系：**C1 是 C4/C6 的硬前置**（无头则会话按天合并，台账与档案无消费价值）；C2 是 C3 的硬前置；C5/C7 彼此独立。

## 4. 需求详述

### C1（P0）：会话标识注入

**用户故事**：作为 bench 分析者，我需要代理台账按「任务/子任务」粒度归会话，而不是按天合并，才能消费 R14/R15/R16。

**功能要求**：

1. agent_go 自身 HTTP 客户端（`api.py call_api`）在探测/调用本地代理时发送 `X-Claude-Code-Session-Id: agent_go-{task_id}[-{sub_id}]`；无任务上下文（如 router 探测）发送 `agent_go-probe`。
2. worker / bench harness（spawn `claude -p` 路径）通过包装层或 env 注入会话头，key 为 `{task_id}-{sub_id}`（bench 为 `{batch}-{task}-{repeat}`），保证一次子任务 = 代理侧一个会话 key。
3. key 只含 `[a-zA-Z0-9-_]`，长度 ≤ 64（代理内部截断 8 字符，agent_go 侧应保证前 8 字符可区分——建议 key 以短哈希前缀开头）。
4. 不改动云端直连路径的行为（头仅对本地代理 base_url 注入，避免向第三方端点泄露内部 task 标识）。

**边界**：`call_api` 非流式，R13 流式 SSE 尾注通道本期不消费；worker 路径（claude CLI）是否流式由 CLI 内部决定，agent_go 只保证头发送到位，尾注解析由代理落盘 jsonl 兜底（R16），不阻塞本期。

**验证方法**：

- 单测：mock urllib，断言请求头包含 `X-Claude-Code-Session-Id` 且格式合法；断言云端 URL 不注入。
- 实测：对运行中的代理发一次带头发起调用 → `GET /api/sessions` 出现对应 key（前 8 字符匹配），`key_source` 为 header 而非 fallback。
- 回归：批跑 3 个 bench 任务后 `/api/sessions` 应见 3 个独立会话，而非 1 个按天合并会话。

### C2（P1）：R13 诊断头采集入 metering

**用户故事**：作为质量分析者，我需要每次 planner/worker 调用的 prefill 实算数与注入标记落进 metering.jsonl，与成本/token 同口径可查。

**功能要求**：

1. `api.py` R8 解析块（`api.py:156-166`）扩展增读：`X-Proxy-Prompt-Processed-N`（int，缺省不发假值则字段缺失）、`X-Proxy-Epoch-Count`（int，Phase 1 前不出现）、`X-Proxy-Feedback-Injected`（kind 列表）、`X-Proxy-Diag-Request-Id`（string）。
2. metering 事件新增字段：`prompt_processed_n`、`epoch_count`、`feedback_injected`、`diag_request_id`、`session_key`（C1 的 key）。**字段缺失即缺省，不写 null 占位**（与代理「不发假值」语义对齐）。
3. worker 侧：经 `executor.py` 既有 R8 探测（`_probe_route_attribution`，`executor.py:133-172`）同通道扩展，经 env 传 `subtask.py` 写入 worker metering 事件。
4. 派生字段 `hit_ratio = 1 − prompt_processed_n / prompt_tokens` 在写入时计算（分母为 0 或分子缺失则缺省）。

**验证方法**：

- 单测：mock 响应头注入四字段，断言 metering.jsonl 事件含全部新字段且 hit_ratio 计算正确；mock 缺头响应，断言字段缺省而非 None/0。
- 实测：对代理发起一次非流式调用，检查 metering.jsonl 末行含 `diag_request_id`；用该 id 在代理 `logs/diag/sessions.jsonl` 中 grep 到同 request_id 记录（互查闭环）。
- 契约脚本：`tools/check_llama_defender_contract.py` 增 R13 头存在性用例（safe 模式）。

### C3（P1）：eval 聚合维度扩展

**用户故事**：作为 bench 负责人，我需要 `eval bench` 报告按模型/会话给出缓存命中率分档与注入分布，支撑上下文工程 A/B 出数。

**功能要求**：

1. `eval.py analyze_performance` 增 hit_ratio 统计：by_model / by_difficulty 的 mean/p50/p90，样本数 < 3 时标注不参评。
2. `analyze_cost` 消费既有 `route_target/route_actual_model`（当前零消费）：输出 route 分布（cloud/local/local_forced 占比），cloud 回退率超阈值时 warning。
3. 注入维度：`feedback_injected` 非空的调用占比、按 kind 分布，写入报告 diagnostics 段。
4. 全部新维度在字段缺失时跳过统计（旧批次结果可继续分析，不报错）。

**验证方法**：

- 单测：构造含/不含新字段的 metering fixture，断言聚合输出结构与旧批次兼容。
- E2E：跑 `eval bench --suite smoke`（本地模型），`eval.py` 报告出现 hit_ratio 段；与代理 `/api/session/<key>/metrics` 聚合值对账（误差 < 1%）。

### C4（P2）：轮级看门狗消费会话台账

**用户故事**：作为任务编排者，我希望 worker 原地打转（重复轮）时提前被发现并重试，而不是等到超时。

**功能要求**：

1. subtask 执行期间按可配置间隔（默认 30s，`diag_watchdog.poll_interval_sec`）轮询 `GET /api/session/{task_id}-{sub_id}/ledger`。
2. 判定规则（初版，阈值可配 `diag_watchdog.dup_threshold`，默认 3）：`dup_queries` 中存在 count≥阈值且 last_turn 距当前 turns_seen ≤2 → 标记 `looping`，记录事件并走既有 verify/retry 通道（不新增干预通道）。
3. 台账 404（会话未建立/代理过旧）→ fail-open，跳过检测。
4. 无效轮信号（dup 计数、`loop_detected`）写入 metering（`role="worker_diag"` 事件），供 C3 聚合。

**验证方法**：

- 单测：mock ledger 响应（dup 递增/平稳/404 三种），断言看门狗状态机转换与 fail-open。
- 实测：构造一个会重复的失败子任务（如验证命令恒失败），观察看门狗先于超时触发 retry。

### C5（P2）：批次口径快照入 manifest

**用户故事**：作为 bench 负责人，我需要每个批次的 manifest 记录代理侧口径（压缩模式/注入开关/diag 开关），跨批次对比时可排除配置漂移。

**功能要求**：

1. `eval bench` 启动探测阶段（既有的后端探测+定价校验）增读 `GET /api/status`，提取 `ctx_config{compression_mode, feedback_injection_enabled, epoch_S, window_K, diag_enabled}` 与 `route_config` 段。
2. 快照写入 `build_batch_manifest()`（`batch_governance.py:30-65`）新增 `proxy_context` 段；代理不可达或字段缺失时记 `proxy_context: {"available": false}`，不阻断 bench。
3. manifest 仍是 immutable——快照只增字段不改语义。

**验证方法**：

- 单测：mock `/api/status` 含/缺 ctx_config，断言 manifest 结构。
- 实测：跑一批 smoke bench，`agent_go eval batch-manifest` 输出含 proxy_context 且与代理实时 `/api/status` 一致。

### C6（P2）：失败复盘入口

**用户故事**：作为失败任务的诊断者，我需要看到模型实际所见的 payload（含代理注入块），而非客户端转录。

**功能要求**：

1. `agent_go inspect <task-id>` 输出附加诊断提示：失败 subtask 的会话 key 及对应代理查询命令（ledger / archive / metrics 三条 URL）。
2. （可选增强）`agent_go review --task <id> --deep` 对失败 subtask 拉 `archive?view=sent` 摘要（首/末各 K 字符 + 注入标注列表）附入分析报告；拉取失败静默降级。
3. 全部只读，不新增代理侧写操作。

**验证方法**：

- 单测：mock archive 响应，断言摘要截取与注入标注解析；mock 501/404，断言降级。
- 实测：对一个真实失败任务执行 inspect，确认输出中的 URL 可手动 curl 通。

### C7（P3）：健康检查增强

**功能要求**：`profiles.py health_check()` 增读 `/api/status` 的 `ctx_config` 与 `GET /api/backend/props`（n_ctx/total_slots）；`agent_go config status` 与 web 配置中心展示「压缩/注入/diag 开关、后端上下文规格」。501（后端不支持 props，返回 `{"supported": false}`）→ 显示 unknown，fail-open。

**验证方法**：单测 mock 三种响应（正常/501/不可达）；实测 `agent_go config status` 输出含新段。

## 5. 验证体系汇总

| 层 | 工具 | 覆盖 | 时机 |
|---|---|---|---|
| 单元测试 | pytest（mock HTTP / mock 响应头 / fixture metering） | C1-C7 全部逻辑分支，含降级路径 | 随 PR，CI 强制 |
| 契约脚本 | `tools/check_llama_defender_contract.py` 扩展（safe 模式增 R13-R16 端点/头用例） | C1/C2/C5/C6/C7 的代理侧存在性 | 代理升级后、bench 前 |
| 实测 E2E | `eval bench --suite smoke` + 对账代理 `/api/session/<key>/metrics` | C2/C3/C5 数据闭环 | 每个批次 |
| 故障演练 | 手停 diag（`PROXY_DIAG_ENABLED=off`）验证 agent_go 全链路 fail-open | 降级兼容 | 首次上线 |

**通过标准**：P0/P1（C1-C3）单测全过 + 实测对账一致；P2（C4-C6）单测全过 + 至少一次实测；C7 单测通过即可。所有需求在「旧版代理/字段缺失」下不阻断主流程（E 组降级用例全过）。

## 6. 实施顺序建议

1. **C1**（半天量级，改 api.py + harness env 注入）——不解锁则 C4/C6 无意义。
2. **C2 → C3**（metering 字段 → eval 聚合）——立刻产出可见指标。
3. **C5**（小改动，随 C3 同批）。
4. **C4 / C6**（看门狗与复盘入口，可并行）。
5. **C7**（有余力时）。

## 7. 实测偏差记录（2026-08-19 对 127.0.0.1:4000 实测）

R13-R16 交付后实测核对，与契约文档的三处偏差（消费侧已按实测适配）：

| # | 发现 | 影响 | 处置 |
|---|---|---|---|
| 1 | `GET /metrics/history?session=` 返回 404（裸 `/metrics/history` 正常） | C3/C5 的 session 时序对账暂不可用 | 服务方补齐（已承诺接口未生效）；契约脚本 F6 标记 known-issue SKIP，不阻塞 |
| 2 | `/api/sessions` 实际字段为 `key/key_source/turns/actions/materials/last_seen/idle_min`，无契约中的 `route/hit_ratio_p90/evict_in_min` | C4 会话面板不能用列表页取 hit_ratio | 消费侧适配：逐会话调 `/api/session/<key>/metrics`（64 会话上限内可接受），不动代理 |
| 3 | rapid-mlx 后端不返回 `timings` → `prompt_processed_tokens`/`hit_ratio` 全 null（19 轮本地会话实测） | C2/C3 的缓存命中率指标在当前后端无数据 | 设计 D9 已预判：字段 null + 离线 cache_analyzer 兜底；数据源决策（上游 timings / 换 llama-server / 容忍 null）属服务方与上游议题，不在本文档范围 |

另实测确认正常：`ctx_config` 段、`/api/sessions` key_source=header 归会话、ledger/metrics/archive 三端点、`/api/backend/props|slots` 501 结构化降级、`X-Proxy-Diag-Request-Id` 头。

## 8. 实施记录（2026-08-19 落地）

C1-C7 已全部实施（计划：排期见会话批准版）：

- 新模块 `agent_go/diag.py`：诊断端点唯一客户端（session_key 构造/截断口径、fail-open fetch、ledger/metrics/archive/ctx_config/props 封装、`local_proxy_base_url` 配置解析）。
- C1：`api.py call_api` 本地 URL 注入 `X-Claude-Code-Session-Id`（云端不注入）；`executor._probe_route_attribution` 探测带头；worker 路径经 env `ANTHROPIC_CUSTOM_HEADERS` + `AGENT_GO_SESSION_KEY` 注入。
- C2：`api.py` R13 四头解析入 metering（`diag_request_id/prompt_processed_n/hit_ratio/epoch_count/feedback_injected/session_key`，缺省不写入）；`subtask.py` 子任务结束按会话 key 查代理 session metrics 追加 `role="worker_diag"` 事件。
- C3：`eval.py analyze_cost` 增 `route_distribution`（cloud>30% 告警）、`hit_ratio_by_model`（mean/p50/p90，样本<3 标注）、`diagnostics.injection_counts`。
- C4：`executor._start_diag_watchdog` 轮级看门狗（v1 检测+上报不杀进程，`diag_watchdog.poll_interval_sec/dup_threshold` 可配），subtask result 增 `loop_detected` advisory。
- C5：`bench.py` 启动探测后快照 ctx_config/route_config → `{results}.proxy_context.json` sidecar；`build_batch_manifest` 增 `proxy_context_path` 参数并入 manifest `proxy_context` 段；`eval batch-manifest --proxy-context` 旗标。
- C6：`agent_go inspect` 输出失败 subtask 的 ledger/archive/metrics curl 提示；`review_agent` 只读审查 prompt 附 sent_view 档案摘要。
- C7：`profiles.health_check` 增 `ctx_config`/`backend_props` 段（501 结构化降级显示）。
- 契约脚本 `tools/check_llama_defender_contract.py` 增 F 组 6 用例（safe）：实测 20 PASS / 0 FAIL / 1 SKIP（F6 known-issue）。
- 测试：`tests/test_diag.py`（新）+ `test_api.py`/`test_eval.py`/`test_batch_governance.py`/`test_profiles.py` 增补。

### 8.1 架构 review 跟进（2026-08-19，three-project-architecture-review A-1/A-2/A-3）

- A-1：`config local` 模板不再生成 `worker_backends`（deprecated）；`diag.local_proxy_base_url`、`profiles.health_check`/`_profile_mode`、web 启动前探测全部 `worker_base_url` 优先，旧配置兼容读取保留。
- A-2：`executor._probe_local_model` 切换为 `/api/status` JSON（`backend.model_name`）优先，HTML 解析降级为旧代理兜底（保留一个版本周期）。
- A-3：`diag.CONTRACT_API_VERSION = "2"` 显式标注契约版本，header 构造/截断口径注释指向契约文档为唯一权威。


## 9. 非目标（Out of Scope）

- 流式 SSE 尾注 `: x-proxy-diag` 的实时解析——`call_api` 非流式，worker 流式数据代理由 R16 jsonl 落盘兜底，本期不引入流式客户端改造。
- 上下文工程本体（canonical history / epoch 状态机 / 压缩策略）——服务方职责，agent_go 只消费诊断字段（`epoch_count`/`is_epoch_turn` 等预留字段出现即透传，不做语义解释）。
- 主动干预代理行为（如按 ledger 强制清会话）——本期全部只读消费，干预走既有 verify/retry 通道。
