# workflow_gen 模块规格说明

## 概述

`workflow_gen.py` 是 agent_go 的 GitHub Actions CI 工作流自动生成模块，对应 CLI 的 `ci` 子命令。它通过检测仓库根目录下的标志性文件（如 `pyproject.toml`、`go.mod`）识别项目语言，从内置模板库中取出对应的 workflow YAML，写入目标仓库的 `.github/workflows/test.yml`。该模块与核心的 Plan -> Decompose -> Execute 编排流程无耦合，是一个独立的辅助工具命令，纯 stdlib 实现，仅依赖内部的 `console` 输出抽象。

## 公共接口

`__all__ = ["cmd_ci"]`（`agent_go/workflow_gen.py:10`），但模块级还有以下可复用成员：

- **`TEMPLATES: dict`** — 模块级常量（`workflow_gen.py:12-33`）。键为语言名（`python` / `go` / `node` / `rust` / `java`），值为 dict：
  - `detect: list[str]` — 用于语言探测的标志性文件名列表；
  - `workflow: str` — 完整的 workflow YAML 文本（均为 `name: Test`，触发器 `on: [push, pull_request]`，单 job 跑测试）。

- **`detect_language(repo: Path) -> Optional[str]`**（`workflow_gen.py:36`）
  - 参数：`repo` 仓库根目录路径。
  - 返回：匹配到的语言名；未匹配返回 `None`。
  - 语义：按 `TEMPLATES` 的 dict 迭代顺序（python 最先），对每个语言依次检查其 `detect` 列表中的文件是否存在，命中即返回。只读操作，无副作用。

- **`generate_workflow(repo: Path) -> tuple[Optional[str], Optional[str]]`**（`workflow_gen.py:44`）
  - 返回 `(lang, content)`；未检测到语言时返回 `(None, None)`。
  - 注意：`content` 直接返回 `TEMPLATES[lang]["workflow"]` 的引用，不是副本。

- **`cmd_ci(args=None) -> None`**（`workflow_gen.py:51`）— CLI 命令入口，由 `agent_go/cli.py:850` 在 `args.command == "ci"` 时调用。
  - 参数：`args` 通常为 argparse Namespace（`cli.py:90-92` 注册：`repo` 位置参数可选，`--dry-run` flag）。仅当 `args` 为真且含 `dry_run` 属性时走 Namespace 分支；否则回退到解析 `sys.argv`（`--dry-run` 及第 3 个位置参数作为 repo，见 `workflow_gen.py:56-60`）。
  - 副作用：非 dry-run 且文件不存在时，创建 `<repo>/.github/workflows/` 目录并写入 `test.yml`；所有结果通过 `console.print` 输出（中文提示）。

模块级 `console = get_default_console()`（`workflow_gen.py:8`）在 import 时绑定当前默认 Console 实例。

## 关键逻辑与流程

`cmd_ci` 的执行流程：

1. **参数解析**（`workflow_gen.py:52-60`）：优先从 `args` Namespace 取 `dry_run` 和 `repo`（`repo` 为空则取 `Path.cwd()`），`repo` 经 `.resolve()` 规范化。无 `args` 时回退到 `sys.argv` 手工解析。
2. **语言检测与工作流生成**（`workflow_gen.py:62`）：调用 `generate_workflow`；未检测到语言时打印"未检测到已知项目语言。支持: python, go, node, rust, java"并返回（`workflow_gen.py:63-65`）。
3. **目标路径**：固定为 `<repo>/.github/workflows/test.yml`（`workflow_gen.py:67-68`）。
4. **dry-run 分支**（`workflow_gen.py:70-73`）：打印语言、目标路径及完整 YAML 内容，不写任何文件。
5. **写入分支**（`workflow_gen.py:75-80`）：`mkdir(parents=True, exist_ok=True)` 创建目录；若 `test.yml` 已存在则打印"已存在"并返回（**不覆盖**）；否则以 UTF-8 写入模板内容并打印"已生成: <path> (<lang>)"。

语言检测的核心规则（`workflow_gen.py:36-41`）：双重循环按 `TEMPLATES` 插入顺序遍历，第一个存在标志文件的语言胜出——多语言混合仓库时 python 优先级最高，java 最低。

## 依赖关系

