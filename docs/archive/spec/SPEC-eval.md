# eval 模块规格说明

源码：`agent_go/eval.py`（606 行）

## 概述

`eval` 是 agent_go 的离线评估与报表模块，对应 `agent_go eval` CLI 子命令。它不参与 Plan -> Decompose -> Execute 主流程，而是事后读取历史任务目录（`~/.agent_go/task-*/`）中的 `meta.json` 与 `execution.log`，计算五类指标报表：质量（Q1–Q8）、性能（P1–P6）、成本（token/费用估算）、可靠性（完成率/sandbox 分布/重试率）、使用习惯（文档挂载/Agent 多样性/Skill 使用率），并通过 console 输出格式化报告。模块为纯读取分析，不修改任何任务数据。

## 公共接口

`__all__` 导出 5 个符号（`agent_go/eval.py:13`）：`analyze_quality`、`analyze_performance`、`aggregate_quality`、`aggregate_performance`、`cmd_eval`。此外 `analyze_cost` / `analyze_reliability` / `analyze_ux` 虽未列入 `__all__`，但被 `cmd_eval` 直接调用，属于事实上的公共接口。

### 指标分析（单任务）

- `analyze_quality(meta: Optional[dict]) -> Optional[dict]`（:45）
  输入单个任务的 meta dict，输出 Q1–Q8 质量指标及综合评分。`meta` 为 `None` 或 `results` 为空时返回 `None`。无副作用。
- `analyze_performance(meta: Optional[dict], log_path: Optional[Path] = None) -> Optional[dict]`（:127）
  输出 P1–P6 性能指标及评分。`log_path` 为 `None` 时 P1=0、P2=0、P6=100（跳过日志解析）。无副作用。
- `analyze_cost(tasks_dir: Path) -> dict`（:282）
  跨任务聚合：扫描 `tasks_dir` 下所有 `task-*/execution.log` 中的 `api_call` / `api_error` / `plan_complete` 事件，统计 token 用量并按 `MODEL_PRICES` 估算美元成本。始终返回 dict（空目录时各计数为 0）。
- `analyze_reliability(tasks_dir: Path) -> dict`（:347）
  跨任务聚合：任务完成率、`sandbox_type` 分布（greywall/native/headless）、重试统计。
- `analyze_ux(tasks_dir: Path) -> dict`（:395）
  跨任务聚合：`reference_docs` 挂载率、plan 迭代次数（`plan_generate` 事件）、`agent_type_source` 分布与多样性、subtask 的 `skills` 使用率。

### 跨任务聚合

- `aggregate_quality(tasks_dir: Path) -> Optional[dict]`（:216）
  对每个任务目录跑 `analyze_quality`，取各 Q 指标与 score 的平均值。无有效任务时返回 `None`。
- `aggregate_performance(tasks_dir: Path) -> Optional[dict]`（:236）
  汇总所有 subtask 的 `duration_sec`（P50/P95/P99、均值）与各任务 P1 均值。注意：无有效任务时**不返回 None**，而是返回 `tasks_analyzed: 0` 的 dict（调用方 `_print_aggregate_perf` 靠 `tasks_analyzed == 0` 判断空态，:598）。

### CLI 入口

- `cmd_eval(args=None) -> None`（:435）
  由 `cli.py:854` 在 `args.command == "eval"` 时调用。支持两种参数来源：带 `subcommand` 属性的 argparse Namespace（读取 `args.subcommand` / `args.task_id` / `args.eval_all`），或 `args=None` 时直接解析 `sys.argv`（`agent_go eval <quality|perf|cost|reliability|ux|all> [task-id|--all]`）。`task_id` 缺省时自动选最新的 `task-*` 目录（`_resolve_task_dir`，:491）。所有输出经 `console.print`，安静模式下被抑制。

### 常量

- `MODEL_PRICES: dict`（:267）— 4 个模型的每百万 token 价格（`prompt` / `completion`）：`claude-sonnet-4-20250514`、`claude-sonnet-4`（3.0/15.0），`gpt-4o`（2.5/10.0），`deepseek-chat`（0.27/1.1）。
- `PROVIDER_DEFAULT_MODEL: dict`（:275）— provider → 默认模型映射，用于旧日志缺 `model` 字段时的回退：anthropic→claude-sonnet-4-20250514，openai→gpt-4o，deepseek→deepseek-chat。

