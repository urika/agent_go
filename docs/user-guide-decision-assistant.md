# agent_go 决策辅助（M6）用户使用说明书

> 版本：M6（2026-08-17）
> 适用版本：agent_go 2.0+（commits 4f743c4 起）
> 适用读者：平台工程师 / AI 团队负责人 / 需要"用数据驱动模型与配置决策"的用户

决策辅助（M6）让 agent_go 从"被驱动执行"升级为"**用真实 bench 证据驱动策略决策**"：
跑一次 bench → AI 分析证据给建议 → 你确认 → 一键应用 → 决策留痕可审计。

---

## 一、功能地图（5 个命令/界面）

| 命令/界面 | 用途 | 场景 |
|-----------|------|------|
| `agent_go eval insight --results <批次>` | **AI 分析证据给建议** | "这个批次为什么通过率掉了？该换模型还是开降级链？" |
| `agent_go eval insight ... --apply-suggestion N` | **确认后应用建议** | 把第 N 条建议落到配置（改模型/降级链/预算） |
| `agent_go eval recommend --llm --results <批次>` | **规则初筛 + LLM 精排** | 基于 bench 实测推荐 worker_models/角色路由 |
| `agent_go decision log` | **查看决策历史** | "上次为什么把 planner 换成 K3？效果符合预期吗？" |
| Web `🧠 洞察` tab | **可视化全流程** | 浏览器里选批次→生成报告→看建议→回溯决策历史 |

**贯穿机制（已内建，无需配置）**：
- **证据强制绑定**：LLM 只能基于真实批次数据推理，建议必须带证据引用（`evidence_refs`），无效引用自动丢弃
- **人工确认红线**：所有建议默认 `requires_approval=true`，必须你确认才应用——**LLM 从不自动改配置**
- **决策自动留痕**：每次配置修改/模型切换/recommend 应用自动写入决策日志（含证据引用与预期影响）

---

## 二、用户案例（按典型工作流）

### 案例 1：分析 bench 批次，找出通过率/成本问题（最常用）

**场景**：你跑完一轮 hard 任务 bench，通过率 5/6（83%）但成本偏高（$/pass $0.45），想知道该怎么优化。

**步骤**：

```bash
# 1. 跑完 bench（已归档到 baselines/）
agent_go eval bench --tasks eval_suite --source-batch my-hard-batch \
  --output eval_suite/results_my_hard.jsonl

# 2. 让 AI 分析这批证据，针对目标给建议
agent_go eval insight \
  --results eval_suite/baselines/m4-mixB-hard \
  --analysis-goal "hard 通过率保持 100% 且 $/pass 降到 $0.1 以下" \
  --analysis-plan "换更便宜模型/调整降级链/白名单控制"
```

**预期输出**（决策洞察报告）：

```
📊 证据物化完成: m4-mixB-hard (6 条记录, hash=4319752f...)
   通过率: 1.0 | $/pass: 0.447595
   失败模式: []

🤖 LLM 分析推理中...

# 决策辅助洞察（m4-mixB-hard）

## 建议 1: 通过率=1.0 但 $/pass=0.447595 偏高，plan_model=deepseek-v4-pro 成本较高
- 根因假设: 高成本主要来自 deepseek-v4-pro；部分 hard 任务可能可由更便宜模型通过
- 证据: metrics/dollar_per_pass_usd, environment/plan_model
- 建议动作: 在 hard 批次试行降级链——便宜模型先跑，失败时升级回 deepseek-v4-pro
- 预期影响: $/pass 预期下降 20%-40%
- 成本/风险: 若便宜模型首次通过率不足，可能增加重试成本
- 置信度: 0.6 | 需人工确认: True
```

**关键点**：每条建议都带证据引用（`metrics/dollar_per_pass_usd` 等），这些是**校验过的真实数据路径**——LLM 不能凭空编造。

---

### 案例 2：确认 AI 建议并一键应用（确认后自动应用）

**场景**：案例 1 的建议 1（降级链）你觉得合理，想直接落到配置。

**步骤**：

```bash
# 应用第 1 条建议（自动改配置 + 备份 + 留痕）
agent_go eval insight \
  --results eval_suite/baselines/m4-mixB-hard \
  --analysis-goal "hard 通过率保持 100% 且成本降低" \
  --apply-suggestion 1
```

**预期输出**：

```
🔧 应用建议 1: 通过率=1.0 但 $/pass=0.447595 偏高...
   action_type=fallback_chain | payload={"difficulty":"hard","chain":["deepseek-v4-flash","deepseek-v4-pro"]}
✅ 已应用: insight apply fallback_chain: {"difficulty":"hard","chain":[...]}
   备份: ~/.agent_go/config.json.insight-backup-20260817-101500.json
   建议复跑验证: agent_go eval bench --tasks ... 后对比通过率
```

**自动完成的事**：
1. 按 action_type 改配置（fallback_chain → `worker_models_fallback_chain.hard`）
2. 原配置自动备份（`*.insight-backup-<时间戳>.json`，可回滚）
3. 写入决策日志（含证据引用、预期影响）
4. Web 操作审计同步记录

