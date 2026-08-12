# 角色-模型配置矩阵

> 状态：As-Built（对应 `config.py` DEFAULT_CONFIG + 运行时路由）
> 更新日期：2026-08-12
> 关联：[config-schema.md](config-schema.md)（字段级 schema）、[local-model-management-design.md](local-model-management-design.md)（本地模型）、[router-multi-provider-extension.md](router-multi-provider-extension.md)（角色路由）
> 用途：回答"哪个角色用哪个模型、走哪个后端、优先级怎么排"，避免凭记忆拼配置。

---

## 1. 角色总览

agent_go 的 LLM 消费点按角色分 6 类。同一份配置可能被多个角色消费，但**每个角色有明确的模型来源与优先级**。

| 角色 | 职责 | 模型来源（优先级高→低） | 触发开关 |
|------|------|------------------------|---------|
| **planner** | 生成 Plan / 子任务分解 | `planner_api` → `plan_api` | 每次 run |
| **worker** | 执行子任务（写代码，`claude -p`） | `worker_models_by_cognitive` → `worker_models_by_type` → `worker_models[difficulty]` → degrade 降档 | 每次子任务执行 |
| **evaluator** | LLM 语义评估（验证循环 Phase 3） | `evaluator` → `plan_api` | `evaluator.enabled=true` |
| **reviewer** | 独立只读审查（失败根因） | `verification.readonly_review` → `evaluator` → `plan_api` | `readonly_review.enabled=true` |
| **architect** | 架构审查（执行前 AD） | `architecture_review` → `plan_api` | `architecture_review.enabled=true` |
| **router** | 可选角色路由覆盖 | `router.roles[planner/worker/reviewer]` | `router.enabled=true` |

> **router 是横切覆盖层**：开启时按 `router.roles.<role>` 覆盖上述各角色的 model/provider/base_url，优先级高于角色自身的多级路由。

---

## 2. Planner 模型

**用途**：`generate_plan()` 生成结构化 JSON Plan（`api.py:call_api`）。

**来源**：
```python
api_cfg = config.get("planner_api") or config["plan_api"]   # api.py:26
```

**优先级**：`planner_api`（若配置）> `plan_api`。

**当前生效**（混合模式关键设计）：

| 配置块 | provider | base_url | model |
|--------|----------|----------|-------|
| `planner_api` | openai | `https://api.deepseek.com/v1/chat/completions` | `deepseek-v4-pro` |
| `plan_api`（fallback） | openai | 同上 | `deepseek-v4-pro` |

> **设计意图**：Planner 保持云端强模型（deepseek-v4-pro），与 Worker 本地模型解耦——"规划用强云端，执行用本地"。

---

## 3. Worker 模型（最重要、最复杂）

**用途**：`run_subtask()` 内 `claude -p <TASK.md>` 执行子任务（`subtask.py:277-306`）。

**3.1 模型名路由**（`executor.py:2151` 注入 `env["AGENT_GO_CLAUDE_MODEL"]`）——优先级高→低：

| 优先级 | 配置项 | 说明 |
|--------|--------|------|
| 1 | `worker_models_by_cognitive[cognitive_mode]` | 认知模式（explore/implement/review） |
| 2 | `worker_models_by_type[task_type]` | 任务类型覆盖（agent_type：developer/tester 等） |
| 3 | `worker_models[difficulty]` | **最常用**——按难度 easy/medium/hard |
| 4 | degrade 降档 | `budget_mode=degrade` 时按 `worker_models_degrades` 下移难度 |

**3.2 后端 endpoint 路由**（`executor.py:2056-2162`）——优先级高→低：

| 优先级 | 配置项 | 说明 |
|--------|--------|------|
| 1 | `worker_backends[routed_model]` | 按模型名精确映射 base_url |
| 2 | `plan_api.worker_base_url` | 统一 worker 后端 |
| 3 | （默认）`plan_api.base_url` | 退化为 plan 后端 |

> 最终设 `env["ANTHROPIC_BASE_URL"]`，Claude Code 自动追加 `/v1/messages`。

**3.3 当前生效**（Worker 本地、Planner 云端）：

| difficulty | 模型 | 后端（worker_backends） |
|-----------|------|------------------------|
| easy | `claude-haiku-4-5` | `http://localhost:4000`（本地） |
| medium | `claude-sonnet-4-6` | `http://localhost:4000`（本地） |
| hard | `claude-opus-4-7` | `http://localhost:4000`（本地） |

**3.4 成本与本地判定**（`executor.py:2163-2189` + `_verify_local_backend:95-148`）：

- `_is_local_url()`：base_url 含 `127.0.0.1/localhost/0.0.0.0/[::1]` → 视为本地
- 探测：`claude -p "hi"`（**不带 --model**）→ 解析响应 `message.model`
- 响应模型 == `/status` 声明本地模型 → `AGENT_GO_IS_LOCAL=1` 成本清零
- 真实模型名优先级：探测 `/status` > `local_model_names` 静态映射 > `routed_model`
- 探测失败 → 保守不清零（按云计）

