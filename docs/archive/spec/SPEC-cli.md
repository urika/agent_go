# cli 模块规格说明

## 概述

`agent_go/cli.py` 是 agent_go 的命令行入口与命令分发层。它负责构建 argparse 子命令体系、解析参数、初始化运行环境（Console、logger、任务目录），并驱动 `run` 命令的完整 Plan -> Confirm -> Decompose -> Pipeline 前置流程；其余子命令（list/show/status/clean/pr/review/cache/skills/agents/config）多为读取 `~/.agent_go/` 下持久化数据并格式化输出的查询/管理操作。真正的子任务执行调度委托给 `pipeline._run_pipeline`，TUI 状态界面委托给 `tui.cmd_status_tui`，评估与 CI 生成分别委托给 `eval.cmd_eval` 和 `workflow_gen.cmd_ci`。

## 公共接口

`__all__`（`agent_go/cli.py:21-24`）声明导出：`main, cmd_run, cmd_resume, cmd_list, cmd_show, cmd_status, cmd_config, cmd_clean, cmd_pr, cmd_review`。模块实际还定义了若干未列入 `__all__` 但由 `main` 分发的命令函数。

| 函数 | 签名 | 说明 |
|---|---|---|
| `main` | `main() -> None`（:818） | CLI 入口。构建 parser、按 `args.command` 分发到各 `cmd_*`；捕获 `KeyboardInterrupt`（exit 130）和 `BrokenPipeError`（exit 0）。无子命令时打印 help。 |
| `cmd_run` | `cmd_run(args=None)`（:117） | 核心命令：规划 + 拆解 + 执行一个任务。`args=None` 时自行解析 argv 并在非 run 命令时回退到 `main()`。副作用：创建任务目录、写 `PLAN.md`/`meta.json`、调用 `_run_pipeline`。路径不存在时 `sys.exit(1)`。 |
| `cmd_resume` | `cmd_resume(args=None)`（:299） | 恢复 running/paused 状态的任务。读取 `meta.json`，从独立 `result.json` 恢复结果，重建 `worktree_map`/`results_map`/`completed_ids` 后调用 `_run_pipeline`。任务不存在或状态不可恢复时 `sys.exit(1)`。 |
| `cmd_list` | `cmd_list() -> None`（:388） | 列出 `AGENT_GO_DIR` 下所有 `task-*` 目录的概要表格。 |
| `cmd_show` | `cmd_show(args=None)`（:405） | 打印单个任务详情：子任务列表、Agent 类型及来源、Skill、各子任务结果 summary。任务不存在时 `sys.exit(1)`。 |
| `cmd_status` | `cmd_status(args=None)`（:551） | 状态监控路由：默认调用 `cmd_status_tui()`（TUI），`--no-tui` 时走 `_cmd_status_text`。 |
| `cmd_config` | `cmd_config() -> None`（:689） | `json.dumps` 打印 `load_config()` 结果。 |
| `cmd_clean` | `cmd_clean() -> None`（:693） | 交互确认后删除所有任务目录，并对每个关联 repo 执行 `git worktree prune` 和删除 `<task_id>/*` 标签。 |
| `cmd_pr` | `cmd_pr(args=None)`（:482） | 由 `meta.json` + `SHARED_CONTEXT.md` 生成 PR 描述。`--offline` 仅写 `PR.md`；否则经临时文件调用 `gh pr create`，失败时备份到 `PR.md`。任务不存在时 `sys.exit(1)`。 |
| `cmd_review` | `cmd_review(args=None)`（:445） | 代码审查。headless（`--yes`）模式执行 `claude -p <prompt> --permission-mode bypassPermissions --no-session-persistence`；交互模式执行 `claude <repo>`。 |
| `cmd_skills` | `cmd_skills() -> None`（:731） | 列出 `list_skills()` 返回的可用 Skill（未列入 `__all__`）。 |
| `cmd_agents` | `cmd_agents() -> None`（:807） | 列出 `list_agent_types()` 返回的 Agent 类型，标注内置/用户来源（未列入 `__all__`）。 |
| `cmd_cache` | `cmd_cache(args=None)`（:745） | Plan 缓存管理，子命令 `list/clean/clear/stats`（未列入 `__all__`）。`clear` 直接 `shutil.rmtree` 缓存目录后重建。 |

内部函数：

- `_build_parser()`（:26，内部）— 构建完整 argparse parser，定义 15 个子命令：run / resume / list / show / status / clean / config / skills / agents / pr / ci / review / cache / eval。
- `_cmd_status_text(args=None)`（:564，内部）— 文本模式状态监控，含僵尸任务检测（见下）。
- `_cache_size() -> str`（:794，内部）— 统计缓存目录所有 `*.json` 总大小，返回人类可读字符串（B/KB/MB）。

