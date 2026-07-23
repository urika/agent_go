# agents 模块规格说明

## 概述

`agent_go/agents.py` 定义 Plan Mode 编排中的 **Agent 角色类型系统**：将"开发者 / 架构师 / 审查者 / 测试工程师"等角色抽象为 `AgentType` 数据类，负责从用户配置文件或内置表加载角色定义，并据此构建 `claude` CLI 的启动命令和子进程环境变量。它本身不执行任何进程，是被 `executor.py` 和 `cli.py` 消费的纯配置/命令构建层。模块共 188 行，仅依赖 Python 标准库。

## 公共接口

模块顶部声明 `__all__ = ["load_agent_type", "list_agent_types"]`（`agent_go/agents.py:23`），但实际对外使用的接口还包括 `AgentType`、`get_claude_command`、`get_agent_env` 和常量 `AGENT_GO_AGENTS_DIR`（均被 `executor.py` / `tests/test_agents.py` 引用）。

### 常量

- `AGENT_GO_AGENTS_DIR = Path.home() / ".agent_go" / "agents"`（`agent_go/agents.py:27`）
  用户自定义 Agent 配置目录。**注意：模块导入时即执行 `mkdir(parents=True, exist_ok=True)`（第 28 行），有文件系统副作用。**
- `_BUILTIN_AGENTS`（内部，第 31-68 行）：内置 4 种角色定义 —— `developer`（完整读写）、`architect`（只读：`Read, Grep, Glob`）、`reviewer`（只读 + `extra_args: []`）、`tester`（`bypassPermissions` + `Read, Write, Bash`）。

### `AgentType`（dataclass，第 71-76 行）

| 字段 | 类型 | 默认值 |
|---|---|---|
| `type_name` | `str` | （必填） |
| `description` | `str` | `""` |
| `claude_config` | `dict` | `{}` |
| `preload_skills` | `list[str]` | `[]` |

### `load_agent_type(name: str, project_root: Optional[Path] = None) -> Optional[AgentType]`（第 79 行）

按优先级加载角色定义：

1. `~/.agent_go/agents/<name>.json`（用户全局配置）
2. `<project_root>/.agent_go/agents/<name>.json`（项目级配置，仅当传入 `project_root`）
3. `_BUILTIN_AGENTS[name]`（内置降级）

均未命中返回 `None`。无副作用（除读文件）。

### `list_agent_types() -> list[dict]`（第 112 行）

返回所有可用角色列表，每项为 `{"type": name, "description": str, "source": "builtin" | "user"}`。内置类型在前；用户目录下 `*.json` 按文件名排序追加，与内置同名的用户定义**不会覆盖**列表中的内置条目（`seen` 去重，第 126 行）。

### `get_claude_command(agent: AgentType, worktree: Path, headless: bool = False) -> list[str]`（第 141 行）

根据角色配置构建 `claude` CLI 参数列表（未列入 `__all__`，但被 `executor.py:207` 使用）：

- **headless=True**：固定输出 `claude -p "" --permission-mode <mode> --no-session-persistence --output-format stream-json --verbose --include-partial-messages`。`permission_mode` 仅接受 `bypassPermissions` / `acceptEdits`，其他值（含 `default`）强制降级为 `bypassPermissions`（第 152-153 行）。有 `allowed_tools` 时追加 `--allowedTools Read,Write,...`（逗号连接）。此分支**不使用 `worktree` 参数**。
- **headless=False（交互模式）**：若 `shutil.which("greywall")` 命中则前缀 `greywall --`（沙箱包装，第 148-149、167 行），随后 `claude <worktree>`；`permission_mode` 为 `bypassPermissions` 或 `acceptEdits` 时追加 `--permission-mode`，`default` 不追加；同样按需追加 `--allowedTools`。

### `get_agent_env(agent: AgentType) -> dict[str, str]`（第 183 行）

返回 `{"AGENT_GO_AGENT_TYPE": agent.type_name}`（`type_name` 为空字符串时返回空 dict）。每次调用构造新 dict，无共享引用。

## 关键逻辑与流程

1. **配置加载三级降级**（`agent_go/agents.py:82-109`）：候选路径列表按"用户全局 → 项目级"排列，第一个存在且解析成功的 JSON 即返回；解析失败（`json.JSONDecodeError` / `OSError` / `KeyError`）仅 `logger.debug` 记录后继续尝试下一个候选（第 96-97 行）；全部失败后降级内置表，再失败返回 `None`。
2. **headless 权限收紧**（第 152-153 行）：headless 模式无人交互，非白名单的 `permission_mode` 一律强制 `bypassPermissions`，保证子任务能自动执行完。
3. **greywall 沙箱包装单点**（第 148-149、167 行）：仅在交互模式下检测并包装 `greywall --`；`executor.py:204` 注释明确要求"greywall 包装单点完成，禁止重复包装"。

## 依赖关系

- **内部依赖**：无（不 import 任何 `agent_go` 内部模块）。本模块是被依赖方：
  - `executor.py:8` import `load_agent_type, get_claude_command, get_agent_env`；`executor.py:444` 局部 import `list_agent_types`（用于未注册角色时打印可用列表）。
  - `cli.py:14` import `load_agent_type, list_agent_types`。