> ⚠️ **已知问题**（2026-08-12）：探测命令不带 `--model`，claude 默认模型映射到本地代理的路由名，可能与 `/status` 声明不一致 → 误判为云后端（成本未清零，功能正常）。静态修复可配 `local_model_names`（当前为空）。

**3.5 降级链**（`worker_models_fallback` / `worker_models_degrades`）：

| 配置项 | 值 | 语义 |
|--------|-----|------|
| `worker_models_fallback` | easy→claude-sonnet-4-6, medium/hard→claude-opus-4-7 | 首选模型不可用时备选 |
| `worker_models_degrades` | easy→"", medium→easy, hard→medium | budget_mode=degrade 时难度下移 |

---

## 4. Evaluator 模型

**用途**：验证循环 Phase 3 的 LLM 语义评估（`evaluator.py:_default_semantic_eval`）。

**来源**（`evaluator.py:263-268`）：
```python
eval_api_cfg = dict(config.get("plan_api", {}))    # 基础
for key in ("provider","model","base_url","api_key"):
    if evaluator_cfg.get(key): eval_api_cfg[key] = evaluator_cfg[key]   # evaluator 覆盖
```

**优先级**：`evaluator` 块（provider/model/base_url/api_key 任一非空）> `plan_api`。

**当前生效**：

| 配置项 | 值 |
|--------|-----|
| `evaluator.enabled` | true |
| `evaluator.model` | deepseek-v4-pro |
| `evaluator.provider`/`base_url` | openai / `api.deepseek.com`（云端） |
| `evaluator.strategy` | default（可换 visual） |

> **设计意图**：语义评估用云端独立模型审查 Worker（本地模型）的产出——独立审查，成本与 worker 分开计量。

---

## 5. Reviewer 模型（readonly_review，默认关闭）

**用途**：验证失败时独立只读审查（黑盒根因分析，`review_agent.py:run_readonly_review`）。

**来源**（`review_agent.py:156-175`）：
```python
rr_cfg = config["verification"]["readonly_review"]
api_cfg = dict(config["plan_api"])
for key in ("model","provider","base_url"):
    if rr_cfg.get(key): api_cfg[key] = rr_cfg[key]
    elif eval_cfg.get(key): api_cfg[key] = eval_cfg[key]
```

**优先级**：`verification.readonly_review` > `evaluator` > `plan_api`。

**当前生效**：`enabled=false`（模型字段空，未启用）。

---

## 6. Architect 模型（architecture_review，默认关闭）

**用途**：执行前架构审查，生成 approved/rejected/changes_requested 决策（`governance.py`）。

**来源**（`governance.py:147-177`）：
```python
cfg = config.get("architecture_review") or {}
api_cfg = dict(config["plan_api"])
# architecture_review 的 model/provider/base_url 覆盖 plan_api
```

**优先级**：`architecture_review` > `plan_api`。

**当前生效**：`enabled=false`（未启用）。

---

## 7. Router（可选横切覆盖）

**用途**：角色感知路由，`router.roles` 可覆盖 planner/worker/reviewer 的 provider/model/base-url。

**当前生效**：`enabled=false`，`roles={}`。开启时覆盖所有角色默认路由（优先级最高）。

---

## 8. 配置冲突点与建议

| # | 现状 | 风险 | 建议 |
|---|------|------|------|
| 1 | `plan_api` 三重职责：plan 生成默认 + worker 统一后端（worker_base_url）+ evaluator fallback | 改一处影响三处 | 文档标注；迁移时拆 `worker` 独立配置块 |
| 2 | `worker_backends` 与 `worker_base_url` 并存 | 记忆负担；改统一值不生效于已配置模型 | 文档已列优先级（按模型名 > 统一值） |
| 3 | `local_model_names` 空 + `local_models` 已填 | /status 探测失败时成本不清零 | 本地环境补 `local_model_names`（claude-haiku-4-5→真实名等） |
| 4 | evaluator 云端 + worker 本地 | 成本口径混合 | 符合"独立审查"意图，勿混计量 |
| 5 | 本地探测不带 `--model` | 误判云后端（成本不清零） | 改进探测：带 routed_model 或用 /status 直接读 |

---

## 9. 快速诊断命令

```bash
# 查看全部配置块（含默认值）
agent_go config

# 查看 router 角色路由
agent_go router show

# 查看某任务实际用的 worker 模型（metering 里 actual_model）
agent_go show <task-id>
```

---

## 10. 变更记录

- **2026-08-12**：首次编写。基于 claude -p 运行机制分析（`subtask.py:277-306` / `executor.py:2056-2189`）+ 用户 config 快照。当前为"Planner 云端 deepseek-v4-pro + Worker 本地 localhost:4000 + Evaluator 云端 deepseek-v4-pro"混合模式。