### 内部函数

- `_read_meta(task_dir: Path) -> Optional[dict]`（:18，内部）— 读 `task_dir/meta.json`，不存在返回 `None`；JSON 损坏时不捕获异常（会抛 `JSONDecodeError`）。
- `_read_log_events(log_path: Path, event_name: str) -> list[dict]`（:25，内部）— 逐行子串匹配 `"event": "<name>"`（**带空格形式**），取 ` | ` 分隔的最后一段做 `json.loads`；解析失败的行记 debug 日志并跳过。文件不存在返回空列表。
- `_percentiles(data, percents) -> dict[int, float]`（:195，内部）— 线性插值百分位；空数据返回全 0。
- `_perf_score(p1, p95, p6) -> float`（:186，内部）— 性能评分：`p1 <= 0` 时固定返回 50；否则 `p1_score`(100 - p1/3)、`p95_score`(100 - p95/6)、`p6_score` 按 0.3/0.3/0.4 加权。
- `_scan_task_dirs(base_dir) -> list[Path]`（:212，内部）— `glob("task-*")` 按名称降序（新的在前）。
- `_resolve_task_dir(base_dir, task_id)`（:491，内部）— 指定 `task_id` 时校验存在性，否则取最新任务目录。
- `_print_*_report` / `_print_aggregate_*`（:499–606，内部）— 7 个格式化输出函数，全部走 `console.print`。

## 关键逻辑与流程

### 日志事件解析约定（跨模块契约）

`execution.log` 由 `config.py:115` 的 `log_event` 写入，格式为
`时间戳 | LEVEL | logger名 | {"timestamp": ..., "event": "<name>", ...}`。
`_read_log_events`（:25–38）依赖两个硬约定：

1. 事件匹配用子串 `"event": "<name>"`（带空格）。生产端 `json.dumps` 默认分隔符恰好产生该形式，因此线上日志可解析；**紧凑分隔符（`"event":"<name>"`）的 JSON 匹配不到**。行 29 注释声称"兼容有无空格"，与实现不符。
2. JSON 载荷取 `line.split(" | ")[-1]`，依赖 `config.py` 的 logging 格式串（`%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`）。若消息体自身含 ` | `，取最后一段仍正确；但若格式串变动则解析静默失效。

### analyze_quality 指标定义（:45–120）

- Q1 任务成功率 = completed/total；Q2 = (completed+no_changes)/total。
- Q3 首次通过率 = `retry_count == 0` 的比例。
- Q4 验证通过率：仅统计 `status != "no_changes"` 的 result 中 `verify_ok` 为真的比例；无变更结果时默认 100。
- Q5 新文件遗漏率 = `status == "no_changes"` 但 `change_stats.new_files > 0` 的比例（no-op 却产生了新文件，视为计划遗漏信号）。
- Q6 产物传递（merge）成功率：遍历所有 `merge_results`，`status == "success"` 的比例；无 merge 记录默认 100。
- Q7 计划准确性（:89–104）：`subtasks[].files_hint`（逗号分隔，忽略 `"*"` 与空值）构成 planned 集合，所有 `change_stats.actual_files` 构成 actual 集合；precision = |交集|/|planned|，recall = |交集|/|actual|。任一集合为空时默认 100/100。
- Q8 变更规模：`files_changed` / `insertions` / `deletions` 的平均值（保留 1 位小数）。
- 综合评分（:119）：`Q1*0.4 + Q3*0.3 + Q4*0.3`。

### analyze_performance 指标定义（:127–183）

- P3/P4：基于 `results[].duration_sec` 的均值与 P50/P95/P99。
- P5 阶段占比（:138–152）：累加 `results[].timing` 中 5 个固定键（`worktree_create_ms`、`merge_upstream_ms`、`claude_execute_ms`、`verification_ms`、`git_commit_ms`），输出各占百分比。
- P2：取日志中最后一个 `plan_complete` 事件的 `plan_duration_ms`（:160–162，循环覆盖取值）。
- P1 端到端耗时（:164–170）：解析日志首行与末行的 `YYYY-MM-DD HH:MM:SS` 时间戳之差；解析失败记 debug 并保持 0。
- P6 并发效率（:171–172）= 所有 subtask 时长之和 / P1 × 100，反映波次并发的收益；P1 为 0 时默认 100。
- 评分由 `_perf_score` 计算。

