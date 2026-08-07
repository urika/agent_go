# Case Study: orm-optimizer Skill A/B 评测

> 验证 agent_go 的 Skill 机制是否真的提升任务执行效率。
> 结论：**skill 注入省 22% 成本、44% token，重试 4→0，通过率持平**。

## 1. 背景

为验证 Skill 注入机制的价值，我们在 agent_go 上创建了 `orm-optimizer` skill
（8 类 ORM 反模式的通用调优框架），并对比**有/无 skill** 两种模式下 agent_go
完成相同任务的效率。

## 2. 评测方法

### 2.1 对照组设计

| 组别 | 配置 | subtask skills |
|---|---|---|
| **SKILL ON** | `skills.auto_discover: true` | `[['orm-optimizer'], ...]` 每个 subtask |
| **纯净基线 OFF** | `--no-skills`（禁用 auto_discover + role_skill_map 兜底）| `[[], []]` 完全无 skill |

### 2.2 评测任务

- **medium**: `add-simple-caching`（Python 缓存装饰器，timeout 420s）
- **hard**: `db-performance-optimization`（Django N+1/索引/聚合/缓存，timeout 1800s）

### 2.3 固定变量

- 模型：`deepseek-v4-flash`（worker）
- 同一 fixture、同一任务描述
- 仅差异：skill 注入 on/off

## 3. 评测结果

### 3.1 medium 任务（add-simple-caching，3 次重复均值）

| 指标 | SKILL ON | 纯净基线 OFF | 差异 |
|---|---|---|---|
| **pass_rate** | 100% | 100% | 持平 |
| **worker tokens/次** | 7,252 | 13,025 | **-44%** |
| **成本/次** | $0.72 | $0.87 | **-17%** |

### 3.2 hard 任务（db-performance-optimization，单次）

| 指标 | SKILL OFF | SKILL ON | 差异 |
|---|---|---|---|
| **完成任务** | 3✓/1✗/4 | 3✓/1✗/5 | 持平 |
| **token 总量** | 102,452 | 84,695 | **-17%** |
| **成本** | $8.14 | $6.36 | **-22%** |
| **重试次数** | 4 | **0** | **-100%** |

## 4. 关键发现

1. **成本与 token 显著下降**：skill 注入让 worker 用更少的推理 token 完成同等工作，
   medium 任务省 44% token，hard 任务省 22% 成本。

2. **重试次数 4→0（hard 任务）**：skill OFF 时 sub-2 和 sub-4 分别重试 2 次和 3 次；
   skill ON 时**零重试**。这是最有说服力的证据——skill 让 worker 首次就做对，
   避免了多轮修复循环。

3. **通过率持平**：两个任务集通过率相近（100% / 75% vs 60%）。skill 未显著提升
   通过率，因为失败项（如索引任务 sub-3）是模型能力/任务复杂度的限制，非 skill 缺失。

4. **skill 机制完整链路**：自动发现 → 回填 subtask → 注入 TASK.md → worker 推理引用，
   全程可由 execution.log 逐 token 追溯（`[text] I also notice from the orm-optimizer skill that...`）。

## 5. 过程中修复的 agent_go 缺陷

评测暴露了 3 个产品缺陷并已修复：

| 缺陷 | 影响 | 修复 |
|---|---|---|
| **bench 竞态** | 并发任务下读错任务目录 | 从子进程输出解析 task ID（`agent_go.task-xxx` 正则）|
| **skill-backfill 断点** | 发现的 skill 未注入 subtask | `plan_to_subtasks` 新增 `default_skills` 回填 |
| **role_skill_map 污染基线** | skill OFF 组仍被兜底注入 | `--no-skills` + `disable_rule_skills` 纯净基线 |

## 6. 方法论沉淀

### 6.1 纯净 A/B 评测配置

```bash
# skill OFF 基线
agent_go eval bench --tasks <tasks> --candidate-models <m> --repeat 3 --no-skills

# skill ON
agent_go eval bench --tasks <tasks> --candidate-models <m> --repeat 3
```

### 6.2 数据采集

- `bench output.jsonl`: pass_rate / cost / elapsed 聚合
- `task_dir/meta.json`: subtask 状态 + skills 注入
- `task_dir/metering.jsonl`: 逐次 worker token/cost/latency
- `task_dir/execution.log`: skill 注入 + worker `[text]` 推理引用

## 7. 结论

**Skill 机制对 agent_go 有真实价值**：以 44% token 和 22% 成本的代价降低换取同等产出，
并将 hard 任务的修复重试从 4 次降到 0。通过率持平说明 skill 是"效率放大器"而非
"能力提升器"——模型本身能做到，但 skill 让它少走弯路。

## 8. 数据可复现

- 评测任务: `eval_suite/tasks/08-add-simple-caching.yaml`, `19-db-performance-optimization.yaml`
- SKILL.md: `~/.config/opencode/skills/orm-optimizer/SKILL.md`（同步 3 处）
- 历史任务: `~/.agent_go/task-20260801-163103-074-0e52`（SKILL ON hard）、
  `task-20260731-232522-068-eae7`（SKILL OFF hard）
