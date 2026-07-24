# 角色感知模型路由 — 技术设计

> 状态：方案设计 | 日期：2026-07-24 | 目标迭代：P1

## 1. 目标

Plan 走前沿模型，Execute 走便宜模型，成本降 3-5 倍，质量不降。

```
当前：所有子任务 → 同一个 plan_api 模型 → ~$0.05-0.15/任务
目标：Planner → 前沿模型 | Worker → 快速/本地模型 | Reviewer → 不同源模型
```

## 2. 现有基础设施（可直接复用）

| 现有组件 | 位置 | 改造方式 |
|---------|------|---------|
| `call_api()` | `api.py:20` | 增加 `role` 参数，按角色选 provider |
| `generate_plan()` | `api.py:100` | 调用时传入 `role="planner"` |
| `run_subtask()` | `executor.py:376` | 已有 `agent_type`，传给路由层作为 key |
| `DEFAULT_CONFIG` | `config.py:19` | 扩展 `plan_api` → 新增 `router` 配置块 |
| `log_event()` | `config.py:115` | 已有结构化日志，扩展字段即可 |
| `AgentType` | `agents.py:70` | 已有 `type_name`，可作为路由 key |
| 三级 fallback | `api.py:253` | 已有 `decompose_fallback()`，可复用其本地模型调用逻辑 |

## 3. 配置设计

### 3.1 新增 `router` 配置块

```json
{
  "plan_api": { },
  "router": {
    "enabled": false,
    "roles": {
      "planner": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "temperature": 0.2,
        "fallback": null
      },
      "worker": {
        "provider": "custom",
        "base_url": "http://localhost:11434/v1/chat/completions",
        "model": "qwen3-coder",
        "max_tokens": 4096,
        "temperature": 0.1,
        "max_concurrency": 4,
        "timeout_ms": 120000,
        "fallback": {
          "provider": "anthropic",
          "base_url": "https://api.anthropic.com/v1/messages",
          "model": "claude-haiku-4-5-20251001",
          "max_tokens": 4096,
          "temperature": 0.1
        }
      },
      "reviewer": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com/v1/messages",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4096,
        "temperature": 0.0,
        "fallback": null
      }
    },
    "agent_type_mapping": {
      "developer": "worker",
      "architect": "planner",
      "reviewer": "reviewer",
      "tester": "worker"
    },
    "circuit_breaker": {
      "failure_threshold": 5,
      "cooldown_seconds": 60,
      "half_open_requests": 2
    }
  }
}
```

### 3.2 设计要点

- **`router.enabled: false` 默认关闭** — 不配置路由时行为与现在完全一致，走 `plan_api`
- **`agent_type_mapping`** — 将现有 `agent_type`（developer/architect/reviewer/tester）映射到路由角色（planner/worker/reviewer）
- **每个 role 独立配置 provider/model/fallback** — 与 `plan_api` 同构，多了 `max_concurrency`、`timeout_ms`、`fallback`
- **`fallback: null` 表示不降级** — Planner 铁律：不给 Planner 配降级到弱模型
- **`circuit_breaker` 全局配置** — 所有 provider 共享熔断参数

## 4. 新增模块：`router.py`

### 4.1 模块职责

```
router.py
├── ProviderConfig        — 单个 provider 配置
├── RoleRoute             — 一个角色的路由配置（primary + fallback）
├── resolve_provider()    — 根据 agent_type 解析路由
├── call_with_role()      — 按角色路由调用 LLM API
├── CircuitBreaker        — 熔断器状态机
└── log_metering()        — 结构化计量日志
```

### 4.2 核心接口

