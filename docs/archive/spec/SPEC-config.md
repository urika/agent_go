# config 模块规格说明

## 概述

`agent_go/config.py` 是 agent_go 的配置中心与基础设施模块，负责用户配置的持久化加载（`~/.agent_go/config.json`）、API Key 解析、日志器装配以及交互输入的安全包装。它被 `cli.py`、`api.py`、`executor.py`、`subtask.py`、`ui.py`、`eval.py`、`tui.py` 等几乎所有核心模块依赖，是整个 Plan → Decompose → Execute 工作流中最底层、无业务逻辑的共享模块之一。模块在 import 时即有副作用：创建 `~/.agent_go/` 目录。

## 公共接口

模块通过 `__all__`（`agent_go/config.py:10`）显式导出以下符号：

### 常量

- `AGENT_GO_DIR: Path`（`config.py:15`）— `Path.home() / ".agent_go"`，模块加载时立即执行 `mkdir(exist_ok=True)`。注意 `parents=False`，若 `~` 不存在会失败（正常环境不会发生）。
- `CONFIG_PATH: Path`（`config.py:17`）— `AGENT_GO_DIR / "config.json"`，用户配置文件路径。
- `DEFAULT_CONFIG: dict`（`config.py:19`）— 默认配置模板，顶层键包括：
  - `plan_api`：`provider`（默认 `"anthropic"`）、`base_url`、`api_key`（默认空）、`model`（默认 `"claude-sonnet-4-20250514"`）、`max_tokens`（4096）、`temperature`（0.2）
  - `behavior`：`auto_confirm_plan` / `auto_confirm_subtasks` / `auto_verify_subtask`（均默认 `False`）、`show_agent_prompt` / `show_resource_map`（均默认 `True`）、`max_plan_iterations`（5）
  - `fallback`：`local_model_url`（`http://localhost:8000/v1/chat/completions`）、`local_model_name`（`"qwen"`）、`enable_rules`（`True`）
  - `skills`：`auto_discover`（`False`）、`max_auto_skills`（3）
  - `agents`：`default`（`"developer"`）
  - `cache`：`enabled`（`True`）、`plan_ttl`（86400 秒）、`max_entries`（100）
- `DECOMPOSE_RULES: list[dict]`（`config.py:55`）— 基于关键词的规则式任务分解模板，供 `api.py` 在无可用 LLM 时做 fallback 分解。每条规则含 `patterns`（关键词列表）与 `subtasks`（含 `id`/`title`/`description`/`files_hint` 的子任务列表）。当前内置两条规则：JWT/认证类（3 个子任务）、测试覆盖类（2 个子任务）。

### 函数

- `safe_input(prompt: str = "") -> str`（`config.py:73`）
  包装内置 `input()`；捕获 `EOFError`（非交互环境/stdin 关闭）时打印一个空行并返回空字符串，让调用方走"默认确认"路径。其余异常（如 `KeyboardInterrupt`）不捕获。
- `load_config() -> dict[str, Any]`（`config.py:81`）
  加载用户配置并与 `DEFAULT_CONFIG` 做一层浅合并（仅顶层 dict 键做 `update`，不递归）；配置文件不存在时创建默认配置文件（权限 `0o600`）并打印提示。详见"关键逻辑与流程"。
- `get_api_key(config: dict[str, Any]) -> str`（`config.py:96`）
  按优先级返回 API Key：环境变量 `AGENT_GO_API_KEY`（非空才生效）> `config["plan_api"]["api_key"]` > 空字符串。
- `setup_logger(task_id: str, task_dir: Path) -> logging.Logger`（`config.py:99`）
  创建/复用名为 `agent_go.{task_id}` 的 logger（DEBUG 级），先移除全部已有 handler（保证重复调用幂等、不重复输出），再挂载两个 handler：写入 `task_dir / "execution.log"` 的 `FileHandler`（DEBUG 级）和输出到 stderr 的 `StreamHandler`（INFO 级），格式统一为 `%(asctime)s | %(levelname)-8s | %(name)s | %(message)s`。
- `log_event(logger: logging.Logger, event: str, data: dict[str, Any]) -> None`（`config.py:115`）
  以 DEBUG 级别写入一条 JSON 结构化事件，字段为 `timestamp`（`datetime.now().isoformat()`）、`event` 以及 `data` 展开的内容。被 `executor.py`、`subtask.py`、`api.py`、`ui.py`、`utils.py` 广泛用于执行轨迹记录。

模块级变量 `console`（`config.py:8`）来自 `console.get_default_console()`，用于上述打印，但不在 `__all__` 中。

## 关键逻辑与流程

### 配置加载与合并（`load_config`，`config.py:81-94`）

1. 若 `CONFIG_PATH` 存在：读取并 `json.loads` 解析用户配置。
2. 通过 `json.loads(json.dumps(DEFAULT_CONFIG))` 深拷贝默认配置（`config.py:84`），避免污染模块级常量。
3. 逐顶层键合并：用户值与默认值均为 dict 时执行 `merged[key].update(value)`（**仅一层**，嵌套 dict 会被整体替换）；否则直接覆盖（`config.py:85-89`）。
4. 若配置文件不存在：将 `DEFAULT_CONFIG` 以 `indent=2, ensure_ascii=False` 写入 `CONFIG_PATH`，`os.chmod(..., 0o600)` 限制权限（含 API Key，属敏感文件），打印"已创建默认配置"提示，并直接返回模块级 `DEFAULT_CONFIG` 本身（**不是拷贝**，见"维护注意事项"）。

### API Key 解析（`config.py:96-97`）

