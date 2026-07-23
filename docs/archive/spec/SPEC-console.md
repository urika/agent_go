# console 模块规格说明

## 概述

`console.py` 是 agent_go 的统一输出抽象层，用一个 `Console` 类取代散落在各处的裸 `print()` 调用。它实现了 `--quiet`（headless/CI 场景静默输出）与 `--verbose`（调试信息）两种模式的开关控制，并附带语义化输出（success/warning/error 等）与结构化输出（表格、JSON 美化打印）能力。模块同时维护一个模块级默认实例，供未通过依赖注入获得 `Console` 的模块使用。该模块处于架构最底层，被 `cli.py`、`config.py`、`workflow_gen.py`、`pipeline.py`、`executor.py`、`eval.py` 依赖，自身只依赖标准库。

## 公共接口

### 类 `Console`（`agent_go/console.py:21`）

统一输出抽象类。构造签名：

```python
Console(quiet: bool = False, verbose: bool = False)
```

- `quiet=True` 时抑制除 `force()` 之外的所有输出；`verbose=True` 时允许 `debug()` 输出。
- 实例属性：`quiet`、`verbose`（均为 `bool`，构造后可直接读写）。

**原始输出方法**

| 方法 | 签名 | 行为 |
|---|---|---|
| `force` | `force(*args, **kwargs) -> None` | 无条件透传给内建 `print()`，绕过 quiet 模式，用于交互式提示（`console.py:34`） |
| `print` | `print(*args, **kwargs) -> None` | `print()` 的 drop-in 替换，quiet 模式下静默（`console.py:38`） |

**语义化输出方法**（均只在非 quiet 时输出，签名为 `(msg: str) -> None`）

- `info(msg)` — 纯文本信息（`console.py:45`）
- `success(msg)` — 前缀 `✅`（`console.py:50`）
- `warning(msg)` — 前缀 `⚠️  `（两个空格，`console.py:55`）
- `error(msg)` — 前缀 `❌`（`console.py:60`）
- `debug(msg)` — 前缀 `🔍`，仅在 `verbose=True 且 quiet=False` 时输出（`console.py:65`）

**布局辅助方法**

- `sep(char: str = "─", width: int = 50) -> None` — 打印 `char * width` 分隔线（`console.py:72`）
- `title(msg: str) -> None` — 输出带 `=` × 60 上下装饰线的章节标题，共 3 次 `print`（`console.py:77`）
- `subtitle(msg: str) -> None` — 输出 `\n── {msg} ──` 形式的子标题（`console.py:84`）

**结构化输出方法**

- `table(headers: list[str], rows: list[list[str]], col_widths: list[int] | None = None) -> None`（`console.py:91`）
  - `col_widths` 为 `None` 时自动按「headers + rows 中该列最长字符串长度 + 2」计算。
  - 输出：一行左对齐表头 + 一行总宽度的分隔线。
- `data(data: Any) -> None`（`console.py:107`）
  - 以 `json.dumps(data, indent=2, ensure_ascii=False, default=str)` 美化打印；`json` 在方法体内惰性 import。
- `data_table(rows: list[dict[str, Any]], columns: list[str] | None = None) -> None`（`console.py:113`）
  - 将 dict 列表渲染成表格；`columns=None` 时取第一行的全部键；单元格值 `str()` 后截断至 60 字符；`rows` 为空或 quiet 时直接返回。

### 模块级默认实例管理（`agent_go/console.py:129-156`）

- `_default_console = Console()`（内部）— 模块级默认实例，默认为非 quiet 模式。
- `set_default_console(console: Console) -> None` — 替换默认实例。`cli.py` 在 `cmd_run()` 中调用，按 CLI 参数配置全局输出模式。
- `get_default_console() -> Console` — 获取当前默认实例。
- **`_LazyConsole`**（`console.py:150`）— 代理对象，每次属性访问时动态调用 `getattr(get_default_console(), name)`。解决了 `config.py`、`workflow_gen.py`、`eval.py` 在 import 时绑定默认实例导致后续 `set_default_console()` 不生效的时序问题。这三个模块已从 `console = get_default_console()` 迁移为 `console = _LazyConsole()`。

## 关键逻辑与流程

模块为无状态输出工具，无复杂算法。核心控制流是统一的 quiet/verbose 判定：

1. 除 `force()` 与 `debug()` 外，每个输出方法开头检查 `self.quiet`，为真则直接返回（如 `console.py:40`、`:47`、`:74`、`:99`）。
2. `debug()` 的判定为 `self.verbose and not self.quiet`（`console.py:67`）——quiet 优先级高于 verbose，两者同开时 debug 不输出。
3. `force()` 无任何判定，直接 `print`（`console.py:36`），是 quiet 模式下唯一的输出通道，保留给交互式 prompt。
4. `table()` 的列宽自动计算（`console.py:102`）：对每列取 `max(len(str(row[i])) if i < len(row) else 0 ...)`，即对越界短行按 0 处理，最后 +2 作为 padding。
5. `data_table()` 先组装 `[[str(row.get(c, ""))[:60] for c in columns] ...]`（`console.py:125`），缺失键补空串、超长截断，再委托给 `table()`。

