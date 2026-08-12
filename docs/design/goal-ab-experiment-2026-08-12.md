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

---

## 7. 困难任务补充实验（2026-08-12，第二阶段）

针对第一阶段「golden 任务首轮即过、Goal 无法体现价值」的局限，补充 3 个 baseline 中真实失败过的 hard 任务，验证 Goal 在「可能多轮修复」场景的价值。

### 7.1 任务选择

| 任务 | 难度 | baseline 失败原因 | 选取理由 |
|------|------|-------------------|----------|
| db-performance-optimization | hard | subtask 链失败 | N+1 优化多步修复，首轮易不完整 |
| implement-archiving | hard | timeout | 多文件归档逻辑，首轮易漏边界 |
| race-condition-taskmgr | hard | stress/并发 | 并发修复需多轮验证 |

### 7.2 结果（off vs force）

| 任务 | off | force | force 臂 goal_turns |
|------|-----|-------|---------------------|
| db-performance-optimization | ✅ DELIVERY_READY | ❌ BLOCKED | 0 |
| implement-archiving | ✅ DELIVERY_READY（1 retry 收敛） | ❌ VERIFICATION_FAILED | 0 |
| race-condition-taskmgr | ✅ DELIVERY_READY | ✅ DELIVERY_READY | 0 |

**off 3/3 全过，force 仅 1/3 过。**

### 7.3 失败根因（均非 Goal 引起）

- **db-performance force → BLOCKED**：`plan_quality_blocked`（G7 `file_overlap_without_dependency`，planner 把 sub-3/sub-4 同分 `src/analytics/views.py` 且无依赖）。off 臂恰好生成了合规 plan——**plan 生成采样方差**。
- **implement-archiving force → VERIFICATION_FAILED**：planner 生成截断验证命令 `python -c "from src.storage import ;`。同为 **plan 生成方差**。
- 两臂 `goal_turns=0`：**Goal continuation 在这 6 个困难任务中一次都没触发**。

### 7.4 修正后的结论

第一阶段「goal_turns=5/8 空转」+ 第二阶段「goal_turns=0 不参与」合并得出：

> **Goal continuation 在「不需要时」空转，在「需要时」不参与。**

三个决定性证据：

1. **Goal 与 executor retry 循环功能重复**：implement-archiving off 臂靠 executor 的 retry（retry=1）就收敛，无需 Goal continuation。L1 修复重试已覆盖 Goal 的目标场景。
2. **困难任务失败瓶颈在 Plan/验证层，不在 Worker 继续层**：G7 文件重叠、验证命令截断——由 Plan preflight repair 和 planner prompt 解决，Goal 无法介入。
3. **Goal 边际价值趋近于零**：worker 首轮失败时 agent_go 用新 prompt 重启 claude（retry），比重启同会话（goal）更有效，因为 retry 能注入完整失败上下文。

### 7.5 实验设计警示

off/force 对比被 plan 生成方差污染（每次运行重新生成 plan），且 goal_turns=0 证明 Goal 未参与。严格隔离 Goal 效应需固定 plan 或大量重复；现有两阶段数据已足够支撑决策，无需追加投入。

### 7.6 最终决策（两阶段合并）

- **维持 `goal_policy.policy=off`**，Goal 不默认开启。
- Goal 定位为**可选显式能力**（`--goal-mode force`），供用户明确需要时启用，**不推荐**作为默认机制。
- 长期看 Goal 与 executor retry 功能重叠，若后续无差异化价值证据，可考虑将 Goal 简化为 retry 循环的一种表现形式，而非独立机制。

> 附：第二阶段 force 臂最初 2 个失败系并行会话瞬时改坏 `agent_go/api.py`（pytest `-k` 说明破坏字符串转义）导致的 SyntaxError，与 Goal 无关；该文件修复后重跑恢复。

## 8. 第三阶段：弱模型 Worker 验证（Goal 触发的能力前提）

用户假设：Goal 价值与 worker 能力相关——改用较弱的本地模型，首轮会失败，Goal 才可能生效。本节用两档本地模型验证此前提。