内部依赖：
- `agent_go/console.py`：`get_default_console()`（`console.py:142`）返回模块级 `Console` 实例；`Console.print`（`console.py:38`）是 `print()` 的替代，尊重 `--quiet` 静默模式。

外部依赖：
- 标准库：`pathlib.Path`、`typing.Optional`、`sys`（仅在无 `args` 的回退分支内 import）。
- 无第三方包、无 CLI 外部命令（不调用 git/claude/gh）、无环境变量。
- 文件系统：读取 `repo` 下的语言标志文件；写入 `<repo>/.github/workflows/test.yml`。

调用方：`agent_go/cli.py:17` import `cmd_ci`，`cli.py:849-850` 分发 `ci` 子命令；参数定义在 `cli.py:90-92`。

## 数据结构与持久化

- `TEMPLATES`：见"公共接口"。各模板硬编码了工具链版本：python 3.11、go 1.22、node 20、java 17 (temurin)，rust 无 setup 步骤直接 `cargo test`。
- 持久化：仅写入一个文件 `<repo>/.github/workflows/test.yml`（UTF-8，YAML）。模块自身无状态文件，不读写 `~/.agent_go/`。

## 错误处理与边界情况

- **无显式异常处理**：整个模块没有 try/except。`repo` 不存在或无写权限时，`mkdir`/`write_text` 的 `OSError` 会直接向上抛出（在 CLI 层会被 `cli.py` 主循环的通用处理捕获与否取决于外层）。
- **未检测到语言**：打印提示后正常返回，退出码仍为 0，不视为错误。
- **文件已存在**：静默跳过（打印"已存在"），不覆盖、不报错，保证幂等。
- **语言优先级**：多语言标志文件同时存在时按模板顺序取第一个，可能不符合用户预期（如同时有 `pyproject.toml` 和 `package.json` 的项目会判定为 python）。
- **无超时/中断处理**：操作为瞬时文件 IO，不涉及。
- `dry_run` 分支不会创建 `.github/workflows` 目录。

## 测试覆盖

对应测试文件 `tests/test_workflow_gen.py`（167 行，全覆盖），测试类：
- `TestTemplates`：模板结构完整性（`detect`/`workflow` 键、支持语言集合、python/go 模板内容关键词）。
- `TestDetectLanguage`：五种语言的标志文件检测（含 maven/gradle 两种 java 文件）、未知语言返回 `None`、多语言时的优先级。
- `TestGenerateWorkflow`：返回语言与模板内容一致、未知语言返回 `(None, None)`、内容为非空字符串。
- `TestCmdCi`：dry-run 不写文件、正常模式创建 `test.yml`、已存在时不覆盖、未检测到语言时打印提示、指定 `repo` 参数。均通过 `tmp_path` 和 mock `console.print` 隔离。

## 维护注意事项

- **模板为硬编码字符串**：YAML 内联在 Python 字符串里，修改模板需直接改 `TEMPLATES`；缩进错误不会有任何校验（无 YAML parse），改动后应人工检查或补一个 YAML 合法性测试。
- **输出文件名固定为 `test.yml`**：与"已存在则跳过"逻辑耦合——用户若已有自己的 `test.yml`，本命令直接失效；也无法生成多个 workflow。
- **工具链版本硬编码**（python 3.11 等），不会跟随项目实际版本（如不读 `pyproject.toml` 的 `requires-python`），生成的 CI 可能与项目要求不符。
- **`sys.argv` 回退分支**（`workflow_gen.py:56-60`）是 argparse 之外的第二套解析逻辑，与 `cli.py:90-92` 的定义存在隐式耦合：`sys.argv[2]` 假定命令名在 `sys.argv[1]`，独立调用 `agent_go.workflow_gen` 之外入口时可能错位。正常 CLI 路径走 Namespace 分支，该回退基本只在直接以脚本方式调用时生效。
- **`generate_workflow` 返回模板字符串引用**：调用方若原地修改返回值会污染全局 `TEMPLATES`（当前无此用法，但新增调用方时需注意）。
- **检测只看文件存在性**：空文件也算命中（测试即如此使用），无内容校验。
- 改进方向（如需）：支持 `--force` 覆盖已存在文件；从项目配置推断版本；将模板外置为 YAML 文件并做格式校验。
