# 模型评测方法论（Model Evaluation Methodology）

> 日期：2026-08-17
> 阶段：M5.4 生态沉淀
> 数据基础：本会话 m4 系列 10+ 批次 bench 实测（本地 35B/K3/GLM/v4-pro/v4-flash × 6 hard/6 medium-easy 任务集 + 多次复跑）
> 关联：[model-selection-report.md](model-selection-report.md)（一份完整的选型报告实例）、[model-entity-config-design.md](model-entity-config-design.md)（三层配置）、`agent_go eval bench`、`agent_go models add`

---

## 1. 目的

评测新模型（或新模型组合）对 agent_go 执行能力的影响，为**模型选型**与**生产配置**提供可复现、可信的数据依据。本方法论文档由实战沉淀，包含了踩过的所有坑与规避方法。

## 2. 评测前置（准备）

### 2.1 注册模型（零代码）

```bash
agent_go models add <id> --provider <p> --base-url <url> [--key-ref ...] [--thinking] [--json-loose] [--tco 0.0005] [--tags plan_strong,coding_strong]
```

models.json 声明：endpoint / key_ref / thinking 特性 / JSON 遵从 / context / TCO / quality_tags。声明式 thinking/JSON 后 call_api 自动适配，**无需改代码**（实测 GLM/K3/v4-pro 接入均零代码）。

### 2.2 角色绑定

```json
// router.roles：指定该模型的角色（planner/evaluator/worker）
{"router": {"enabled": true, "roles": {
  "planner": {"model": "<new-model>"},
  "evaluator": {"model": "<评估用模型——建议固定最强 JSON 稳定者如 GLM>"}
}}}
```

### 2.3 环境检查（**务必先做**，否则数据作废）

| 检查项 | 方法 | 教训 |
|--------|------|------|
| 云端 key 有效 | 直连测 `POST /v1/messages` | ZAI_API_KEY 过期 → glm-5.3 401 回退 deepseek，数据不可信 |
| 代理后端与 cloud_model | `GET /api/status` + `GET /api/route/policies` | 本地后端被外部改（Qwen3.6→Qwen3.8-27B）→ worker 走错后端 |
| 模型路由可达 | `curl opus-4-7 → 代理` | opus-4-7 云端候选链（glm→deepseek 回退）需确认首选可用 |
| R8 归因在线 | 响应头 `X-Proxy-Route-*` | 无 R8 时 metering 归因不准（按 URL 假判 local） |

**规则**：环境有不确定 → 跑一个小任务冒烟（如 email-validator medium），确认 DELIVERY_READY + metering 归因正确，再跑全量。

## 3. 评测任务集（固定，跨模型可比）

| 任务集 | 组成 | 用途 |
|--------|------|------|
| **hard 6** | add-tag/security-hardening/race/stage-validation/conditional-branching/db-performance（跨 3 fixture） | 判定模型上限（功能系统级） |
| **medium-easy 6** | add-format-helper/math-helpers/add-limit-stage/implement-done-command/email-validator/add-count-stage | 判定日常负载性价比 |

任务 YAML 自带 `difficulty`（进 `min_difficulty` 触发 e2e）+ `verification`（任务级验收）。

## 4. 评测执行

### 4.1 命令

```bash
# 固定 worker（避免 worker 差异干扰 planner/evaluator 评测）
python3 -m agent_go eval bench --tasks /data/bench-hard-local \
  --candidate-models claude-sonnet-4-6 --hard-model claude-opus-4-7 \
  --repeat 1 --bench-parallel 1 \
  --source-batch <unique-batch-id> --output eval_suite/results_<batch>.jsonl
```

### 4.2 次数（**单次不可信**）

| 场景 | 复跑次数 | 依据 |
|------|---------|------|
| 首次/选型 | **3 次** | 模型随机性大：GLM 两次 5/6 vs 4/6、security-hardening 波动 67%、方案 B 单次 100% 实际 94.4% |
| 回归对比 | 2 次 | 确认不劣化 |
| 一次性探索 | 1 次 | 结果只算"探索级"，不进入选型依据 |

