# llama-defender 集成需求（agent_go → 服务方）

> 状态：需求稿 v2（2026-08-15 更新：并入模型实体三层设计的接口契约，补充 R8-R12）
> 关联：[local-model-management-design.md](local-model-management-design.md)（agent_go 侧设计）、[model-entity-config-design.md](model-entity-config-design.md)（模型实体三层配置设计）
> 目标项目：`/Users/jinsongwang/APP/llama.cpp`（llama-defender）
> 背景：agent_go 将本地模型纳入管理（启停/切换/状态监控/保活分工）。按「谁拥有进程谁保活」原则，agent_go 作为消费方只负责就绪检查与一次性修复触发；本文档列出需要 llama-defender（服务方）提供或增强的接口与功能契约。
> v2 增补：按模型实体三层设计（① 模型固有 / ② 角色绑定 / ③ 部署拓扑），③ 部署拓扑归代理侧，需代理提供**部署可视**与**路由归因**接口（R8-R12），支撑 agent_go 侧 ① registry 数据采集与计量归因。

## 0. 待办清单速览（实施视图：已完成 / 待做 / 顺序）

### 0.1 已完成（保留，勿回退）

| 能力 | 说明 |
|------|------|
| 双协议端点 | `POST /v1/messages`（Anthropic）+ `POST /v1/chat/completions`（OpenAI） |
| thinking/response_format 透传 | `FormatConverter` + `convert_openai_request_to_anthropic` 已修复透传（v4-pro 推理必需，b14 修复） |
| SmartRouter 智能路由 | 模型偏好（MODEL_ROUTE_PREFERENCES 三模式）+ 阈值/内存/会话/sticky + 云端熔断冷却回退 |
| manage.sh 生命周期 | `start/stop/status/restart/reload/switch/watchdog`（pidfile + 日志 + 热重载 SIGHUP） |
| 基础接口 | `GET /v1/models`、`GET /status`(HTML)、`GET /api/status`(JSON)、`GET /metrics`、`GET /api/profiles`、`GET /api/watchdog`、`POST /admin/route/force-local`、`POST /admin/route/force-cloud` |

### 0.2 待做（R8-R12，按优先级与四层闭环支撑映射）

| 优先级 | 需求 | 支撑层 | 解决的 Gap |
|--------|------|--------|-----------|
| **P0 R8** | 路由归因返回（`X-Proxy-Route-Target/Actual-Model/Reason/Cost` 响应头） | **④ 观测归因** | metering 按 URL 标 is_local，force_fallback ~36% 回退本地导致归因全错（bench 成本/归因最大误差源） |
| **P0 R9** | `GET /api/route/policies`（MODEL_ROUTE_PREFERENCES + 云端配置脱敏） | ③ 部署拓扑可视 | 部署拓扑对 agent_go 不可见（G3） |
| **P1 R10** | `/v1/models` 能力元数据（route/real_model/thinking_supported/json_compliance/context_chars） | ① registry 数据源 | /v1/models 只回别名无能力元数据（G1） |
| **P1 R11** | `/api/status` 增 `route_config`（cloud_model/route_enabled/cloud_key_set） | ③ 探测依据统一 | 健康检查探测依据不一致（G4） |
| **P2 R12** | `POST /admin/reload`（HTTP 热重载） | ③ 运维 | 远程/容器场景 reload（等效 manage.sh reload） |

**四层闭环支撑**：① registry ← R10；③ 部署拓扑 ← R9/R11/R12；④ 观测归因 ← **R8（最关键，决定 bench 成本/归因可信度）**；② 角色绑定无代理依赖。

### 0.3 实施顺序

`R8 → R9 → R10 → R11 → R12`（P0 先行，R8 是归因可信度的前提）

## 1. 需求场景

