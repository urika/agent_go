# Split Design Benchmark — 跨 Agent 拆分设计对比报告

> 目标：针对「子任务编排 / 拆分设计」场景，用同一 prompt 分别让 Claude Code 与
> OpenCode 输出结构化拆分设计，对照 agent_go 的拆分算法（G5/G6/G7）合理性。
> 全部使用 **deepseek-v4-flash**（agent_go 同款模型），保证同模型对比公平。

日期：2026-08-10
测试集：eval_suite/split_design/（6 任务 × 2 agent = 12 次调用）
脚本：tools/bench_split_design.py（`--agent claude|opencode|all [--task <id>]`）

## 一、结论速览

| # | 任务 | 难度 | 文件面 | 期望 | Claude Code | OpenCode | 一致性 |
|---|------|------|--------|------|-------------|----------|--------|
| 1 | add-format-helper | easy | 1 文件 | 不拆 | 1 | 1 | ✅ 一致 |
| 2 | fix-missing-default | easy | 1 文件 | 不拆 | 1 | 1 | ✅ 一致 |
| 3 | add-simple-caching | medium | 2 文件(耦合) | 1-2 | 1 | 1 | ✅ 一致 |
| 4 | implement-done-command | medium | 2 文件(耦合) | 1-2 | 1 | 1 | ✅ 一致 |
| 5 | security-hardening-taskmgr | hard | 4 文件(独立) | 必须拆 | 3 | 2 | ✅ 一致(均拆) |
| 6 | conditional-branching-datapipeline | hard | 3 文件(跨模块) | 必须拆 | 3 | 2 | ✅ 一致(均拆) |

**12/12 次调用全部返回合法 JSON，且全部命中期望**。两个 agent 在「何时拆、拆几个」
上的判断高度一致——这验证了拆分判据本身是**可被独立复现的共识**。

## 二、关键发现

### 发现 1：文件耦合是「不拆」的首要判据（与 agent_go G6 一致）

4 个 ≤2 文件任务，两侧**全部**判 1 个子任务，即使文件有 2 个：
- add-simple-caching：storage.py 依赖 utils.py 的装饰器 → 强耦合
- implement-done-command：cli.py 调用 storage.py 的新 API → 强耦合

两侧 rationale 几乎一致：**「改动面 ≤2 文件优先 1 任务；拆开需串行依赖，无并行收益
反而增加 merge 开销」**。这正是 agent_go G6 升级（over_decomposition blocking）的设计
依据——本基准用独立 Agent 复现了同一结论。

### 发现 2：文件互斥是「拆」的前置约束（与 agent_go G7 一致）

2 个 hard 任务两侧都拆分了，且都严格遵守文件互斥：
- security-hardening：claude 把 cli.py 两项改动合并到同一子任务（「同一文件不可分给
  两个子任务」），opencode 同理；storage.py+models.py 合并为数据层子任务
- conditional-branching：两侧都让 pipeline.py 独占一个子任务，transform 与测试
  独立成另一个

与 agent_go G7（file_overlap_without_dependency → blocking）结论一致。

### 发现 3：拆分数量差异——claude 略细、opencode 略粗

hard 任务拆分粒度：claude 给 3、opencode 给 2。差异原因：
- claude 倾向把「测试文件」独立成第 3 个子任务（security 的 test_security.py、
  branching 的 test_branching.py）
- opencode 倾向把测试合并进最后一个实现子任务（sub-2 含 transform + tests）

两者都在 2-4 的合理区间，且都让测试依赖实现子任务（deps 指向实现）。无本质分歧。

### 发现 4：小改动的「单步原子性」得到两 agent 确认

最值得注意：**implement-done-command 两侧都判 1 个子任务**。而 agent_go 的真实
bench 中该任务曾被 LLM 拆成 2 个，导致 sub-2 越界改 3 个文件交叉污染 →
VERIFICATION_FAILED（task-20260809-123021-784-042c）。本基准独立证明：该任务的正确
粒度就是 1 个——agent_go 的 G6/G7 拦截方向正确，能阻止这类失败。

## 三、对 agent_go 的参考结论

1. **拆分判据已验证**：文件作用域互斥（拆的前提）+ 文件耦合（不拆的理由）+ 小改动
   不拆（≤2 文件单任务）——与主流 agent 的独立判断一致，agent_go 无需改变判据。
2. **G6 over_decomposition 阻断有效**：≤2 文件但 ≥3 子任务的场景，主流 agent 明确
   判「1 个任务」，agent_go 将其升级为 blocking 是正确的护栏。
3. **G7 file_overlap 阻断有效**：主流 agent 用「同一文件合并进同一子任务」规避重叠，
   agent_go 用「无依赖重叠 → 阻断」强制纠正，效果等价。
4. **粒度差异可接受**：claude 3 vs opencode 2 的差异在 2-4 合理区间内，不需要追求
   统一数量——比数量更重要的是「文件互斥 + 依赖表达 + 小改动不拆」这三个约束。

## 四、方法局限与后续

- 本次只评估「拆分设计」，未执行实际代码（低成本，12 次调用）。
- 单一模型（deepseek-v4-flash）；如需更强结论可扩模型矩阵。
- 后续可将该基准接入 eval bench（`eval bench --tasks eval_suite/split_design/`），
  用两 agent 的拆分结果作为 agent_go 拆分质量的对照基线。

## 五、原始数据

- 每个结果：eval_suite/split_design/results/{task_id}/{agent}.json（原始输出）
  + {agent}.parsed.json（解析后的 subtasks 结构）
- 汇总：eval_suite/split_design/results/summary.json
- Prompt 模板：eval_suite/split_design/prompts/split_design_prompt.md
- 测试集说明：eval_suite/split_design/manifest.md
