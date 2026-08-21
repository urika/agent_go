# 模型评价与路由机制缺口修复设计

> 日期：2026-08-08
> 状态：**设计稿**（待评审 → 分阶段实施）
> 性质：对 [model-eval-routing-mechanism-2026-08-07.md](model-eval-routing-mechanism-2026-08-07.md) 第五节「现状缺口」6 条的可执行修复设计
> 关联：[model-evaluation-and-tiering.md](../../design/model-evaluation-and-tiering.md)（分级策略）、[router-multi-provider-extension.md](../../design/router-multi-provider-extension.md)（角色路由）
> 目标对齐：PRD「预算限制下高通过率、高效率」+ 原则 #5「复杂度判断在规划阶段收敛」

## 代办清单与排期

| # | 缺口 | 阶段 | 风险 | 依赖 | 任务 |
|---|------|------|------|------|------|
| G6 | `cmd_bench` ImportError | Phase 0 | trivial | — | #1 |
| G1 | `_recommend` 不看成本 | Phase 0 | small | — | #2 |
| G2 | `MODEL_TIER` 未接进路由 | Phase 1 | small-med | — | #3 |
| G4 | 难度全靠 planner 主观 | Phase 1 | medium | — | #4 |
| G5 | bench→worker_models 无自动衔接 | Phase 2 | large | **G1, G2** | #5 |
| G3 | 任务类型不路由模型 | Phase 2 | large | — | #6 |

**排序逻辑**：G6/G1 是"既有功能坏了/失真"，先修；G2/G4 是"加护栏不改变现有路径"，中段；G5/G3 是"新增能力、有架构选择"，最后且需评审。**G5 依赖 G1+G2**——自动衔接的前提是推荐本身可信（成本感知 + tier 校验）。

---

## Phase 0 — 修坏件 + 修失真（trivial/small，低风险）

### G6. 修复 `cmd_bench` ImportError

**问题**：`eval.py:878-879` `from .bench import cmd_bench; cmd_bench(args)`，但 `bench.py` 全代码无 `def cmd_bench`（仅在 `__all__:33` 声明）。`agent_go eval bench` → `ImportError`。bench.py 文档（`bench.py:6-8`）描述的入口 `agent_go eval bench --tasks ... --candidate-models ... --repeat ... --output ...` 形同虚设。

**现状**：实际能跑的编排是 `cmd_baseline`（`bench.py:398-461`）+ 单任务执行 `_run_one_task`（`bench.py:464-587`，subprocess 隔离跑 `python -m agent_go run`）。缺的只是把 `_run_one_task` 在 `tasks × candidate_models × repeat` 上展开 + 聚合的编排层。

**方案**：在 `bench.py` 定义 `cmd_bench(args)`：

```python
def cmd_bench(args) -> None:
    """eval bench 编排器：tasks × candidate_models × repeat → results.jsonl → 报告。"""
    tasks_dir = Path(getattr(args, "tasks_dir", "eval_suite/tasks"))
    models = [m.strip() for m in getattr(args, "candidate_models","").split(",") if m.strip()]
    repeat = int(getattr(args, "repeat", 3))
    out = Path(getattr(args, "output", "eval_suite/results.jsonl"))
    source_batch = getattr(args, "batch", "bench")
    repo_root = Path(getattr(args, "repo", "."))
    # 1. 载入 task YAML 列表（复用 _load_tasks）
    tasks = _load_tasks(tasks_dir)
    # 2. 笛卡尔积 × repeat，逐个 subprocess 隔离跑（复用 _run_one_task）
    with open(out, "a", encoding="utf-8") as f:
        for task in tasks:
            for model in models:
                for r in range(repeat):
                    rec = _run_one_task(task, repo_root, model, task["id"],
                                        source_batch=source_batch, ...)
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
    # 3. 出报告
    data = analyze_model_productivity(out)
    cmd_models(data)
```

