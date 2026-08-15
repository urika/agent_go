# 模型实体三层配置设计（Model Registry / Role Binding / Deployment Topology）

> 状态：设计评审（Draft）
> 日期：2026-08-15
> 关联：[config-schema.md](config-schema.md)、[model-eval-routing-mechanism-2026-08-07.md](model-eval-routing-mechanism-2026-08-07.md)、`agent_go/router.py`、`agent_go/pricing.py`、`agent_go/profiles.py`、代理 `proxy_state.py MODEL_ROUTE_PREFERENCES`
> 实验依据：m4 系列批次（v4-pro thinking 空响应、GLM 接入、evaluator 假阳性、worker_backends 与代理路由重复）

---

## 1. 背景与问题

接入一个新模型（GLM、deepseek-v4-pro、本地 MLX）目前要在 **5+ 处**改配置，且概念互相纠缠：

```
接入 GLM 的实测改动面：
  plan_api.base_url/model/api_key/max_tokens   ← planner 场景
  planner_api（同上，重复）                      ← 冗余
  evaluator.provider/base_url/model              ← evaluator 场景
  plan_api.worker_base_url                       ← 部署拓扑（worker 走哪）
  worker_backends                                ← 部署拓扑（模型→URL）
  call_api thinking 分支                         ← 模型推理特性（硬编码检测）
  proxy_state.py MODEL_ROUTE_PREFERENCES         ← 代理侧路由（与 worker_backends 重复）
  pricing.py MODEL_PRICES                        ← 成本（正确归属）
```

**核心诊断**：现有 `config.json` 把**模型固有属性**、**场景使用方式**、**部署拓扑**三层压平混放，导致：
1. 每接一个模型多点改动、易漏（本次 GLM 接入漏了 worker_base_url）
2. 模型推理特性（v4-pro/GLM 需 thinking enabled）散落在 `call_api` 硬编码检测里，新模型要改代码
3. `worker_backends`（agent_go 侧）与 `proxy_state.MODEL_ROUTE_PREFERENCES`（代理侧）是**同一部署拓扑职责的两处实现**——漂移源
4. `plan_api` vs `planner_api` 语义重复；`evaluator` 不进 router.roles（角色覆盖缺口）

## 2. 核心原则

> **模型是单一实体。按属性归属拆三层：模型固有 → 一次注册全局复用；场景使用 → 角色绑定时指定；部署拓扑 → 收敛到部署方。**

| 层 | 回答的问题 | 改动时机 |
|----|-----------|---------|
| ① Model Registry（模型固有） | 这个模型**是什么**：端点、推理特性、输出特性、成本 | 接入新模型时（一次） |
| ② Role Binding（场景使用） | 这个模型**在这个角色怎么用**：温度、token、thinking 开关、goal | 调整某角色行为时 |
| ③ Deployment Topology（部署拓扑） | 请求**物理上发到哪**：本地后端、别名→后端路由、智能分流 | 部署环境变化时 |

## 3. 三层属性分析

### ① Model Registry（模型固有，公共配置）

| 属性 | 说明 | 实验证据 |
|------|------|---------|
| `id` | 唯一名（glm-5.3 / deepseek-v4-pro / claude-sonnet-4-6） | — |
| `provider` | 协议族（anthropic/openai/deepseek 兼容） | 决定 payload 格式 |
| `endpoint.base_url` | API 地址 | GLM 直连 vs 代理 |
| `endpoint.key_ref` | key 引用（env 名/secret 路径，**不存明文**） | secret.local.conf 模式 |
| `reasoning.thinking` | 推理特性：`{format: anthropic\|openai, required: bool, budget_param, budget_tokens}` | v4-pro/GLM 无 thinking→空响应（本次根因） |
| `output.json_compliance` | 输出特性：`{level: strict\|loose\|poor, needs_response_format: bool}` | deepseek JSON 包裹 vs GLM strict |
| `limits.context_chars` | 实际上下文上限（用于压缩/路由阈值） | 代理 80K 阈值 |
| `cost.pricing` | 云端单价（pricing.py，已正确归属） | — |
| `cost.tco_per_call` | 本地 TCO/次（现 local_model_cost，**放错层**） | m4 基线 TCO 口径 |
| `tier` | 成本档（MODEL_TIER，已正确归属） | — |
| `quality_tags` | 能力标签（plan_strong / eval_strong / code_strong） | GLM 评估强、本地 35B 评估弱 |

### ② Role Binding（场景化使用方配置）