## 依赖关系

**内部依赖**：无。本模块不 import 任何 agent_go 内部模块，是依赖图的叶子。

**被依赖方**（均为 `from .console import ...`）：

- `cli.py:8` — import `Console` 与 `set_default_console`；`cmd_run()` 中构造 `Console(quiet=quiet or headless, verbose=verbose)` 并调 `set_default_console()`（`cli.py:142-143`）。
- `config.py:6`、`workflow_gen.py:6`、`eval.py:7` — import `get_default_console` 并在**模块顶层**执行 `console = get_default_console()`（import 时绑定，见「维护注意事项」）。
- `pipeline.py:7`、`executor.py:4` — import `get_default_console` 并在**函数体内**调用（如 `executor.py:195`、`:378`）。

**外部依赖**：仅标准库 `sys`（注意：`sys` 在 `console.py:17` import 但当前未被使用）、`typing.Any`、`json`（`data()` 内惰性 import）。无 CLI 命令、环境变量、文件系统路径依赖。

## 数据结构与持久化

无持久化。模块不读写任何文件。

关键数据仅为 `Console` 实例的两个布尔属性 `quiet`、`verbose`，以及模块级全局变量 `_default_console`（进程内单例，非线程安全但项目内单线程/多进程使用场景下无竞争）。

## 错误处理与边界情况

- 模块整体不抛自定义异常；所有方法的策略是「quiet 时静默返回」，不做任何错误上报。
- `table()`：行长度短于 `headers` 时越界列按宽度 0 处理（`console.py:102` 的条件表达式）；行比 `headers` 长时多余单元格会被 `zip(headers, col_widths)` 静默丢弃。
- `data_table()`：空列表或 quiet 时静默返回；某行缺失列键时补空串（`row.get(c, "")`）。
- `data()`：`default=str` 兜底不可 JSON 序列化的对象，不会抛 `TypeError`。
- 无超时/中断处理——输出调用不涉及阻塞 IO 管理。

## 测试覆盖

对应测试文件 `tests/test_console.py`（252 行），通过 `unittest.mock.patch("builtins.print")` 断言输出行为，覆盖：

- `TestConsolePrint` — quiet 抑制、`force()` 绕过 quiet、verbose 开关及 quiet+verbose 组合下 `debug()` 的行为；
- `TestConsoleSemantic` — `info/success/warning/error` 的前缀格式精确断言（如 `"⚠️  warn"`）；
- `TestConsoleLayout` — `sep/title/subtitle` 的默认参数、自定义参数与 quiet 抑制；
- `TestConsoleStructured` — `table`（含自定义 `col_widths` 的精确输出）、`data` 的 JSON 格式、`data_table` 的空列表/列指定/长值截断；
- `TestConsoleInit` — 构造参数默认值；
- `TestDefaultConsole` — `get/set_default_console` 的替换与恢复、默认实例非 quiet。

## 维护注意事项

1. **模块顶层绑定默认实例的隐式耦合**：~~此问题已通过 `_LazyConsole` 代理对象修复（`console.py:150`）。`config.py`、`workflow_gen.py`、`eval.py` 已迁移为 `console = _LazyConsole()`，每次属性访问动态解析当前默认实例，`set_default_console()` 的 quiet 配置现在对这些模块生效。~~ 其他模块如需 import 时绑定，应同样使用 `_LazyConsole` 而非 `get_default_console()`。
2. **emoji 前缀是测试契约的一部分**：`✅`/`⚠️  `（含两个空格）/`❌`/`🔍` 的字面前缀被测试精确断言，修改前缀需同步改测试；某些终端/日志环境对 emoji 显示不友好，但当前无纯文本 fallback。
3. **已修复（2026-07-23，docs/ISSUES.md ISSUE-3）**：`table()` 曾只打印表头与分隔线、不打印数据行（`rows` 仅参与列宽计算），且 `self.sep(sum(col_widths))` 把宽度误传给 `sep` 的 `char` 参数。现已补数据行打印、改为 `sep(width=...)`，并补 3 个回归测试。
4. **硬编码值**：分隔线默认宽 50、title 装饰线宽 60、表格列 padding +2、`data_table` 单元格截断 60 字符，均为字面量，未提取为常量。
5. **未使用的 import**：`sys`（`console.py:17`）未被使用，可清理。
6. **`data()` 的惰性 import `json`**（`console.py:109`）无必要——`json` 是标准库且无循环导入风险，提升到模块顶部更清晰。
7. **线程安全性**：`_default_console` 的替换无锁；当前在 `cmd_run()` 启动早期一次性设置，实际无竞争，但若未来引入多线程调度需注意。