```python
# router.py

@dataclass
class ProviderConfig:
    provider: str          # "anthropic" | "openai" | "deepseek" | "custom"
    base_url: str
    model: str
    max_tokens: int
    temperature: float
    max_concurrency: int   # 本地模型并发上限
    timeout_ms: int        # 超时时间

@dataclass
class RoleRoute:
    """一个角色的路由配置"""
    role: str                    # "planner" | "worker" | "reviewer"
    primary: ProviderConfig
    fallback: Optional[ProviderConfig]  # None = 不降级

def resolve_provider(
    agent_type: str,            # "developer" | "architect" | "reviewer" | "tester"
    config: dict,
) -> Optional[RoleRoute]:
    """
    根据 agent_type 解析路由配置。

    如果 router.enabled=false，返回 None（走现有 plan_api 路径）。
    如果 agent_type 不在 mapping 中，默认映射到 "worker"。
    """
    ...

def call_with_role(
    route: RoleRoute,
    messages: list[dict],
    api_key: str,
    logger: logging.Logger,
    task_id: str = "",
    subtask_id: str = "",
) -> tuple[str, dict]:
    """
    按角色路由调用 LLM API。

    返回 (响应内容, 计量信息)。

    计量信息格式：
    {
        "task_id": str,
        "subtask_id": str,
        "role": str,              # "planner" | "worker" | "reviewer"
        "virtual_model": str,     # 角色名
        "actual_provider": str,   # 实际使用的 provider
        "actual_model": str,      # 实际使用的 model
        "prompt_tokens": int,
        "completion_tokens": int,
        "cost_usd": float,
        "latency_ms": float,
        "result": str,            # "success" | "fallback" | "quality_fail"
        "fallback_reason": str,   # 降级原因（如有）
    }
    """
    ...

class CircuitBreaker:
    """熔断器状态机：正常 → 熔断 → 半开 → 正常"""

    def __init__(self, failure_threshold: int, cooldown_seconds: int, half_open_requests: int):
        ...

    def allow_request(self) -> bool:
        """当前是否允许请求通过"""
        ...

    def record_success(self) -> None:
        ...

    def record_failure(self) -> None:
        ...
```

### 4.3 路由决策流程

```
call_with_role(route, messages, api_key, logger)
  │
  ├─ 1. 尝试 primary provider
  │     ├─ 检查熔断器状态
  │     │   ├─ 熔断中 → 跳到 fallback
  │     │   └─ 正常/半开 → 继续
  │     ├─ 调用 call_api_internal(primary, messages, api_key)
  │     │   ├─ 成功 → 记录 success，返回内容 + 计量信息
  │     │   └─ 可用性失败（超时/连接/429）
  │     │       ├─ 记录 failure，熔断计数 +1
  │     │       └─ 如果有 fallback → 跳到 fallback
  │     │           如果无 fallback → 抛出异常
  │     └─ 质量性失败（输出不可解析）
  │         └─ 原 provider 重试 1 次 → 仍失败则升级到 fallback
  │
  └─ 2. Fallback provider（如有）
        ├─ 调用 call_api_internal(fallback, messages, api_key)
        ├─ 记录计量信息（result="fallback", fallback_reason="..."）
        └─ 返回内容 + 计量信息
```

## 5. 集成点改造

### 5.1 `api.py` — `generate_plan()` — 最小改动

```python
# 改造前
content = call_api(config, messages, logger)

# 改造后
from .router import resolve_provider, call_with_role
route = resolve_provider("architect", config)  # Plan 固定用 architect → planner
if route:
    api_key = get_api_key(config)
    content, metering = call_with_role(route, messages, api_key, logger, task_id=task_id)
    log_event(logger, "api_call", metering)
else:
    content = call_api(config, messages, logger)  # 回退到现有逻辑
```

### 5.2 `executor.py` — `run_subtask()` — 不改造（当前阶段）

`run_subtask()` 不直接调用 LLM API——它 spawn claude CLI 进程。当前阶段路由在 Plan 阶段生效。

Worker 子任务仍走 claude CLI，未来可扩展为直接 API 调用以利用本地模型。

### 5.3 `config.py` — `DEFAULT_CONFIG` — 新增 router 块

```python
DEFAULT_CONFIG = {
    "plan_api": { },  # 保持不变
    "router": {
        "enabled": False,  # 默认关闭，向后兼容
        "roles": {},
        "agent_type_mapping": {
            "developer": "worker",
            "architect": "planner",
            "reviewer": "reviewer",
            "tester": "worker",
        },
        "circuit_breaker": {
            "failure_threshold": 5,
            "cooldown_seconds": 60,
            "half_open_requests": 2,
        },
    },
}
```

