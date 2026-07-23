# agent_go 已知问题清单

> 来源：2026-07 模块 spec 梳理（`docs/spec/`）中发现的代码缺陷。
> 以下每条均已对照源码逐行核实（核实日期 2026-07-23，基于 v2.0.0 工作区代码）。
> ISSUE-1 ~ ISSUE-6 于 2026-07-23 修复；ISSUE-7 ~ ISSUE-14 于同日修复。
> 全部 14 项均已修复，655 个测试通过（`pytest tests/`）。

## P0 — 必须修复

### ISSUE-1 `__FALLBACK__` 降级路径必然崩溃（AttributeError）

- **位置**：`agent_go/cli.py:241-273`（触发点 `agent_go/ui.py:252`）
- **状态**：✅ 已修复（2026-07-23）— cli.py 的拆解/保存块加 `if confirmed_plan is not None` 守卫；补 4 个回归测试（`tests/test_cli.py::TestCmdRunFallback`）

**问题**：`confirm_plan` 返回 `"__FALLBACK__"` 时（用户在 Plan 确认环节选择降级），代码走 `decompose_fallback` 得到 `subtasks` 并设 `confirmed_plan = None`（cli.py:243-245）。但 cli.py:272 无条件执行：

```python
subtasks = plan_to_subtasks(confirmed_plan, logger, repo=repo)  # confirmed_plan=None
```

`plan_to_subtasks` 首行即 `plan.get("shared_resources", {})`（ui.py:252），传入 `None` 必抛 `AttributeError`，降级得到的 `subtasks` 也被覆盖。cli.py:275 的 `plan_to_md(confirmed_plan)` 同样会拿到 `None`。

受影响路径共三条（全部通向同一个崩溃点）：

- cli.py:241-245 — 首次确认时选择降级
- cli.py:255-258 — 重试生成 Plan 失败后的降级（`break` 后落到 272 行）
- cli.py:261-266 — 重试后再次选择降级（同上）

另注意 cli.py:273 `doc_paths = final_doc_paths` 会覆盖降级路径中已设置的 `doc_paths = []`。

**修复建议**：272-275 行加守卫，仅当 `confirmed_plan is not None` 时执行 `plan_to_subtasks` / `plan_to_md` / 覆盖 `doc_paths`；并为三条降级路径补测试。

---

## P1 — 功能受损

### ISSUE-2 Plan 缓存 key 混入 commit hash，缓存在活跃仓库中近乎失效

- **位置**：`agent_go/api.py:296-307`（`get_cache_key`）
- **状态**：✅ 已修复（2026-07-23）— `key_parts` 移除 `commit`，docstring 同步修正并注明原因

**问题**：`key_parts` 包含 `git_info.get("commit", "")`（api.py:305）。仓库每产生一次提交，缓存 key 即变化，此前缓存的 Plan 全部 miss——与 README 宣称的"Plan cache 减少 API 成本"的设计意图相悖。在持续开发的目标仓库上使用该工具时，缓存命中率趋近于零。

同时 docstring（`SHA256(task + project_files[0:100] + remote + branch)`）与实现不符：实现取 `project_files[:2000]` 且多了 `commit`，误导维护者。

**修复建议**：从 `key_parts` 中移除 `commit`（如担心仓库状态漂移影响 Plan 质量，可改用更粗粒度的信号或直接接受漂移——Plan 本来也会经用户确认）；同步修正 docstring。

### ISSUE-3 `Console.table()` 只打印表头，不打印数据行

- **位置**：`agent_go/console.py:91-105`（`table`），连带影响 `data_table`（console.py:113+）
- **状态**：✅ 已修复（2026-07-23）— 补数据行打印循环；顺带修复同函数内 `self.sep(sum(col_widths))` 把宽度误传给 `char` 参数的隐藏 bug（改为 `sep(width=...)`）；补 3 个回归测试

**问题**：`table()` 用 `rows` 计算列宽后，只 `print(header_line)` + `sep()`，从不遍历打印数据行。`data_table()` 基于 `table()` 实现，同样只显示表头——任何用这两个方法展示数据的命令输出都缺数据行，疑似未完成实现。