- **标准库**：`json`、`logging`、`pathlib.Path`、`typing.Optional`、`dataclasses`；`get_claude_command` 内局部 import `shutil`。
- **外部 CLI**：构建的命令引用 `claude`（Claude Code CLI）与可选的 `greywall`（沙箱包装器，`shutil.which` 检测，不存在则不包装）。
- **文件系统**：读 `~/.agent_go/agents/*.json` 与 `<project_root>/.agent_go/agents/*.json`；导入时创建 `~/.agent_go/agents/` 目录。
- **环境变量**：本模块只**生产** `AGENT_GO_AGENT_TYPE`（由 `executor.py:441` 合入子进程 env），不读取任何环境变量。

## 数据结构与持久化

- **Agent 配置文件**（读，格式见模块 docstring `agent_go/agents.py:5-14`）：
  ```json
  {
    "type": "developer",
    "description": "开发者 — 实际编码实现",
    "claude_config": {
      "permission_mode": "default",
      "allowed_tools": ["Read", "Write", "Edit", "Bash"]
    },
    "preload_skills": ["security-review"]
  }
  ```
  读取路径：`~/.agent_go/agents/<name>.json`、`<project_root>/.agent_go/agents/<name>.json`。
- **`AgentType` dataclass**：见"公共接口"。`claude_config` 支持的键：`permission_mode`（`default` / `bypassPermissions` / `acceptEdits`）、`allowed_tools`（list[str]）、`extra_args`（内置 reviewer 中出现，但**代码未消费该键**）。
- 本模块无写持久化（除导入时 `mkdir`）。

## 错误处理与边界情况

- 配置文件 JSON 损坏 / 读文件失败：静默降级（`logger.debug`），继续下一候选或内置表，**不抛异常**（第 96-97 行）。
- `list_agent_types` 中单个用户 JSON 损坏：跳过该文件，不影响其他条目（第 135-136 行）。
- 未知角色名：`load_agent_type` 返回 `None`，由调用方处理 —— `executor.py:443-446` 打印 warning 并降级为无 agent 配置运行。
- `greywall` 未安装：交互模式不包装直接调 `claude`；`executor.py:214-217` 另有一层 `FileNotFoundError` 兜底。
- `headless=True` 时 `worktree` 参数被忽略 —— 签名要求传入但无效，属接口噪音。
- `list_agent_types` 假定 `AGENT_GO_AGENTS_DIR` 下 `*.json` 均为 UTF-8 文本；`OSError`（如权限问题）未被捕获，会向上抛出。

## 测试覆盖

对应测试文件：`tests/test_agents.py`（191 行，4 个测试类）。

- `TestLoadAgentType`：4 个内置角色加载（含 tester 的 `bypassPermissions` 断言）、用户自定义 `security-reviewer`（**条件断言** `if agent:`，依赖真实 `~/.agent_go/agents/` 内容，环境缺失时形同空跑）、不存在角色返回 `None`、列表至少含 4 个内置。
- `TestAgentTypeProperties`：dataclass 默认值与 `preload_skills`。
- `TestGetClaudeCommand`：headless 的默认权限强制为 `bypassPermissions`、`--allowedTools` 逗号拼接；交互模式下 mock `shutil.which` 覆盖 greywall 有/无、`bypassPermissions`/`acceptEdits` 透传、`default` 不加 `--permission-mode`。
- `TestGetAgentEnv`：`AGENT_GO_AGENT_TYPE` 值、返回 dict 类型、多次调用无共享引用（隔离性）。

运行方式：`pytest tests/test_agents.py`。注意测试直接读写真实 `~/.agent_go/agents/`（无 tmpdir 隔离）。

## 维护注意事项

- **导入副作用**：第 28 行模块级 `mkdir` 会在 import 时触碰用户 home 目录，测试与沙箱环境下不友好，建议移入函数懒执行。
- **`__all__` 不完整**：`get_claude_command` / `get_agent_env` / `AgentType` 被外部模块实际使用却未列入 `__all__`，容易误导"仅两个公共函数"的判断。
- **`preload_skills` 与 `extra_args` 是死字段**：全仓 grep 显示 `preload_skills` 仅在 `agents.py` 内读写、`executor.py` 的 skill 注入走的是独立的 `skill_names` 路径（`executor.py:434-435`）；`extra_args` 只在内置 reviewer 定义中出现，无任何代码消费。接入或删除需明确决策。
- **硬编码**：headless 的 claude 参数串（第 154-161 行）、4 个内置角色的工具白名单、目录名 `.agent_go/agents` 均硬编码；claude CLI 参数变更需同步改这里。
- **隐式耦合**：headless 权限收紧白名单 `("bypassPermissions", "acceptEdits")`（第 153 行）与 `executor.py` 对 headless `allowed_tools` 的强制生效逻辑（`executor.py:199-202`）共同保证只读角色在无人模式下不越权 —— 修改任一侧都需检查另一侧。
- **list 去重语义**：`list_agent_types` 中用户定义无法覆盖同名内置条目（内置先入 `seen`），与 `load_agent_type` 的"用户优先"语义相反，属不一致点。
- **项目级配置仅 `load_agent_type` 支持**：`list_agent_types` 不扫描项目级目录，CLI 列表展示可能与实际加载结果不一致。
