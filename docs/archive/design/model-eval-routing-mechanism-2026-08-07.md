# 模型评价与路由机制速查（current-state survey）

> 日期：2026-08-07
> 性质：**当前代码机制调研**（how it works），非设计稿。回答两个问题：① 现在如何对模型打分评价？② 运行时如何为任务选模型？
> 关联（互补）：
> - [model-evaluation-and-tiering.md](../../design/model-evaluation-and-tiering.md) — 定价/分级**策略设计稿**（定价表、SWE-bench 背景、该用哪档）
> - [router-multi-provider-extension.md](../../design/router-multi-provider-extension.md) — 角色路由多 provider 扩展设计
> - [bench-metric-validity-2026-08-06.md](bench-metric-validity-2026-08-06.md) / [bench-v2-data-requirements.md](bench-v2-data-requirements.md) — 度量口径与 bench 数据规范
> - [timeout-kill-strategy-2026-08-06.md](../../design/timeout-kill-strategy-2026-08-06.md) — kill_reason / 成本控制（影响评价口径）

## 核心结论（TL;DR）

agent_go 的"模型评价"和"模型选择"是**两套独立、靠人工衔接的机制**：

- **离线 bench 评价**（`bench.py` / `eval.py` / `cross_judge.py` / `evaluator.py`）：在 `eval_suite`（22 个 canonical 任务，按 `smoke/core/decision/stress` suite 运行）上跑候选模型 → 算诊断指标 → 出**推荐结论**（recommended / conditional / discouraged + 推荐角色）。
- **运行时路由**（`executor.py` / `router.py` / `subtask.py`）：planner 给每个子任务打 `difficulty` → `worker_models[difficulty]` 选模型 → `claude --model`。
- **衔接点**：bench 的 `recommended_roles` 告诉你"模型 X 可用于 worker_easy/medium/hard"，**人工据此填 `config.worker_models`**，运行时按难度自动选。代码不会自动把 bench 结论接进路由。

---

## 一、模型评价（离线 bench）——「这个模型行不行」

### 1.1 跑分入口与单任务记录

`eval_suite/`：**22 个 canonical 任务**（`01`–`20`），难度分布 **5 easy / 5 medium / 12 hard**。任务由 `task_catalog.json` 分为 `smoke/core/decision/stress`，支持 `agent_go eval bench --suite ...`。任务 YAML 字段：`id / difficulty / repo / initial_tag / task(自然语言) / verification(校验命令列表) / timeout / cost_control`。

每个 (task × candidate_model) 跑一次 → `_collect_result`（`bench.py:798-1007`）产出单任务记录，再由 `analyze_model_productivity`（`bench.py:1014-1120`）按模型聚合。

### 1.2 指标体系

| # | 指标 | 公式 / 含义 | 位置 |
|---|------|------------|------|
| 1 | `pass_rate` | completed / 总子任务数 | `bench.py:987` |
| 2 | `avg_corrected_pass_rate` | headline pass **或** cleanup_race（全子任务完成已验证、仅收尾被杀 → 计通过） | `bench.py:1049` |
| 3 | `dollar_per_pass` | `Σcost / Σpass_rate`（仅同 suite、同 source_batch 内诊断） | `bench.py` |
| 4 | `efficiency_score` | `avg_pass_rate / avg_cost`（每美元通过率） | `bench.py:1145` |
| 5 | `k8_zero_retry_pass_rate` | 通过记录中零重试占比（首次通过率） | `bench.py:1071` |
| 6 | `false_positive_rate` | 语义评估判通过、但 L2 判错的比例 | `bench.py:1123` → `assessment.py:175` |
| 7 | `code_regression_rate` | 通过但 `tests_broken>0` 的比例 | `bench.py:1085` |
| 8 | `avg_lint_errors` / `avg_tests_broken` | 代码质量维度均值 | `bench.py:1081-1084` |
| 9 | `kill_reason` | none / cleanup_race / stuck / hard_timeout / over_budget_l2/l3 / infra / interrupted | `bench.py:953` |
| 10 | `binary_pass` | `all_verify_ok AND semantic_pass is not False` | `bench.py:947` |
| 11 | `semantic_pass` | 子任务级语义评估裁决聚合（评估跳过/API 故障 → None，不计 False） | `bench.py:869` |

成本基线另有 `compute_cost_baseline`（`bench.py:1220`）：`mean / p90 / budget=p90×tolerance`，按 `难度 × 模型` 切片，并对 `timed_out` 记录做**右删失**剔除。

产品主指标已统一为 `Cost per Accepted Delivery = valid_cost / accepted_delivery_count`。`dollar_per_pass` 和旧 Q1-Q11 只用于历史/诊断分析，不作为当前产品 KPI。

### 1.3 推荐结论（`_recommend`，`bench.py:1164-1180`）

**纯通过率门控**（60/70/75/80 四档，对齐 PRD §3.7）。⚠ 注意：`avg_cost` 形参收了但**未使用**——成本维度只在外面的 `efficiency_score`/`$/pass` 列体现，不进推荐判定。