| 属性 | 说明 | 现有载体 |
|------|------|---------|
| `model` | 引用 ① 的 id | — |
| `temperature` / `max_tokens` | 采样/长度 | plan_api.temperature/max_tokens |
| `thinking.enabled` / `budget_tokens` | 推理开关与预算（**覆盖** ① 的 required 默认值） | call_api 硬编码 |
| `timeout_ms` | 超时 | plan_api.timeout_ms |
| `goal.enabled/policy/max_turns` | goal 循环 | goal 配置块 |
| `min_difficulty` / `e2e` | 难度下限/端到端 | min_difficulty（本次新增） |
| `parallel` / `max_retries` | 执行策略 | run 参数 |
| `tools` / `skills` / `agent_type` | 工具与技能 | subtask 字段 |

角色集合（与 router.roles 对齐）：`planner / worker(easy\|medium\|hard) / evaluator / reviewer / fallback`。

### ③ Deployment Topology（部署方配置，**收敛到代理侧**）

| 属性 | 归属 | 说明 |
|------|------|------|
| 本地后端（llama/rapid-mlx 端口、模型文件、采样参数） | 代理 configs/active.conf | 已就位 |
| 别名 → 后端路由（MODEL_ROUTE_PREFERENCES 三模式） | 代理 proxy_state.py | 已就位 |
| 智能路由（超长转云/熔断/冷却/预算/sticky session） | 代理 pipeline SmartRouter | 已就位 |
| secret（PROXY_CLOUD_API_KEY） | 代理 configs/secret.local.conf | 已就位 |
| ~~worker_backends~~ | **agent_go 侧应收敛/删除** | 与代理路由重复（漂移源） |

**关键决策**：agent_go 的 `worker_backends` 收敛为**单值 `worker_base_url`**（或干脆由 profiles 管理），模型→后端的细粒度路由**全部留代理侧**（它是唯一 7×24 部署方）。

## 4. 目标结构（schema 设计）

### 4.1 ① Model Registry —— `~/.agent_go/models.json`（或 config.json `models:` 块）

```json
{
  "glm-5.3": {
    "provider": "anthropic",
    "endpoint": {"base_url": "https://open.bigmodel.cn/api/anthropic/v1/messages",
                 "key_ref": "GLM_API_KEY"},
    "reasoning": {"thinking": {"format": "anthropic", "required": true,
                               "budget_param": "budget_tokens", "budget_tokens": 8192}},
    "output": {"json_compliance": "strict", "needs_response_format": false},
    "limits": {"context_chars": 1000000},
    "cost": {"pricing": "glm-5.3", "tco_per_call": 0},
    "quality_tags": ["plan_strong", "eval_strong"]
  },
  "deepseek-v4-pro": {
    "provider": "openai",
    "endpoint": {"base_url": "https://api.deepseek.com/v1/chat/completions",
                 "key_ref": "DEEPSEEK_API_KEY"},
    "reasoning": {"thinking": {"format": "openai", "required": true}},
    "output": {"json_compliance": "loose", "needs_response_format": true},
    "quality_tags": ["code_strong"]
  },
  "local-mlx": {
    "provider": "openai",
    "endpoint": {"base_url": "http://localhost:4000/v1/chat/completions", "key_ref": ""},
    "reasoning": {"thinking": {"format": "openai", "required": false}},
    "output": {"json_compliance": "loose", "needs_response_format": false},
    "cost": {"pricing": null, "tco_per_call": 0.0005},
    "quality_tags": ["cheap"]
  }
}
```

### 4.2 ② Role Binding —— `config.json` 的 `router.roles`（复用现有，扩展字段）

```json
{
  "router": {
    "enabled": true,
    "roles": {
      "planner":  {"model": "glm-5.3", "temperature": 0.2, "timeout_ms": 120000},
      "evaluator":{"model": "glm-5.3", "temperature": 0.0, "thinking": {"enabled": true}},
      "worker":   {"easy": {"model": "local-mlx"},
                   "medium": {"model": "local-mlx"},
                   "hard":  {"model": "deepseek-v4-pro", "thinking": {"enabled": true}}},
      "reviewer": {"model": "deepseek-v4-pro"}
    },
    "fallback": {"planner": "local-mlx", "evaluator": "local-mlx"}
  }
}
```

### 4.3 ③ Deployment —— 留代理侧（不动），agent_go 仅保留：

```json
{
  "worker_base_url": "http://localhost:4000",
  "local": {"enabled": true, "proxy_url": "http://localhost:4000"}
}
```

## 5. 与现有机制的关系与迁移

