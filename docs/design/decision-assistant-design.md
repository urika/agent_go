# 决策辅助设计（Decision Assistant）—— 初步设计与 Roadmap

> 状态：初步设计（v0.1）
> 日期：2026-08-17
> 输入依据：产品评估结论（M6.1 目标驱动 LLM 分析器）+ [harness-driving-architecture.md](harness-driving-architecture.md) 讨论文档
> 定位：**证据驱动的策略建议层**（不升级为自动决策执行层）

---

## 1. 设计原则（三边界）

| 边界 | 含义 | 落地 |
|------|------|------|
| 1. **目标由人定义** | LLM 不改目标，避免目标漂移 | 分析 Goal 与执行 Goal 字段区分（`--analysis-goal` / `--execution-goal`） |
| 2. **建议不直接执行** | 输出建议需 agent/人确认 | 现有 `router recommend --apply` 作为人工确认后入口；建议结构化输出不落配置 |
| 3. **证据强制绑定** | LLM 只能基于真实数据推理 | 强制 `--results`（manifest 校验），无证据拒答；输出必须带 `evidence_refs` |

## 2. 功能架构

### 2.1 核心能力：`agent_go eval insight`（M6.1）

```
输入：
  --results <batch>    必选：baselines/<batch>/（manifest 校验，immutable 证据）
  --goal <analysis-goal>   人类可读的分析目标（如"hard 通过率≥95% 且 $/pass≤$0.1"）
  --plan <预设计划>     行动候选（可省略，默认从已知策略族：换模型/降级链/难道路由/e2e/白名单）
  --output <path>      输出（md 报告 / json 结构化，stdout 默认）

处理（pipeline）：
  1. 证据物化：失败模式聚合（failure_class × 模型 × 任务）＋ 成本/延迟/归因 + 环境快照
  2. LLM 推理（绑定证据上下文 + 目标 + 计划候选，约束输出 schema）
  3. 输出结构化建议列表

输出（每条建议）：
  {
    "problem":          "问题描述",
    "evidence_refs":    ["task-x/failure_reason", "baseline-y/pass_rate", ...],   // 证据引用
    "cause_hypothesis": "根因假设",
    "action":           "建议动作（行动候选之一）",
    "expected_impact":  "预期影响（量化目标方向）",
    "cost_risk":        "成本/风险",
    "confidence":       0-1,
    "requires_approval": true,   // 需要人类确认后走 --apply
  }
```

### 2.2 决策流（功能视角）

```
人/agent 设目标 + 收集批次证据
   ↓ --results（强制）
评估证据物化（失败模式/成本/环境快照）
   ↓
LLM 推理（受约束：目标不漂移 + 证据绑定 + 建议 schema）
   ↓
结构化建议（problem→evidence→action→impact→confidence）
   ↓ 人/agent 审阅确认（requires_approval=true 项）
执行确认后应用（复用 router recommend --apply 入口）
   ↓
decision log（记录为何改/基于何证据/期望影响）→ 复跑验证
```

### 2.3 辅助能力

| 能力 | 说明 | 里程碑 |
|------|------|--------|
| **decision log** | 统一决策记录（change/evidence/goal/expected/actual），可复盘可审计 | M6.2 |
| **Web 展示** | 洞察报告 + decision log 可视化（配置中心/运维页） | M6.3 |
| **recommend 接入 LLM** | 现有规则 recommend 升级为「规则初筛 + LLM 精排」（--apply 仍人工确认） | M6.4 |
| **确认后自动应用** | `insight --apply-<action-id>` 走 config 修改 + 备份 + 审计（仍非全自动） | M6.5 |
| 全自动决策 | 目标由机器设、建议自动执行 | **暂不做**（高漂移/成本/自评风险） |

## 3. 技术架构

### 3.1 模块归属与数据流

```
agent_go/eval.py（扩展 insight 子命令）
   │ 物化证据
   ▼
eval_suite/baselines/<batch>/（manifest+results+summary，immutable 数据源）
   │ LLM 推理（复用 call_api/声明式 thinking/降级链）
   ▼
决策建议（json/md）──► decision log（~/.agent_go/decision_log.jsonl）
   │                                 │
   ▼                                 ▼
router recommend --apply（人工确认入口）  复盘/审计（web/CLI）
```

### 3.2 关键复用（不新建重复模块）