**修复建议**：在 `sep()` 之后补数据行打印循环（逐行 `"".join(f"{cell:<{w}}")`），并补断言数据行内容的测试。

---

## P2 — 边界路径资源泄漏

### ISSUE-4 pipeline resume 提前返回路径不恢复 `gc.auto` 与信号处理器

- **位置**：`agent_go/pipeline.py:53-62`
- **状态**：✅ 已修复（2026-07-23）— 提前 return 前恢复信号处理器与 `gc.auto`（与其他退出路径同一模式）；`try/finally` 重构留作后续改进

**问题**：`_run_pipeline` 在 pipeline.py:33 将目标仓库 `gc.auto` 置为 `"0"`、在 :53-54 安装 SIGINT/SIGTERM 处理器后，:58-62 的"所有子任务已完成，无需恢复执行"分支直接 `return`——既不恢复信号处理器，也不恢复 `gc.auto`。正常结束路径（:134-135、:195-196）和中断路径（:124-128）都有恢复逻辑，唯独这条 resume 提前返回路径遗漏。后果是用户仓库的 git config 被永久留在 `gc.auto=0`。

**修复建议**：将恢复逻辑抽为统一的清理函数并用 `try/finally` 包裹，或在 :62 return 前补齐恢复；根治方案是整体重构为 `try/finally` 结构，消除多条路径各自维护恢复代码的现状。

---

## P3 — 代码质量（不影响运行）

### ISSUE-5 `_is_safe_verification_command` 返回类型注解与实际不符

- **位置**：`agent_go/utils.py:145`
- **状态**：✅ 已修复（2026-07-23）— 注解改为 `-> tuple[bool, str]`

**问题**：签名标注 `-> bool`，实际返回二元组 `(is_safe, reason)`（如 utils.py:158 `return False, "空命令"`）。docstring 第 154 行描述是正确的，仅注解错误，会误导类型检查与调用方。

**修复建议**：改为 `-> tuple[bool, str]`。

### ISSUE-6 `cmd_list` 表头格式串使用不存在的填充符

- **位置**：`agent_go/cli.py:395`（`cmd_list`）
- **状态**：✅ 已修复（2026-07-23）— 填充符改为空格（`:<26` 等）

**问题**：`f"{'任务ID':<<26} ..."` 中 `:<<26` 的填充字符是 `<`（左对齐符被重复解析为填充符），实际效果是用 `<` 字符填充，与相邻数据行的空格填充不一致，表头显示为 `任务ID<<<<<<<<...`。且中文按字符数而非显示宽度对齐，中英文混排时列对不齐。

**修复建议**：去掉重复的 `<`（`{:<26}`），或统一按显示宽度（East Asian Width）对齐。

---

## 待处理改进项（已全部修复）

> 以下为 spec 梳理（`docs/spec/`）中发现、2026-07-23 逐条核实确认成立的非阻塞问题，均已于当日修复。

### ISSUE-7 依赖循环时 meta.json 误标 `completed`

- **位置**：`agent_go/pipeline.py:76-78`（break）+ `:200-201`（状态判定）
- **状态**：✅ 已修复（2026-07-23）— wave 为空时把未调度子任务以 `failed` 写入 `results_map`；补回归测试 `tests/test_pipeline.py::TestPipelineDependencyFailure`
- **严重度**：P2

**问题**：波次调度中若 `wave` 为空（依赖循环或依赖不可满足），仅 `logger.error` 后 `break`。未执行的子任务不在 `results_map` 中，而收尾处 `has_failed = any(r.get("status") == "failed" for r in results_map.values())` 只看已执行结果——因此存在子任务从未执行、meta 却被标记为 `completed` 的情况。

**修复建议**：break 前把未完成的子任务以 `status="failed"`（原因：依赖不可满足）写入 `results_map`，或收尾判定中额外检查 `len(results_map) < len(confirmed)` 时标记失败。

### ISSUE-8 `read_reference_docs` 路径穿越校验可被兄弟前缀目录绕过

- **位置**：`agent_go/utils.py:12`
- **状态**：✅ 已修复（2026-07-23）— 改用 `path.is_relative_to(repo_root)`；补 2 个回归测试（兄弟前缀目录拒绝 + repo 内文件放行）
- **严重度**：P2（安全相关）

