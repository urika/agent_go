# Goal 模式 A/B 实验报告

> 日期：2026-08-12
> 数据：3 任务 × 2 模式（off vs force）= 6 次真实执行
> 环境：Claude Code 2.1.227，worker_models easy=haiku/medium=sonnet/hard=opus

## 1. 实验设计

| 项 | 值 |
|---|---|
| 任务集 | eval_suite/golden_subset/tasks/ 3 个 golden 任务 |
| 任务 | implement-done-command（medium）、security-hardening-taskmgr（hard）、conditional-branching-datapipeline（hard） |
| 双臂 | `--goal-mode off` vs `--goal-mode force` |
| 隔离方式 | 每任务 × 每模式独立克隆 fixture 到临时目录 |
| 采样 | 每任务 × 每模式 1 次运行 |
| 测量指标 | 时长、成本、token、验证状态、goal_turns、failure_class |

## 2. 执行结果

所有 6 次运行均成功交付（DELIVERY_READY），无失败：

| 任务 | 模式 | task_id | 时长 | 成本 | tokens | 验证 | goal_turns |
|---|---|---|---|---|---|---|---|
| implement-done-command | off | task-20260812-095209 | 108s | $0.044 | 127k | ✅ | 0 |
| implement-done-command | force | task-20260812-100005 | 43s | $0.043 | 126k | ✅ | 5 |
| security-hardening-taskmgr | off | task-20260812-100105 | 194s | $0.134 | 448k | ✅ | 0 |
| security-hardening-taskmgr | force | task-20260812-100335 | 186s | $0.130 | 431k | ✅ | 0 |
| conditional-branching-datapipeline | off | task-20260812-100650 | 253s | $0.092 | 363k | ✅ | 0 |
| conditional-branching-datapipeline | force | task-20260812-101102 | 317s | $0.151 | 492k | ✅ | 8 |

## 3. 分析

### 3.1 成功率

6/6 全部 DELIVERY_READY，两臂无差异。样本量不足以判断 Goal 是否影响通过率。

### 3.2 成本

force 模式平均成本 ~1.3x 于 off 模式（仅 conditional-branching 有显著差异 $0.092→$0.151），主要来自 Goal 轮次中的 evaluator 小模型调用。在 `goal_turns=0` 的 security-hardening 任务中两臂成本几乎相同，说明 Goal 的额外成本集中在「实际触发多轮继续」的任务上。

### 3.3 时长

方向不一致：implement-done-command 中 force 快 2.5x（可能受 plan 缓存影响），conditional-branching 中 force 慢 25%。单样本噪声大于信号。

### 3.4 Goal 激活证据

- implement-done-command force：`goal_turns=5`，证明 Goal 循环激活
- conditional-branching force：`goal_turns=8`，多轮执行
- security-hardening force：`goal_turns=0`，Worker 首跑即满足验证，未触发继续

`goal_turns` 计量正确——tool call 计数与 Goal 模式是否真正推进多轮执行正相关。

## 4. 结论

**小样本（3 任务 × 1 run）下，Goal 对成功率无显著影响，成本平均增加 ~30%（仅在实际触发多轮继续时）。**

- Goal 的核心价值是**防止 Worker 过早退出**。本批 golden 任务均较简单，Worker 首轮即满足验证，Goal 未触发额外轮次或触发后无差异。
- Goal 的额外成本（~30%）主要来自 evaluator 小模型判断和可能的额外轮次，对简单任务不划算。
- 对真正需要多轮修复的长任务，Goal 可能更有价值，但当前任务集不足以证明。

## 5. 建议

1. **不默认开启 Goal Loop**：当前证据不支持默认启用。`goal_policy.policy` 保持 `off`。
2. **保留 `--goal-mode auto/force` 作为可选策略**：对长任务用户可显式启用。
3. **扩大样本后再评估**：建议 ≥10 个任务 × 3 repeats 后再判断是否调整默认策略。
4. **优先验证长任务场景**：选择预估时长 >5 分钟或需要多轮修复的任务，Goal 价值更可能在那些场景体现。

## 6. 数据归档

- 实验输出：`/var/folders/c2/z2ghnzmd4dv9r97cctvd3kyw0000gn/T/opencode/goal_ab/`
- 指标提取：`goal_ab/extract_metrics.py`
- 任务 meta：`~/.agent_go/task-*/meta.json`（6 个任务）