注：`ci` 和 `eval` 子命令在 `main` 中直接分发给 `workflow_gen.cmd_ci` 和 `eval.cmd_eval`，本模块不提供对应包装函数。

## 关键逻辑与流程

### cmd_run 主流程（agent_go/cli.py:117-297）

1. 参数提取（:127-139）：`repo`  resolve 为绝对路径；`--docs`/`--skill` 按逗号拆分；`headless = auto_yes or args.headless`（:136），即 `--yes` 隐含无头模式。
2. Console 初始化（:142-143）：`Console(quiet=quiet or headless, verbose=verbose)` 并设为默认 console —— headless 隐含 quiet。
3. 并发保护（:146-148）：`parallel > 1` 且非 headless 时警告并强制降为串行（`parallel = 1`），避免同时打开多个交互式 Claude Code 终端。
4. repo 存在性检查（:150-152），失败 `sys.exit(1)`。
5. `load_config()`；`--yes` 时覆写 `config["behavior"]` 的三个 auto 开关（:156-159）。
6. 任务 ID 生成（:162-174）：格式 `task-{YYYYMMDD-HHMMSS-mmm}-{4位hex}`（毫秒 + `os.urandom(2).hex()`），`mkdir(exist_ok=False)` 碰撞时最多重试 5 次（间隔 10ms），仍失败则 `exist_ok=True` 兜底。
7. `setup_logger` + `_detect_tool_versions` 记录环境信息（:176-185）。
8. Skill 加载（:187-199）：显式 `--skill` 走 `load_skills`；否则若 `config.skills.auto_discover` 为真则 `discover_skills(task, repo, max_auto)`（`max_auto_skills` 默认 3）。
9. Agent 类型（:201-206）：`--agent-type` 优先，否则 `config.agents.default`，再兜底 `"developer"`，经 `load_agent_type` 加载。
10. Plan 生成与确认（:219-280）：
    - `generate_plan(...)` 最多尝试 3 次（:228-235），全部失败则直接 `decompose_fallback`（:277-280）。
    - 成功后进入 `confirm_plan`；返回 `"__FALLBACK__"` 哨兵字符串表示用户选择降级，改走 `decompose_fallback`（:241-245）。
    - 用户拒绝（返回 `None`）且未达 `max_plan_iterations`（默认 5）时迭代重新生成 + 重新确认（:247-266）。
    - 达到最大迭代且从未降级时使用最后一版 plan（:268-270）。
    - `plan_to_subtasks` 转换、写 `PLAN.md`（:272-276）。
11. `confirm_subtasks` 确认子任务（:283），组装 `meta` dict 写 `meta.json`（:285-295），字段含 `task_id/task/repo/created/status/reference_docs/issue/subtasks/results/tool_versions/skills/agent_type/remote_url`。
12. 调用 `_run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta, remote_url=remote_url)`（:297）。

### cmd_resume 恢复流程（agent_go/cli.py:299-386）

1. 双路径取参：优先 `args.task_id`，否则解析 `sys.argv`（:301-311）。
2. 校验：任务目录必须存在；`meta.status` 必须是 `running` 或 `paused`（:313-319）。
3. 结果恢复（:321-343）：`meta.results` 为空时，遍历子任务读取 `<task_dir>/<subtask_id>/result.json` 重建 results；按 `subtask_id` 建立 `results_map`，存在 `<id>/work/.git` 的重建 `worktree_map`；状态为 `completed/no_changes/degraded` 的计入 `completed_ids`。
4. 命令行 `--remote` 优先于 `meta.remote_url`，合并后回写（:374-376）。
5. `meta.status` 重置为 `running` 并落盘（:382-383），带恢复状态调用 `_run_pipeline`（:385-386）。

### _cmd_status_text 僵尸检测（agent_go/cli.py:585-656）

- `status == "running"` 且 `execution.log` 的 mtime 超过 `ZOMBIE_TIMEOUT = 600` 秒未更新（:593-598），判定为僵尸任务：将 meta 改为 `failed` 并写入 `_zombie_note`，然后遍历任务目录下所有 `*.pid` 文件尝试 `SIGKILL` 残留进程（:602-612），最后回写 meta.json。
- `--watch` 模式每 5 秒清屏刷新（:667-687，`os.system("clear"/"cls")`）；`--verbose` 且 running 时从日志尾 50 行筛选 claude 事件行显示最后 2 条（:573-583）。

### cmd_clean 清理流程（agent_go/cli.py:693-729）

`safe_input` 确认后：先收集 `repo -> {task_ids}` 映射（仅 repo 路径仍存在时），`shutil.rmtree` 删除全部任务目录，再对每个 repo 执行 `git worktree prune`，并用 `git tag -l "<task_id>/*"` + `git tag -d` 删除关联标签（:717-726）。