| 通过率 | 档位 | 图标 | `recommended_roles` |
|--------|------|------|---------------------|
| < 60% | `discouraged`（"省钱产出垃圾"） | ✗ | `[]` |
| 60–70% | `conditional`（仅 easy） | ⚠ | `["worker_easy"]` |
| 70–75% | `conditional`（easy+medium） | ⚠ | `["worker_easy","worker_medium"]` |
| 75–80% | `conditional`（全角色） | ⚠ | 全角色 |
| ≥ 80% | `recommended`（全角色可用） | ★ | 全角色 |
| 样本 < 3 | `insufficient_data` | ? | `[]` |

由 `cmd_models`（`bench.py:1183-1214`）打印成对照表。无"支配(dominated)"代码标签——"Opus 被支配"只出现在度量诊断文档的分析里，不是代码赋值。

### 1.4 eval 门禁（`eval.py`，`cmd_eval` `eval.py:896`）

两种互斥模式：
- **绝对阈值**（`gate_cost` `eval.py:522`）：默认基线 **$0.05/pass**；`actual > baseline` → FAIL。无完成子任务时 `passed=True`（门禁不生效，避免绿地仓库卡 CI）。
- **回归**（`gate_cost_regression` `eval.py:613`）：对比 `.agent_go/cost_baseline.json`，容差 `_REGRESSION_TOLERANCE=0.10`；超 10% → FAIL。基线文件存在但读不出时 **FAIL（拒绝静默重置）**；`--update-baseline` 强制重置。
- FAIL → `sys.exit(1)`，CI 变红。

### 1.5 语义评估 + 交叉评判（防自评偏置）

- **`evaluator.py`**（`_default_semantic_eval` `evaluator.py:230-309`）：不只看 verify 命令过没过，让 LLM 看 `git diff`（截 4000 字符）做代码审查，输出 `{passed, confidence(0-1), reason, suggestions}`，并**显式检查"验证通过是否假阳性"**。关键防过判（`evaluator.py:214`）：有确定性测试且通过 → 默认通过；`confidence≤0.5` → `passed=true`（不确定按通过）。结果落 `assessment.jsonl`。
- **`cross_judge.py`**（`cross_judge_results` `cross_judge.py:94`）：交叉评判矩阵——用**别的 provider** 的模型给候选模型打分；**硬约束**：`_same_provider` 命中则跳过（`cross_judge.py:150`），矩阵对角线留空防 LLM-as-Judge 自偏。`calibrate_judge`（`cross_judge.py:240`）支持对人工 CSV 校准（avg 分歧 ≤1.0=可靠 / ≤1.5=边际 / >1.5=不可靠）。P1 现状：四维评分退化为从 reason 文本启发式抽 `semantic_score`（`_heuristic_score` `cross_judge.py:323`）。

---

## 二、模型选择（运行时路由）——「这个任务用哪个模型」

### 2.1 主路径：难度路由（默认开启）

完整链路（核心 `executor.py:1349-1375`）：

```
planner LLM 给每个 step 打 difficulty(easy/medium/hard)      ← LLM 判定，非计算
  → config["worker_models"][difficulty] 取模型名
  → env["AGENT_GO_CLAUDE_MODEL"]
  → claude -p --model <name>                                  (subtask.py:291)
```

- `difficulty` 是 planner 输出 schema 的**必填字段**（`api.py:226`），判据：`easy=单文件小改 / medium=单特性 / hard=跨文件架构`（`api.py:247`）；Spec 注入会提示"高风险步骤标 hard"（`api.py:350`）。
- `worker_models` 默认全空（`config.py:103-107`）= 回退 claude CLI 自身默认模型。

### 2.2 三个调节阀

| 机制 | 何时触发 | 代码 |
|------|---------|------|
| **`worker_models_fallback`**（重试升级） | verify 失败重试时换更强模型 | `executor.py:1012-1020` |
| **`worker_models_degrades`**（预算降级） | 任务超预算 + `budget_mode=degrade` → 剩余子任务降档（hard→medium→easy） | 触发 `pipeline.py:355-366`；执行 `executor.py:1355-1371`；安全阀（连续 3 失败回退 stop）`pipeline.py:296-303` |
| **`worker_backends`** | 模型名 → 对应 API base URL（per-model `ANTHROPIC_BASE_URL`，优先级高于全局 `worker_base_url`）；localhost → 判本地模型、成本归零 | `executor.py:1376-1407` |

重试预算本身也按难度缩放：max_retries `easy=2/medium=3/hard=5`（`executor.py:663`），retry_timeout 倍数 `easy=1/medium=1.5/hard=2.5`（`executor.py:1057`）。

### 2.3 可选路径：角色路由（`router.enabled=true` 才生效）

按 **planner/worker/reviewer 角色**（由 agent_type 映射：developer/tester→worker、architect→planner、reviewer→reviewer）选 provider，带熔断器（连续 5 次可用性失败 → 熔断 60s）+ fallback（`router.py`）。默认关闭（`config.py:127-141`）。planner 铁律：不允许配 fallback（`router.py:321`）。三个预设（国际/国产/混合）见 `config.example.json`。

### 2.4 容易误解的边界

