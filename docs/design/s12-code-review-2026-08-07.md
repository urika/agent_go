# S12 提交 Code Review

> 日期：2026-08-07
> 审查范围：S12 timeout/kill/成本控制优化的 5 个提交（P0 初始 / P0 完成 / P1 / P2 / P3）
> 代码基线：`feat/s10-bench-v2` 分支（HEAD = `ad59d91`）
> 关联文档：[timeout-kill-strategy-2026-08-06.md](timeout-kill-strategy-2026-08-06.md)（设计 G1–G8）、[bench-metric-validity-2026-08-06.md](bench-metric-validity-2026-08-06.md)（度量诊断）

## 审查范围

| 提交 | 主题 | 主要改动 |
|------|------|---------|
| `4f9d428` | S12-P0 度量修复 | `bench.py`：kill_reason 分类 + `_collect_result` cleanup_race + `all([])` 陷阱 |
| `2b5444b` | S12-P0 完成 | kill_reason 运行时贯穿（`subtask`/`executor`/`pipeline`）+ eval 修正通过率列 |
| `78b1253` | S12-P1 成本控制 | per-task `--budget`/`--budget-mode` 三态 + 动态默认预算 + L3 降级(degrade) + G8 kill_reason 感知 |
| `90115fc` | S12-P2 降级+规划守卫 | `worker_models_degrades` 对称降级表 + G5 欠分解检测 + bench `_dynamic_timeout` 按难度 |
| `ad59d91` | S12-P3 stuck 误杀规避 | 多维活性（S2 文件 + S3 进程树 CPU）+ grace 复检门（`STUCK_GRACE_SEC=120`） |

## 结论摘要

**P0 度量修复质量高**：kill_reason 贯穿链路清晰、`all([])` 陷阱与 cleanup_race 修正根因准确、测试扎实。设计文档（G1–G8）是本次工作最强的一环。

**P1/P3 运行时控制有两处实质性 bug，且都落在测试盲区**：

| # | 严重度 | 位置 | 问题 |
|---|--------|------|------|
| H1 | 🔴 HIGH | `pipeline.py:294-356` | 降级安全阀被 L3 在同一轮循环里重新置位 → "回退 stop" 永不生效 |
| H2 | 🔴 HIGH | `subtask.py:154` `_process_cpu_ticks` | S3 进程树 CPU 在 macOS 恒返回 0（`ps` 时间格式非数字）→ 静默失效 |
| M1 | 🟡 MED | `pipeline.py:340` | 动态任务预算用 `remaining` 而非 `confirmed` → 阈值随完成缩小 |
| M2 | 🟡 MED | `subtask.py:142` `_file_activity_snapshot` | S2 缺 mtime（设计要求），实现弱于设计 |
| L1 | 🟢 LOW | `executor.py:913` | G8 的 `cleanup_race` 分支是死代码 + 测试假阳性 |
| L2 | 🟢 LOW | `tests/test_cost_control.py` | 集成路径无端到端测试，H1/M1 因此漏网 |
| L3 | 🟢 LOW | `cli.py` | `--budget` 与 `--max-cost` 语义重复 |

---

## 🔴 H1：降级安全阀被 L3 同轮重新置位

**位置**：`agent_go/pipeline.py:294-300`（安全阀）vs `agent_go/pipeline.py:344-356`（L3 检查）

**问题**：安全阀与 L3 检查在 `while remaining` 循环的**同一轮**里先后执行，互相抵消。

```python
while remaining:
    # ① 安全阀（line 294）：streak≥3 → config["_degraded"]=False, streak=0  ← "回退 stop"
    if config.get("_degraded"):
        if int(config.get("_degrade_fail_streak",0)) >= 3:
            config["_degraded"] = False
            config["_degrade_fail_streak"] = 0
    ...
    # ② L3 检查（line 344）：_spent≥_max_budget 仍成立（成本只增不减）
    #    budget_mode 仍是 "degrade"（安全阀没改它）→ config["_degraded"]=True  ← 重新武装
    if _spent >= _max_budget:
        if _budget_mode == "degrade":
            config["_degraded"] = True   # line 355：把刚才置 False 的又改回 True
```

**失败场景**：
1. `--budget X --budget-mode degrade` → 预算超限 → 进降级
2. 降级模型做不动 hard 任务 → 连续 3 个降级子任务 verify 失败 → `streak=3`
3. 安全阀 trip：`_degraded=False`、`streak=0`、日志打印"回退 stop"
4. **同一轮** L3 检查：`_spent` 仍 ≥ `_max_budget`（成本不会下降）→ `budget_mode` 仍 `"degrade"` → `_degraded=True`
5. 下一波继续降级，计数器从 0 重新攒 → 回到第 2 步