### main 分发与信号处理（agent_go/cli.py:818-859）

if/elif 链按 `args.command` 分发 15 个子命令；`KeyboardInterrupt` 打印中断提示并 `sys.exit(130)`，`BrokenPipeError` 静默 `sys.exit(0)`。

## 依赖关系

### 内部模块（agent_go/cli.py:7-17）

- `config`：`load_config`、`safe_input`（cmd_clean 确认）、`setup_logger`、`AGENT_GO_DIR`（= `Path.home() / ".agent_go"`，config.py:15）。
- `console`：`Console`、`set_default_console`。
- `api`：`generate_plan`、`decompose_fallback`；`cmd_cache` 内延迟导入 `list_cache_entries`、`clean_expired_cache`、`_cache_dir`。
- `ui`：`confirm_plan`、`plan_to_md`、`plan_to_subtasks`、`confirm_subtasks`。
- `utils`：`read_reference_docs`、`_detect_tool_versions`。
- `pipeline`：`_run_pipeline`（执行管线的唯一入口，cmd_run/cmd_resume 共用）。
- `skills`：`load_skills`、`discover_skills`、`render_skill_for_plan`、`list_skills`。
- `agents`：`load_agent_type`、`list_agent_types`。
- `eval.cmd_eval`、`tui.cmd_status_tui`、`workflow_gen.cmd_ci`（仅在 `main` 分发）。

### 外部依赖

- CLI 命令：`claude`（cmd_review）、`gh`（cmd_pr 在线模式）、`git`（cmd_clean 的 worktree prune / tag 操作）；`_detect_tool_versions` 亦探测工具版本。
- 环境/终端：`os.system("clear" | "cls")`（status --watch）；`os.urandom`（任务 ID 后缀）。
- 文件系统：`~/.agent_go/` 为全部任务数据与配置的根目录；`tempfile.NamedTemporaryFile`（cmd_pr 在线模式，`delete=False`，用后 `os.unlink`）。

## 数据结构与持久化

本模块读写 `~/.agent_go/<task_id>/` 下文件：

- `meta.json`（写：cmd_run :295、cmd_resume :383、_cmd_status_text 僵尸处理 :613；读：cmd_resume/cmd_show/cmd_list/cmd_pr/_cmd_status_text）。关键字段见上文 cmd_run 第 11 步；resume 时依赖 `subtasks[].id`、`results[].subtask_id/status/summary/sandbox_type/duration_sec`；`base_branch`（cmd_pr，缺省 `"main"`）。
- `PLAN.md`（cmd_run :275 写入，`plan_to_md` 生成的 Markdown）。
- `SHARED_CONTEXT.md`（cmd_pr :507-508 只读，不存在则 Verification 段为占位文本）。
- `PR.md`（cmd_pr 离线写入或在线失败时备份）。
- `execution.log`（_cmd_status_text 读取 mtime 与尾部事件行）。
- `<task_dir>/<subtask_id>/result.json`（cmd_resume 恢复读取）、`<subtask_id>/work/.git`（worktree 有效性判定）。
- `*.pid`（僵尸检测时读取并 kill 对应进程）。
- Plan 缓存目录（`api._cache_dir()`），cmd_cache 的 list/clean/clear/stats 操作对象；条目结构为 `{"cache_key": ..., "meta": {"task","created_at","hit_count"}}`。

## 错误处理与边界情况

- **Plan 生成重试**：`generate_plan` 捕获一切 `Exception`，最多 3 次，全部失败降级 `decompose_fallback`（:228-235, :277-280）。
- **用户降级**：`confirm_plan` 返回 `"__FALLBACK__"` 哨兵时切换到本地规则拆解（:241-245, :261-266）；迭代中 `generate_plan` 抛错同样降级（:249-258）。
- **sys.exit 路径**：repo/任务不存在、任务状态不可恢复、缺参数时退出码 1；`main` 层 Ctrl+C 退出码 130，BrokenPipe 退出码 0。
- **cmd_clean 容错**：单个 meta.json 读取失败仅 `logger.debug` 跳过（:715-716）；git 命令全部 `capture_output=True` 且忽略返回码。
- **cmd_resume 结果恢复**：单个 `result.json` 的 `JSONDecodeError/OSError` 被吞掉仅记 debug（:331-332）。
- **argv 解析容错**：手工解析 `--parallel`/`--remote`/`--pr` 时 `IndexError/ValueError` 降级（parallel 默认 3 或直接忽略，:354-366, :461-465）。
- **僵尸检测的 kill**：`ValueError/FileNotFoundError/ProcessLookupError/PermissionError` 逐个忽略，外层还有裸 `except Exception` 兜底（:609-612）。
- **任务 ID 碰撞**：5 次重试后 `exist_ok=True` 强行复用目录（:173-174），极端情况下可能混入旧数据。

