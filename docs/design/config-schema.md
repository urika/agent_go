# 配置 Schema 参考文档

> 状态：As-Built（对应 `config.py` `DEFAULT_CONFIG`）
> 更新日期：2026-08-08
> 关联：[spec.md](../spec.md) config.py 接口签名、[ADR-005](adr/ADR-005-cost-control-layers.md) 成本控制层、[timeout-kill-strategy-2026-08-06.md](timeout-kill-strategy-2026-08-06.md) L1/L2/L3

本文档列出 `~/.agent_go/config.json` 的全部顶层配置块、字段、默认值、类型和语义。配置文件不存在时自动创建，与 `DEFAULT_CONFIG` 浅合并（用户配置覆盖默认值）。

API key 解析优先级：环境变量 `AGENT_GO_API_KEY` > `config.json` `plan_api.api_key`。模板变量 `${VAR_NAME}` 从环境变量解析。

---

## 顶层配置块一览（21 个）

| # | 配置块 | 默认开关 | 用途 |
|---|---|---|---|
| 1 | `plan_api` | — | LLM API 全局配置（provider / model / key / timeout） |
| 2 | `planner_api` | — | Plan 生成专用 API（非空时覆盖 `plan_api`） |
| 3 | `behavior` | — | 交互行为控制（自动确认 / 显示选项） |
| 4 | `verification` | — | 验证循环参数（重试次数 / 超时） |
| 5 | `goal` | `enabled: false` | Claude Code `/goal` Stop Hook 机制 |
| 6 | `agent_loop` | `enabled: false` | 自主 agent 循环模式 |
| 7 | `evaluator` | `enabled: false` | LLM 语义评估器 |
| 8 | `fallback` | — | 三级降级：外部 API → 本地模型 → 规则分解 |
| 9 | `cost_control` | `enabled: false` | 三层成本控制（L1/L2/L3）+ budget_mode |
| 10 | `skills` | — | Skill 自动发现和加载上限 |
| 11 | `agents` | — | Agent 类型默认配置 |
| 12 | `artifact_dir` | `null` | 产物导出目录 |
| 13 | `worker_models` | — | 难度→模型路由 |
| 14 | `worker_models_fallback` | — | 难度→模型降级备选 |
| 15 | `worker_models_degrades` | — | 预算降级时难度下移映射 |
| 16 | `worker_models_by_type` | — | Agent type→模型映射 |
| 16a | `worker_backend` / `worker_backend_by_difficulty` / `worker_backend_by_type` | `""` | B3/B4 worker backend 显式选择与声明式路由（见 13–16 末节） |
| 17 | `local_model_names` | — | 路由名→本地真实模型名映射 |
| 18 | `cache` | `enabled: true` | Plan 缓存 |
| 19 | `router` | `enabled: false` | 角色（planner/worker/reviewer）→ provider/model 路由 |
| 20 | `mcp_servers` | — | 外部 MCP server 配置 |
| 21 | `knowledge` | `enabled: false` | C4 KnowledgeStore A/B 臂 + 葬礼回写质量 |

---

## 1. `plan_api`

LLM API 全局配置。所有 LLM 调用（Plan 生成、评估器、agent loop）的默认基础。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `provider` | str | `"anthropic"` | API 提供商：`anthropic` / `openai` / `deepseek` / `custom` |
| `base_url` | str | `"https://api.anthropic.com/v1/messages"` | API 端点 |
| `api_key` | str | `""` | API 密钥（支持 `${VAR_NAME}` 模板变量） |
| `model` | str | `"claude-sonnet-4-20250514"` | Plan 生成使用的模型名 |
| `max_tokens` | int | `4096` | 最大输出 token 数 |
| `temperature` | float | `0.2` | 采样温度 |
| `timeout_ms` | int | `180000` | 请求超时（毫秒） |
| `worker_base_url` | str | `""` | Worker 子任务专用 base_url（覆盖 `base_url`，用于本地代理）。**推荐**（模型实体三层设计 P2）：worker 统一走此单值，模型→后端细粒度路由留代理侧。`worker_backends`（按模型名映射 ANTHROPIC_BASE_URL）已 **DEPRECATED**（部署拓扑放错层，与代理路由重复）——保留兼容（有则优先、无则本字段 fallback），使用时 warning 提示迁移，`config local` 自 2026-08-19 起不再生成该字段，新配置请只用本字段 |
| `worker_max_tokens` | int | `0` | Worker 最大 token（`0` = 使用 claude CLI 默认） |
| `local_models` | list | `[]` | 标记为本地模型的名称列表（metering cost 归零） |