- 复用 `_run_one_task`（已含 subprocess 隔离 + `_collect_result`），**不重写执行逻辑**。
- args 字段已在 `cli.py:224-236` 注册（`--tasks-dir`/`--candidate-models`/`--repeat`/`--output`/`--batch`）——只需接通。
- 备选（更省事）：若判定 `cmd_bench` 与 `cmd_baseline` 职责重叠，让 `eval bench` 在 `eval.py:878` 直接委派 `cmd_baseline(args)` 并删 `__all__` 里的 `cmd_bench`。**倾向定义 `cmd_bench`**（baseline 是"采基线"、bench 是"跨模型对照"，语义不同）。

**测试**：mock `_run_one_task`，断言 `cmd_bench` 对 N tasks × M models × R repeat 恰好调用 N×M×R 次、结果按行写入、末尾调 `cmd_models`。

---

### G1. `_recommend` 接入成本维度

**问题**：`bench.py:1164` `_recommend(model, pass_rate, avg_cost, n)` 形参 `avg_cost` 收了不用——纯通过率门控（60/70/75/80）。贵 5×、通过率相同的模型与便宜的拿到**相同** `recommended`，成本只在外面的 `$/pass` 列体现，推荐结论本身误导"性价比"判断。

**方案**：保留通过率门控（不改既有四档），叠加成本维度——

1. **成本降档**：在 `analyze_model_productivity` 里算全部受评模型的 `dollar_per_pass` 中位数 `_dpp_median`。`_recommend` 增加 `dollar_per_pass` 入参，当 `dollar_per_pass > 2 × _dpp_median` 时推荐**降一档**（recommended→conditional）并在 reason 附 _"成本过高（$/pass {x} > 2× 中位数 {m}）"_。

2. **best_value 徽章**：在 ≥70% 通过的模型中，`efficiency_score`（passes/$，`bench.py:1145`）最高者打 `best_value=True`，`cmd_models` 表里用 `💰` 标注。独立于推荐档，回答"性价比最优选哪个"。

```python
def _recommend(model, pass_rate, avg_cost, n, dollar_per_pass=None, dpp_median=None):
    ...  # 既有四档逻辑得 (cat, roles, reason)
    if dpp_median and dollar_per_pass and dollar_per_pass > 2 * dpp_median:
        cat = _downgrade_once(cat)          # recommended→conditional
        reason += f"；成本过高（$/pass ${dollar_per_pass:.4f} > 2× 中位数 ${dpp_median:.4f}）"
    return cat, roles, reason
```

- 备选（未采纳）：复合分数 `score = pass_rate - λ·cost`——会把"省钱但通过率 60%"误排到前面，违背 PRD 反指标（<60% 即垃圾）。**不改门控、只加降档**更稳。
- 触点：`bench.py:1091`（调用点传 `dollar_per_pass`/`dpp_median`）、`1164-1180`（函数体）、`1183-1214`（`cmd_models` 加 `💰` 列）。

**测试**：构造同通过率、不同 $/pass 的两模型，断言贵者降档；断言 best_value 落在 efficiency 最高者。

---

## Phase 1 — 加护栏（不改变现有执行路径，中低风险）

### G2. `MODEL_TIER` 接进路由校验

**问题**：`pricing.py:98` `MODEL_TIER`（frontier/value/lite，43 模型）**零消费者**，填错 `worker_models`（如 lite 模型放 hard 槽）代码不拦，运行时直接用错档模型却不报警。

**方案**：advisory 校验（告警不阻断）——

1. `pricing.py` 加 `_MODEL_TO_TIER` 反查表（模型名→tier）+ `model_tier(name) -> str|None`（复用 `resolve_price` 的后缀剥离逻辑，`pricing.py:164`）。

2. `config.py`（或新 `validation.py`）加 `_validate_worker_tier(config) -> list[(slot, model, tier, msg)]`：
   - `worker_models.hard` ∈ lite → _"hard 槽用 lite 模型 {m}，能力恐不足"_
   - `worker_models.easy` ∈ frontier → _"easy 槽用 frontier 模型 {m}，过贵"_
   - 未分级模型（不在 MODEL_TIER）→ 不报（自定义/本地模型合法）。

3. 在 `cmd_run`（`cli.py`）启动时调一次，logger.warning 输出；`cmd_models`（`bench.py:1183`）表里给每个模型补 tier 列。