## 测试覆盖

对应测试文件：`tests/test_cli.py`（291 行，其 docstring 自述：全覆盖 `_build_parser, cmd_list, cmd_show, cmd_config, cmd_clean, cmd_status` 基础路由；部分覆盖 `cmd_run`/`cmd_resume`）。

主要场景：

- `TestBuildParser`：9 个用例验证各子命令参数解析与默认值（如 run 的 task 默认值 `"请根据项目情况完成改进"`、parallel 默认 1）。
- `TestCmdList`：空目录与多任务列表输出（patch `AGENT_GO_DIR`）。
- `TestCmdShow`：不存在任务触发 `SystemExit`；存在任务用 `SimpleNamespace` 传参打印详情。
- `TestCmdConfig`：验证输出含 `plan_api`、`behavior` 键。
- `TestCmdClean`：确认/取消/无任务三分支（patch `safe_input`、`shutil.rmtree`、`subprocess.run`）。
- `TestCmdStatus`：`--no-tui` 路由到文本模式、默认调 `cmd_status_tui`。
- `TestMain`：8 个用例验证命令分发与无命令时打印 help。

未覆盖：`cmd_pr`、`cmd_review`、`cmd_cache`、`cmd_skills`、`cmd_agents`、`_cmd_status_text` 的僵尸检测逻辑、`cmd_run` 的 Plan 迭代/降级路径、`cmd_resume` 的实际恢复执行。

## 维护注意事项

- **已修复（2026-07-23，docs/ISSUES.md ISSUE-1）— fallback 路径崩溃**（原 cli.py:241-273）：现已对拆解/保存块加 `if confirmed_plan is not None` 守卫，并补 4 个回归测试（`tests/test_cli.py::TestCmdRunFallback`）。原问题：：`confirm_plan` 返回 `"__FALLBACK__"` 时 `confirmed_plan` 被置 `None` 且 `subtasks` 已由 `decompose_fallback` 赋值，:268 的 `'subtasks' not in locals()` 条件因此为 False 而跳过；随后 :272 无条件执行 `plan_to_subtasks(confirmed_plan, ...)`，而 `plan_to_subtasks` 首行即 `plan.get(...)`（ui.py:252），传入 `None` 会抛 `AttributeError`。:244 设置的 `doc_paths = []` 也会被 :273 的 `final_doc_paths` 覆盖。降级路径的测试缺失使该问题未暴露。
- **cmd_resume 双重解析**（:349-366）：即使已通过 `args` Namespace 取得参数（:301-306），:349-352 仍无条件重扫 `sys.argv` 覆写 `auto_yes/headless/parallel/remote_url`。当前从 `main` 进入时两者来源相同故结果一致，但单元测试中直接传 Namespace 调用会产生依赖进程 argv 的隐式行为；`--parallel` 解析失败时默认值 3 与 argparse 默认值 1 不一致。
- **硬编码值**：僵尸超时 600 秒（:593）、status 刷新间隔 5 秒（:687）、Plan 重试 3 次（:228）、任务 ID 重试 5 次（:162）、PR 标题截断 72 字符（:537）、run 默认任务描述 `"请根据项目情况完成改进"`（:37）。
- **隐式耦合**：`__all__` 与实际分发集合不一致（cmd_skills/cmd_agents/cmd_cache 可经 CLI 调用但未导出；cmd_eval/cmd_ci 在他模块）；`_run_pipeline` 的位置参数多达 10+4 个，cmd_run 与 cmd_resume 两处调用点需同步维护；status/list/show/pr 均直接读 `meta.json` 的字段名，与 pipeline/executor 的写入方形成无 schema 约束的字符串耦合。
- **`__FALLBACK__` 哨兵**：以字符串魔法值跨 `ui.confirm_plan` 与本模块通信，改动任一端都需同步。
- **cmd_list 表头格式串**（:393）：`{'任务ID':<<26}` 把 `<` 当作填充字符，表头实际输出带 `<` 补齐，疑似笔误（数据行 :403 用的是 `:<25`）。
- **跨平台**：`os.system("clear"/"cls")` 与 `os.kill(..., SIGKILL)` 假定 posix 语义为主，Windows 支持有限。
- **改进建议**：（fallback 路径崩溃已修复）统一 cmd_resume 参数来源（删除 sys.argv 重扫）；为 meta.json 引入集中式读写/校验层；将 `__FALLBACK__` 改为 None 或专用枚举；为 cmd_pr/cmd_cache/僵尸检测补测试。