| 现有模块 | 复用点 |
|---------|--------|
| `eval.py` quality/perf/cost | `--results` 解析、指标聚合、`_read_jsonl` |
| `basis/基失败聚合`（evaluate failure_class counts） | 失败模式物化 |
| `call_api` + registry + 降级链 | LLM 推理调用（声明式 thinking/JSON、多级 fallback） |
| `router.recommend` + `apply_recommendation` | 建议应用入口（人工确认） |
| `problems.py`（跨任务失败记忆） | 注入历史失败模式作为分析上下文 |
| `metric_report.py`（Metric Freeze） | 复用报告渲染与校验基建 |

### 3.3 证据物化层（新增）：`evidence.py`

```
def materialize_evidence(batch_path) -> dict:
    # 1. manifest 校验（immutable：schema/source_batch/task_catalog hash）
    # 2. results.jsonl → 失败模式聚合（failure_class × model × task）
    # 3. summary.json → 通过率/$/pass/延迟/成本
    # 4. metering 归因（R8：actual_model/route_target/is_local）
    # 5. 环境快照（config 模型池/代理配置摘要/关键 env）
    # 6. problems.py 历史失败模式（跨任务）
    # 输出：结构化证据包（自身含 hash，供 LLM 与审计校验）
```

### 3.4 LLM 推理约束（防越界的关键工程）

- **schema 输出**：声明式 response_format（registry JSON compliance）＋ prompt 强约束
- **证据引用必须存在**：`evidence_refs` 里的路径需在证据包内（后校验，缺引用拒收该条建议）
- **目标不变式**：LLM 输出不得修改 goal；目标字段只读注入
- **降级可用**：LLM 不可用时返回「基于规则的初步建议 + 标记未达置信」，不全故障

## 4. Roadmap

| 里程碑 | 内容 | 依赖 | 退出标准 |
|--------|------|------|---------|
| **M6.1** 证据物化 + `eval insight` MVP | evidence.py（失败模式/成本/环境快照）+ insight 命令（--results/--goal/--plan/--output，结构化建议输出） | eval.py/evidence.py | 真实批次产出建议：证据引用率 100%、结构化 schema 校验通过 |
| **M6.2** decision log | 统一决策记录（append jsonl：ts/change/evidence_refs/goal/expected/actual/confirmer）+ CLI 查看 | M6.1 | 每次 --apply/改配置自动落 log；可复盘 |
| **M6.3** Web 展示 | 配置中心/运维页：洞察报告渲染 + decision log 列表（复用 api_audit 模式） | M6.1/6.2 | 浏览器可看建议与历史决策 |
| **M6.4** recommend 接入 LLM | 规则初筛 + LLM 精排；`router recommend --results X` 关联证据 | M6.1 | 建议采纳 ≥60%（对照人工决策基准） |
| **M6.5** 确认后自动应用 | `insight --apply-<action-id>`：改配置 + 备份 + 审计 + 复跑指引（仍人工确认触发） | M6.2 | 无证据不入 --apply（强制校验）；应用可回滚 |
| **M6.6** 全自动决策 | — | — | **暂不**（目标由人定义是产品红线） |

## 5. 成功指标（M6.1-M6.5 验收）

- 证据引用率 100%（每条建议都有 evidence_refs，后校验拦截）
- 建议采纳 ≥60%（对照人工决策基准）
- 分析时间降 ≥70%（人工找数据→机器给证据包）
- 无证据不入 `--apply`（强制校验，防 LLM 提出无据动作）
- 重复分析一致率 ≥90%（同批次同目标 → 建议稳定，防 LLM 随机漂移）

## 6. 风险与对策

| 风险 | 对策 |
|------|------|
| LLM 建议循环（反复改配置震荡） | 建议不自动执行 + decision log 记录"上轮改了啥/结果如何"注入下轮上下文 |
| 证据环境漂移（ZAI key/后端被改如 FB1） | 环境快照进证据包；insight 校验环境一致性（对照 models.json/代理状态） |
| 自我评估偏差（LLM 评自己产出） | insight 不评估任务成败（那由 evaluator 做）；只做跨任务规律分析 |
| 成本失控（LLM 分析消耗） | 分析用 cheap 模型（glm-4.5-air/haiku 级）；单次分析 token 预算上限 |

## 7. 讨论题（待收敛）

1. 消费方：insight 输出供 human 看报告 / agent 读 JSON 接决策——两者都要？优先级？
2. `--execution-goal`（任务本身目标）与 `--analysis-goal`（分析目标）是否需要独立字段，还是人只在 CLI 注入 analysis-goal？
3. decision log 与现有 web_audit.jsonl 是否合并（统一审计视图）？
4. M6.4 recommend 的"规则+LLM 混合"中，规则初筛边界（哪些规则可靠到可直接出候选）？