- 备选（未采纳）：硬阻断（启动失败）——会挡住合法的本地/自定义模型，且用户可能有意为之；advisory 更尊重配置所有权。
- 触点：`pricing.py`（反查表 + helper）、`cli.py:cmd_run`（启动校验）、`bench.py:cmd_models`（展示）。

**测试**：构造 worker_models.hard=claude-haiku-4-5（lite），断言校验返回 hard 槽告警；easy 槽放 frontier 同理；未知模型不报。

---

### G4. 难度计算校验（planner 主观难度交叉核对）

**问题**：`difficulty` 全靠 planner 主观赋值（`api.py:226` 必填字段，判据 `easy=单文件/medium=单特性/hard=跨文件架构`，`api.py:247`）。标错（如把跨文件重构标成 easy）→ 直接用 easy 档模型 → 能力不足失败。唯一事后检查是欠分解告警（`planning.py:check_under_decomposition`，只看 hard+子任务数）。

**方案**：与 G5 欠分解检测**同模块、同风格**（warning-only，不覆盖 LLM）——加 `_difficulty_hint(subtask) -> str` 从子任务信号算一档参考难度，与 planner 标的不一致时告警。

V1 启发式信号（纯子任务元数据，无需跑）：
- **描述关键词**：含"重构/架构/跨模块/refactor/architecture/migrate" → 倾向 hard；"添加 helper/单点/格式化" → 倾向 easy。
- **预估文件数**：从 `agent_prompt` 提及的路径/模块数估算（多文件 → 升档）。
- **预期产物**：含"测试套件/新模块/配置体系" → 升档。

```python
def check_difficulty_mismatch(subtasks, logger=None) -> int:
    """G4：planner 标的 difficulty 与计算 hint 不一致时告警（不覆盖）。"""
    for st in subtasks:
        hinted = _difficulty_hint(st)
        if hinted and hinted != st.get("difficulty") and _tier_distance(hinted, st["difficulty"]) >= 2:
            logger.warning(f"[G4] {st['id']} difficulty={st['difficulty']} 但信号倾向 {hinted}（可能用错档模型）")
```

- `_tier_distance("easy","hard")=2`，仅"跨两档"才报（easy↔medium 噪声大，不报）。
- V2（后续）：从 `verify_state.json` 历史 retry 率数据驱动——_"difficulty=hard 且子任务数≤N 的 retry 率是否显著高"_（与欠分解 V2 同数据源）。
- 触点：`planning.py`（新增 `_difficulty_hint` + `check_difficulty_mismatch`，紧挨 `check_under_decomposition`）、`cli.py:cmd_run`（Plan 后调用，与 G5 同处）。

**测试**：构造 planner 标 easy、描述含"跨模块重构"的子任务，断言告警；easy↔medium 不报。

---

## Phase 2 — 新增能力（large，有架构选择，需评审后实施）

### G5. bench → worker_models 自动衔接

**问题**：bench 推荐结论（`recommended_roles`）不自动写回 config，全靠人工把模型填进 `worker_models{easy,medium,hard}`。两闭环脱节，且人工填易错（G2 的 tier 错配多源于此）。

**前置**：依赖 G1（推荐成本感知）+ G2（tier 校验）先落地，否则自动建议不可信。

**方案**：新增 `agent_go eval recommend` 命令，**两步、永不静默自动改**——

```
agent_go eval recommend                    # 读最新 results.jsonl，打印建议表（dry-run 默认）
agent_go eval recommend --apply            # 写入 ~/.agent_go/config.json 的 worker_models
agent_go eval recommend --results FILE     # 指定数据源
```

逻辑：
1. `analyze_model_productivity(results)` + G1 增强 `_recommend` → 每模型得 (档位, 推荐角色集, best_value)。
2. **槽位分配规则**（确定性、可审计）：
   - `worker_models.hard` ← 候选中 `recommended` 且通过率最高者；同等取 best_value。
   - `worker_models.medium` ← `recommended` 或 `conditional(全角色)` 中 $/pass 最低者。
   - `worker_models.easy` ← `conditional` 或 ≥70% 中 $/pass 最低者（easy 槽优先省钱）。
   - 无合格候选 → 该槽留空（不退而求其次塞弱模型）+ reason 标注。