**不能自动应用的建议**（action_type=manual 或未识别）会提示"需人工执行"，不会擅自改配置。

---

### 案例 3：模型选型推荐（规则初筛 + LLM 精排）

**场景**：你在 GLM 和 K3 之间犹豫该用哪个做 worker，想要数据驱动的推荐。

**步骤**：

```bash
# 基于 bench 实测推荐 worker_models 配置
agent_go eval recommend --results eval_suite/results_m4_mixB_hard.jsonl --llm

# 满意后人工确认应用（写回 config）
agent_go eval recommend --results eval_suite/results_m4_mixB_hard.jsonl --apply
```

**流程**：
1. **规则初筛**（确定性问题识别）：自动标出 $/pass 超预算 / failure_class 集中 / 环境漂移 / problems 复发
2. **LLM 精排**：把候选+问题+模型指标喂 LLM，输出精排的 worker_models/角色路由 + 理由 + 风险
3. `--apply` 时人工确认 → 写回 config + 记录决策日志

**LLM 失败/解析失败时**：自动回退到纯规则推荐（可用但无精排理由），不会中断。

---

### 案例 4：回溯"上次为什么改配置"（决策审计）

**场景**：上周通过率掉了，怀疑是某次配置变更引入，想查证。

**CLI**：

```bash
agent_go decision log
```

**输出**（最新在前）：

```
共 31 条决策记录
[2026-08-18 08:52:04] insight.apply | config 字段修改: worker_models_fallback_chain
  目标: insight 建议应用（M6.5 确认后自动应用）| 确认人: cli
[2026-08-18 08:51:47] config.put | config 字段修改: plan_api.worker_base_url
  ...
```

**Web**：http://127.0.0.1:8091 → 🧠 洞察 tab →「📜 决策历史」表格（时间/变更/目标→预期影响/来源/确认人）。

---

### 案例 5：Web 全流程（零 CLI）

**场景**：你不想敲命令，全程浏览器操作。

**步骤**：
1. 启动 web：`agent_go web --port 8091`
2. 打开 http://127.0.0.1:8091 → 点「🧠 洞察」标签
3. **生成表单**：批次下拉选 `m4-mixB-hard` → 填分析目标 → 可选预设计划 → 点「🤖 生成洞察」
4. 报告自动归档并渲染展示（列表里可点开查看）
5. 「📜 决策历史」表格实时看所有配置变更/模型切换记录

---

## 三、完整闭环示例（一次完整的决策循环）

```
① 跑 bench 收集证据
   agent_go eval bench --tasks eval_suite --source-batch hard-v3 --output results.jsonl
        ↓
② AI 分析证据给建议
   agent_go eval insight --results eval_suite/baselines/hard-v3 --analysis-goal "..." 
        ↓ 建议带证据引用，无效引用自动丢弃
③ 你审阅建议（Web 洞察 tab 或 CLI 报告）
        ↓ 确认第 2 条合理
④ 应用建议（自动改配置 + 备份 + 留痕）
   agent_go eval insight --results ... --apply-suggestion 2
        ↓
⑤ 决策可查（decision log / Web 决策历史）
   agent_go decision log
        ↓
⑥ 复跑验证效果
   agent_go eval bench --tasks eval_suite --source-batch hard-v4 ...
   → 对比 hard-v3 vs hard-v4 的通过率/成本，确认改进
```

## 四、安全与边界（使用须知）

| 规则 | 说明 |
|------|------|
| **LLM 不自动改配置** | 所有建议 `requires_approval=true`，必须你 `--apply-suggestion` 或 `--apply` 确认 |
| **证据引用强制** | 建议必须带 `evidence_refs`，引用不存在的证据路径会被丢弃（防凭空编造） |
| **应用自动备份** | 每次应用生成 `*.insight-backup-<ts>.json`，可随时回滚 |
| **决策全留痕** | 所有配置修改/模型切换/推荐应用写入 `~/.agent_go/decision_log.jsonl` |
| **目标人定义** | `--analysis-goal` 由你输入，LLM 不修改目标（防目标漂移） |

## 五、命令速查

| 命令 | 说明 |
|------|------|
| `agent_go eval insight --results <批次> --analysis-goal <目标>` | 生成决策洞察（CLI 打印 + 自动归档） |
| `agent_go eval insight ... --output <path.md>` | 指定报告输出路径 |
| `agent_go eval insight ... --apply-suggestion <N>` | 应用第 N 条建议 |
| `agent_go eval recommend --results <结果> [--llm] [--apply]` | 模型推荐（规则初筛/LLM 精排/应用） |
| `agent_go decision log` | 查看决策历史 |
| Web `🧠 洞察` tab | 可视化生成/查看报告 + 决策历史 |

**批次数据位置**：`eval_suite/baselines/<批次>/`（manifest + results + summary，不可变）——`--results` 指向它。
**洞察报告位置**：`~/.agent_go/insights/<批次>-<时间戳>.md`（自动生成）。
**决策日志位置**：`~/.agent_go/decision_log.jsonl`（追加式）。