**问题**：`str(path).startswith(str(repo.resolve()))` 是纯字符串前缀匹配，不含路径分隔符。repo 为 `/tmp/proj` 时，`--docs ../proj-secret/xx.md` 解析为 `/tmp/proj-secret/xx.md`，仍通过校验——可读取 repo 外的文件内容并注入 Plan prompt。

**修复建议**：改用 `path.is_relative_to(repo.resolve())`（pathlib，Python 3.9+ 可用），或比较前在 repo 路径末尾补 `os.sep`。

### ISSUE-9 TUI 状态栏快捷键提示与过滤映射错位

- **位置**：`agent_go/tui.py:168`（提示文本）vs `:88-93`（filter_mode 分支）、`:182-183`（按键映射）
- **状态**：✅ 已修复（2026-07-23）— 按键映射改为 `{1:0, 2:1, 3:2, 4:3}`，与状态栏提示对齐
- **严重度**：P3

**问题**：状态栏提示 `[1]all [2]run [3]done [4]fail`，但按键 1-4 映射为 `filter_mode = key - ord('0')`，而分支中 1=running、2=completed、3=failed，4 无分支（等于不过滤）。即按 `1`（提示 all）实际只看 running，按 `4`（提示 fail）实际显示全部——提示与行为整体错一位。

**修复建议**：按键映射改为 `{'1': 0, '2': 1, '3': 2, '4': 3}`（0=全部）。

### ISSUE-10 `cache.enabled=false` 只禁写、不禁读

- **位置**：`agent_go/api.py:313-356`（`load_cached_plan`）vs `:361`（`save_cached_plan`）
- **状态**：✅ 已修复（2026-07-23）— `load_cached_plan` 开头检查 `cache.enabled`；补 2 个回归测试（禁读 + 正常读取）
- **严重度**：P3

**问题**：`save_cached_plan` 在 `cache.enabled=false` 时直接返回（不写），但 `load_cached_plan` 不检查 `enabled`——`:354` 的 `enabled` 判断仅控制一条日志。用户在 config 中关闭缓存后，旧缓存仍会被读取命中，与配置语义不符。

**修复建议**：`load_cached_plan` 开头检查 `config.get("cache", {}).get("enabled", True)`，为 False 时直接返回 None。

### ISSUE-11 全局 `role_skill_map.json` 是死代码，README 描述与行为不符

- **位置**：`agent_go/role_skill_map.py:46-47`（`_global_map_path`）、`:65-69`（`load_role_skill_map`）；`README.md:125`
- **状态**：✅ 已修复（2026-07-23）— `load_role_skill_map` 实现三层合并加载（项目 > 全局 > 默认，规则拼接 + 标量覆盖）；补 2 个回归测试
- **严重度**：P3

**问题**：`_global_map_path()` 返回 `~/.agent_go/role_skill_map.json`，但全仓库无任何调用方；`load_role_skill_map` 只读项目级文件，读不到则返回 `DEFAULT_MAP`。而 README 宣称「`~/.agent_go/role_skill_map.json` 定义规则」——用户按文档放全局规则文件不会生效。另外项目级规则是整体替换 `DEFAULT_MAP`，无合并语义，内置规则全部丢失。

**修复建议**：二选一——(a) 在 `load_role_skill_map` 中补上全局路径加载（项目级优先，规则 dict 做合并而非替换）；(b) 删除 `_global_map_path` 并修正 README。倾向 (a)，与 README 对齐。

### ISSUE-12 三个模块 import 时绑定默认 Console，quiet 配置不生效

- **位置**：`agent_go/config.py:8`、`agent_go/eval.py:12`、`agent_go/workflow_gen.py:8`
- **状态**：✅ 已修复（2026-07-23）— `console.py` 新增 `_LazyConsole` 代理，三模块改用它做模块级绑定，`set_default_console()` 后生效
- **严重度**：P3

**问题**：这三个模块在 import 时执行 `console = get_default_console()`，绑定的是模块级默认实例（非 quiet）。`cli.py` 在 `cmd_run` 中才 `set_default_console(...)`，晚于 import——因此经由 `config.console` / `eval.console` / `workflow_gen.console` 的输出永不响应 quiet 配置。`pipeline.py:19`、`executor.py:195/378` 在函数内运行时取值，无此问题。