### analyze_cost 费用估算（:282–340）

遍历所有任务日志：`api_call` 事件累计 token 并按 model 分桶（缺 `model` 时按 provider 回退，:301）；`api_error` 计数；`plan_complete` 事件的 `cache_hit` 统计缓存命中率。未知模型按 `deepseek-chat` 最低价保守估算（:318）。另统计每任务/每 subtask 平均成本。

### cmd_eval 分发流程（:435–488）

`quality` / `perf` 单任务模式经 `_resolve_task_dir` 定位目录（指定 task_id 或取最新），目录不存在打印"暂无任务"；`--all` 模式走聚合。`cost` / `reliability` / `ux` 只有聚合模式。`all` 依次输出全部五类报告。未知子命令打印用法提示。

## 依赖关系

### 内部模块

- `.console.get_default_console()`（:11）— 模块级单例 `console`，所有输出经 `Console.print`（`console.py:38`），安静模式下被抑制。
- `.config.AGENT_GO_DIR`（:437，函数内延迟 import）— `Path.home() / ".agent_go"`（`config.py:15`），作为 `tasks_dir` 的默认根。延迟 import 避免了模块级循环依赖。

### 数据生产者（隐式依赖，非 import）

- `config.py:115` 的 `log_event` / `execution.log` 格式串（见"日志事件解析约定"）。
- `meta.json` 由执行器（executor）写入，本模块消费其 `task_id`、`status`、`results[]`（含 `status`、`retry_count`、`verify_ok`、`duration_sec`、`change_stats`、`timing`、`merge_results`、`sandbox_type`、`agent_type_source`）、`subtasks[]`（`files_hint`、`skills`）、`reference_docs` 等字段——任一字段缺失时靠 `.get()` 默认值退化，不报错。

### 外部依赖

- 仅 Python stdlib：`json`、`logging`、`pathlib.Path`、`datetime`、`typing`；`sys`（cmd_eval 内局部 import）。
- 文件系统：`~/.agent_go/task-*/meta.json`、`~/.agent_go/task-*/execution.log`。无 CLI 命令（git/claude/gh）调用，无环境变量读取。

## 数据结构与持久化

本模块**不写任何文件**（无持久化输出），只读取以下两类文件：

### meta.json（读）

```json
{
  "task_id": "...", "status": "completed|failed|...",
  "reference_docs": [...],
  "subtasks": [{"id": "...", "files_hint": "a.py,b.py|*", "skills": [...]}],
  "results": [{
    "status": "completed|no_changes|failed",
    "verify_ok": true, "retry_count": 0, "duration_sec": 45.0,
    "sandbox_type": "greywall|native|headless",
    "agent_type_source": "default|...",
    "change_stats": {"files_changed": 0, "insertions": 0, "deletions": 0,
                     "new_files": 0, "actual_files": ["..."]},
    "timing": {"worktree_create_ms": 0, "merge_upstream_ms": 0,
               "claude_execute_ms": 0, "verification_ms": 0, "git_commit_ms": 0},
    "merge_results": [{"upstream": "...", "status": "success"}]
  }]
}
```

### execution.log（读）

文本日志，行格式 `YYYY-MM-DD HH:MM:SS | LEVEL    | agent_go.<task_id> | <message>`；结构化事件以 JSON 作为 message，`event` 字段标识类型。本模块消费的事件：`plan_complete`（`plan_duration_ms`、`cache_hit`）、`plan_generate`（`iteration`）、`api_call`（`provider`、`model`、`prompt_tokens`、`completion_tokens`）、`api_error`。

## 错误处理与边界情况

- **宽容读取**：`_read_meta` 对缺失文件返回 `None`；各 analyze/aggregate 函数对 `None` meta、空 `results`、空目录均以返回 `None` 或零值 dict 兜底，不抛异常。所有除法都有分母为 0 保护，多数比率型指标在无数据时默认 100（Q4/Q6/Q7、P6）。
- **静默降级**：日志事件行 JSON 解析失败（:36）与日志首末行时间戳解析失败（:169）只记 `logger.debug`，指标退化为 0/默认值，用户无感知。
- **未捕获的异常**：`meta.json` 存在但 JSON 损坏时 `_read_meta` 直接抛 `JSONDecodeError`；`execution.log` 为空文件时 `lines[0]` 会抛 `IndexError`——注意 :164 的 `strip().split("\n")` 对空文件产生 `[""]`，取 `[0]` 不越界但 `strptime("")` 抛 `ValueError` 已被捕获，实际安全。
- **无超时/中断处理**：纯本地文件遍历，不处理 KeyboardInterrupt（由 cli.py 顶层捕获）。

