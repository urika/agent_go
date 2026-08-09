# ADR-008: 数据驱动 timeout 设置模型（实测 P95 × 余量）

## 状态

Accepted

## 决策

任务墙钟 timeout 采用三层融合：

```
effective_timeout = max(YAML 下限, 难度公式, 实测 P95 × 余量)
```

- **YAML 下限**：任务显式配置的 `timeout`，作为下限，不缩短既有配置。
- **难度公式**：难度基准 × 难度 mult + 缓冲（`_DIFFICULTY_MULT = {easy:1, med:1.5, hard:2.5}`）——无历史数据时的兜底。
- **实测 P95 × 余量**：从 bench results 累计该任务实测 `elapsed_sec`，取 **P95 × 1.3**（high_variance 任务 **×1.5**）。

## 原因

任务 YAML 的 `timeout` 是拍脑袋硬编码（180~3600s），实测发现多个任务余量不足：

- **db-performance**：实测 2182s，YAML 2232s → 仅 50s 余量，收尾稍慢就 cleanup_race（工作已交付但进程被墙钟杀）；
- **add-caching-layer**：实测 2207s，YAML 1200s → 靠动态公式兜底，但没人知道真实耗时。

timeout 不从实测耗时推导，就无法区分"任务本来就慢"和"任务被截断"——前者需要放宽，后者才是失败信号。数据驱动让 timeout 随实测自动收敛，不再靠人工校准。

## 约束

- **样本 <3 时 P95 不可靠** → 返回 None，回退难度公式（首跑/样本不足）。
- **P95 而非均值/最大值**：
  - `mean` 被快速 run 拉低，不覆盖慢 run；
  - `max` 被一次异常长 run 拉爆，timeout 虚高；
  - `P95` 代表"最慢的典型 run"，配合余量覆盖波动。
- **high_variance 任务余量 1.5**（如 add-caching-layer）——方差任务需要更多余量防极端波动截断。
- **timeout 随批次收敛**：同批内前几次 run 为后续 run 提供实测数据（`results_path` 是当前 batch 输出，边跑边读）；跨批继承（results 持续追加）。

## 实现

`agent_go/bench.py`：`_measure_elapsed_p95` + `_dynamic_timeout`。

### `_measure_elapsed_p95`

| 方面 | 说明 |
|------|------|
| 输入 | `task_id` + `results_path`（results.jsonl）|
| 采集 | 该 `task_id` 的历史 `elapsed_sec` |
| 样本门槛 | ≥3，否则返回 None（P95 不可靠，回退难度公式）|
| 计算 | 排序 → `idx = int(0.95 × n)` → 返回 P95 |

### `_dynamic_timeout` 三层融合

| 层 | 来源 | 优先级 |
|----|------|--------|
| YAML `timeout` | 任务配置 | 下限，`max` 兜底不缩短 |
| 难度公式 | `难度基准 × mult + 缓冲`；多子任务取 `子任务数 × 基准 + 缓冲` | 无历史兜底 |
| 实测 P95 × 余量 | results 累计 | 数据驱动，取 `max` |

### 余量

| 任务 | 余量 | 原因 |
|------|------|------|
| 默认 | **1.3** | 覆盖典型波动 |
| `high_variance=true` | **1.5** | 方差任务需要更多余量 |

### 收敛过程

```
批次开始 → 首跑（无历史，P95=None）→ 难度公式
  → 完成写 elapsed_sec 到 results.jsonl
  → 后续 run → P95 累计 → timeout = max(YAML, 公式, P95 × 余量)
  → 跨批继承 → timeout 收敛到任务真实耗时 × 余量
```

## 实测验证

| 任务 | P95 | 动态 timeout | 效果 |
|------|-----|-------------|------|
| db-performance | 2181s | **2836s**（×1.3）| 覆盖实测 2182 + 30%，不再 cleanup_race |
| add-caching-layer | 2207s | **3310s**（×1.5）| high_variance 更多余量 |

## 相关决策

- 补充 [ADR-006（bench 进程隔离与批次治理）](ADR-006-bench-isolation-and-batches.md) 与 S12-P2 G6（按难度 timeout）。
- `retry_timeout`（修复重试）是独立难度分档（easy 600 / medium 900 / hard 1500，S12 建议 #2），本 ADR 只管**任务级墙钟**。
- 度量删失校正（排除被截断 run 的右删失）见 `compute_cost_baseline`，后续可将 P95 纳入删失口径。