**修复建议**：去掉模块级绑定，改为函数内调用 `get_default_console()`（与 pipeline/executor 一致）。

### ISSUE-13 `list_agent_types` 去重顺序使用户同名覆盖不可见

- **位置**：`agent_go/agents.py:112-138`
- **状态**：✅ 已修复（2026-07-23）— 列表改为用户优先，同名覆盖标注 `user (overrides builtin)`；移除 `agents.py`/`skills.py` 的 import 时 mkdir 副作用；补 2 个回归测试
- **严重度**：P3

**问题**：列表先加内置类型并标记 `seen`，用户目录中的同名 JSON 被 `if name not in seen` 跳过——但 `load_agent_type` 是用户定义优先。结果：用户覆盖了内置 `developer` 后，运行时生效的是用户版，`agent_go agents` 列表却只显示内置条目，无任何被覆盖提示。另：`agents.py:28` 在 import 时执行 `mkdir ~/.agent_go/agents`（`skills.py` 有同类副作用），import 即写文件系统，不利于测试与打包。

**修复建议**：列表改为用户定义优先（同名时显示 `source: user (overrides builtin)`）；mkdir 副作用移到实际写入处。

### ISSUE-14 `git_utils.analyze_project` 用 `lstrip("./")` 误改文件名

- **位置**：`agent_go/git_utils.py:19`
- **状态**：✅ 已修复（2026-07-23）— 改为仅剥离 `./` 前缀（`f[2:] if f.startswith("./")`）；补 dotfile 名保留回归测试
- **严重度**：P3

**问题**：`f.lstrip("./")` 按字符集剥离——`./.gitignore` 变成 `gitignore`（前导 `.` 被吃掉），`./..foo` 等更混乱。仅影响喂给 LLM 的项目文件清单上下文，不直接影响执行，但会让 Plan 基于错误文件名生成。

**修复建议**：改为 `f[2:] if f.startswith("./") else f` 或 `os.path.relpath`。

---

## 已排除项

### spec 梳理中报告、经核实不成立的问题

- **`tests/test_eval.py` 3 个用例失败（日志 JSON 格式契约错位）** — 2026-07-23 实测 `pytest tests/test_eval.py` 31 passed。`eval.py:35` 现用正则 `"event"\s*:\s*"..."` 兼容紧凑/带空格两种 JSON 格式，该问题在当前代码中不存在（可能已在近期提交中修复）。

### spec 梳理中发现、经评估决定不录入的问题

以下经核实存在但属风格/预置设计/低风险项，保留在各模块 spec 的「维护注意事项」中即可，不占 issue 编号：

- `pipeline.py`：`config` 参数未使用、`degraded_count` 统计后未消费、`worktree_map` 硬编码路径（重构建议，`try/finally` 统一清理已在 ISSUE-4 修复说明中记录）
- `utils.py`：`_slugify` / `_detect_commit_scope` 无生产调用方（测试辅助保留）；`_detect_commit_prefix` 英文关键词子串匹配偶发误命中（影响仅为 commit 前缀选词）
- `metrics.py`：`extract_usage` 无生产调用（PRD 规划中的预置接口）
- `eval.py`：`analyze_reliability` 中 `interrupted`/`resumed` 死变量；`aggregate_quality` 与 `aggregate_performance` 空数据返回语义不一致（`None` vs 零值 dict）
- `agents.py`：`preload_skills`/`extra_args` 当前为死字段；`get_claude_command(headless=True)` 忽略 `worktree` 参数
- `skills.py`：关键词匹配 `\w+` 对中文 description 几乎无效；frontmatter 解析仅支持单层 key-value；全局 Skill 优先于项目级（设计选择，改语义需产品决策）
- `workflow_gen.py`：无异常处理、模板版本号硬编码（低风险辅助命令）
- `cli.py`：`cmd_resume` 重扫 `sys.argv` 覆写参数、`__FALLBACK__` 魔法字符串（设计重构建议）
- `__init__.py` / `config.py`：import 时创建 `~/.agent_go/` 目录（既有设计，CODE_REVIEW 已记录）