### 5.4 `metrics.py` — 新增 `estimate_cost()`

```python
# 定价表（$/M tokens）
DEFAULT_PRICING = {
    ("anthropic", "claude-sonnet-4-20250514"):  (3.0, 15.0),
    ("anthropic", "claude-haiku-4-5-20251001"): (0.80, 4.0),
    ("anthropic", "claude-opus-4-20250514"):    (15.0, 75.0),
    ("openai", "gpt-4o"):                       (2.50, 10.0),
    ("openai", "gpt-4o-mini"):                  (0.15, 0.60),
    ("deepseek", "deepseek-chat"):              (0.27, 1.10),
    ("custom", "*"):                             (0.0, 0.0),   # 本地模型
}

def estimate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算 API 调用成本（美元）"""
    key = (provider, model)
    wildcard = (provider, "*")
    prices = DEFAULT_PRICING.get(key) or DEFAULT_PRICING.get(wildcard, (0, 0))
    return (prompt_tokens / 1_000_000) * prices[0] + (completion_tokens / 1_000_000) * prices[1]
```

## 6. 计量日志格式

每 API 请求落一条结构化日志（DEBUG 级别 JSON）：

```json
{
  "timestamp": "2026-07-24T15:30:00",
  "event": "api_call",
  "task_id": "task-abc123",
  "subtask_id": "sub-1",
  "role": "worker",
  "virtual_model": "agentgo-worker",
  "actual_provider": "custom",
  "actual_model": "qwen3-coder",
  "difficulty": "easy",
  "prompt_tokens": 1200,
  "completion_tokens": 800,
  "cost_usd": 0.0,
  "latency_ms": 3421.5,
  "result": "success",
  "fallback_reason": ""
}
```

## 7. 实施步骤

| 步骤 | 内容 | 预估 | 涉及文件 |
|------|------|------|---------|
| S1 | 扩展 `DEFAULT_CONFIG`，新增 `router` 配置块 | 0.5h | `config.py` |
| S2 | 新增 `router.py`：`ProviderConfig`、`RoleRoute`、`resolve_provider()` | 1h | `router.py` (new) |
| S3 | 新增 `router.py`：`call_with_role()` + `CircuitBreaker` | 1.5h | `router.py` |
| S4 | 新增 `metrics.py`：`estimate_cost()` + 定价表 | 0.5h | `metrics.py` |
| S5 | 改造 `api.py`：`generate_plan()` 注入路由逻辑 | 0.5h | `api.py` |
| S6 | 改造 `api.py`：`call_api()` 日志增加计量字段 | 0.5h | `api.py` |
| S7 | CLI 命令：`agent_go config router` 交互式配置 | 1h | `cli.py` |
| S8 | 单元测试：router、circuit_breaker、cost estimation | 2h | `tests/test_router.py` |
| S9 | 集成测试：端到端 Plan + Worker 路由 | 1h | `tests/test_router_integration.py` |

**总计：~8.5h**

## 8. 向后兼容保证

1. **`router.enabled: false` 默认关闭** — 不配置路由时，所有代码路径走现有 `plan_api`，行为与现在完全一致
2. **`plan_api` 配置块不动** — 现有用户配置无需修改
3. **`resolve_provider()` 返回 `None` 时** — `generate_plan()` 回退到 `call_api(config, messages, logger)` 现有路径
4. **`agent_type_mapping` 缺失的 agent_type** — 默认映射到 `"worker"`

## 9. 铁律（不可妥协）

| # | 铁律 | 实现方式 |
|---|------|---------|
| 1 | Planner 不给配降级到弱模型 | `planner.fallback` 强制为 `null`，配置校验时拒绝 |
| 2 | 每次降级必须留痕 | `call_with_role()` 在 fallback 路径强制写 `fallback_reason` |
| 3 | 本地模型并发上限显式设置 | `worker.max_concurrency` 必填，默认值 4，配置校验时警告 |
| 4 | 降级对 Agent 透明，对审计不透明 | 降级不改变返回给 Agent 的内容格式，但计量日志必须记录 |