- **Agent 类型**（developer/architect/reviewer/tester，`agents.py:30`）只影响 **tools/权限**，不直接选模型；仅在角色路由开启时间接经角色影响。
- **任务类型（安全/重构/bugfix）→ 模型** 的映射**不存在**。关键词/文件模式只决定 **skills 和 agent_type**（`role_skill_map.py`），碰不到 `worker_models`。
- **难度全靠 planner 主观**：无计算式校验；唯一事后检查是欠分解告警（hard 但子任务数 <3，`planning.py:check_under_decomposition`），且**只告警不强制**。

---

## 三、两套机制如何衔接

```
[离线 bench]  跑 eval_suite → 指标 + _recommend(推荐角色)
                                  │
                     人工据此填 config:
                       worker_models.hard   ← frontier 档 / bench≥80% 的模型
                       worker_models.medium ← value 档
                       worker_models.easy   ← lite 档
                                  │
[运行时]  planner 打 difficulty → worker_models[difficulty] → claude --model
```

即："哪个模型更适合哪类任务" 的答案来源是 **bench 的 `recommended_roles`**。bench 直接告诉你"模型 X 通过率 82% → recommended → 全角色可用"或"65% → 仅 worker_easy"；你据此把模型塞进 `worker_models` 对应难度档，运行时按难度自动选。**代码层没有把 bench 结论自动接进路由**——是两个闭环靠人工配置衔接。

---

## 四、MODEL_TIER（`pricing.py:98-127`）

手工标注的三档（共 43 模型）：

| 档 | 定位 | 代表模型 |
|----|------|---------|
| `frontier`（9） | 顶级旗舰，最高风险/回报 | claude-fable-5 / opus-4-8/4-7、gpt-5.7/5、gemini-3.1-pro、qwen-max、glm-5.2/5.1 |
| `value`（19） | 主力性价比 | claude-sonnet-5/4-6/4、gpt-4.1/4o、gemini-2.5-pro、deepseek-chat/v3.2/v4、qwen3-max、kimi-k2/k2.5、glm-5/4.6/4.7 |
| `lite`（15） | 高频低成本、延迟敏感 | claude-haiku-4-5、gpt-5-mini/nano、gpt-4.1-mini/nano、gemini-2.5-flash、doubao-lite、glm-4.7-air |

⚠ **`MODEL_TIER` 是 advisory 元数据，路由代码从不读它**——只给人填 `worker_models` 时参考。`MODEL_PRICES`（`pricing.py:19`，50+ 条）才是被 `resolve_price` 实际消费的计价表。

---

## 五、现状缺口（值得改进）

1. **推荐纯看通过率，不看成本**：`_recommend` 收了 `avg_cost` 但没用。贵 5 倍、通过率相同的模型会和便宜的拿到一样"recommended"——要选"性价比最优"得人眼看 `$/pass`/`efficiency` 列。
2. **`MODEL_TIER` 没接进路由**：填错 `worker_models`（如把 lite 放 hard）代码不拦。
3. **任务类型不路由模型**：安全任务和重构任务难度相同就用同一模型。若要"安全审查用 Opus、普通改写用 Haiku"，目前只能 Spec 手动标 difficulty 或开角色路由。
4. **难度全靠 planner 主观**：标错 difficulty → 直接用错档模型（仅"欠分解"有告警）。
5. **两闭环无自动衔接**：bench 结论不会自动写回 `worker_models`，全靠人。
6. **实现瑕疵**：`cmd_bench` 在 `__all__` 声明（`bench.py:33`）但全代码无定义，`agent_go eval bench` 会 ImportError；实际可用入口是 `cmd_baseline` / `cmd_models` / `cmd_cost_baseline`。

---

## 附：关键文件速查

| 文件 | 作用 |
|------|------|
| `bench.py` | 跑分编排、`_collect_result`、`analyze_model_productivity`、`_recommend`、`cmd_models`、`compute_cost_baseline` |
| `eval.py` | eval 门禁（`gate_cost` / `gate_cost_regression`）、`analyze_cost`、`analyze_quality`（Q1–Q11） |
| `evaluator.py` | 语义评估（LLM 审 diff + 假阳性检查 + confidence） |
| `cross_judge.py` | 交叉评判矩阵（防自偏）+ 人工校准 |
| `pricing.py` | `MODEL_PRICES`（计价）、`MODEL_TIER`（advisory 分级）、`resolve_price` |
| `executor.py` | 难度路由（`run_subtask` 1349-1375）、fallback/重试、degrade、worker_backends |
| `router.py` | 角色路由（opt-in）、熔断器 + fallback |
| `subtask.py` | `AGENT_GO_CLAUDE_MODEL` → `--model`（291） |
| `pipeline.py` | degrade 触发（`_degraded` flag）+ 安全阀 |
| `planning.py` | 欠分解告警（warning-only） |
| `role_skill_map.py` | 任务关键词/文件 → skills/agent_type（不碰模型） |
| `config.py` | `DEFAULT_CONFIG`：`worker_models` / `_fallback` / `_degrades` / `router` / `planner_api` / `cost_control` |
| `eval_suite/` | 22 个任务 YAML + fixtures + 历史 results_*.jsonl |
