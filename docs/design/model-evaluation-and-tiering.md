# 模型评估与分级策略设计稿

> **状态**：✅ P0 已实施（2026-07-25，`pricing.py` / `bench.py` / `cross_judge.py` / `eval_suite`）；✅ P1 `router recommend` 已实施（2026-08-11，`bench.py` / `cli.py` / `pricing.py`）
> **基线日期**：2026-07-25
> **S12 更新（2026-08-07）**：✅ 智谱后端定价补全（glm-4.7/glm-5.1/glm-5.2/glm-4.5-air，联网查证）；✅ 运行前模型-价格预检（`bench.py _probe_actual_model` 探测实际后端 + `pricing.resolve_price` 校验定价覆盖，缺定价告警/中止）；✅ 实际路由验证：claude-haiku-4-5/sonnet-4-6/opus-4-7 → 智谱统一归一 glm-4.7（有定价，成本按真实模型计价）
> **基线日期**：2026-07-25
> **关联**：[prd.md §P1 角色感知模型路由](../prd.md) · [router-multi-provider-extension.md](router-multi-provider-extension.md) · [ISSUES.md ISSUE-26 计价失真](../ISSUES.md)
> **数据源**：定价来自厂商官网 + 聚合平台（2025-2026 公开数据）；能力 benchmark 来自 SWE-bench 官方 + 独立第三方测试

---

## 0. 问题背景：为什么需要本设计

前期"角色感知模型路由"（S4）解决了**按角色路由不同模型**的机制，但留下两个未闭环的关键问题：

1. **选哪个模型？** 当前 `MODEL_PRICES` 表仅 7 个模型，新模型（`claude-code-executor`、本地模型、国产新旗舰）全部兜底为最便宜的 deepseek 单价，导致 [ISSUE-26](../ISSUES.md)：**$/pass rate 被低估 11-22 倍，gate 假性通过**。分级策略需要回答"每个角色/difficulty 该用哪一档模型"。