| # | 场景 | agent_go 行为 | 需要的 llama-defender 能力 |
|---|---|---|---|
| S1 | 任务启动前就绪检查 | pipeline pre-flight 探测后端是否可用 | 快速健康探针 + 结构化状态 |
| S2 | Plan 阶段模型感知 | generate_plan 前取当前模型名/能力档注入 planner 上下文 | 结构化输出当前激活模型与后端类型 |
| S3 | 故障诊断分级 | 任务失败后区分 proxy 死/backend 死/模型漂移/加载中 | 分级状态字段（而非 HTML 解析） |
| S4 | 一次性修复触发 | pre-flight 发现问题时触发 reload/start-backend/restart | 幂等命令 + 可靠退出码 + 可轮询的就绪信号 |
| S5 | 模型切换 | 编排 switch→stop-backend→reload→start-backend 原子序列 | 非交互保证 + 失败可回滚的 ground truth |
| S6 | 并发保护 | agent_go 修复与 llama-defender watchdog 恢复可能并发 | 变更操作的互斥锁 / 单入口序列化 |
| S7 | 状态监控展示 | status 面板 / web 页面展示后端健康与指标 | 稳定的 JSON metrics 接口 |

## 2. 已有接口（可直接使用，需保持稳定）

以下能力已验证存在，agent_go 侧直接使用；**请保持行为与输出稳定，变更需通知**：

| 接口 | 用途 | 现状 |
|---|---|---|
| `GET /v1/models` | 健康探针（S1） | ✅ 200 + 别名/路由 metadata |
| `GET /metrics[?n=N]` | 监控指标（S7） | ✅ JSON |
| `manage.sh start / start-backend` | 启动（S4） | ✅ 幂等 |
| `manage.sh reload`（SIGHUP） | 热重载（S4/S5） | ✅ 幂等，~0.5s |
| `manage.sh switch <name>` | 切换软链（S5） | ✅ 非交互自动跳过确认 |
| `manage.sh stop / stop-backend / restart` | 停止/重启（S4/S5） | ✅ 可用，有副作用 |
| `configs/active.conf` 软链 | 当前 profile ground truth（S2/S5） | ✅ 文件级可信 |
| `*.pid` + pgrep 自愈 | 进程 ground truth（S3） | ✅ |
| `GET /api/status` | 结构化状态（R1，**已实现**） | ✅ JSON：proxy/backend/active_profile/state |
| `GET /api/profiles` | profile 列表（R6，**已实现**） | ✅ JSON |
| `GET /api/watchdog` | watchdog 状态（R5，**已实现**） | ✅ JSON |
| `GET /metrics/history` | 历史指标（S7） | ✅ JSON |
| `POST /admin/route/force-local` / `force-cloud` | 手动路由覆盖（会话级） | ✅ |

## 3. 需新增/增强的接口（gap）

### R1（P0）：结构化状态 JSON 端点

**现状缺口**：`GET /status` 返回 HTML，agent_go 需解析 HTML 取 `MODEL_NAME`，脆弱易碎。

**需求**：新增 `GET /api/status`（或 `GET /status?format=json`），返回：

```json
{
  "proxy":   {"pid": 12345, "uptime_sec": 3600, "alive": true},
  "backend": {"pid": 23456, "alive": true, "model_name": "Qwen3.6-35B-A3B-UD-MLX-4bit",
              "backend_type": "rapid-mlx", "base_url": "http://127.0.0.1:8081/v1"},
  "active_profile": "rapid-mlx-35b-opt",
  "state": "healthy",
  "ready": true
}
```

字段要求：
- `state` 枚举：`healthy | starting | backend_down | proxy_down | model_drift | down`（对应 S3 诊断分级）。
- `model_name` 为后端**真实加载**的模型名（S2 感知与计量映射依赖）。
- `active_profile` 为 active.conf 软链目标名（S5 漂移检测依赖：`model_name` 对应配置 ≠ `active_profile` → `model_drift`）。
- 响应 < 1s，失败返回非 200 + JSON error body。

### R2（P0）：明确的就绪（readiness）语义

**现状缺口**：`/v1/models` 返回 200 不代表模型加载完成（35B 加载需数十秒）。

**需求**：R1 的 `ready` 字段语义固定为「模型加载完成且可接受推理请求」；`starting` 状态下 `ready=false`。agent_go 的 `wait_ready` 轮询以该字段为准，不再自行猜测。

