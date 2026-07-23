# git_utils 模块规格说明

## 概述

`agent_go/git_utils.py` 是 agent_go 的 git 操作与项目信息收集工具模块，封装了所有与 `git` CLI 的直接交互。它提供两类能力：一是面向 Plan/Decompose 阶段的项目上下文收集（文件列表、远程地址、分支/commit、关键目录与文件清单），二是面向 Execute 阶段的 git worktree 生命周期管理（创建、移除、prune）以及 `gc.auto` 配置开关。模块纯 stdlib 实现（`subprocess` + `logging` + `pathlib`），被 `api.py`、`pipeline.py`、`executor.py` 三个上层模块调用。

## 公共接口

模块通过 `__all__ = ["analyze_project", "get_git_info", "get_resource_map"]`（`agent_go/git_utils.py:7`）声明了三个公共函数；另有四个 `_` 前缀的"私有"函数被其他模块直接 import 使用，属于事实上的跨模块内部接口。

### 公共函数

- `analyze_project(repo: Path) -> str`（`agent_go/git_utils.py:9`）
  - 分析项目结构，返回文件清单字符串（每行一个相对路径）。
  - 副作用：在 `repo` 下执行 `git ls-files` 或 `find`（只读）。
  - 异常时返回 `""`（不抛出）。

- `get_git_info(repo: Path) -> dict[str, str]`（`agent_go/git_utils.py:24`）
  - 返回 `{"remote": ..., "branch": ..., "commit": ...}`，分别来自 `git remote get-url origin`、`git branch --show-current`、`git rev-parse --short HEAD`。
  - 任一命令失败则对应字段保留 `""`；整体异常返回全空 dict，不抛出。

- `get_resource_map(repo: Path, git_info: dict[str, str]) -> dict[str, Any]`（`agent_go/git_utils.py:91`）
  - 生成共享资源清单 dict（见"数据结构"节），由文件系统扫描 + `git_info` 合成。
  - 纯只读，不执行子进程，不做异常处理（`git_info` 缺键时用 `.get` 兜底为 `""`）。

### 内部函数（跨模块使用，注明"内部"）

- `_worktree_create(repo: Path, branch: str, worktree_path: Path) -> tuple[bool, str]`（内部，`agent_go/git_utils.py:41`）
  - 执行 `git worktree add -b <branch> <worktree_path> HEAD`，返回 `(success, error_message)`。
  - 错误消息取自 stderr，截断至 200 字符。被 `executor.py` 用于子任务 worktree 创建。

- `_worktree_remove(repo: Path, worktree_path: Path) -> tuple[bool, str]`（内部，`agent_go/git_utils.py:52`）
  - 执行 `git worktree remove --force <worktree_path>`；路径不存在时直接返回 `(True, "")`（幂等）。
  - 被 `pipeline.py` 在子任务完成后清理 worktree。

- `_worktree_prune(repo: Path) -> tuple[bool, str]`（内部，`agent_go/git_utils.py:65`）
  - 执行 `git worktree prune`，清理失效 worktree 元数据。被 `pipeline.py` 在全部清理后调用一次。

- `_set_gc_auto(repo: Path, value: str = "0") -> tuple[str, bool, str]`（内部，`agent_go/git_utils.py:76`）
  - 读取当前 `git config gc.auto`（为空时记为 `"1"`），再写入 `value`。
  - 返回 `(original_value, success, error_message)`。被 `pipeline.py` 用于执行前禁用 GC、执行后恢复原值。

## 关键逻辑与流程

模块无复杂状态机，各函数为独立的子进程封装，要点如下：

1. **项目分析双路径**（`agent_go/git_utils.py:11-22`）：以 `(repo / ".git").exists()` 分支——git 仓库走 `git ls-files`（取前 50 个文件），非 git 目录走 `find . -maxdepth 2 -type f`（取前 30 个文件，并用 `lstrip("./")` 去掉 `./` 前缀）。注意 `lstrip` 是字符集剥离而非前缀剥离，对以 `.` 或 `/` 开头的异常文件名可能过度剥离。
2. **git 信息串行采集**（`agent_go/git_utils.py:27-36`）：三条 git 命令顺序执行，每条独立判断 `returncode`，单条失败不影响其他字段（部分成功语义）。
3. **超时配置**：`analyze_project` 超时 5 秒（`:13`,`:17`），`get_git_info` 三条命令各 3 秒（`:28`,`:31`,`:34`）；worktree 系列函数与 `_set_gc_auto` **均未设 timeout**。
4. **gc.auto 保护协议**（由 `pipeline.py` 编排，本模块提供原语）：`pipeline.py:33` 执行前调 `_set_gc_auto(repo, "0")` 禁用自动 GC 并记录原值，`pipeline.py:128`/`:196` 在清理与异常路径恢复 `original_gc_value`，防止并发 worktree 操作期间后台 GC 干扰。
5. **资源清单扫描**（`agent_go/git_utils.py:103-112`）：对硬编码目录列表 `["src", "lib", "app", "components", "pages", "tests", "docs"]` 和关键文件列表 `["package.json", "requirements.txt", "Cargo.toml", "go.mod", "README.md", ".env.example", "docker-compose.yml"]` 逐个 `exists()` 探测，命中即入清单。

## 依赖关系

**内部模块依赖**：无 import 任何项目内模块（仅 stdlib）。

**被以下模块调用**（隐式契约，改动签名需同步）：