**后果**：设计文档「降级质量门（review 修订）」明确要求的 _"降级后连续 N 个子任务 verify 失败 → 自动回退 stop（不再降级烧钱）"_ **根本不会发生**。这恰恰是安全阀本该防止的失败模式——"延长死亡时间而非保产出"。

**爆炸半径**：有界。每个降级子任务仍受 `executor.py:671` `max_retries=min(max_retries,1)` 与 L1/L2 约束，不会无限烧；但违背了"回退 stop"的明确意图，且让日志/文档承诺的护栏形同虚设。

**修复方向**（二选一）：
- **哨兵法（推荐）**：安全阀置 `config["_degrade_aborted"]=True`；L3 改为 `if _budget_mode=="degrade" and not config.get("_degrade_aborted"):`，已 abort 则走 `else`（stop）分支。
- **改 budget_mode**：安全阀直接 `config["cost_control"]["budget_mode"]="stop"`。

---

## 🔴 H2：S3 进程树 CPU 活性在 macOS 恒返回 0

**位置**：`agent_go/subtask.py:154` `_process_cpu_ticks`

**问题**：函数调 `ps -o pid=,ppid=,utime=,stime= -A`，用 `float(_parts[2])` 解析 utime/stime。但 **macOS 上这两列是 `M:SS.cc` 时间格式**（本机实测 `2:33.26`、`11:26.55`），`float("2:33.26")` 抛 `ValueError` → 内层 `except ValueError: continue` → **每一行都被跳过** → `_rows` 为空 → 返回 `int(0.0*100)=0`。

```
失败时返回的不是 -1（"测量失败"），而是 0（"无 CPU 活动"）——
与"进程真的空闲"不可区分。于是 _cpu_active = (0 > 0) = False，永远 False。
```

**后果**：三信号活性（S1 事件 ∨ S2 文件 ∨ S3 CPU）在开发平台（darwin）上退化为两信号，**S3 是死代码**。设计文档第六节/review 自己标注了 _"`ps` 跨平台过乐观、macOS/Linux 语法不通用"_，但实现没处理 macOS 时间格式。

**为何测试没抓到**：`tests/test_subtask.py:900` 把 `subprocess.run`（ps 调用）mock 成 `MagicMock(stdout="")` → 解析出空 → 与真实 macOS 行为一致地返回 0，测试反而全绿。即测试与 bug 产物相同，掩盖了问题。

**修复方向**：
- 解析 `M:SS`/`H:MM:SS` 格式（按 `:` 切分加权求和：`h*3600 + m*60 + s`），Linux 上 utime/stime 是纯 clock ticks，`float()` 仍可用——两种格式都兼容；
- 或解析失败时返回 -1（让 `_cpu_active = _recheck_cpu > _base_cpu` 在基线 -1 时自然视为"测不到=不杀"，与"无活性"区分开）；
- 文档明确 macOS/Linux 支持矩阵（Linux 正确，macOS 需时间格式解析）。

---

## 🟡 M1：动态任务预算用 `remaining` 而非 `confirmed`

**位置**：`agent_go/pipeline.py:340`

**问题**：

```python
_max_budget = _cc_cfg.get("max_budget_usd") or 0.0
if not _max_budget:
    _max_budget = _dynamic_task_budget(_cc_cfg, remaining)   # ← 用 remaining（剩余）
```

`remaining` 每完成一波就缩短，于是**预算上限单调下降**，而 `_spent` 单调上升 → 任务越接近完成越容易误触 L3。一个本在预算内的 80% 完成任务，可能因为分母缩到只剩 2 个子任务而突然熔断/降级。

设计 G3 与函数 docstring 都写的是任务级（`Σ per_subtask_budget × mult × len(subtasks)` = 全量计划），意图是 `confirmed`，调用点却传了 `remaining`。

**潜伏性**：CLI 路径（`--budget`/`--max-cost`）总会设 `max_budget_usd`，动态分支只在"config 文件开 `cost_control.enabled=True` 但不设 `max_budget_usd`"时才走到。`test_cost_control.py` 的 `test_dynamic_task_budget_*` 只测纯函数且传入全量列表，所以测试通过但集成调用是错的。

**修复**：改传 `confirmed`（任务级预算应在规划时一次性确定，不随波次变动）。

---

## 🟡 M2：S2 文件活性快照缺 mtime，实现弱于设计

**位置**：`agent_go/subtask.py:142` `_file_activity_snapshot`

**问题**：设计第八节明确写 _"S2 = `git status --porcelain` **+ mtime 快照**"_，实现只做了 `git status --porcelain`。`git status` 反映的是 dirty/untracked 文件的**集合（路径列表）**，不反映单个文件持续被写。