### R3（P0）：manage.sh 调用契约

**需求**（多为现状确认+固化）：
1. **非交互保证**：stdin 非 tty 时，所有命令（含 `switch`/`restart`/`stop`）不得阻塞等待输入。
2. **退出码**：成功=0，失败=非 0；失败原因写 stderr。
3. **幂等**：`start`/`start-backend`/`reload`/`switch` 重复调用结果一致。
4. **超时上限**：命令自身应有硬超时（参考现有 `WAIT_HARD_LIMIT=1800`），不得无限挂起。

### R3.1 manage.sh CLI 命令参考（服务启停主路径）

manage.sh 是**服务启停的主路径**，尤其在 HTTP API 生效前或代理不可用时（proxy_down 场景下 `/api/*` 全部不可达，CLI 是唯一控制面）。工作目录：`/Users/jinsongwang/APP/llama.cpp`。

**只读命令（任意频率，零副作用）**：

| 命令 | 说明 |
|------|------|
| `status` | 文本状态：后端/代理存活、PID、内存、当前配置（人类可读） |
| `current` | 当前激活配置详情 |
| `list` | 列出所有可用 profile |
| `watchdog-status` | watchdog 结构化状态 JSON（R5） |
| `logs [N]` | 后端日志 tail |
| `proxy-logs [N]` | 代理日志 tail |

**幂等启动/恢复命令（重复调用安全）**：

| 命令 | 说明 | 场景 |
|------|------|------|
| `start` | 启动后端+代理；已运行则跳过/补启动 | 服务全停后拉起（S4） |
| `start-backend` | 仅启动本地后端 | backend_down 修复（S4 阶梯 level 1） |
| `reload` | SIGHUP 热重载代理配置（~0.5s，不断连） | 配置漂移修复（S4 阶梯 level 0） |
| `switch <name>` | 改 active.conf 软链（非交互自动跳过确认） | 切换序列第一步（S5） |

**变更/停止命令（有副作用，需并发保护）**：

| 命令 | 说明 | 注意 |
|------|------|------|
| `stop-backend` | 停本地后端，释放 GPU 内存 | 在途请求失败；执行前查活跃任务 |
| `stop` | 停 watchdog+代理+后端 | 同上 |
| `restart` | stop + start（含模型重新加载，35B 需数十秒） | S4 阶梯 level 2，重操作 |
| `start-cloud` | 仅启云端代理 | **会先停本地后端**，勿当纯加云端用 |
| `watchdog [--daemon]` / `stop-watchdog` | 启停 watchdog | 保活归服务方，agent_go 不调用 |

**调用约定**（与 R3 契约一致）：工作目录必须为 llama-defender 仓库根目录；`subprocess.run` 收集退出码（0=成功）；非 tty stdin 下不得等待输入；变更命令自动获取 `.manage.lock`（R4），持锁冲突时快速失败。

### R4（P1）：变更操作互斥锁

**现状缺口**：agent_go pre-flight repair 与 llama-defender watchdog 自动重启可能并发触发（S6）。

**需求**：manage.sh 的变更类命令（`start/stop/restart/start-backend/stop-backend/switch`）进入时获取仓库级文件锁（如 `.manage.lock`，flock），已持锁则快速失败（非 0 + stderr 说明锁持有者）。watchdog 内部操作同样走该锁。

### R5（P1）：watchdog 状态查询

**需求**：`manage.sh watchdog-status`（或 HTTP 端点）输出结构化信息：`enabled / last_restart_at / restart_count_1h / last_failure_reason`。agent_go 诊断时只读该状态，用于判断「后端是否正被 watchdog 恢复中」（避免重复修复）。

### R6（P1）：profile 列表端点（可选增强）

**现状**：agent_go 解析 `configs/*.conf` + active.conf 软链（可用但耦合目录结构）。