- `agent_go/api.py:8` — import `analyze_project`, `get_git_info`, `get_resource_map`，用于 `decompose`（`api.py:120-122`）与 `run`（`api.py:298-299`）阶段组装 LLM prompt 上下文。
- `agent_go/executor.py:9` — import `_worktree_create`（`executor.py:89` 处调用，失败时由 executor 做 clone fallback）。
- `agent_go/pipeline.py:9` — import `_set_gc_auto`, `_worktree_remove`, `_worktree_prune`，负责 gc.auto 保护协议与 worktree 批量清理。

**外部依赖**：

- CLI 命令：`git`（ls-files / remote / branch / rev-parse / worktree / config）、`find`（非 git 路径回退）。
- 文件系统：仅只读探测 `repo/.git`、`repo/<subdir>`、`repo/<keyfile>`；worktree 系列会在 `repo` 的 git 元数据中增删 worktree 记录，并修改 `repo` 的本地 git config（gc.auto）。
- 无环境变量依赖；不读写 `~/.agent_go/`。

## 数据结构与持久化

无持久化（不写任何文件；`_set_gc_auto` 修改的 git config 是 git 自身的 `.git/config`，非本模块定义的数据格式）。

关键返回值结构：

- `get_git_info` 返回 `{"remote": str, "branch": str, "commit": str}`，三键恒存在。
- `get_resource_map` 返回（`agent_go/git_utils.py:93-100`）：
  ```
  {"project_root": str, "git_remote": str, "git_branch": str,
   "git_commit": str, "directories": list[str], "key_files": list[str]}
  ```
  该 dict 被 `api.py` 直接嵌入 LLM prompt。
- worktree 系列统一返回 `tuple[bool, str]`（成功标志 + 错误消息，消息截断 200 字符）；`_set_gc_auto` 返回三元组多带一个 `original_value`。

## 错误处理与边界情况

- **不抛出策略**：`analyze_project` / `get_git_info` 捕获 `FileNotFoundError` 与 `subprocess.SubprocessError`（含 `TimeoutExpired`），`logger.debug` 记录后返回空值（`""` 或全空 dict），上层无需 try/except。
- **非零退出不算异常**：`get_git_info` 对每条命令单独查 `returncode`，支持部分成功；worktree 系列把非零退出的 stderr 作为错误消息返回给调用方决策（如 `executor.py` 的 clone fallback）。
- **幂等清理**：`_worktree_remove` 对不存在的路径直接返回成功（`agent_go/git_utils.py:54-55`）。
- **gc.auto 原值兜底**：读取为空时记为 `"1"`（`:82`），恢复时写回 `"1"` 而非删除配置项——会在 `.git/config` 中留下一个原本不存在的 `gc.auto = 1` 条目（与 git 默认行为等价，但配置文件有残留差异）。
- **无超时风险**：worktree 创建/移除/prune 与 `_set_gc_auto` 未设 `timeout`，极端情况下（如锁文件等待、NFS 卡死）可能阻塞调用线程。
- stderr 解码使用 `errors="replace"`，非 UTF-8 输出不会导致解码崩溃。

## 测试覆盖

无 `tests/test_git_utils.py`；直接单元测试位于 **`tests/test_project.py`**，覆盖三个公共函数：

- `TestAnalyzeProject`：git 仓库走 `git ls-files`、非 git 走 `find`、subprocess 抛异常返回 `""`（通过 mock `subprocess.run`）。
- `TestGetGitInfo`：全成功、git 不存在（FileNotFoundError）、部分命令失败时字段保留/置空。
- `TestGetResourceMap`：目录与关键文件命中、无命中时返回空列表。

四个内部 worktree/gc 函数**无直接单元测试**，仅经调用方间接验证：`tests/test_executor.py` mock `agent_go.executor._worktree_create`（含 clone fallback、已有 worktree 复用等场景），`tests/test_pipeline.py` mock `agent_go.pipeline._worktree_remove/_worktree_prune/_set_gc_auto`（含 gc.auto 禁用+恢复调用次数、每子任务 remove 一次、prune 一次的断言），`tests/test_integration.py:501` 有 worktree 创建的集成测试。

## 维护注意事项

- **私有函数实为跨模块接口**：`_worktree_create` 等四个函数虽带 `_` 前缀且不在 `__all__`，但被 `executor.py`/`pipeline.py` 直接 import，且测试按 `agent_go.executor._worktree_create` 等路径 mock（依赖"按使用处 import"的绑定方式）。重命名或改签名必须同步三处调用方与对应测试。
- **硬编码值**：目录白名单 7 项、关键文件白名单 7 项（`:103`,`:109`）、文件数截断 50/30、超时 5s/3s、错误消息截断 200 字符、gc.auto 空值兜底 `"1"`——均散落在代码中，无常量集中定义。
- **`analyze_project` 的 `lstrip("./")` 陷阱**（`:19`）：`str.lstrip` 按字符集剥离，文件名若以 `.`/`/` 开头会被误删；应改为 `removeprefix("./")`。
- **超时策略不一致**：信息收集函数有 timeout，worktree/gc 操作没有；若后续要统一，注意 `git worktree add` 在大仓库上可能耗时较长，需留足余量。
- **`analyze_project` 截断后可能含空串**：`git ls-files` 输出为空时 `"".split("\n")` 得 `[""]`，返回字符串可能含空行。
- **改进建议**（非必须）：为 worktree/gc 函数补直接单元测试（当前只能通过调用方 mock 间接覆盖）；将白名单与超时提为模块级常量；`_set_gc_auto` 恢复路径可考虑用 `git config --unset` 还原"原本不存在"的状态，避免配置残留。