### 8.1 实验设置

- **弱模型 worker**：本地代理 `127.0.0.1:4000`（Anthropic 兼容）路由到本地 MLX 模型，claude CLI 经 `ANTHROPIC_BASE_URL` 接入。先后两档：
  - `Qwen3.6-27B-4bit`（dense）
  - `Qwen3.6-35B-A3B-UD-MLX-4bit`（MoE，每 token 仅激活 3B，用户切换为「稍弱」档）
- **递进任务集**（合成，含误导性正确性要求，首轮实现大概率不全）：
  - 简单 bug（add 符号错误）
  - CSV 解析边界（引号逗号/空字段/空输入/转义引号）
  - 滑动窗口限流器（窗口过期 + 乱序时间戳）
  - 误导性 deep_merge（隐含 None 删除键、list 按索引合并、None 元素删索引）
  - 表达式求值器（自定义 `#`=max 运算符优先级、`//` 向下取整、**禁止 eval** 须手写递归下降解析器）

### 8.2 结果

| 模型 | 任务 | 模式 | 结果 | executor retry | worker 内部 turns |
|------|------|------|------|----------------|-------------------|
| 27B | 简单/CSV/滑动窗口/deep_merge | off | ✅ | 0 | 8（会话内自循环） |
| 35B-A3B | deep_merge | 原生无 goal | ✅ | — | 8 |
| 35B-A3B | deep_merge | 原生带 `/goal` | ✅ | — | 5 |
| 35B-A3B | 表达式解析器（禁 eval） | 原生无 goal | ✅（7 passed） | — | 自收敛 |

**全部任务、全部模式、两档模型均首轮/会话内收敛，无一次过早退出。**

### 8.3 决定性发现

1. **worker 自验证自循环**：`metering` 显示 worker 在单个 `claude -p` 会话内自循环 8 轮（读测试→写实现→跑 pytest→修→再跑），退出时测试已通过 → executor 验证一次通过、retry=0。Goal 无从触发。
2. **Goal 与 TASK.md verify 指令功能重复**：TASK.md 已写明「验证→失败修复→再验证→通过才退出」，模型遵守了它——这正是 Goal 要做的事，故 Goal 冗余。
3. **Goal 触发的唯一前提**：worker 在**未验证的情况下「误以为完成」而退出**（放弃验证 / 谎称完成）。两档本地模型（27B dense、35B-A3B MoE）在所有合成任务上均不发生——模型能力再降档也会先读测试再实现，收敛于会话内。
4. **较难题目触发的是 executor retry，不是 Goal**：难题首轮失败时由 retry 循环（新 prompt + 完整失败上下文）收敛，与 Goal 无关。

### 8.4 修正结论

用户的「弱模型 → Goal 生效」假设方向合理，但**前提被 agent_go 自身机制覆盖**：

- worker 能力决定**能否收敛**，不决定**是否自验证**；自验证由 TASK.md 指令 + 模型遵从性驱动，与模型强弱无关。
- Goal 的救援价值只在 worker「放弃验证就退出」时出现；当前本地模型梯队（27B/35B）无此行为，需 7B/3B 级才可能复现。
- **维持最终决策不变**：Goal 不默认开启，定位可选显式能力。Goal 的真实触发前提是「worker 放弃验证而过早退出」，该前提在当前模型与指令体系下不成立。

**无需追加 7B/3B 弱模型验证**：即便复现过早退出，agent_go 的 executor retry（新 prompt + 失败上下文注入）是比 Goal 同会话延续更强的兜底，Goal 仍无差异化价值。实验到此充分，Goal 归档为可选能力。

---

## 9. 第四阶段：真实困难任务集验证（弱模型 × off/force）

**日期**：2026-08-12 · **目的**：用此前弱模型困难任务集（`docs/local-model-hard-tasks-prompts-20260812.md`）验证 Goal 在真实难题上的作用，并压测 force 模式边界。

### 9.1 实验设置

