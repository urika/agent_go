# tui 模块规格说明

## 概述

`tui.py` 提供基于 curses 的终端交互式状态面板，是 `agent_go status` 命令的默认展示方式（`agent_go/cli.py:551` 中 `cmd_status` 默认路由到 `cmd_status_tui()`，`--no-tui` 时回退文本模式）。模块只读地扫描 `~/.agent_go/task-*` 目录下的 `meta.json` 与 `execution.log`，以任务列表 + 详情面板 + 日志尾部的双栏布局实时展示各任务的执行进度。模块不启动、不修改任何任务，属于纯展示层。

## 公共接口

模块声明 `__all__ = ["cmd_status_tui"]`（`agent_go/tui.py:11`），对外唯一入口是 `cmd_status_tui`；其余均为内部实现，但 `_get_task_status` / `_get_tail_lines` 被测试直接引用。

### `cmd_status_tui() -> None`

- 位置：`agent_go/tui.py:194`
- 参数：无；返回值：无。
- 行为：通过 `curses.wrapper(tui_main)` 进入 curses 主循环；捕获并静默吞掉 `KeyboardInterrupt`。
- 副作用：接管终端进入 curses 全屏模式，退出时由 `curses.wrapper` 恢复终端。

### `_get_task_status(task_dir: Path) -> Optional[dict[str, Any]]`（内部）

- 位置：`agent_go/tui.py:13`
- 读取 `task_dir/meta.json` 与 `task_dir/execution.log`，聚合出单行任务状态 dict：
  - `id`：目录名；`status`：meta 中的 status（缺省 `"unknown"`）；`task`：任务描述截断至 50 字符；
  - `progress`：`"completed数/total数"`（完成数统计 results 中 status ∈ `completed/no_changes/degraded`），无 subtasks 时为 `"-"`；
  - `current`：从 execution.log 尾部 10 行反向查找最近一条含 `subtask_start` 的行，解析其中 JSON 的 `title`；
  - `elapsed`：由 meta 的 `created`（格式 `%Y%m%d-%H%M%S`）计算耗时，格式 `"XmYs"`；
  - `results`、`subtasks`：原样透传 meta 中的字段。
- `meta.json` 不存在时返回 `None`（该任务行被跳过）。
- 特殊判定：status 为 `running` 且 `execution.log` 的 mtime 距今超过 600 秒（10 分钟）时，改判为 `failed`（`agent_go/tui.py:21-23`）。

### `_get_tail_lines(log_path: Path, count: int = 10) -> list[str]`（内部）

- 位置：`agent_go/tui.py:58`
- 读取日志最后 30 行，过滤不含 `"|"` 的行，取每行最后一个 `" | "` 分隔段（即 message 部分）截断至 100 字符，返回最后 `count` 条。文件不存在时返回 `[]`。

### 常量（内部，但被测试直接引用）

- `STATUS_COLORS: dict[str, int]`（`agent_go/tui.py:66`）：状态 → curses 颜色对编号映射。`completed/no_changes`→2(绿)，`degraded/running/paused`→3(黄)，`failed/aborted`→1(红)。
- `ICONS: dict[str, str]`（`agent_go/tui.py:67`）：状态 → 两字符图标，如 `completed`→`"ok"`、`running`→`"> "`、`failed`→`"!!"`。

## 关键逻辑与流程

### 主循环 `tui_main(stdscr)`（`agent_go/tui.py:70`）

1. **初始化**（:72-77）：隐藏光标、启用颜色，初始化颜色对 1-5（红/绿/黄/青/白，前景配默认背景 -1），颜色对 6 为黑底青字用于标题栏与状态栏。
2. **非阻塞输入**（:79-80）：`nodelay(True)` + `timeout(500)`，即每 500ms 自动刷新一轮，无需按键。
3. **每轮刷新**：
   - 扫描 `AGENT_GO_DIR.glob("task-*")`，倒序（新任务在前），逐目录调用 `_get_task_status` 聚合行数据（:86-87）。
   - 按 `filter_mode` 过滤：0=全部，1=running，2=completed，3=failed（:88-93）。
   - 终端尺寸下限检查：`max_y < 8 or max_x < 50` 时跳过渲染，仅响应 `q` 退出（:96-101）。