3. **dry-run** 打印 `slot → model (tier, pass%, $/pass, 理由)` 表；**--apply** 原子写 config（tmp+rename，复用 config 既有原子写），并在写入前跑 G2 校验，tier 错配时**拒绝写入并提示**。

- 备选（未采纳）：运行时自动从最新 bench 结果选模型——破坏"配置即合约"心智、引入非确定性、resume 时模型可能变。**坚持显式 --apply**。
- 触点：`eval.py`（新增 `cmd_recommend`）、`cli.py`（注册 `eval recommend` 子命令 + `--apply`/`--results`）、复用 `analyze_model_productivity`/`_recommend`/G2 校验器。

**测试**：mock analyze 结果，断言槽位分配规则；--apply 前置 G2 校验，tier 错配时拒绝写；dry-run 不碰 config。

---

### G3. 任务类型 → 模型路由

**问题**：任务类型（安全/重构/bugfix）不影响模型选择——关键词只决定 `skills`/`agent_type`（`role_skill_map.py:14-43`），难度相同就用同一模型。无法表达"安全审查用 Opus、普通改写用 Haiku"。

**方案**：新增一个路由维度，**与难度路由正交、优先级明确**——

1. **task_type 检测**：复用 `role_skill_map` 关键词基础设施，规则扩展可选字段 `task_type`：

   ```python
   {"match": {"keywords": ["安全","security","auth","加密"]},
    "task_type": "security", "skills": {"required": ["security-review"]}}
   ```

   `apply_rules` 返回值增加 `task_type`（None 表示无类型偏好）。

2. **config 新增** `worker_models_by_type: {security: "", refactor: ""}`（空 = 不覆盖，回退难度路由）。

3. **优先级**（在 `executor.py:1349-1375` 路由块）：

   ```
   Spec/子任务显式 model 字段  >  worker_models_by_type[task_type]  >  worker_models[difficulty]
   ```

   即：planner 先打 difficulty → 查 task_type，若 `worker_models_by_type[type]` 非空则**覆盖**难度模型；degrade/fallback 仍在其后生效。

4. metering 记 `task_type`（与 difficulty 同列），供 bench 按 类型×模型 切片评价。

- 备选（未采纳）：把 task_type 塞进 difficulty（如 security 一律 hard）——扭曲难度语义、丢失"简单安全检查"的便宜路由。**正交维度更干净**。
- 触点：`role_skill_map.py`（规则 + `apply_rules` 返回 task_type）、`config.py`（DEFAULT_CONFIG 加 `worker_models_by_type`）、`executor.py:run_subtask`（路由块加 task_type 分支）、`subtask.py`/metering（记 task_type）、`bench.py`（按 task_type 切片）。
- 风险：检测误判（关键词命中但非该类型）→ 用错模型。缓解：仅当 `worker_models_by_type[type]` 显式配置时才生效（不配则无影响）；Spec 可显式指定/覆盖。

**测试**：task_type=security 且配置了 worker_models_by_type.security → 路由到该模型而非难度模型；未配置 → 回退难度路由；Spec 显式 model > task_type > difficulty 优先级链。

---

## 实施顺序建议

1. **Phase 0（本次可一起做）**：G6 + G1。trivial+small，纯 bench.py，无运行时影响，立即可用。
2. **Phase 1**：G2 + G4。advisory 护栏，不改执行路径，与既有 G5 欠分解检测同风格。
3. **Phase 2（逐项评审）**：G5（先，因解锁自动化）→ G3（后，最大改动）。每项单独 PR，G3 需先敲定 task_type 词表与优先级。

## 验证口径

- 所有改动补单测（触点已标），跑 `pytest tests/` 全绿。
- bench 侧改动（G1/G5）用 `eval_suite/results_v3.jsonl` 历史数据离线重算验证，不重跑。
- 路由侧改动（G2/G3/G4）在 dry-run/告警层验证，不改既有 run 路径的成功用例。