**密钥解析**：`AGENT_GO_API_KEY` 环境变量 > `plan_api.api_key`。

---

## 2. `planner_api`

Plan 生成专用 API 配置。非空时**完全覆盖** `plan_api`（Planner 与 Worker 使用不同 provider/model/endpoint）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| (同 `plan_api` 结构) | dict | `{}` | 空字典 = 不覆盖，使用 `plan_api` |

---

## 3. `behavior`

交互行为控制。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `auto_confirm_plan` | bool | `false` | 自动确认 Plan（`--yes`） |
| `auto_confirm_subtasks` | bool | `false` | 自动确认子任务分解 |
| `auto_verify_subtask` | bool | `false` | 自动执行验证命令 |
| `show_agent_prompt` | bool | `true` | 显示 agent prompt 详情 |
| `show_resource_map` | bool | `true` | 显示项目资源映射 |
| `max_plan_iterations` | int | `5` | Plan 生成最大重试次数 |
| `plan_preflight_repair_enabled` | bool | `true` | Plan 门禁不通过时启用预修复（LLM 定向修复 plan 缺陷后重提门禁） |
| `max_plan_repairs` | int | `1` | 预修复最大次数（防循环） |
| `notify_on_complete` | bool | `true` | 任务完成时发送通知 |
| `notify_command` | str | `""` | 自定义通知命令（覆盖内置通知通道） |

---

## 4. `verification`

