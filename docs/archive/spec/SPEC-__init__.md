# __init__ 模块规格说明

## 概述

`agent_go/__init__.py` 是 `agent_go` 包的顶层入口与公共 API 门面。它自身不含任何业务逻辑，只做两件事：定义包版本号 `__version__`，以及从 `cli`、`config`、`executor` 三个子模块中筛选性地 re-export 一组符号，构成对外承诺的最小公共接口。文件头部的注释（`agent_go/__init__.py:5-11`）明确约定了"模块级符号请直接从子模块导入"的使用约定，即本文件只承担聚合导出的角色。实际的 CLI 启动入口是项目根目录的 `agent_go.py`（直接 `from agent_go.cli import main`，不经过本文件）。

## 公共接口

本模块全部为 re-export，无自有函数/类定义。`__all__`（`agent_go/__init__.py:27-36`）共导出 15 个符号：

| 符号 | 类型 | 实际定义位置 | 说明 |
|---|---|---|---|
| `__version__` | 常量 `str`，值为 `"2.0.0"` | `agent_go/__init__.py:3` | 包版本号，本文件唯一定义的符号 |
| `main` | 函数 | `agent_go/cli.py:818` | CLI 主入口，签名 `main() -> None` |
| `cmd_run` | 函数 | `agent_go/cli.py:117` | `run` 子命令，签名 `cmd_run(args=None)` |
| `cmd_resume` | 函数 | `agent_go/cli.py:299` | `resume` 子命令，签名 `cmd_resume(args=None)` |
| `cmd_list` | 函数 | `agent_go/cli.py:388` | `list` 子命令，签名 `cmd_list() -> None` |
| `cmd_show` | 函数 | `agent_go/cli.py:405` | `show` 子命令，签名 `cmd_show(args=None)` |
| `cmd_status` | 函数 | `agent_go/cli.py:551` | `status` 子命令，签名 `cmd_status(args=None)` |
| `cmd_config` | 函数 | `agent_go/cli.py:689` | `config` 子命令，签名 `cmd_config() -> None` |
| `cmd_clean` | 函数 | `agent_go/cli.py:693` | `clean` 子命令，签名 `cmd_clean() -> None` |
| `cmd_pr` | 函数 | `agent_go/cli.py:482` | `pr` 子命令，签名 `cmd_pr(args=None)` |
| `cmd_review` | 函数 | `agent_go/cli.py:445` | `review` 子命令，签名 `cmd_review(args=None)` |
| `load_config` | 函数 | `agent_go/config.py:81` | 加载配置，签名 `load_config() -> dict[str, Any]` |
| `AGENT_GO_DIR` | 常量 `Path`，值为 `Path.home() / ".agent_go"` | `agent_go/config.py:15` | 用户级数据目录 |
| `DEFAULT_CONFIG` | 常量 `dict` | `agent_go/config.py:19` | 默认配置字典 |
| `run_subtask` | 函数 | `agent_go/executor.py:376` | 在 git worktree 中执行单个子任务，签名为 `run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref="", active_pids=None, active_pids_lock=None)` |

注意：上述 re-export 的函数签名、默认值均以各子模块定义为准；本文件仅做名称绑定，不附加任何包装或适配。

## 关键逻辑与流程

模块自身无流程逻辑，唯一值得关注的是 **import 时的副作用链**：

1. `import agent_go` 触发执行 `agent_go/__init__.py:12-25` 的三组 import；
2. 其中 `from .config import ...`（第 24 行）会执行 `agent_go/config.py` 的模块级代码，而 `config.py:16` 在导入时即执行 `AGENT_GO_DIR.mkdir(exist_ok=True)`；
3. 因此 **任何 `import agent_go`（哪怕只是 `import agent_go.api` 这类子模块导入，也会先执行包 `__init__`）都会在用户主目录下创建 `~/.agent_go/` 目录**。

除此之外，`__all__` 列表（第 27-36 行）控制了 `from agent_go import *` 的导出范围，是模块边界维护的核心点：新增顶层导出需同步修改 import 语句与 `__all__` 两处。

## 依赖关系

内部模块依赖（均为 re-export 来源）：

- `agent_go.cli`：`main` 及 9 个 `cmd_*` 子命令函数（`agent_go/__init__.py:12-23`）。`cli` 模块内部又会级联导入 `api`、`pipeline`、`executor` 等，因此 `import agent_go` 的依赖面实际覆盖整个包。
- `agent_go.config`：`load_config`、`AGENT_GO_DIR`、`DEFAULT_CONFIG`（`agent_go/__init__.py:24`）。
- `agent_go.executor`：`run_subtask`（`agent_go/__init__.py:25`）。

外部依赖：本文件本身不直接使用任何外部 CLI 命令或环境变量，但通过导入链引入的间接副作用包括：

- 文件系统：`~/.agent_go/` 目录（由 `agent_go/config.py:15-16` 在导入时创建）。

## 数据结构与持久化

无持久化。本文件不定义任何数据结构、不读写任何文件。导入链带来的唯一文件系统副作用是创建空目录 `~/.agent_go/`（见上文）。

## 错误处理与边界情况

- 本文件无任何异常处理逻辑；导入期异常（如子模块语法错误、循环导入）会直接向上抛出，导致整个包不可导入。
- `~/.agent_go/` 目录创建使用 `mkdir(exist_ok=True)`（`agent_go/config.py:16`），目录已存在时静默跳过，但未传 `parents=True`，主目录不可写等场景会抛 `OSError`。
- `__all__` 与 import 列表需手工保持一致，漏加不会报错，只会导致 `from agent_go import *` 静默缺少符号。

## 测试覆盖

无直接测试文件（不存在 `tests/test___init__.py`）。间接覆盖点：

- `tests/test_p0_p1_fixes.py:23-24`：`import agent_go` 及 `from agent_go import cli as cli_mod`，验证包可导入；
- `tests/test_p0_p1_fixes.py:49-56`：逐个 `import agent_go.api / cli / eval / executor / pipeline / subtask / ui / workflow_gen`，间接验证 `__init__` 导入链不抛错；
- `TESTING.md:232` 文档约定了顶层 API 形态：`from agent_go import main, cmd_run, load_config`。

## 维护注意事项

- **导入副作用已被两次代码评审点名**：`CODE_REVIEW.md:181` 与 `CODE_REVIEW_v2.md:187` 均指出"任何 `import agent_go` 都会触发文件系统副作用（创建 `~/.agent_go`）"，对类型检查、文档生成等非交互式场景不友好。改进方向：将 `mkdir` 延迟到 `load_config()` 或 CLI 启动时执行（需改 `config.py`，非本文件）。
- **导出面偏大**：`CODE_REVIEW_v2.md:202` 指出 `from agent_go import *` 暴露了全部 `cmd_*` 函数和 `run_subtask` 等实现细节，建议收敛为 `main`、`__version__` 等用户级接口。若采纳，需同步修改本文件的 import 块与 `__all__`。
- **版本号硬编码**：`__version__ = "2.0.0"`（第 3 行）为唯一版本来源，发布时需手工更新；`pyproject.toml` 中无 `[project]` 段和版本字段，两者不存在同步机制。
- **文件头注释中的导入示例**（第 7-11 行）引用了 `agent_go.utils` 模块，属于使用约定说明；修改模块结构时需同步更新该注释，避免误导。
- **两处手工同步点**：新增/移除顶层导出时，import 语句（第 12-25 行）与 `__all__`（第 27-36 行）必须同时修改，目前无测试强制校验二者一致性。