| 现有 | 归类 | 迁移 |
|------|------|------|
| `plan_api` / `planner_api` | ①+② 混合 | 拆：endpoint→①，temperature/max_tokens→② roles.planner |
| `evaluator.*` | ①+② 混合 | 同上 →② roles.evaluator（**补齐 router 角色缺口**） |
| `worker_models{easy,medium,hard}` | ② roles.worker 特例 | 并入 router.roles.worker |
| `worker_backends` | ③（放错层） | **收敛删除**，worker 统一走 `worker_base_url`（代理内部路由） |
| `local_model_cost` | ①（放错层） | 移入 models registry `cost.tco_per_call` |
| `pricing.py` / `MODEL_TIER` | ① | 已正确，registry 引用 |
| `router.roles` | ② | **直接复用扩展**（补 evaluator 角色 + thinking 字段） |
| `call_api` 硬编码 thinking 检测 | ① 推理特性 | 改读 ① `reasoning.thinking`（删硬编码） |
| `min_difficulty` / `e2e` | ② 场景策略 | 保留 config（任务级），或入 roles.worker 默认 |

**迁移原则（兼容优先，不破坏现有）**：
- `models.json` 缺失 → 全量走现有 config.json 逻辑（fallback）
- `router.enabled=false` → 现有 plan_api/worker_models 逻辑
- 分阶段：P1 只加 registry + evaluator 角色 + thinking 声明式（GLM/v4-pro 不再改代码）；P2 worker_backends 收敛；P3 plan_api/planner_api 合并

## 6. call_api 目标逻辑（消除硬编码）

```python
def call_api(config, messages, logger, role="planner"):
    binding = resolve_role_binding(config, role)          # ②（router.enabled 时）
    model_id = binding["model"]
    reg = model_registry().get(model_id, {})              # ①
    # 推理特性（① 声明式，覆盖默认；② 可覆盖①）
    thinking = binding.get("thinking", reg.get("reasoning", {}).get("thinking", {}))
    if thinking.get("required") or binding.get("thinking", {}).get("enabled"):
        payload["thinking"] = _thinking_payload(thinking, reg)  # 按 ① format 构造
    # JSON 输出（① output.json_compliance + ② 场景需要）
    if binding.get("json_output") or reg.get("output", {}).get("needs_response_format"):
        payload["response_format"] = {"type": "json_object"}
```

接新模型只加 `models.json` 一条记录，**零代码改动**（thinking/JSON 特性声明式）。

## 7. 验收标准

1. 接入新模型只需在 `models.json` 加一条 + `router.roles` 指派角色，**不改任何 .py**
2. GLM / deepseek-v4-pro / local-mlx 三模型在 registry 声明后，planner/evaluator/worker 按角色正常工作（thinking/JSON 自动适配）
3. `worker_backends` 删除后，worker 经 worker_base_url → 代理路由正常（hard→云端验证）
4. 现有 config.json 无 models.json 时行为完全不变（回归测试全过）
5. m4-glm-hard 用 registry 配置重跑，通过率 ≥ 5/6

## 8. 风险与开放问题

| 风险 | 对策 |
|------|------|
| 与 router.roles 现有 fallback/熔断的语义合并复杂 | P1 只加字段不改 fallback 逻辑 |
| worker_backends 收敛影响现有 local profile | 保留兼容读取（有 worker_backends 时优先，新增不推荐） |
| 代理侧路由是黑盒（agent_go 无法感知 hard→云端） | metering 按 URL 标 is_local 的局限已记录（b17），需代理路由归因接口（见 §8.1） |
| models.json 与 pricing.py 双定价源 | registry.pricing 只做引用（pricing.py 为唯一价格表） |

## 8.1 ③ 部署拓扑接口契约（llama.cpp 提供）

③ 归代理侧，但 agent_go 需要代理提供**部署可视**与**路由归因**接口才能闭环。详见 [llama-defender-integration-requirements.md](llama-defender-integration-requirements.md) §3.1（R8-R12）。

### 现有接口盘点（按三层映射，已覆盖 ~80%）

| 层 | 接口 | 状态 |
|----|------|------|
| 推理 | `/v1/messages`、`/v1/chat/completions` | ✅（thinking/response_format 透传已修复） |
| 模型 | `GET /v1/models` | ✅ 别名+route metadata（能力元数据不足，R10） |
| 状态 | `GET /status`(HTML)、`GET /api/status`(JSON)、`/api/profiles`、`/api/watchdog` | ✅ |
| 监控 | `GET /metrics`、`/metrics/history`、`/session` | ✅ |
| 控制 | `manage.sh`（start/stop/status/reload/switch/watchdog）、`POST /admin/route/force-local|force-cloud` | ✅ |
| 路由 | SmartRouter 智能路由（模型偏好/header/冷却/阈值/内存/会话/sticky/熔断/预算） | ✅ |

### Gap 与需提供接口（优先级）