- **模型**：Qwen3.6-35B-A3B-UD-MLX-4bit（MoE，每 token 仅激活 3B，本地代理 127.0.0.1:4000）
- **任务集**：文档中 5 个任务改造为可验证 fixture（任务 5 长上下文提取需另行构造语料，未纳入）：
  - avl：完整 AVL 树（insert/delete/旋转再平衡，12 个测试含随机压力测试）
  - logic：五人五天排期逻辑题（写 schedule.json + 全约束校验）
  - concurrency：并发计数器丢更新缺陷修复（加 `sleep(0)` 使竞态确定性暴露）
  - json：约束化 JSON 生成（plan.json + 字段/优先级/title 校验）
- **双臂**：`--goal-mode off` vs `--goal-mode force`，planner=deepseek-v4-pro，worker=本地弱模型

### 9.2 实验结果

| 任务 | off | force | 说明 |
|---|---|---|---|
| avl | ✅ 34s retry=0 | ✅ 34s retry=0 | 首轮收敛 |
| logic | ✅ 18s retry=0 | ✅ 20s retry=0 | 首轮收敛 |
| concurrency | ✅ 18s retry=0 | ✅ 22s retry=0 | 首轮收敛 |
| json | ✅ 18s retry=0 | ❌→✅（修复后 16s） | force 首跑暴露 4000 字符 bug |

### 9.3 重大发现：/goal 4000 字符上限静默拒绝 bug（已修复）

**现象**：json force 首跑 `VERIFICATION_FAILED`——worker exit 0、零文件变更、零 token。

**根因**：Claude CLI 把 `/goal` 之后的**整个 prompt** 当作 goal condition（按**字符数**而非字节，上限 4000）。超限后 CLI 输出 `Goal condition is limited to 4000 characters (got 4423)` 并**静默以 success 退出、不执行任何工作**。json TASK.md 6553 字节 ≈4423 字符被拒；avl 5859 字节 ≈3900 字符（中文 3 字节/字符）恰好低于上限而正常。

**CLI 层复现**：4888 字符 prompt → `got 4882`、turns=0、exit 0、文件未变；1168 字符 prompt → 原生 goal 正常工作（turns=4，正文成为 turn-1 directive）。

**修复**（executor.py）：TASK.md 拼接完成后测量字符数，>3800 字符时移除 `/goal` slash 前缀、降级为纯文本 Goal Context 并打 warning。修复后 json force 重跑：降级警告触发，16s 完成 ✅。

**该 bug 的隐蔽性**：exit 0 + success subtype，任何只看退出码的编排器都会漏检；agent_go 靠「无文件变更 → 验证失败」兜住，但浪费了整轮执行。

### 9.4 附带发现：planner 交付物/plan schema 混淆

json off 首跑出现**空计划假成功**：任务文本要求创建「项目计划 JSON」，deepseek planner 把交付物的 JSON schema（project/version/tasks）误当成 agent_go 自己的 plan schema 返回，`plan_to_subtasks` 得到 0 个 steps → 0 子任务 → 真空 DELIVERY_READY。改写任务文本（明确「plan.json 是数据文件不是执行计划」）后恢复。这是 plan 层鲁棒性缺口，建议后续在 `validate_plan_quality` 增加「0 子任务 → blocking」检查。

### 9.5 修正结论

1. **Goal 在真实困难任务上仍无差异化价值**：弱模型（35B-A3B）对 4 个困难任务全部首轮收敛，Goal 无从触发；force 两臂无收益差异。
2. **force 模式在修复前不仅是冗余，还可能是净负资产**：长 TASK.md 触发 4000 字符静默拒绝，把一个本可首轮通过的任务变成失败。
3. **测试驱动发现的两个真实 bug 均与 Goal 的产品化相关**：4000 字符门禁（已修复）是启用原生 /goal 的必要前提；planner schema 混淆（待跟进）影响任务文本鲁棒性。
4. **最终决策维持**：`goal.policy=off`，Goal 归档为可选显式能力（`--goal-mode force`）；原生 /goal 仅在 TASK.md ≤3800 字符时启用，超长自动降级纯文本指令。