一个在 grace 进入前就已 dirty 的文件，grace 期间继续被改写但不产生新文件 → 快照字符串不变 → S2 报"无活性"。

**影响**：增量产出产物的 build（新文件不断出现）能被 S2 抓到；但"改写单个既有文件的长操作" + H2（S3 在 macOS 失效）= 多维活性可能同时全死 → 误杀。这削弱了 S12-P3"慢工具不再被误杀"的核心目标。

**修复方向**：补 worktree 顶层目录 mtime 快照，或对 dirty 文件取 `max(mtime)`；成本很低。

---

## 🟢 L1：G8 的 `cleanup_race` 分支是死代码

**位置**：`agent_go/executor.py:913`

**问题**：

```python
if _kill_reason_now == "cleanup_race":
    verify_ok = True   # ← 运行时永远不会进入
```

`cleanup_race` 只在 bench 的 `_collect_result`（`bench.py`）里被**合成**，subtask/executor/pipeline 从不把它作为运行时 kill_reason 写入（grep 确认运行时只会写 `stuck/hard_timeout/goal_*/over_budget_l2/over_budget_l3`）。

**测试假阳性**：`tests/test_cost_control.py::test_cleanup_race_counts_as_pass` 只是在测试里构造 `["cleanup_race"]` 然后断言 `[0]=="cleanup_race"`——纯字符串字面量比较，**根本没经过 executor 路径**，却给人"G8 cleanup_race 生效"的错觉。

**建议**：`cleanup_race` 是度量层概念，运行时一个完成的子任务正常 success 即可，这层死分支可删（或注明仅防御性保留）。相应测试改为断言"运行时 kill_reason 集合不含 cleanup_race"才有价值。

---

## 🟢 L2：集成路径无端到端测试

**位置**：`tests/test_cost_control.py`

**问题**：S12 新增测试全是**纯函数 + 字符串字面量比较**：
- `_dynamic_task_budget` 测试传入全量列表（掩盖 M1）；
- G8 测试构造列表再读 `[0]`，断言 `==`（掩盖 L1）；
- `TestS12P2DegradeTable` 全是 dict 查表；
- 没有一个测试驱动 `_run_pipeline` 走完 degrade → streak → 安全阀（掩盖 H1）。

**建议**：至少补一个 pipeline 级回归——mock `_meter_total_cost` 恒返回超预算 + 喂 3 个失败 result → 断言安全阀 trip 后**不再**降级（当前会失败，即为 H1 的回归测试）。

---

## 🟢 L3：`--budget` 与 `--max-cost` 语义重复

**位置**：`agent_go/cli.py`

**问题**：两个 flag 都映射到 `max_budget_usd`，`_budget_flag = args.budget or args.max_cost` → 同传时 `--budget` 静默胜出。help text 已注明等价，算可接受的小 API smell。

**建议**：二选一，或让 `--budget` 成为 `--max-cost` 的显式别名。

---

## ✅ 做得好的部分

- **P0 kill_reason 贯穿**（subtask → executor → pipeline → bench）链路清晰；方案 A（决策点写 `kill_state` metering）+ 方案 B（事后反推兜底）双保险设计正确，对 SIGKILL 鲁棒。
- **`all([])` 陷阱修复**（`bench.py:756`）+ binary_pass 时序后移——根因诊断准确，修正点精确。
- **grace 复检门的两阶段结构**（suspected → 复检 → 确认）方向正确，"单次静默永不直接杀"是对原设计的安全改进。
- **G5 欠分解检测只告警不强改 Plan**（`planning.py::check_under_decomposition`）——克制，尊重 LLM 的分解决定，符合"确需少量子任务的 hard 任务是合法场景"。
- **设计文档本身**是高质量交付，三表面（A 测量 / B 运行时 / C 成本）的区分尤其有价值，是后续可审计的基础。

---

## 优先级建议

| 优先级 | 项 | 理由 |
|--------|-----|------|
| **合并前必修** | H1、H2 | 两者都属"文档/日志承诺了保护但实际不工作"——比没有护栏更危险，因为它让人误以为有 |
| **建议尽快** | M1、M2 | 视启用频率；M1 在 config 文件启用 cost_control 时可触发，M2 削弱 P3 核心目标 |
| **可顺带** | L1、L2、L3 | 清理死代码 + 补回归测试 + 收敛 CLI flag |

H1（加 `_degrade_aborted` 哨兵）与 H2（macOS `ps` 时间解析 + 失败返回 -1）改动都很小、风险低，建议带上对应回归测试一起修。
