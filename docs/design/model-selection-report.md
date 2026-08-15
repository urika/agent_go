# 模型选型报告（全模型 bench 对比）

> 日期：2026-08-15
> 实验：同一 6 个 canonical hard 任务（add-tag/security-hardening/race-condition/stage-validation/conditional-branching/db-performance），e2e 端到端模式，worker 固定 opus-4-7→代理（云端），变化 planner/evaluator 模型组合
> 数据源：eval_suite/baselines/ m4 系列 6 个批次（同任务集、同口径，可直接对比）
> 配套机制：模型三层（models.json registry）+ 声明式 thinking/JSON + R8 归因 metering + e2e 端到端 + 验证白名单扩展

## 1. 对比总表

| 批次 | planner/evaluator | 通过率 | 总成本 | $/pass | 平均延迟/任务 | 6 任务总耗时 |
|------|------------------|--------|--------|--------|--------------|-------------|
| m4-local-hard-goal | 本地 Qwen3.6-35B | 2/6 (33%) | $0 | $0 | 669s | 67 min |
| m4-e2e-hard-goal | deepseek-v4-flash | 2/6 (33%) | $0* | $0* | 235s | 24 min |
| m4-e2e-hard-pro-v2 | deepseek-v4-pro | 3/6 (50%) | $0.54 | $0.18 | 405s | 40 min |
| m4-glm-hard | **GLM glm-5.3** | **5/6 (83%)** | **$0.15** | **$0.03** | 311s | 31 min |
| m4-k3-hard | Kimi K3 | 3/6 (50%) | $1.29 | $0.43 | 584s | 58 min |
| m4-mixB-hard | **K3 planner + GLM evaluator** | **6/6 (100%)** | $2.69 | $0.45 | 370s | 37 min |

\* m4-e2e-hard-goal 在 R8 归因修复前采集，云端成本未完整记录。

## 2. 维度分析

### 2.1 通过率（能力）

```
本地 35B   ██░░░░  33%  ← 拆分模式 0%，e2e 后 33%
v4-flash   ██░░░░  33%
v4-pro     ███░░░  50%
K3         ███░░░  50%
GLM        █████░  83%  ← 单模型最佳
K3+GLM     ██████  100% ← 组合最佳
```

- **planner 质量决定拆解质量**：GLM/K3（Anthropic 兼容、JSON strict）planner 拆解明显优于 v4-pro（JSON loose）
- **evaluator 质量决定验收可信度**：GLM evaluator 稳定（thinking+text 双 block）；K3 evaluator 缺陷（纯 thinking 无 text，security/conditional 误判失败）；本地 35B 评估弱
- **组合 100% 的机制**：K3 planner（coding 拆解强）+ GLM evaluator（JSON 评估稳定）互补

### 2.2 成本（$/pass 越低越划算）

```
本地 35B   $0        ← 免费但 33%
v4-flash   ~$0
GLM        $0.03     ← 极致性价比（5/6 通过仅 $0.15）
v4-pro     $0.18
K3         $0.43     ← K3 定价高（$3/$15 per 1M）
K3+GLM     $0.45     ← 100% 但最贵（K3 planner 贵 + db timeout 多轮）
```

- **GLM 性价比断层领先**：5/6 通过仅 $0.15，$/pass $0.03——比 K3 便宜 14 倍、比组合便宜 15 倍
- K3 单模型贵且只有 50%；组合贵但 100%

### 2.3 延迟

```
v4-flash   235s   ← 最快（轻量）
GLM        311s
K3+GLM     370s
v4-pro     405s
K3         584s   ← 最慢（thinking 全程 + 大模型）
本地 35B   669s   ← 本地推理慢
```

- v4-flash 最快（但能力弱）；K3/本地最慢（thinking/本地推理）
- GLM 在通过率/延迟间平衡好

## 3. 选型建议（按场景）

| 场景 | 推荐 | 理由 |
|------|------|------|
| **默认生产**（质量优先） | **K3 planner + GLM evaluator**（方案 B） | 100% 通过率，硬任务全过 |
| **性价比优先**（日常/预算敏感） | **GLM glm-5.3 全角色** | 83% 通过 + $0.03/pass，14x 便宜 |
| **最快响应**（交互/轻任务） | v4-flash / local-mlx | 235s/任务、成本极低（medium 任务足够） |
| **离线/合规** | local-mlx（TCO $0.0005/次） | 数据不出内网（hard 任务能力不足，配 e2e+goal 可用） |
| **评估稳定兜底** | evaluator 永远用 GLM | K3 纯 thinking 缺陷、本地评估弱 |

## 4. 通过率演进（完整实验链，架构改进视角）

```
拆分模式（局部上下文丢失）：
  本地 35B          0/6  ← Plan→拆分→worker 局部上下文 → hard 全失败
  混合 v4-flash     0/6

e2e 端到端模式（全局上下文保留）：
  +v4-flash        2/6   33%
  +v4-pro          3/6   50%
  +GLM             5/6   83%
  +K3              3/6   50%
  +K3 planner + GLM evaluator  6/6  100%
```

关键架构改进（按贡献排序）：
1. **e2e 端到端模式**（不拆分保留全局上下文）：0/6 → 2/6（最大跃迁）
2. **planner/evaluator 上强模型**（GLM）：2/6 → 5/6
3. **角色互补**（K3 planner + GLM evaluator）：5/6 → 6/6
4. **验证白名单扩展**（pip install）：补 1 个（db-performance）
5. **R8 归因 + 声明式 thinking/JSON**：保证链路可信（成本/归因准确）

## 5. 口径与局限

- 通过率口径：accepted_delivery / binary_pass（results.jsonl）；v4-flash 批次 R8 前采集
- 成本口径：metering cost_usd（R8 归因修正后云端正确计费）；本地 TCO 按 registry tco_per_call
- 单次运行，模型有随机性（GLM 两次 5/6 vs 4/6 波动区间）；同任务集同 worker 可对比
- worker 固定 opus-4-7（代理路由，实际后端随代理配置），各批次 worker 后端一致