**需求（可选）**：`GET /api/profiles` 返回 `[{"name": "...", "desc": "...", "memory_gb": ..., "active": true}]`。若实现，agent_go 改走 HTTP；不实现则维持文件解析（可接受）。

### R7（P2）：事件通知（可选）

**需求（可选）**：模型切换完成 / 自动重启发生时写 `logs/lifecycle_events.jsonl`（每行 `{ts, event, detail}`），agent_go 监控页可消费。轮询 status 已可满足，此条非必需。

---

## 3.1 模型实体三层设计增补需求（R8-R12，2026-08-15）

以下需求来自 [model-entity-config-design.md](model-entity-config-design.md) 的三层设计：③ 部署拓扑归代理侧，需代理提供**部署可视**与**路由归因**接口，支撑 agent_go 侧 ① 模型 registry 数据采集与计量归因。

### R8（P0）：路由归因返回（响应头/字段）

**现状缺口（G2）**：agent_go metering 按 URL（localhost）标 `is_local=True`，但代理 force_fallback 时 opus-4-7 有 ~36% 概率回退本地——**代理知道实际路由（cloud/local）与真实模型，但不回传**，导致计量归因全错（云端调用被记为本地、成本错算）。

**需求**：每个推理响应（`/v1/messages`、`/v1/chat/completions`）带路由归因：

| 方式 | 字段 | 说明 |
|---|---|---|
| 响应头（推荐，流式兼容） | `X-Proxy-Route-Target: cloud\|local\|local_forced` | 实际路由 |
| | `X-Proxy-Route-Actual-Model: deepseek-v4-pro` | 真实后端模型名 |
| | `X-Proxy-Route-Reason: model_forced_fallback_cloud` | 路由原因 |
| | `X-Proxy-Route-Cost: 0.0002` | 本次云端费用（本地为 0） |
| 或响应体扩展字段（非流式） | `"proxy_route": {"target","actual_model","reason","cost"}` | 同上 |

流式响应在 `message_start` / 首帧携带。agent_go metering 据此标 `is_local`（target=local 才是本地）与实际模型，修正成本归因。

### R9（P0）：路由策略可视端点 `GET /api/route/policies`

**现状缺口（G3）**：`MODEL_ROUTE_PREFERENCES`（模型→本地/混合/云端三模式路由表）对 agent_go 不可见——agent_go 无法预知某模型会走哪、cloud_model 是什么、云端 key 是否就绪。

**需求**：返回脱敏后的路由配置：

```json
{
  "route_enabled": true,
  "cloud_model": "deepseek-v4-pro",
  "cloud_key_set": true,
  "threshold_chars": 80000,
  "preferences": {
    "claude-haiku-4-5": {"route_bias": "prefer_local", "behavior": "prefer", "cloud_model": "deepseek-v4-flash"},
    "claude-sonnet-4-6": {"route_bias": "auto", "behavior": "prefer", "cloud_model": "deepseek-v4-flash"},
    "claude-opus-4-7": {"route_bias": "prefer_cloud", "behavior": "force_fallback", "cloud_model": "deepseek-v4-pro"}
  }
}
```

`cloud_key_set` 仅布尔（**不返回 key 明文**）。agent_go 健康检查/配置中心据此展示「该模型实际会走本地还是云端」，替代当前盲猜。

### R10（P1）：`/v1/models` 能力元数据增强

**现状缺口（G1）**：`/v1/models` 只回别名 + route，无能力元数据——agent_go 的 ① 模型 registry 需手工录入 thinking/json_compliance/context 等固有属性，无法自动采集。

**需求**：每个模型条目增强 metadata：

```json
{"id": "claude-opus-4-7", "object": "model",
 "metadata": {"route": "cloud", "real_model": "deepseek-v4-pro",
              "thinking_supported": true, "thinking_required": true,
              "json_compliance": "loose", "context_chars": 200000}}
```

字段：`route`（已有）、`real_model`、`thinking_supported/required`、`json_compliance`（strict/loose/poor）、`context_chars`。agent_go registry 可定时同步，替代手工维护。

### R11（P1）：`/api/status` 路由配置段增强