## 测试覆盖

对应 `tests/test_eval.py`（391 行，31 个用例），docstring 自称"全覆盖"：

- `_percentiles`（基本/空/单元素/同值）、`_perf_score`（满分/最差/p1=0 默认 50/中间值）；
- `analyze_quality`（基本指标、None meta、空 results、Q7 计划准确性、Q4、no_changes 计数）；
- `analyze_performance`（基本、带日志、P5 阶段占比、None meta）；
- `analyze_cost`（基本、带日志数据、零调用）、`analyze_reliability`（基本、混合状态、空目录）、`analyze_ux`（基本、带数据）；
- `aggregate_quality` / `aggregate_performance`（空与聚合）。

**当前实测 3 个用例失败**（`test_with_log_path`、`test_with_log_data`、`test_with_data`）：测试辅助函数 `_make_log_file` 用紧凑分隔符写 JSON（`"event":"api_call"`），而 `_read_log_events` 只匹配带空格的 `"event": "api_call"`，导致事件解析为空。生产端 `log_event` 用默认 `json.dumps`（带空格），所以线上功能正常，是测试与实现之间的格式契约错位。

`cmd_eval` 及各 `_print_*` 输出函数无直接测试。

## 维护注意事项

- **日志格式双向耦合**：`_read_log_events` 的子串匹配与 `config.py` 的 `log_event` JSON 序列化、logging 格式串三者构成隐式契约。改任何一处（如 json.dumps 加 `separators`、改 Formatter 的 ` | ` 分隔）都会让 eval 静默拿到空数据。行 29 注释"兼容有无空格"名不副实，要么修注释要么修匹配逻辑，并同步修复上述 3 个失败测试。
- **死代码**：`analyze_reliability` 中 `interrupted`、`resumed` 两个计数器（:351–352）初始化后从未使用，也未出现在返回 dict 中；返回值的 `interrupted` 语义缺失。`analyze_quality` 中 `meta.get("subtasks")` 仅在 Q7 用到。
- **硬编码值**：评分权重（Q: 0.4/0.3/0.3；P: 0.3/0.3/0.4）、`_perf_score` 的归一化除数（p1/3、p95/6）、`MODEL_PRICES` 价格表（会随厂商调价过时）、P5 的 5 个 timing 键、sandbox 三类枚举，均无配置化入口。新增模型必须改 `MODEL_PRICES`，否则按 deepseek-chat 最低价兜底。
- **`__all__` 不完整**：`analyze_cost` / `analyze_reliability` / `analyze_ux` 是公共入口却未导出，靠 `cmd_eval` 内部直接引用维系；`MODEL_PRICES` 被测试 import 但也不在 `__all__`。
- **聚合语义不一致**：`aggregate_quality` 空数据返回 `None`，`aggregate_performance` 空数据返回零值 dict，调用方需分别用 `is None` 和 `tasks_analyzed == 0` 判断，新增聚合函数时易踩坑。
- **P2 取值的脆弱性**：多个 `plan_complete` 事件时循环取最后一个（:161），若日志含重试/续跑的多次 plan，语义是"最后一次 plan 耗时"而非累计，文档化不足。
- **性能**：`analyze_cost`/`analyze_reliability`/`analyze_ux` 每次调用都全量重读所有历史日志，`execution.log` 随任务数增长会变慢；`aggregate_performance` 中同一日志被 `analyze_performance` 内部再读一遍（P1 时间戳 + 事件两遍扫描）。任务量大时可考虑缓存或单次扫描。
- **改进建议**：统一事件解析为逐行 `json.loads` 后按 `event` 键过滤（取代子串匹配），顺带修复测试契约；把评分权重与价格表挪到 config；补齐 `__all__`；删除 `interrupted`/`resumed` 死变量或接入真实统计。