### 4.3 正确模式

```
planner/evaluator → 直连（Anthropic 兼容模型如 GLM/K3，JSON strict）
worker → 代理（opus-4-7 → 云端，困难任务）
e2e 模式（min_difficulty=hard 自动触发，不拆分保留全局上下文）
goal force（验证指引提升，本地模型尤甚：0.600→0.792）
```

## 5. 指标与归因

| 指标 | 来源 | 说明 |
|------|------|------|
| 通过率 | results `accepted_delivery` / `binary_pass` / `pass_rate_diagnostic` | 三者口径略异，报告需标注 |
| 成本 | metering `cost_usd`（R8 归因后准确） | 直连模型走 pricing 表；经代理走 R8 route_cost 或真实后端重算 |
| 延迟 | results `elapsed_sec` | 平均/中位数，标注任务数 |
| $/pass | $/pass_diagnostic | 性价比核心指标 |
| failure_class | results `failure_class` | 失败模式归因（verify_failure/timeout/infra） |

**R8 归因关键**：force_fallback 模型 ~36% 概率回退本地——按 URL 标 is_local 会把云端误判本地。必须消费 `X-Proxy-Route-Target/Actual-Model/Cost` 修正（call_api/executor 已实现）。

## 6. 可信度保障

1. **排除异常批次**：环境漂移/API 瞬时故障（如 R2 的 `API Error: Can't reach the API server`、网络低谷）跑出的数据剔除并注明
2. **失败归因**：每失败任务查 `failure_reason`——区分「模型能力」「evaluator 假阳性」「验证白名单限制」「网络/API 故障」
3. **evaluator 选型**：evaluate 用稳定 JSON 输出的模型（GLM），避免 K3（纯 thinking 无 text）或本地弱评估造成的假阳性/假阴性
4. **worktree 保留**：失败任务保留现场（--preserve-worktrees）供人工复核（语义评估与真实改动的差异——曾发现"评估说无代码但 commit 有 112 行"的假阳性）

## 7. 结果判定与选型

| 通过率区间 | 判定 |
|-----------|------|
| ≥90%（多任务复跑） | 生产可用 |
| 60-89%（波动） | 可行但需重试/降级链兜底；定位波动任务 |
| <60% 或单次 | 探索级，需 3 次确认；检查环境 |

配套产出：[模型选型报告](model-selection-report.md) 模板（通过率/成本/延迟×模型组合 + 场景推荐 + 演进链）。

## 8. 归档规范

```
eval_suite/baselines/<source-batch>/
  manifest.json   # 不可变批次清单（hash/schema/catalog）
  results.jsonl   # 原始结果（不可改）
  summary.json    # metric-freeze 汇总
baselines/README.md  # 批次索引（标注批次地位：基线/对比/探索）
```

批次分档：**基线**（决策依据，immutable）/ **对比**（候选）/ **探索**（环境异常，如 FB1-FB3 标注"数据不可信"）。

## 9. 本会话积累的关键教训（速查）

1. **同任务集是金字标准**：跨批次可比的前提是任务/worker/口径一致（本方法论的 hard 6 / medium-easy 6）
2. **单次 100% ≠ 稳定 100%**：方案 B 单跑 6/6，三连跑 17/18 = 94.4%——结论必须多次样本
3. **环境漂移是头号数据杀手**：key 过期/后端切换/代理配置变化都会静默污染数据——检环境再跑
4. **evaluator 决定"过没过"**：评估模型选错方向，通过率整个失真（K3 纯 thinking → security/conditional 假失败）
5. **拆解失败 ≠ 性能问题**：bench 卡住先看 failure_class（unverifiable_upstream 是 planner 拆解质量，不是并发）
6. **声明式配置消噪**：models.json 声明 thinking/JSON 后，模型接入与评测一致（零代码）
7. **worker 固定**：评测 planner/evaluator 时 worker 用同一固定配置，否则差异归因不清
8. **成本要双确认**：直连模型对 pricing 表、经代理对 R8 route_cost，两者口径统一才可比