**现状缺口（G4）**：健康检查的模型名探测依据不一致（/status HTML vs /v1/models 别名），且不知云端配置状态。

**需求**：`/api/status` 增加 `route_config` 段：`{"cloud_model": "...", "route_enabled": bool, "cloud_key_set": bool, "cloud_concurrent": n}`。与 R9 互补（R9 全量策略，R11 当前状态摘要）。

### R12（P2）：HTTP 热重载 `POST /admin/reload`

**现状**：热重载仅 `manage.sh reload`（CLI，需到仓库目录执行）。proxy_down 以外的远程/容器场景无 HTTP 路径。

**需求（可选）**：`POST /admin/reload` 等效 SIGHUP 热重载（读 active.conf 应用变更），返回 `{reloaded: true, active_profile: "..."}`。幂等。

### 边界（不需代理提供，agent_go 侧职责）

- ① 逻辑模型 registry：`quality_tags`、pricing 表、角色绑定（router.roles）——agent_go `models.json`/pricing.py
- ② 角色场景参数：temperature/max_tokens/thinking 开关/goal/min_difficulty——agent_go config
- Plan 生成/拆解/e2e 判定——agent_go 核心流程

## 4. 接口协议要求汇总

| 维度 | 要求 |
|---|---|
| 传输 | HTTP on `127.0.0.1:4000`，无鉴权（本机回环） |
| 格式 | 程序化端点返回 `application/json`；字段名为 snake_case 且**稳定不更名**（更名=breaking change 需通知 agent_go） |
| 错误 | 非 2xx + JSON `{"error": "...", "state": "..."}`；agent_go 按 state 归因 |
| 超时 | 探测类端点响应 < 2s；status 类 < 1s；manage.sh 变更命令有硬上限（≤1800s） |
| 幂等 | start/start-backend/reload/switch 幂等；stop 类幂等收尾 |
| 并发 | 变更操作必须经文件锁互斥（R4）；只读端点无锁 |
| 版本 | 建议响应含 `api_version`（如 `"1"`），便于后续演进 |
| 降级 | 端点不存在/字段缺失时 agent_go 侧 fail-open（回退 HTML 解析或标记 unknown），不阻断任务 |

## 5. 优先级与验收

| 需求 | 优先级 | agent_go 侧验收 |
|---|---|---|
| R1 结构化状态 | P0 | `agent_go model status/diagnose` 全部走 JSON，不再解析 HTML |
| R2 readiness 语义 | P0 | 模型加载期 `ready=false` 时 pre-flight 等待而非误判失败 |
| R3 manage.sh 契约 | P0 | 非交互调用全部命令无阻塞；失败退出码非 0 |
| R4 互斥锁 | P1 | watchdog 恢复中 agent_go repair 快速失败并提示，不双重重启 |
| R5 watchdog 状态 | P1 | diagnose 输出含「watchdog 恢复中」判定 |
| R6 profile 端点 | P1（可选） | `model list` 走 HTTP（若实现） |
| R7 事件通知 | P2（可选） | 监控页展示生命周期事件 |
| R8 路由归因返回 | **P0** | metering 按 route_target 标 is_local（不再按 URL），成本归因正确 |
| R9 路由策略可视 | **P0** | 配置中心展示模型实际路由（本地/云端），替代盲猜 |
| R10 模型能力元数据 | P1 | ① registry 自动同步能力属性，免手工录入 |
| R11 status 路由配置段 | P1 | 健康检查探测依据统一（cloud_model/key_set） |
| R12 HTTP 热重载 | P2（可选） | 远程/容器场景可 reload |

## 6. 兼容策略

- R1/R2 未实现前，agent_go 维持现有 HTML 解析 + pidfile 兜底，功能可用但脆弱。
- 全部需求按「新增不破坏」原则：llama-defender 新增端点/字段，不改变现有 `/v1/models`、`/metrics`、manage.sh 既有行为。
- agent_go 侧对每个需求做存在性探测，缺失则降级（fail-open），保证旧版 llama-defender 可继续工作。