2. **怎么知道选得对？** 厂商 benchmark（如 SWE-bench）与真实 agentic 场景存在巨大鸿沟——Qwen3-Coder-30B-A3B 官方上榜，但 [SambaNova 独立测试](https://sambanova.ai/blog/are-llms-truly-solving-software-problems)显示真实世界解决率仅 **7%**。**不能靠厂商 benchmark 决策**，必须用 agent_go 自己的运行数据建立"模型 × 难度 × 生产力"三维评估。

本设计同时回答这两个问题：**分级策略（该用什么）+ 评估机制（怎么验证选对了）**。

---

## 1. 模型分级策略

### 1.1 一线大模型定价全景（2025-2026）

> 汇率按 1 USD ≈ 7.2 CNY 折算，统一以 **USD / 百万 tokens** 对比。标准 API 价（非 batch/cache 命中价）。

#### 国际厂商

| 模型 | 输入 $/M | 输出 $/M | 定位 |
|------|:---:|:---:|------|
| Claude Opus 4.1 | 15.0 | 75.0 | 顶级旗舰（最强推理/长任务） |
| Claude Opus 4.6/4.7 | 5.0 | 25.0 | 降价旗舰（Opus 4.5 起 67% 降幅） |
| **Claude Sonnet 4** | 3.0 | 15.0 | 主力（当前 planner 默认） |
| Claude Haiku 4.5 | ~1.0 | ~5.0 | 轻量（当前 evaluator 默认） |
| GPT-5.6 Sol / 5.5 | 5.0 | 30.0 | OpenAI 旗舰 |
| GPT-5.6 Terra | 2.5 | 15.0 | OpenAI 中端 |
| **GPT-4.1** | 2.0 | 8.0 | 主力 |
| GPT-4.1 Nano | 0.1 | 0.4 | 轻量 |
| **Gemini 2.5 Pro** | 1.25 | 10.0 | Google 旗舰（200K 上下文） |
| Gemini 2.5 Flash | 0.3 | 2.5 | Google 轻量 |

#### 国内厂商（按 1 USD ≈ 7.2 CNY 折算）

| 模型 | 输入 ¥/M | 输出 ¥/M | 输入 $/M | 输出 $/M | 定位 |
|------|:---:|:---:|:---:|:---:|------|
| **Qwen-Max** | 20 | 60 | 2.78 | 8.33 | 阿里旗舰 |
| Qwen-Plus | 0.8 | 2.0 | 0.11 | 0.28 | 阿里主力 |
| **Doubao-1.5-pro-32k** | 0.8 | 2.0 | 0.11 | 0.28 | 字节主力 |
| Doubao-lite | 0.3 | 0.6 | 0.04 | 0.08 | 字节轻量 |
| **Kimi K2** | 4 | 16 | 0.56 | 2.22 | 月之暗面（长上下文） |
| **DeepSeek V3.2 (chat)** | 1 | 2 | 0.14 | 0.28 | 性价比旗舰（7月涨价后） |
| DeepSeek R1 (reasoner) | 3 | 6 | 0.42 | 0.83 | 推理增强 |
| **GLM-4.6** | ~5 | ~15 | ~0.69 | ~2.08 | 智谱旗舰（200K，编码强） |

### 1.2 四条定价规律（影响分级决策）

1. **输出比输入贵 5-25 倍是普遍规律**：所有模型 completion/prompt 比值集中在 4-6 倍。agent_go 的 worker 角色输出占比高（生成大量代码），**选模型不能只看输入价**。
2. **价格呈 3-4 个数量级跨度**：顶级旗舰到轻量约 300 倍，这正是角色路由能省钱的空间。
3. **国内模型在国际比较中处于"性价比档"**：DeepSeek/Qwen-Plus 输出 $0.28/M vs GPT-4.1 $8/M，便宜约 28 倍；但顶级推理能力国内暂无对标 Opus 级。
4. **Claude Code 的 `claude-code-executor` 是特殊存在**：其 `total_cost_usd` 是真实计费（按实际用的 Claude 模型），`actual_model` 字段不映射到具体模型——**应优先用真实 `cost_usd`**（[ISSUE-26](../ISSUES.md) 已修），价目表只是兜底。

### 1.3 三角色 × 三档位分级矩阵

```
                 主力旗舰档           性价比档              轻量档
                 (Frontier)          (Value)               (Lite)
─────────────────────────────────────────────────────────────────
Planner (规划)   ★ Sonnet 4 /        ✗ 铁律不允许降级        ✗
                 GPT-4.1 /
                 Qwen-Max

Worker-复杂      Opus 4.6 /          DeepSeek V3.2 /       —
(difficulty=hard) GPT-5.6 Terra /    Qwen-Plus              (复杂任务
                 GLM-4.6                                    不走轻量)

Worker-中等      Sonnet 4 /          DeepSeek V3.2 /       Doubao-lite
(difficulty=medium) GPT-4.1          Qwen-Plus             (默认性价比)

Worker-简单      —                   DeepSeek V3.2 /       Haiku 4.5 /
(difficulty=easy)                    Doubao-pro           GPT-4.1 Nano
                                                           (默认轻量)

Reviewer (审查)  Gemini 2.5 Pro /    Kimi K2              —
(与 Worker       Qwen-Max            (强制与 Worker
不同源)                              不同 provider)
```

**设计原则（PRD 铁律对齐）：**

| PRD 约束 | 分级策略映射 |
|---------|------------|
| "编排比模型更重要" | 分级是编排的一部分——角色决定档位 |
| "Planner 不降级" | Planner 铁律用主力旗舰起步，无 fallback 到性价比档 |
| "复杂度判断在规划阶段收敛" | Worker 按 S4 difficulty 分三档 |
| "Reviewer 与 Worker 不同源" | Reviewer 用与 Worker 不同的 provider，保证视角低相关 |
| "$/pass rate 不劣化" | 性价比档必须配质量门（K8 首次通过率），否则省钱产出垃圾 |

### 1.4 推荐默认配置

**国际场景（Claude 生态）：**
```jsonc
"router": {
  "roles": {
    "planner": { "provider": "anthropic", "model": "claude-sonnet-4" },  // 无 fallback（铁律）
    "worker": {
      "provider": "anthropic", "model": "claude-haiku-4-5",
      "fallback": { "provider": "deepseek", "model": "deepseek-chat" }
    },
    "reviewer": { "provider": "google", "model": "gemini-2.5-pro" }  // 与 Worker 不同源
  },
  "worker_models": {
    "easy":   "claude-haiku-4-5-20251001",   // $1/$5
    "medium": "claude-sonnet-4",              // $3/$15
    "hard":   "claude-opus-4-6"               // $5/$25（降价后推荐）
  }
}
```

**国内场景（性价比优先）：**
```jsonc
"router": {
  "roles": {
    "planner": { "provider": "deepseek", "model": "deepseek-chat" },
    "worker": {
      "provider": "deepseek", "model": "deepseek-chat",
      "fallback": { "provider": "volcengine", "model": "doubao-1.5-pro-32k" }
    },
    "reviewer": { "provider": "moonshot", "model": "kimi-k2" }
  },
  "worker_models": {
    "easy":   "doubao-lite",       // ¥0.3/0.6
    "medium": "deepseek-chat",      // ¥1/2（默认）
    "hard":   "qwen-max"            // ¥20/60（复杂任务升级）
  }
}
```

### 1.5 成本估算（验证分级策略效果）

以 PRD 北极星目标 **$/pass ≤ $0.05**（Q3）/ **≤ $0.03**（年度）为约束，按典型任务（1 planner + 3 worker + 1 reviewer）估算：

| 策略 | 总成本/pass | 达标？ |
|------|:-----------:|:---:|
| 全 Sonnet 4（现状基线） | ~$0.165 | ❌ |
| 国际分级（Sonnet+Haiku+Gemini） | ~$0.08 | ❌ Q3，接近 |
| **国内分级**（DeepSeek+Qwen+Kimi） | ~$0.008 | ✅✅ |
| **混合**（Sonnet 规划 + DeepSeek 执行） | ~$0.036 | ✅ |

**结论**：纯国际分级难以达到 Q3 目标（PRD 承认的 K4 现状 ~$0.05-0.15）；国内分级轻松达标但需配质量门；**混合策略**（Sonnet 规划保质量 + DeepSeek 执行省成本）是 PRD §P1 line 158-160 的最优解。

### 1.6 成本口径：按实际模型重算（2026-08-01 更新）

**背景**：`claude-*` 路由名经 `~/.claude/settings.json` 的 `ANTHROPIC_DEFAULT_*_MODEL` 映射到实际后端模型。实测映射：

| 路由名 | 实际模型 | DeepSeek 定价 ($/1M) | Anthropic 定价 ($/1M) | 虚高 |
|--------|---------|---------------------|---------------------|------|
| `claude-haiku-4-5` | `deepseek-v4-flash` | 0.14 / 0.28 | 1.0 / 5.0 | ~10x |
| `claude-sonnet-4-6` | `deepseek-v4-flash` | 0.14 / 0.28 | 3.0 / 15.0 | ~16x |
| `claude-opus-4-7` | `deepseek-v4-pro` | 0.435 / 0.87 | 5.0 / 25.0 | ~15x |

**关键**：`claude-sonnet-4-6` 与 `claude-haiku-4-5` 实际都用 `deepseek-v4-flash`（settings.json 中 `ANTHROPIC_DEFAULT_SONNET_MODEL=deepseek-v4-flash[1M]` 的 `[1M]` 后缀未生效）。

**重算机制**：subtask.py 的 worker metering 不再直接采用 claude CLI 返回的 `total_cost_usd`（按 Anthropic 定价），而是：
1. 从 claude 响应 `assistant.message.model` 解析**实际模型名**
2. 用 `MODEL_PRICES` 定价按 token 重算 `cost_usd`
3. 未知模型回退 claude 返回值；本地模型（`AGENT_GO_IS_LOCAL`）成本清零

**效果**：2026-08-01 v2 bench 成本降幅 82-92%（haiku $0.58→$0.106/pass、sonnet $1.44→$0.111、opus $1.90→$0.296），更真实反映 DeepSeek 实际成本。

---

## 2. 本地模型的特殊决策

### 2.1 "免费本地模型"的真实成本

本地模型（如 Qwen3.6-27B）的"免费"是错觉——真实成本是"时间 × 硬件折旧"，且吞吐瓶颈会击穿耗时目标。

| 成本项 | RTX 4090 | M3 Max（darwin arm64） |
|--------|----------|----------------------|
| 功耗 | ~400W | ~30-70W |
| 电费 | ~¥0.25/小时 | ~¥0.05/小时 |
| 折旧（3年摊） | ~¥1.5/小时（¥13k/9000h） | ~¥3/小时（整机含 CPU/内存） |
| 27B Q4 吞吐 | ~30-50 tok/s（单卡可载） | ~15-25 tok/s |
| **典型 worker 任务成本**（5k output） | **$0.24/pass** | **$0.05/pass + 4 分钟延迟** |

**对比**：DeepSeek API 同任务 $0.001/pass——**本地 4090 比贵 240 倍**。

### 2.2 厂商 benchmark ≠ 真实 agentic 生产力

| 模型 | SWE-bench Verified（官方） | 真实 agentic 解决率（独立测） | 差距 |
|------|:---:|:---:|:---:|
| Qwen3.6-27B | 77 分 | ?（未测） | 待评估 |
| Qwen3-Coder-30B-A3B | 上榜 | **7%** | ~10 倍 |

**SWE-bench 是"单文件、单 PR、已知测试"离题**；agent_go 真实场景是"多文件、多步、worktree 隔离、验证循环、依赖拓扑"。两者能力需求完全不同——**77 分官方分不能外推到 agent_go 通过率**。

### 2.3 本地模型决策路径

**不靠 benchmark 和"免费"直觉，用 §3 的评估机制实测：**

1. 配置本地 provider + 成本估算（`local_model_hourly_cost`）
2. 跑对照实验（`eval bench`，见 §3）
3. 根据实测 pass_rate/$/pass/假阳性，定位到以下之一：
   - **Worker easy 档**（简单任务可用 + 延迟可接受 + 隐私场景）
   - **Reviewer 角色**（低频 + 不同源 + 延迟不敏感）
   - **不接入生产**（pass_rate < 70%，省钱产出垃圾）

---

## 3. 模型生产力评估机制

### 3.1 核心原则

**不信任厂商 benchmark，用 agent_go 自己的运行数据建立"模型 × 难度 × 生产力"三维表。**

agent_go 已采集评估所需的全部原始信号（metering.jsonl + meta.json），但**没有按模型关联**——这是 §3.4 要补的关键缺口。

### 3.2 三层评估体系

```
┌─ 第 1 层：确定性评估（客观，无 LLM 偏差）─────────────────┐
│  标准任务集（带 ground-truth 验证命令）× N 模型            │
│  → pass_rate / first_pass_rate / latency / cost            │
│  → 适用于"能否交付"的硬性判定                              │
└────────────────────────────────────────────────────────────┘
                          ↓
┌─ 第 2 层：语义评估（主观，跨模型评判规避自偏）─────────────┐
│  对第 1 层"通过但有疑问"的产出，用 A 模型评 B 模型产出      │
│  → semantic_score / false_positive_rate                    │
│  → 适用于"验证过但功能对不对"的软性判定                     │
└────────────────────────────────────────────────────────────┘
                          ↓
┌─ 第 3 层：决策汇总（产出路由建议）─────────────────────────┐
│  analyze_model_productivity 把 1+2 的结果按模型聚合         │
│  → recommendation: recommended / conditional / discouraged │
│  → 输出模型 × 难度 × 角度的决策矩阵                        │
└────────────────────────────────────────────────────────────┘
```

### 3.3 可量化指标

| 维度 | 指标 | 采集方式 | 决策含义 |
|------|------|---------|---------|
| 正确性 | `pass_rate`（验证通过率） | 确定性验证命令 exit code | <60% 禁用；≥80% 可用 |
| 正确性 | `first_pass_rate`（首次通过率） | `retry_count==0` 比例 | K8 北极星 |
| 正确性 | `semantic_score`（语义完整性） | **跨模型评判**（§3.5） | 假阳性检测 |
| 效率 | `dollar_per_pass`（$/pass） | 真实 cost_usd（本地用折旧估算） | 北极星 |
| 效率 | `avg_latency_ms` | metering 已采集 | 本地模型此值极高，影响 K3 |
| 效率 | `avg_retries`（平均重试） | meta.json retry_count | 高重试=质量差+隐性成本翻倍 |
| 稳定性 | `pass_rate_std`（通过率标准差） | 同模型多次运行 | 本地量化模型可能不稳定 |
| 可信度 | `sample_size` + `confidence` | 任务数 | <5 标记 low_confidence |

### 3.4 标准任务集设计（第 1 层基石）

任务集必须满足：
- **ground truth 可机器判定**（有测试/构建命令，exit code 即对错）
- **覆盖三档难度**（easy/medium/hard），与 S4 difficulty 对齐
- **隔离可复现**（独立 worktree，无状态残留）
- **规模够统计**（每档 ≥10 任务，才能算稳通过率）

**任务集结构（YAML，放 `eval_suite/`）：**
```yaml
id: add-logging
difficulty: easy
repo: ./fixtures/sample-py-project   # 固定的最小可测仓库
task: |
  在 src/utils.py 新增 setup_logging(level="INFO") 函数，
  使用标准库 logging，输出到 stderr。
verification:
  - "python -m pytest tests/test_logging.py"   # ground truth 测试
  - "python -c \"from src.utils import setup_logging; setup_logging()\""
timeout: 180
```

**规模与分布：**

| 难度 | 任务数 | 任务特征 | 评估重点 |
|------|:---:|---------|---------|
| easy | 10-15 | 单文件、单函数、明确 spec | 基础编码能力（本地 27B 应能过） |
| medium | 10 | 多文件、接口实现、需理解上下文 | 工程集成能力（分水岭） |
| hard | 5-10 | 重构、多步、需保持兼容 | 系统级推理（本地 27B 可能崩） |

### 3.5 对照运行编排（控制变量）

新增 CLI：`agent_go eval bench`
```bash
agent_go eval bench \
  --tasks eval_suite/ \
  --models sonnet-4,deepseek-chat,qwen3.6-27b-local \
  --repeat 3 \                      # 每任务重复 3 次（稳定性）
  --judge-model gemini-2.5-pro \    # 跨模型评判（第 2 层）
  --output eval_suite/results.jsonl
```

**编排流程（复用现有 `_run_pipeline` + 新增 harness）：**
```
对每个 model in [--models]:
  对每个 task in eval_suite/:
    对每次 repeat in [--repeat]:
      1. 创建独立 worktree（复用 _worktree_create）
      2. 注入 model 配置（router.set worker model = model）
      3. 跑 run_subtask（headless，复用现有执行链）
      4. 采集：verify_ok / retry_count / latency / cost_usd / difficulty
      5. （第 2 层）若 verify_ok 且配置了 --judge-model：
         用 judge-model 跑 evaluate_semantic → semantic_score
      6. 写一行 results.jsonl：{model, task, repeat, passed, cost, latency, ...}
```

### 3.6 第 2 层语义评估：双盲交叉评判（规避 LLM-as-Judge 自偏）

**核心陷阱**：用 Sonnet 4 跑任务、再用 Sonnet 4 当评判器，会出现系统性自我偏好（研究显示模型对自产输出评分偏高 10-30%）。直接用 agent_go 现有 `evaluate_semantic`（默认 Haiku）评估 Sonnet/本地 27B 产出，得出的"质量分"本身有偏。

**交叉评判矩阵（N 模型互评）：**
```
              评判者 →
              Sonnet    DeepSeek    Gemini    本地27B    人工抽检
产出者 ↓
Sonnet        ✗自偏     ✓           ✓         ✓          ✓（基线）
DeepSeek      ✓         ✗自偏       ✓         ✓          ✓
Gemini        ✓         ✓           ✗自偏     ✓          ✓
本地27B        ✓         ✓           ✓         ✗自偏      ✓（重点）
```

- 每个产出被 **≥2 个不同 provider 的模型评判**，取均值（降低单评判者偏差）
- **禁绝自评**（矩阵对角线）+ 编排器硬约束（`judge != candidate`）
- **人工抽检基线**：随机抽 10% 产出人工打分，校准 LLM 评判者（若分歧 >30%，标记 judge unreliable）

**评分尺度（结构化）：**
```python
JUDGE_RUBRIC = {
    "correctness": "1-5 分：功能是否完整实现 spec",
    "completeness": "1-5 分：是否覆盖边界条件/错误处理",
    "code_quality": "1-5 分：可读性/命名/结构",
    "false_positive": "bool：验证通过但实际功能是否错误（假阳性检测）",
}
# semantic_score = avg(correctness, completeness, code_quality)
```

### 3.7 第 3 层：决策汇总（`analyze_model_productivity`）

```python
def analyze_model_productivity(results_path: Path) -> dict:
    """从 bench 结果聚合每模型的生产力指标 + 决策建议。

    返回每模型：
      pass_rate, first_pass_rate, avg_retries, avg_latency_ms,
      cost_per_pass, dollar_per_pass, semantic_score, false_positive_rate,
      pass_rate_std, sample_size, confidence,
      difficulty_breakdown: {easy/medium/hard: {pass_rate, dollar_per_pass}},
      recommendation: recommended/conditional/discouraged,
      recommended_roles: [worker_easy, reviewer, ...],
      reason: str
    """
```

**决策规则（PRD 对齐，可机器执行）：**
```python
def _recommend(metrics, difficulty_breakdown):
    # 硬性禁用（无论成本多低）
    if metrics["pass_rate"] < 0.60:
        return ("discouraged", [], "通过率 <60%，省钱产出垃圾（PRD 反指标）")
    if metrics["false_positive_rate"] > 0.20:
        return ("discouraged", [], "假阳性 >20%，验证不可信")
    if metrics["sample_size"] < 5:
        return ("conditional", [], "样本不足，需更多数据")
    # 按难度定位 + 综合判定
    roles = [r for r, thresh in [("worker_easy",0.80),("worker_medium",0.75),("worker_hard",0.70)]
             if difficulty_breakdown[r]["pass_rate"] >= thresh]
    if metrics["dollar_per_pass"] <= baseline and metrics["pass_rate"] >= 0.80:
        return ("recommended", roles, "$/pass 达标 + 通过率 ≥80%")
    elif roles:
        return ("conditional", roles, "仅部分难度可用")
    else:
        return ("discouraged", [], "各难度均不达标")
```

### 3.8 决策矩阵示例

```
📊 模型生产力评估（基于 72 次执行）

模型              pass_rate  $/pass   延迟    假阳性  难度(easy/med/hard)  建议
─────────────────────────────────────────────────────────────────────────
sonnet-4          85%        $0.15    30s     3%      100/85/60            ★ recommended (全角色)
deepseek-chat     73%        $0.008   40s     8%      95/70/30             ⚠ conditional (easy/medium)
qwen3.6-27b-local 65%        $0.08    185s    15%     90/50/20             ⚠ conditional (easy only)
```

---

## 4. 规避评估陷阱的设计要点

| 陷阱 | 规避机制 |
|------|---------|
| **LLM-as-Judge 自偏** | 禁绝自评（`judge != candidate`）+ 每 产出 ≥2 不同 provider 评判 + 人工抽检 10% 校准 |
| **任务泄漏** | 任务集 spec 不含答案；每次运行从池中随机抽样；worktree 独立无残留 |
| **通过率方差大** | 每任务 `--repeat 3`，报告 `pass_rate_std`；std >0.2 标记不稳定 |
| **样本不足误判** | `sample_size < 5` 标 low_confidence，不参与自动降级 |
| **本地成本忽略**（ISSUE-26 同类） | 强制 `local_model_hourly_cost` 配置；本地 cost_usd 按 `latency × 折旧` 估算并记入 metering |
| **验证命令覆盖不全**（假阳性） | 第 2 层交叉评判 + `false_positive_rate` 指标 + >20% 禁用 |
| **judge 自身不可信** | 人工抽检校准；LLM 与人工分歧 >30% 时回退到仅第 1 层 |
| **benchmark 失真** | 完全不用厂商 SWE-bench 分，只信 agent_go 自跑数据 |

---

## 5. 落地范围（分阶段）

> **实际落地（2026-07-25）**：P0 已完成，规模超出原规划 —— `MODEL_PRICES` 扩充至 **48 个模型**（规划 20），标准任务集 **22 个任务**（规划 8），4 个 fixture（task-mgr / data-pipeline / django-blog / fp-sandbox）。下表"交付物"列保留原规划数字作为历史记录。

| 阶段 | 交付物 | 价值 |
|------|--------|------|
| **P0** | 扩充 `MODEL_PRICES` 表（规划 20，实际 48 个模型）+ `MODEL_TIER` 元数据 | 修复 ISSUE-26 根因，$/pass 可信 |
| **P0** | 标准任务集种子（规划 8，实际 22 任务 + ground truth）+ `eval bench` 编排器 + `analyze_model_productivity` + `eval models` | 评估机制可用 |
| **P1** | 交叉评判矩阵（N 模型互评，**P1 简化版**：四维评分退化为单一 semantic_score，P2 升级结构化 rubric）+ 人工抽检校准 | 第 2 层，假阳性检测 |
| **P1** | `router recommend`（基于评估结果自动推荐路由，**已实施** 2026-08-11：`agent_go router recommend [--results FILE] [--apply] [--force]`）+ `config.example.json` 三套预设 | 闭环到配置 |
| **P2** | 任务集扩充（社区贡献）+ 难度自动校准 | 长期演进 |

### 改动文件清单（P0）

| 文件 | 改动 |
|------|------|
| `agent_go/bench.py` | **新增** `cmd_bench`（编排器）+ `analyze_model_productivity` |
| `agent_go/pricing.py` | 扩充 `MODEL_PRICES`（规划 20，实际 48 模型）+ `MODEL_TIER` |
| `agent_go/cli.py` | `bench` / `models` 子命令注册 |
| `eval_suite/tasks/*.yaml` | **新增** 标准任务（规划 8，实际 22 个） |
| `eval_suite/fixtures/` | **新增** task-mgr / data-pipeline / django-blog / fp-sandbox 4 个可测仓库 |
| `agent_go/metrics.py` | `estimate_local_cost`（本地模型成本估算） |
| `tests/test_bench.py` | bench 编排器测试（mock LLM，验证控制变量） |

---

## 6. 风险与开放问题

| 风险 | 说明 | 缓解 |
|------|------|------|
| **价格漂移** | 厂商频繁调价（Opus 4.5 降 67%、DeepSeek 7月涨价） | `MODEL_PRICES` 加 `updated_at` 字段 + `eval cost` 报告展示更新日期 |
| **国内模型质量波动** | DeepSeek/Qwen 在复杂 agentic 任务上可能不如 Claude | 配 K8 首次通过率门禁，省钱不能牺牲通过率 |
| **本地模型 benchmark 失真** | 官方 77 分 vs 真实 7% 解决率（precedent） | 必须用 agent_go 实测 ≥20 任务才决策 |
| **本地成本被忽略** | metering 的 `cost_usd=0` 会让 gate 假性通过（ISSUE-26 同类） | 强制 `local_model_hourly_cost`；gate 对高 `unknown_model_events` 报警 |
| **延迟击穿 K3** | 本地 27B 推理 120-250s，K3（简单 ≤3min）岌岌可危 | `estimate_task_duration`（M4）已实现，本地延迟自动反映 |
| **judge 自身不可信** | LLM 评判者可能与人工分歧 | 人工抽检 10% 校准；分歧 >30% 标 unreliable |
| **样本不足误判** | <5 任务时 pass_rate 波动大 | `low_confidence` 标记，不参与自动决策 |
| **本地模型版本漂移** | Qwen 微调/量化版本更新，能力变化 | 加 `model_fingerprint`（版本+量化）字段 |
| **并发瓶颈** | 本地模型吞吐有限，`--parallel N` 不会提升本地并发（PRD line 174） | router 对 local provider 强制并发上限 = 1 |

---

## 7. 数据源引用

**定价（2025-2026 公开数据）：**
- 国际：[Anthropic 定价](https://benchlm.ai/anthropic/api-pricing) · [OpenAI 官方](https://openai.com/api/pricing) · [Gemini 定价](https://ai.google.dev/gemini-api/docs/pricing)
- 国内：[阿里云百炼](https://help.aliyun.com/zh/model-studio/model-pricing) · [Kimi 平台](https://platform.kimi.com/) · [火山方舟](https://www.volcengine.com/docs/82379/1544106) · [DeepSeek](https://api-docs.deepseek.com/quick_start/pricing) · [智谱](https://ai.bimant.com/ai-prices/)

**能力 benchmark：**
- [Qwen3.6-27B 官方](https://qwen.ai/blog?id=qwen3.6-27b)（SWE-bench 77 分）
- [SambaNova 独立测](https://sambanova.ai/blog/are-llms-truly-solving-software-problems)（30B 真实 7%）
- [SWE-bench 排行榜](https://www.swebench.com/)

**本地成本：**
- [SitePoint: 4090 vs M3 Max](https://www.sitepoint.com/mac-m3-max-vs-rtx-4090-local-llm-performance-showdown-2026/)
- [V2EX: 4090 功耗](https://www.v2ex.com/t/1225726) · [什么值得买: 4090 账单](https://post.smzdm.com/p/a6zmz4ro)