| Gap | 影响 | 需提供接口 | 优先级 |
|-----|------|-----------|--------|
| **G2 路由归因无返回**：metering 按 URL 标 is_local，force_fallback ~36% 回退本地归因全错 | 成本错算 | R8：响应头/字段带回 `X-Proxy-Route-Target/Actual-Model/Reason/Cost` | **P0** |
| **G3 部署拓扑不可视**：MODEL_ROUTE_PREFERENCES 对 agent_go 不可见 | 无法预知模型走哪 | R9：`GET /api/route/policies`（脱敏：cloud_model/cloud_key_set/preferences） | **P0** |
| G1 /v1/models 无能力元数据 | ① registry 手工录入 | R10：metadata 增强（real_model/thinking_supported/json_compliance/context_chars） | P1 |
| G4 健康检查探测依据不一致 | 探测不准 | R11：/api/status 增 route_config（cloud_model/route_enabled/cloud_key_set） | P1 |
| HTTP 热重载缺失（仅 CLI） | 远程场景受限 | R12：`POST /admin/reload`（可选） | P2 |

**边界**（agent_go 侧，不需代理做）：① 逻辑 registry（quality_tags/pricing/角色绑定）、② 角色场景参数、Plan/拆解/e2e 判定。

## 9. 分阶段落地

| 阶段 | 内容 | 依赖 |
|------|------|------|
| **P1** | models.json registry + evaluator 角色入 router + thinking/JSON 声明式（call_api 读 ①） | router.py/config.py/api.py |
| **P2** | worker_backends 收敛（worker 统一 worker_base_url）+ local_model_cost 迁入 registry | executor.py/profiles.py |
| **P3** | plan_api/planner_api 合并 → roles.planner；文档与 CLI（`agent_go models list/add`） | cli.py/docs |

---

## 10. 产品价值论证（实验证据量化）

按 m4 系列实验批次，本设计对 agent_go 产品能力的提升：

### 10.1 能力上限：接对强模型 → hard 通过率实质提升

同一批 6 个 hard 任务（canonical hard，跨 3 fixture）的演进链：

```
本地 35B（拆分）          0/6   (0%)
混合 v2（v4-flash 拆分）   0/6   (0%)
e2e 端到端（v4-flash）     2/6   (33%)
e2e + 云端 v4-pro         3/6   (50%)
e2e + GLM glm-5.3 评估     5/6   (83%)
```

**结论**：能力上限不由框架决定，而由"能否方便地接对强模型"决定。三层设计（① 一次注册 + ② 角色绑定）让换模型从"改多处配置+改代码"变为"registry 加一条"——这是 hard 通过率 0%→83% 的底层条件。

### 10.2 接入边际成本：模型池化的前提

| 维度 | 现状（压平混放） | 三层设计后 |
|------|----------------|-----------|
| 接入新模型改动面 | 5+ 处配置 + call_api 改代码 | models.json 加 1 条，**零代码** |
| thinking/JSON 特性 | call_api 硬编码检测（每模型改代码） | ① 声明式（`reasoning.thinking`/`output.json_compliance`） |
| 验证新模型 | 全链路重跑试错 | registry 声明 → 直接 bench 验证 |

GLM 接入实测改动面（5 处+代码）→ 设计后 1 条 registry 记录，边际成本趋零，**模型池化（多模型共存评测）才可行**。

### 10.3 观测归因可信度：决策依据可信

- **问题**：metering 按 URL（localhost）标 is_local，opus-4-7 force_fallback ~36% 回退本地 → bench 成本/通过率归因全错（决策基于错误数据）
- **解法**：R8 路由归因返回（代理响应头 X-Proxy-Route-Target/Actual-Model/Cost）→ metering 按实际 route_target 标注
- **价值**：bench 数据（pass_rate、$/pass、TCO 口径）成为**可信决策依据**（模型选型、门禁、成本核算的前提）

### 10.4 角色/场景扩展效率

新角色（如安全审查员）/新场景（如 e2e 模式）的接入动作 **90% 收敛在 ② 角色绑定**（router.roles 指派 + 场景参数），只有判定/调用需少量一次性代码。变化被隔离在 ②，① 模型固有与 ③ 部署拓扑不受牵连——**扩展不破坏稳定层**。

### 10.5 可维护性：消除双实现漂移

`worker_backends`（agent_go 侧）与 `MODEL_ROUTE_PREFERENCES`（代理侧）是同一部署拓扑职责的两处实现 → 收敛到代理侧单一事实源，消除"改一处忘另一处"的漂移类 bug。

### 10.6 演进路径

三层设计 + R8-R12 接口是**模型池化 + 多模型评测**的基建：模型注册（①）→ 角色绑定（②）→ 部署路由（③）→ 观测归因（④，R8 支撑）→ 反哺①quality_tags 与②场景选择的闭环。没有这个闭环，"持续测评更强模型"只能靠手工试错。

**一句话**：本设计把"接模型、选模型、评模型"从多点手工配置升级为可扩展的模型池体系——直接支撑 hard 能力上限（83%）与数据可信（R8）两大产品指标。