4. **渲染**（:106-169）：
   - 标题栏（行 0）显示快捷键说明；左侧任务列表宽度 `min(max_x - 42, 60)`，右侧为详情面板（:111-112）。
   - 列表行格式：`{选中符}{icon} {id前20字符} {progress} {elapsed}`，选中行加 `A_REVERSE`（:123-125）。
   - 展开的任务（在 `expanded_tasks` 集合中）在下方逐行渲染各 subtask 结果：`{icon} {subtask_id} {agent_type_source前4字符} {duration}`（:128-140）。
   - 详情面板只展示选中任务 `results[0]`（第一个子任务）的 status/duration/retry/verify/change_stats（:145-154）。
   - 日志面板展示该任务 execution.log 尾部 6 行（:157-162）。
   - 底部状态栏汇总任务总数与 running/done/fail 计数（:165-169）。
5. **按键处理**（:172-183）：`q` 退出；`j`/↓、`k`/↑ 移动选择；`Enter`（keycode 10）切换当前任务展开/折叠；`1`-`4` 切换过滤模式。按键 `1`-`4` 经映射表对应 `filter_mode` 0-3，与状态栏提示一致（2026-07-23 修复前曾错位，见 ISSUE-9）。

### `_safe_addstr`（`agent_go/tui.py:186`）

包装 `win.addstr`，捕获一切异常并静默忽略 —— curses 在窗口边界写入或终端 resize 时会抛错，这是有意的容错设计。

## 依赖关系

- **内部模块**：
  - `from .config import AGENT_GO_DIR`（`agent_go/tui.py:7`）：任务根目录，定义为 `Path.home() / ".agent_go"`（`agent_go/config.py:15`）。
  - 被 `agent_go/cli.py:16` 导入，`cmd_status`（`cli.py:551`）在默认及 `--no-tui` 之外的路径调用 `cmd_status_tui()`。
- **stdlib**：`json`、`logging`、`time`、`pathlib.Path`、`datetime`、`typing`；`curses` 为函数内延迟导入（`tui_main` 与 `cmd_status_tui` 各自 `import curses`），使模块在无 curses 环境下仍可被导入。
- **文件系统**：只读访问 `~/.agent_go/task-*/meta.json` 与 `~/.agent_go/task-*/execution.log`。
- **日志格式契约**：依赖 `config.setup_logger` 的格式 `%(asctime)s | %(levelname)s | %(name)s | %(message)s`（`config.py:108`）以及 `executor.py:383` 通过 `log_event` 写入的 `subtask_start` 事件 JSON（含 `title` 字段）。`_get_task_status` 用 `line.split(" | ")[-1]` 取 message 段，`_get_tail_lines` 同理。
- 无外部 CLI（git/claude/gh）依赖，无环境变量依赖。

## 数据结构与持久化

模块自身**无持久化写入**，只读取以下两个由 pipeline/cli 写入的文件：

- `~/.agent_go/task-<id>/meta.json`（JSON，写入方见 `pipeline.py:61,122,202`）：
  - 顶层：`task`（str）、`created`（`"%Y%m%d-%H%M%S"`）、`status`（`pending/running/completed/failed/paused/aborted` 等）、`subtasks`（list）、`results`（list）。
  - `results[]` 元素字段（TUI 实际消费）：`subtask_id`、`status`、`duration_sec`、`retry_count`、`verify_ok`、`agent_type_source`、`change_stats{files_changed, insertions, deletions}`。
- `~/.agent_go/task-<id>/execution.log`（文本日志）：行格式 `时间 | 级别 | logger名 | message`，其中 message 可能是 `log_event` 输出的 JSON（含 `event` 字段，如 `subtask_start`）。

`_get_task_status` 返回的内存结构见"公共接口"一节。

## 错误处理与边界情况

