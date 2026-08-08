# ADR-005: 分层成本控制

## 状态

Accepted

## 决策

成本控制分为三层：

- L1：单次模型调用预算。
- L2：子任务跨重试累计预算。
- L3：任务级预算和并发 reservation。

## 原因

单次调用失控、验证 retry 空转和并发 wave 超支是不同问题，必须分别控制。

## 约束

- L1 默认可独立启用。
- L2/L3 在基线校准后启用。
- budget abort 不计入模型能力失败。
- metering 不可用时 strict/degrade fail-safe。
- `cost_censored` 不重复计费。

## 实现

`config.py`、`executor.py`、`pipeline.py`、`bench.py`。

### L1 单次调用硬上限

| 配置 | 来源 | 说明 |
|---|---|---|
| `per_subtask_budget_usd[difficulty]` | config | 按难度设定单次预算 |
| `l1_enabled` | config（默认 `false`，运行时 cold-start 自动启用） | 开关 |
| 注入方式 | `claude --max-budget-usd` | 通过 CLI 参数注入 |
| 未知 difficulty | 回退 `medium` | |

### L2 子任务跨重试累计

| 配置 | 来源 | 说明 |
|---|---|---|
| `per_subtask_budget_usd[difficulty] × subtask_multiplier` | config 计算 | 单子任务累计上限 |
| `subtask_multiplier` | config（默认 `2.5`） | 倍数 |
| `enabled` | config（默认 `false`） | 总开关；须 `eval cost-baseline` 校准后才开 |
| 触发动作 | 停止修复重试 | 子任务标 `failed`, `kill_reason=over_budget_l2` |

### L3 任务级预算 + 并发 reservation

#### 累计检查（每 wave 前）

| 配置 | 来源 | 说明 |
|---|---|---|
| `max_budget_usd` | config（默认 `0.50`） | 任务级总预算 |
| 动态默认 | `_dynamic_task_budget()` | `Σ(per_subtask_budget × multiplier)` 全量 plan 求和 |
| `enabled` | config（默认 `false`） | 总开关 |
| `budget_mode` | config | `strict` / `degrade` / `ignore` |
| 触发动作 | strict: blocked; degrade: 降级模型 | `kill_reason=over_budget_l3` |

#### 并发 reservation（仅 strict 模式，wave 启动前预扣）

**问题**：L3 只在 wave 开始前检查一次累计成本。并发 wave 内多个子任务同时启动，实际成本可能在 wave 中途超过预算。

**解决**：在 wave 调度前，为每个子任务预扣其 L2 上限作为 reservation。

```
reservation_per_subtask = per_subtask_budget_usd[difficulty] × subtask_multiplier
```

**执行流程**（`pipeline.py` wave 调度前）：

1. **Gate**: `cost_control.enabled=True` AND `budget_mode="strict"` AND wave 非空
2. **Pool**: `pool = max_budget_usd OR _dynamic_task_budget(confirmed)`（全量 plan 求和）
3. **Available**: `available = pool - _meter_total_cost()`（从 `metering.jsonl` 排除 `cost_censored` 后累计）
4. **逐子任务扣减**:
   - `need = reservation_per_subtask`
   - `need ≤ available` → admit，`available -= need`
   - `need > available` → blocked
5. **Blocked 子任务**: `status=blocked`, `kill_reason=over_budget_l3`, `blocked_by=["cost_control"]`, 加入 `completed_ids`（不再重新调度）
6. **未知配置**（`need=0`）: 不阻断，直接 admit

**reservation vs L3 的关系**：L3 累计检查在 reservation 之前运行，使用实际计量成本。reservation 是同一 wave 内并发启动的安全网——预扣的额度是上限估计，实际成本可能更低。下一 wave 的 L3 检查会重新基于实际计量复核。

**设计意图**：reservation 只针对有明确 `per_subtask_budget_usd` 配置的任务。未知 difficulty 或未配置预算的子任务返回 `reservation=0`（不阻断），避免误杀。

### budget_mode 三态

| 模式 | L1 | L2 | L3 累计 | reservation | 行为 |
|---|---|---|---|---|---|
| `strict` | ✅ | ✅ | ✅ | ✅ | 超预算 → blocked |
| `degrade` | ✅ | ✅ | ✅ | ❌ | 超预算 → 切 `worker_models_degrades` 降级模型，`degraded=True`，`max_retries→1` |
| `ignore` | ✅ | ✅ | ❌ | ❌ | 关 L3，仅 L1/L2 |

### fail-safe

- **metering 不可用**: strict/degrade 模式停止调度（不能把成本当零继续）
- **cost_censored 不重复计费**: `_meter_total_cost()` 排除 `cost_censored` 事件
- **budget abort 不计入模型能力失败分母**: `kill_reason=over_budget_*` 排除

### 已知问题（已修复）

- `_dynamic_task_budget` 曾传入 `remaining`（收缩集合）而非 `confirmed`（全量 plan），导致预算池单调递减。已在 s12-code-review 后修复，现传 `confirmed`。