验证循环参数。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_retries` | int | `3` | 验证失败后最大修复重试次数 |
| `retry_timeout` | int | `300` | 每次重试超时（秒） |
| `run_timeout` | int | `1800` | Claude 执行超时（秒） |
| `block_on_failure` | bool | `true` | 验证失败时阻止下游子任务 |
| `diverge_similarity_threshold` | float | `0.3` | 打地鼠检测：连续两次语义评估缺陷指纹相似度低于此值 → 提前终止重试 |
| `revert_threshold` | int | `2` | 连续 revert 次数达到该值终止重试（检测「改了又改回」空转） |
| `readonly_review` | object | `{enabled:false}` | 独立只读审查 subagent（两阶段审查，见下） |
| `architecture_review` | object | `{enabled:false}` | 架构合规独立审查（见下） |

### `verification.readonly_review`（独立只读审查 subagent）

验证失败时，用独立模型黑盒分析失败根因（不参与实现），审查意见注入修复 prompt，消除「实现者盲区」。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用只读审查（默认关，成本可控） |
| `threshold` | int | `2` | Reflexion 阈值化（B5）：retry_count ≥ 该值才触发审查（首次失败先给修复机会，避免每次重试都调独立模型） |
| `model` | str | `""` | 审查模型（空 = 复用 evaluator.model） |
| `provider` | str | `""` | 审查 API 提供商（空 = 复用 evaluator.provider） |
| `base_url` | str | `""` | 审查 API 端点（空 = 复用 evaluator.base_url） |
| `skill` | str | `""` | 审查维度 skill 名（空 = 内置通用模板）。配置后加载 `~/.agent_go/skills/<name>/SKILL.md` 的 body 作为「领域审查维度指引」注入审查 prompt |
| `max_tokens` | int | `2048` | 审查响应最大 token |
| `timeout_ms` | int | `90000` | 审查 API 调用超时（毫秒） |

### `verification.architecture_review`（架构合规独立审查）

对带 Architecture 约束的任务，用独立模型审查实现 diff 是否偏离架构决策（M1.4 架构合规的可选 LLM 复核层）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用架构审查（默认关） |
| `model` | str | `""` | 审查模型（空 = 复用 evaluator.model） |
| `provider` | str | `""` | 审查 API 提供商（空 = 复用 evaluator.provider） |
| `base_url` | str | `""` | 审查 API 端点（空 = 复用 evaluator.base_url） |
| `max_tokens` | int | `2048` | 审查响应最大 token |
| `timeout_ms` | int | `90000` | 审查 API 调用超时（毫秒） |

---

## 5. `goal`

Claude Code `/goal` Stop Hook 机制。启用后在 worktree 中注入 `.claude/settings.json` + `verify-goal.sh`，Claude 完成任务时触发 goal 验收。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用 `/goal` 机制 |
| `max_turns` | int | `20` | Goal 验收最大轮次 |
| `timeout_seconds` | int | `600` | Goal 验收超时（秒） |
| `enable_goal_hook` | bool | `false` | 是否注入 Stop Hook（需要 worktree 写入权限） |
| `policy` | str | `"off"` | 最终执行策略（`off`/`verify`/`enforce`，goal-mechanism-design §3.3；B3 拍板默认 off，policy resolver 见 `goal_policy.py`） |

---

## 6. `agent_loop`

自主 agent 循环模式（`--agent-loop`）。不经过 Claude Code CLI，直接调 API + 执行工具。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用 agent loop |
| `max_turns` | int | `20` | 最大对话轮次 |
| `max_duration` | int | `600` | 最大运行时间（秒） |
| `api_timeout` | int | `120` | 每次 API 调用超时（秒） |

---

## 7. `evaluator`

LLM 语义评估器。验证命令通过后，调用 LLM 对 Claude 输出做语义判断。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 是否启用语义评估 |
| `fail_closed` | bool | `false` | `true` = 评估器故障时判定失败；`false` = 评估器故障时跳过 |
| `provider` | str | `"anthropic"` | 评估器 API 提供商 |
| `model` | str | `"claude-haiku-4-5-20251001"` | 评估器使用的模型（推荐低成本模型） |
| `base_url` | str | `"https://api.anthropic.com/v1/messages"` | 评估器 API 端点 |
| `api_key` | str | `""` | 评估器 API 密钥（空 = 复用 `plan_api.api_key`） |
| `prompt_template` | str | `"default"` | 评估 prompt 模板名称 |

---

## 8. `fallback`

三级降级链。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `local_model_url` | str | `"http://localhost:8000/v1/chat/completions"` | 本地模型 API 端点 |
| `local_model_name` | str | `"qwen"` | 本地模型名称 |
| `enable_rules` | bool | `true` | 允许降级到规则分解（最末级） |

---

## 9. `cost_control`

三层成本控制。详见 [ADR-005](adr/ADR-005-cost-control-layers.md) 和 [timeout-kill-strategy-2026-08-06.md](timeout-kill-strategy-2026-08-06.md)。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | **`false`** | L2/L3 成本控制总开关。**默认关闭**——基线不可信时误杀率高，须 `eval cost-baseline` 校准后才开 |
| `l1_enabled` | bool | **`false`** | L1 单次调用硬上限开关。**注：默认关闭；运行时 cold-start 会自动启用以防止失控调用** |
| `max_budget_usd` | float | `0.50` | L3 任务级总预算上限 |
| `per_subtask_budget_usd` | dict | `{"easy": 0.20, "medium": 0.40, "hard": 1.00}` | L1 单次调用预算（按难度）。也支持标量格式（所有难度共用） |
| `subtask_multiplier` | float | `2.5` | L2 子任务跨重试上限 = `per_subtask_budget × subtask_multiplier` |
| `on_exceed` | str | `"stop"` | 预算超限时的动作（保留字段） |
| `budget_mode` | str | `"strict"` | 预算模式：`strict`（block）/ `degrade`（切换更便宜模型）/ `ignore`（关 L3，仅 L1/L2） |

**budget_mode 语义**：
- `strict`：超预算 → 剩余子任务标记 `blocked`，`kill_reason=over_budget_l3`
- `degrade`：超预算 → 剩余子任务使用 `worker_models_degrades` 降级模型，`degraded=True`，`max_retries→1`
- `ignore`：关闭 L3，仅保留 L1/L2

**reservation 机制**（仅 strict 模式）：wave 启动前预扣每个子任务的 L2 上限，防止并发启动同时超过任务预算。详见 [reservation 设计](#reservation-算法)。

---

## 10. `skills`

Skill 自动发现和加载上限。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `auto_discover` | bool | `false` | 自动扫描 `~/.agent_go/skills/` 并匹配 Skill |
| `max_auto_skills` | int | `3` | 自动加载的 Skill 数量上限（防止 prompt 膨胀） |

---

## 11. `agents`

Agent 类型默认配置。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `default` | str | `"developer"` | 默认 agent 类型（`developer` / `architect` / `reviewer` / `tester`） |

---

## 12. `artifact_dir`

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| (scalar) | str\|null | `null` | 产物导出目录。子任务 `worktree/__artifacts__/` 下的文件在任务完成后导出到此目录。`null` = 不导出 |

---

## 13–16. Worker 模型路由

### `worker_models`（难度→模型）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `easy` | str | `""` | easy 难度子任务使用的模型名 |
| `medium` | str | `""` | medium 难度子任务使用的模型名 |
| `hard` | str | `""` | hard 难度子任务使用的模型名 |

空字符串 = 使用 claude CLI 默认模型。

### `worker_backend` 系列（B3/B4：worker backend 选择与路由）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `worker_backend` | str | `""` | B3 全局显式 backend（如 `"pi"`，需本机已装对应 CLI） |
| `worker_backend_by_difficulty` | dict | `{easy/medium/hard: ""}` | B4 按难度路由 backend（空 = 不覆盖） |
| `worker_backend_by_type` | dict | `{}` | B4 按 agent_type 路由 backend（如 `{"explore": "pi"}`） |

解析优先级（高→低）：`subtask.backend` > `worker_backend` > `worker_backend_by_type` >
`worker_backend_by_difficulty` > agent_loop 自动规则 > claude 兜底。
解析出非 claude 时仅 headless 生效，交互模式回退 claude（pi/opencode 均为非交互 CLI）。
修复路径（fix/replan/reload）走同一解析（`backends/dispatch.run_repair`）。
**命名警示**：勿用 deprecated 的 `worker_backends`（模型名→`ANTHROPIC_BASE_URL` 映射，见
`plan_api.worker_base_url` 条目），与 B4 路由键语义完全不同。

### `worker_models_fallback`（难度→降级备选模型）

结构同 `worker_models`。当主模型不可用时使用。

### `worker_models_fallback_chain`（P0：难度→多级失败升级链）

值为模型 ID 数组，按验证失败/超时的重试顺序切换；空数组表示关闭。
旧的 `worker_models_fallback` 单值配置仍兼容，但新配置优先。

```json
{
  "worker_models_fallback_chain": {
    "easy": [],
    "medium": ["claude-opus-4-7"],
    "hard": ["kimi-for-coding", "glm-5.3", "deepseek-v4-pro", "local-mlx"]
  }
}
```

### evaluator 多级降级与低置信度复核（P0）

在 `router.roles.evaluator` 中使用 `fallbacks` 数组声明 provider/model 降级链。
模型可以只写 `model`，其 endpoint/key/thinking/JSON 能力从 `models.json` registry
解析；也可以显式覆盖 endpoint 或场景参数。primary 返回空内容、非法 JSON 或 API
不可用时自动按顺序尝试下一个 provider；有效但低置信度（默认 `<=0.5`）时自动
调用下一个 evaluator 做一次仲裁。

```json
{
  "router": {
    "enabled": true,
    "roles": {
      "evaluator": {
        "model": "kimi-k3",
        "fallbacks": [
          {"model": "glm-5.3"},
          {"model": "deepseek-v4-pro"},
          {"model": "local-mlx"}
        ]
      }
    }
  },
  "evaluator": {
    "arbitration": {"enabled": true, "confidence_threshold": 0.5}
  }
}
```

### `worker_models_degrades`（预算降级时难度下移）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `easy` | str | `""` | easy 降级目标（空 = 不降级） |
| `medium` | str | `"easy"` | medium 降级到 easy 档模型 |
| `hard` | str | `"medium"` | hard 降级到 medium 档模型 |

### `worker_models_by_type`（Agent type→模型）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| (agent type) | str | — | 按子任务 agent_type 指定模型（覆盖难度路由） |

### `worker_models_by_cognitive`（认知模式→模型，异构模型路由）

按认知模式（explore/implement/review）路由模型。优先级最高：配置后覆盖 `worker_models_by_type` 与 `worker_models[difficulty]`。认知模式来源：`subtask.cognitive_mode`（planner 可标注）或按 agent_type 推断（architect→explore, reviewer→review, 其余→implement）。例：`{"explore": "claude-haiku-4-5", "review": "claude-opus-4-8", "implement": "claude-sonnet-4-6"}`。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `explore` | str | `""` | 探索/分析类子任务模型（便宜模型） |
| `implement` | str | `""` | 实现类子任务模型（强模型） |
| `review` | str | `""` | 审查类子任务模型（独立模型） |

---

## 17. `local_model_names`

路由名→本地真实模型名映射。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| (model name) | str | — | 映射到本地后端真实模型名。如 `{"claude-haiku-4-5": "Qwen3.6-27B-4bit"}` |

当 `/status` 探测失败时的 fallback。

## 17b. `local_model_cost`

本地模型 TCO 显式归算（可选）。键为本地后端真实模型名，值为每次调用摊销成本（美元）。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| (model name) | float | — | 如 `{"mlx-community/Qwen3.6-27B-4bit": 0.0007}`；命中的本地调用按此计费并计入 $/pass 与 gate |

---

## 18. `cache`

Plan 缓存。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 启用 Plan 缓存 |
| `plan_ttl` | int | `86400` | Plan 缓存有效期（秒，默认 24h） |
| `max_entries` | int | `100` | 最大缓存条目数 |

---

## 19. `router`

角色感知路由。详见 [role-aware-routing-design.md](../in/role-aware-routing-design.md)。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 启用角色路由 |
| `roles` | dict | `{}` | 角色→provider/model 映射（`planner` / `worker` / `reviewer`，每个角色可设 `provider` / `model` / `fallback`） |
| `agent_type_mapping` | dict | `{"developer": "worker", "architect": "planner", "reviewer": "reviewer", "tester": "worker"}` | agent type → 路由角色映射 |
| `circuit_breaker` | dict | — | 断路器配置 |
| `circuit_breaker.failure_threshold` | int | `5` | 连续失败次数阈值 |
| `circuit_breaker.cooldown_seconds` | int | `60` | 断路器冷却时间（秒） |
| `circuit_breaker.half_open_requests` | int | `2` | 半开状态试探请求数 |

---

## 20. `mcp_servers`

外部 MCP server 配置。子任务可调用外部 MCP 工具，命名空间为 `mcp__{server}__{tool}`。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| (server name) | dict | — | 每个键是一个 server 配置 |

每个 server 配置：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `command` | str | 是 | 启动命令（如 `npx` / `python3`） |
| `args` | list | 是 | 命令参数 |
| `env` | dict | 否 | 环境变量 |
| `enabled` | bool | 否 | 是否启用（默认 `true`） |
| `tool_filter` | list | 否 | 允许调用的工具白名单 |
| `scope` | str | 否 | 作用域（`subtask` / `task`） |

内置示例：DEFAULT_CONFIG 自带 `playwright` 条目（`npx @playwright/mcp@latest`，`enabled:false`，`scope:worker`），供浏览器自动化任务启用。

---

## 21. `knowledge`

C4 KnowledgeStore：修复重试时向 repair prompt 注入跨任务历史经验（Problem/deviation/verify_state），及葬礼回写的解法质量控制。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 注入臂开关（false=A/B 对照臂；`bench --with-knowledge` 等价开启） |
| `max_items` | int | `3` | 单次注入的最大经验条数 |
| `suppressed_ids` | list | `[]` | 按 Problem id 屏蔽错误知识（可淘汰机制） |
| `resolution_llm` | bool | `true` | 葬礼回写时用 LLM 把「失败报错+修复内容」总结为根因+做法（根因级 `resolution_summary`）；fail-open，失败/关闭降级为 diffstat 级摘要 |

---

## reservation 算法

仅 `budget_mode=strict` 且 `cost_control.enabled=true` 时生效。

```
reservation_per_subtask = per_subtask_budget_usd[difficulty] × subtask_multiplier
```

在 wave 调度前执行：
1. 计算预算池：`pool = max_budget_usd OR Σ(per_subtask_budget × multiplier)`（全量 plan）
2. 计算可用额度：`available = pool - 已花费`（从 `metering.jsonl` 累计）
3. 逐子任务扣减：`reservation ≤ available` 则 admit 并 `available -= reservation`；否则 blocked
4. Blocked 子任务：`status=blocked`, `kill_reason=over_budget_l3`, `blocked_by=["cost_control"]`

reservation 是并发启动的安全网，实际成本仍由 L3 在下一 wave 复核。

---

## 配置加载语义

- 配置文件不存在 → 自动创建，使用 `DEFAULT_CONFIG` 深拷贝
- 配置文件存在 → 与 `DEFAULT_CONFIG` **浅合并**（顶层 key 覆盖，嵌套字段不递归合并）
- `${VAR_NAME}` → 从环境变量解析
- `AGENT_GO_API_KEY` 环境变量 > `plan_api.api_key`

> **注意**：浅合并意味着如果用户配置中写了 `"cost_control": {"max_budget_usd": 1.0}`，则 `per_subtask_budget_usd`、`subtask_multiplier` 等其他字段会丢失（回退到默认）。如需覆盖部分字段，需要在用户配置中写出完整的嵌套结构。