- `meta.json` 缺失 → 返回 `None`，该任务不显示（`tui.py:15-16`）。
- `execution.log` 缺失 → `current` 为空串；非 running 任务的 elapsed 退化为 `datetime.now()` 作为结束时间（:43）。
- running 但日志 10 分钟未更新 → 判定为僵死，显示为 `failed`（:21-23），这是展示层判定，不回写 meta.json。
- 日志行 JSON 解析失败（`JSONDecodeError/IndexError/KeyError`）→ 仅 `logger.debug`，继续（:33-35）。
- `created` 时间戳格式非法（`ValueError`）→ elapsed 留空（:46-48）。
- 终端过小（<8 行或 <50 列）→ 不渲染，仅响应 `q`（:96-101）。
- curses 边界/resize 写入异常 → `_safe_addstr` 全部静默吞掉（:186-191）。
- `KeyboardInterrupt`（Ctrl-C）→ 静默退出（:198）。
- **未防护的边界**：`meta.json` 存在但内容不是合法 JSON 时，`json.loads`（:17）会抛 `JSONDecodeError` 并使整个 TUI 崩溃；`log_path.read_text` 遇非法 UTF-8 同理未捕获。

## 测试覆盖

对应测试文件：`tests/test_tui.py`。

- `TestTaskStatusData`：`_get_task_status` 的 meta 缺失返回 None、completed 任务的 progress/elapsed、running 任务 elapsed、无日志文件容错、results 中扩展指标字段（timing/change_stats/merge_results/verification_results）透传。
- `TestTailLines`：`_get_tail_lines` 的文件缺失返回 `[]`、取尾 N 行、过滤无 `"|"` 的行。
- `TestCLIRouting`：`agent_go status` 默认调用 TUI、`--no-tui` 走文本模式（路由在 cli 侧）。
- `TestTuiModule`：函数/常量可导入性、`ICONS` 与 `STATUS_COLORS` 覆盖全部 7 种状态。

未覆盖：`tui_main` 的渲染与按键交互逻辑（curses UI 未做自动化测试）。

## 维护注意事项

- **快捷键提示与行为不一致**：状态栏与标题栏写的是 `[1]all [2]run [3]done [4]fail`，但代码中 `filter_mode = key - ord('0')`（`tui.py:183`）使 `1`=全部以外……实际映射为 1→running、2→completed、3→failed，`4` 对应 `filter_mode=4` 无任何过滤分支（等同于"全部"）。提示文字与按键映射整体错位，已于 2026-07-23 修复（docs/ISSUES.md ISSUE-9）：按键映射改为 `{1:0, 2:1, 3:2, 4:3}`，与提示对齐。
- **硬编码值**：僵死判定阈值 600 秒（:22）、日志回看 10 行（:29）/30 行（:62）、`task` 截断 50 字符（:51）、消息截断 100 字符（:63）、列表宽度 `min(max_x - 42, 60)`（:111）、终端最小尺寸 8×50（:96）、刷新间隔 500ms（:80）、日志面板显示 6 行（:158）。
- **详情面板只显示 `results[0]`**（:145-146）：多子任务任务的详情面板展示的永远是第一个子任务的信息，与列表中展开的各子任务行无联动，易误导；改进方向是让详情跟随列表内的子任务级选择。
- **隐式耦合**：
  - 与 `config.setup_logger` 的日志格式 `" | "` 分隔强耦合，格式一旦改动，`_get_tail_lines` 与 `current` 提取逻辑会静默失效（返回空而非报错）。
  - 与 `executor.py:383` 的 `subtask_start` 事件结构（须含 `title`）耦合。
  - 与 `meta.json` 的 `results[].status` 取值集合耦合：`progress` 的"完成"口径只认 `completed/no_changes/degraded`，新增终态需同步此处与 `STATUS_COLORS`/`ICONS`。
- **running 改判 failed 仅停留在显示层**：TUI 不回写 meta.json，因此文本模式（`cli.py:_cmd_status_text`）或其他读取方看到的是原始 status，两处口径可能不一致。
- **性能**：每 500ms 全量重读所有任务的 `meta.json` 和日志文件（:86-87），任务数或日志体积大时会有可感知的 IO 开销；可考虑按 mtime 缓存。
- **健壮性改进建议**：为 `meta.json` 的 `json.loads` 与日志文件的 `read_text` 增加异常捕获，使单个损坏任务不至于令整个 TUI 退出。
