# 生产模型配置指南（方案 B：planner=K3 + evaluator=GLM 混合）

> 状态：生产配置（当前生效，`~/.agent_go/config.json`）
> 日期：2026-08-15
> 实验依据：[m4-mixB-hard 基线](../../eval_suite/baselines/m4-mixB-hard/summary.json)（6/6 hard 任务 100% 通过）
> 关联：[model-entity-config-design.md](model-entity-config-design.md)、[config-schema.md](config-schema.md)

---

## 1. 最优配置方案（方案 B）

经过完整实验链验证（本地 0/6 → e2e 2/6 → v4-pro 3/6 → GLM 4-5/6 → K3 3/6 → **方案 B 6/6（100%）**），当前 hard 任务最优配置：

| 角色 | 模型 | 端点 | 选择理由 |
|------|------|------|---------|
| **planner** | **K3（kimi-for-coding）** | `https://api.kimi.com/coding/v1/messages` | coding 拆解强（plan 生成质量高、子任务拆解准确） |
| **evaluator** | **GLM（glm-5.3）** | `https://open.bigmodel.cn/api/anthropic/v1/messages` | JSON 评估稳定（thinking+text 双 block，规避 K3 评估纯 thinking 无 text 缺陷） |
| **worker easy/medium** | claude-haiku-4-5 / claude-sonnet-4-6 | `http://localhost:4000`（本地） | 本地执行省成本 |
| **worker hard** | claude-opus-4-7 | `http://localhost:4000`（代理→云端强模型） | hard 子任务经代理路由到云端强模型 |
| **goal** | force | — | e2e/hard 任务质量补偿（goal 循环验证提升通过率） |

**关键互补**：K3 planner（拆解强）+ GLM evaluator（评估稳）——单独用 K3 evaluator 会"只思考不回答"（纯 thinking 无 text，评估失败），单独用 GLM planner 不如 K3 拆解强。

## 2. 配置结构（config.json）

```json
{
  "router": {
    "enabled": true,
    "roles": {
      "planner":  {"provider": "anthropic", "base_url": "https://api.kimi.com/coding/v1/messages",
                    "model": "kimi-for-coding", "api_key": "<MOONSHOT_API_KEY>", "max_tokens": 8192},
      "evaluator": {"provider": "anthropic", "base_url": "https://open.bigmodel.cn/api/anthropic/v1/messages",
                     "model": "glm-5.3", "api_key": "<GLM_API_KEY>",
                     "thinking": true, "thinking_budget": 8192},
      "worker":   {"easy": {"model": "claude-haiku-4-5"},
                   "medium": {"model": "claude-sonnet-4-6"},
                   "hard": {"model": "claude-opus-4-7"}}
    }
  },
  "plan_api": {"worker_base_url": "http://localhost:4000"},
  "worker_backends": {"claude-haiku-4-5": "http://localhost:4000",
                       "claude-sonnet-4-6": "http://localhost:4000",
                       "claude-opus-4-7": "http://localhost:4000"},
  "local_models": ["claude-haiku-4-5", "claude-sonnet-4-6"],
  "goal": {"enabled": true, "policy": "force", "max_turns": 50, "timeout_seconds": 3600}
}
```

## 3. 配套机制（方案 B 依赖，均已落地）

| 机制 | 提交 | 作用 |
|------|------|------|
| **e2e 端到端模式** | e283184 | hard 任务不拆分保留全局上下文（difficulty=hard 自动触发） |
| **模型实体三层** | f8f9e3a/1a0394a/4e6e77c | models.json registry + router.roles 角色绑定 + 声明式 thinking |
| **验证命令白名单扩展** | 388d042 | `pip install -e .` 等组合命令支持（db-performance 类任务） |
| **R8 路由归因** | 32ca95e | metering 按真实后端标 route_target/is_local（force_fallback 回退正确归因） |

## 4. 使用说明

```bash
# hard 任务自动走方案 B（e2e 触发 + planner=K3 + evaluator=GLM + worker 混合路由）
agent_go run <repo> '<hard 任务描述>' --yes

# 或显式端到端
agent_go run <repo> '<任务>' --e2e --yes

# bench 验证
agent_go eval bench --tasks <dir> --candidate-models claude-sonnet-4-6 \
  --hard-model claude-opus-4-7 --source-batch <name>
```

## 5. 通过率证据（m4 系列基线）

| 配置 | 通过率 | 基线 |
|------|--------|------|
| 本地 35B 拆分 | 0/6 | m4-local-hard-goal |
| e2e 端到端（v4-flash） | 2/6（33%） | m4-e2e-hard-goal |
| e2e + v4-pro | 3/6（50%） | m4-e2e-hard-pro-v2 |
| GLM planner/evaluator | 4-5/6（67-83%） | m4-glm-hard / m4-glm-hard-rerun |
| K3 planner/evaluator | 3/6（50%） | m4-k3-hard |
| **方案 B（K3 planner + GLM evaluator + 白名单扩展）** | **6/6（100%）** | **m4-mixB-hard** |

## 6. 注意事项

- **K3 evaluator 不可单用**：评估任务"只思考不回答"（纯 thinking block 无 text），必须用 GLM 评估
- **GLM/K3 key 管理**：key 存于 config.json（生产）或 env（key_ref），GLM key 有有效期注意更换
- **代理依赖**：worker 混合路由依赖本地代理（manage.sh start，anthropic_proxy:4000）+ 云端 key（secret.local.conf）
- **成本**：planner/evaluator 云端（K3/GLM 按量计费），worker easy/medium 本地（$0），hard 云端（代理 route_cost 计费，metering R8 归因准确）

## 7. 模型池（可扩展）

当前 registry（`~/.agent_go/models.json`）：glm-5.3、deepseek-v4-pro、local-mlx、kimi-k3。接入新模型：

```bash
agent_go models add <id> --provider <anthropic|openai> --base-url <url> \
  [--thinking] [--json-loose] [--tco <$>] [--tags ...]
# 然后 router.roles 绑定角色即可，thinking/JSON 特性声明式自动适配（零代码）
```