单行表达式：`os.environ.get("AGENT_GO_API_KEY", "") or config.get("plan_api", {}).get("api_key", "")`。空环境变量视为未设置，回退到配置文件。

### 日志事件流

`setup_logger` 每次调用先清空旧 handler 再重建，因此同一 `task_id` 重复调用不会产生重复日志行；`log_event` 统一走 DEBUG 级，意味着结构化事件只进 `execution.log` 文件，不进终端（StreamHandler 为 INFO 级）。

## 依赖关系

- 内部模块：
  - `.console.get_default_console()`（`agent_go/console.py:142`）— 返回模块级 `Console` 实例，其 `print()` 在 quiet 模式下静默。`config.py` 用它输出 `safe_input` 的空行与配置创建提示。
- 标准库：`os`、`json`、`logging`、`pathlib.Path`、`datetime.datetime`、`typing.Any`。无第三方依赖。
- 环境变量：`AGENT_GO_API_KEY`（`get_api_key` 读取）。
- 文件系统：`~/.agent_go/`（import 时创建）、`~/.agent_go/config.json`（读写）、调用方传入的 `task_dir/execution.log`（写入）。
- 被依赖方（import 本模块的内部模块）：`__init__.py`、`cli.py`、`api.py`、`executor.py`、`subtask.py`、`ui.py`、`utils.py`、`eval.py`、`tui.py`、`role_skill_map.py`。

## 数据结构与持久化

- 持久化文件：`~/.agent_go/config.json`，JSON 格式，结构同 `DEFAULT_CONFIG`（见"公共接口"），文件权限 `0o600`。仅在文件不存在时写入；之后只读不写，合并结果不落盘。
- 日志文件：`{task_dir}/execution.log`，由 `setup_logger` 创建（`task_dir` 由调用方提供，通常是 worktree 任务目录），追加模式，文本行格式 + DEBUG 级内嵌 JSON 事件行。

## 错误处理与边界情况

- `safe_input` 仅捕获 `EOFError`；`KeyboardInterrupt` 照常上抛，由上层处理中断。
- `load_config` **不做任何异常防护**：配置文件存在但 JSON 损坏时 `json.loads` 直接抛 `JSONDecodeError`，程序启动即失败；无备份/修复/fallback 机制。
- 合并逻辑的类型兜底：用户对某个键写了非 dict 值（如 `"behavior": true`）时直接整体覆盖默认值，不报错，可能导致后续代码取嵌套键时抛 `TypeError`/`KeyError`。
- `setup_logger` 假定 `task_dir` 已存在，否则 `FileHandler` 构造抛 `FileNotFoundError`。
- `log_event` 中 `data` 若含 `timestamp` 或 `event` 键会覆盖内置字段（`**data` 在后）；含不可 JSON 序列化的对象时抛 `TypeError`。

## 测试覆盖

对应测试文件：`tests/test_config.py`，覆盖两个类共 7 个用例：

- `TestLoadConfig`：默认配置创建（含 `0o600` 权限校验）、已有配置的读取与覆盖、嵌套 dict 一层合并、`DEFAULT_CONFIG` 新增字段对旧配置文件的向前兼容。测试通过保存/恢复真实 `CONFIG_PATH` 内容实现隔离。
- `TestGetApiKey`：环境变量优先、无环境变量时用配置文件值、两者皆空返回 `""`。

未覆盖：`safe_input` 的 EOF 路径、`setup_logger`/`log_event`、`DECOMPOSE_RULES`、import 时的目录创建副作用。

## 维护注意事项

- **import 副作用**：`config.py:16` 在模块导入时创建 `~/.agent_go/`，任何 `import agent_go.config`（包括测试和仅想用常量的场景）都会写用户主目录，不利于沙箱化与单元测试。
- **`load_config` 无配置时的返回值是 `DEFAULT_CONFIG` 本体**（`config.py:94`），不是深拷贝；调用方若原地修改返回 dict，会污染模块级默认值，影响后续所有 `load_config` 调用。存在配置文件的分支则返回深拷贝，两条路径语义不一致。
- **合并仅一层**：`behavior`、`plan_api` 等二级 dict 内部若再嵌套 dict，用户值会整体替换默认子树。新增配置键时应保持"顶层 dict + 扁平二级键"的结构。
- 硬编码值：默认模型 `claude-sonnet-4-20250514`（`config.py:24`）、fallback 本地模型地址 `http://localhost:8000/v1/chat/completions` 与名称 `qwen`（`config.py:37-38`）随时间推移会过时，升级时需同步改 `DEFAULT_CONFIG` 与用户已生成的 `config.json`（旧文件不会自动更新已有键）。
- `DEFAULT_CONFIG` 中的注释（如 `config.py:29` "默认同意 Plan 方案" 实际默认值为 `False`，注释措辞易误读）在写入用户 `config.json` 时丢失，用户看不到字段说明；可考虑同步维护 `config.example.json`。
- `DECOMPOSE_RULES` 的子任务硬编码了 `src/auth/**`、`tests/**` 等路径假设，只适用于特定项目布局，属于演示性质的 fallback 数据。
- 隐式耦合：`get_api_key` 假定 `plan_api.api_key` 路径存在，`api.py` 等调用方依赖该结构；`setup_logger` 的 logger 命名约定 `agent_go.{task_id}` 若与 `logging` 全局配置（如 root logger 级别、`propagate`）交互需注意重复输出风险（当前未设置 `logger.propagate = False`，消息会向 root logger 传播）。
- 改进建议：为 `load_config` 增加 JSON 损坏时的备份重建逻辑；统一两条返回路径都返回深拷贝；`setup_logger` 中设置 `logger.propagate = False`；将 import 时的 `mkdir` 延迟到首次写文件时。
