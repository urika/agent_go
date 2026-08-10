# agent_go 已知问题清单

> 来源：2026-07 模块 spec 梳理（`docs/spec/`）中发现的代码缺陷。
> 以下每条均已对照源码逐行核实（核实日期 2026-07-23，基于 v2.0.0 工作区代码）。
> ISSUE-1 ~ ISSUE-6 于 2026-07-23 修复；ISSUE-7 ~ ISSUE-14 于同日修复。
> ISSUE-15 ~ ISSUE-23 来源于 2026-07-25 测试覆盖补强专项（751 → 1012 测试）中发现的缺陷，均已对照源码逐行核实。

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

## 2026-07-25 测试补强专项发现的缺陷

> 来源：2026-07-25 测试覆盖补强专项（新增 261 个测试，总数 751 → 1012）中对照源码逐行核实确认的缺陷。
> 同日全部修复，修复后 1027 个测试通过（`pytest tests/`）。
> 修复摘要：ISSUE-15 `setup_logger` 提前至恢复循环前；ISSUE-16 损坏配置 warning 后回退默认配置深拷贝（不覆写原文件）；ISSUE-17 缺参 tool_call 返回错误 dict；ISSUE-18 命令名改 shlex token 精确匹配（元字符保留子串、解析失败回退子串）；ISSUE-19 语义评估失败原因写入 failure_reason；ISSUE-20 关键词改词边界正则匹配（顺带 `audit`/`探索性` 去重）；ISSUE-21 debug 日志补工具输入 preview；ISSUE-22 重生成前用 final_doc_paths 重读参考文档 + docstring 修正；ISSUE-23 时长估算统一 0.8/1.2 区间公式。各修复均附回归测试（约 15 个新增/改写用例）。

### ISSUE-15 `cmd_resume` 遇到损坏的 result.json 抛 `UnboundLocalError`，恢复中断

- **位置**：`agent_go/cli.py:427-428`（触发点）vs `:442`（`logger` 赋值点）
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P1

**问题**：结果恢复循环的 `except` 块调用 `logger.debug(...)`，但局部变量 `logger = setup_logger(task_id, task_dir)` 在 :442 才赋值。Python 函数级作用域规则使 `logger` 在循环内被视为未绑定局部变量——任何损坏的 result.json 都会抛 `UnboundLocalError` 中断整个恢复流程，与 except 块"跳过损坏文件"的意图相悖。

**修复建议**：将 `setup_logger` 调用提前到结果恢复循环之前。

### ISSUE-16 `load_config` 对损坏的 config.json 无容错，CLI 启动即崩溃

- **位置**：`agent_go/config.py:126-139`
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P1

**问题**：`json.loads(CONFIG_PATH.read_text(...))` 无 try/except——config.json 损坏（非法 JSON）时抛 `json.JSONDecodeError`；内容为合法 JSON 但非 dict（如 `[1,2]`）时 `:130` 的 `saved.items()` 抛 `AttributeError`。任一情况都导致 CLI 启动直接崩溃，用户无法通过任何命令自愈。

**修复建议**：try/except + `isinstance(saved, dict)` 校验，warning 提示后回退 `DEFAULT_CONFIG` 深拷贝（不覆写用户的原文件，保留人工修复机会）。

### ISSUE-17 `ToolRegistry.execute` 对缺参 tool_call 抛 `KeyError`，AgentLoop 整体崩溃

- **位置**：`agent_go/tool_executor.py:199-206`
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P1

**问题**：`execute` 直接以 `arguments["file_path"]` 等方式取参，LLM 产生缺参 tool_call 时抛 `KeyError` 向上传播，整个 AgentLoop 崩溃——与其他错误路径统一返回 `{"success": False, "error": ...}` 的约定不一致。

**修复建议**：dispatch 处捕获 `KeyError`，返回 `{"success": False, "error": f"缺少参数: ..."}`。

### ISSUE-18 `_bash` 拦截规则子串匹配误伤无害命令

- **位置**：`agent_go/tool_executor.py:72-80`
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P2

**问题**：`"rm "`/`"su "`/`"cp "`/`"mv "` 等为子串匹配，`echo warm `（含 "wa**rm** "）等无害命令被误拦截。

**修复建议**：命令名（rm/mv/cp/chmod/chown/sudo/su/mkfs/dd/wget/curl）按 shlex 分词后的 token 精确匹配；shell 元字符（`|`、`;`、`>`、`&&`、`||`）与 `git push` 等多词规则保留子串匹配；shlex 解析失败时回退子串匹配（保守方向）。

### ISSUE-19 纯语义评估失败时 `failure_reason` 丢失具体原因

- **位置**：`agent_go/executor.py:926-935`（原因收集）vs `:590-596`（语义评估记录结构）
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P2

**问题**：语义评估失败时 `verification_results` 中写入的是 `{"type": "semantic", "passed": False, "reason": ...}`，无 `command`/`exit_code` 字段；而 :927 的失败命令过滤要求 `exit_code not in (0, -1)`，语义记录被排除，`failure_reason` 落为兜底文案"验证未通过（无变更或未知原因）"，丢失了具体的评估原因。

**修复建议**：按 `type == "semantic" and not passed` 单独收集语义评估失败，将其 `reason` 写入 `failure_reason`。

### ISSUE-20 `_assess_verification_confidence` 子串匹配误判

- **位置**：`agent_go/executor.py:380-391`
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P2

**问题**：`kw in v_lower` 子串匹配导致误判：`echo latest` 含 "test" 被判为 `deterministic`（`attest`/`contest`/`protest` 同理）。另 `HEURISTIC_KEYWORDS` 中 `"audit"` 重复出现两次。

**修复建议**：关键词匹配改为词边界正则（`\b`）；`"audit"` 去重。顺带清理 `_is_simple_task`（executor.py:744）中冗余的 `"探索性"`（已被 `"探索"` 子串覆盖）。

### ISSUE-21 `parse_and_log` 中工具输入 `preview` 计算后从未使用

- **位置**：`agent_go/subtask.py:194-199`
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P3

**问题**：`content_block_stop` 分支计算了 `preview = ti[:200] ...` 但未出现在任何日志中——累积的工具输入（`tool_input`）既没写日志也没进输出行，调试时无法看到工具实际参数。疑似日志语句遗漏。

**修复建议**：debug 日志补上 `preview`。

### ISSUE-22 R 重新生成丢失 D 挂载的参考文档；`confirm_plan` docstring 与实现不符

- **位置**：`agent_go/cli.py:329`（重生成传 `""`）+ `agent_go/ui.py:228`（R 分支返回值）vs `ui.py:180`（docstring）
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P2

**问题**：用户在确认环节用 D 挂载参考文档后，confirm_plan 的 R 分支返回 `(None, reference_doc_paths)`（与 docstring 声明的 `(None, None)` 不符）；而 cli.py:329 重新生成 Plan 时 `reference_docs` 参数硬编码传 `""`——已挂载的文档内容在重生成时丢失，新生成的 Plan 不再参考这些文档。

**修复建议**：cli.py 重生成前用 `final_doc_paths` 重新 `read_reference_docs`；ui.py docstring 修正为与实际返回值一致。

### ISSUE-23 `_estimate_duration` 低分钟区间显示 `约 4-4 分钟`，秒级分支不可达

- **位置**：`agent_go/ui.py:96-104`
- **状态**：✅ 已修复（2026-07-25）
- **严重度**：P3

**问题**：`1 ≤ minutes < 5` 分支下限用 `int(minutes)` 而非 `int(minutes*0.8)`，与其他分支不一致——单步 240s（4 分钟）时输出 `约 4-4 分钟`（区间两端相同）。另 `minutes < 1` 的 `约 N 秒` 分支不可达：steps 非空时最少 1×240s=4 分钟，空 steps 已在前面返回 `"N/A"`。

---

### ISSUE-24 测试套件 flaky：goal watchdog 线程同步竞争导致 CI 随机红绿

- **位置**：`agent_go/subtask.py:260-294`（poll 循环 + 终检）+ `tests/test_subtask.py::TestGoalWatchdog`
- **状态**：✅ 已修复（2026-07-25）— 生产侧加线程 join 后的终检；测试侧用 `_ListHandler` 替代 caplog 抓线程日志
- **严重度**：P0（CI 随机红绿会使开发者无视失败，比确定性失败更危险）

**问题**：`test_goal_max_turns_exceeded_kills` 在全量跑时 ~10% 概率失败。根因有二：

1. **线程同步竞争（生产侧）**：`goal_turn_count` 在守护线程 `parse_and_log`（subtask.py:178）累加，kill 检查在主线程 poll 循环（subtask.py:282）。主循环 `time.sleep(2)` 期间，读线程处理最后一行累加 goal_turn_count 与主线程的 poll 返回存在竞争——若 poll 在 sleep 期间返回 0，kill 检查可能错过"已达 MAX_GOAL_TURNS"的最后一次累加。
2. **caplog 与守护线程的捕获窗口竞争（测试侧）**：`caplog` 的 `LogCaptureHandler` 在 `with` 块期间附加，但守护线程生命周期与捕获窗口存在竞争，线程日志可能落在捕获区间外，导致 `caplog.text` 缺失关键日志行。

**修复**：
- 生产侧（subtask.py:293-308）：在 `t_out.join()`/`t_err.join()`（所有事件已处理完毕）后加一次确定性终检——若 `goal_turn_count >= MAX_GOAL_TURNS` 且未触发过，补记 `goal_turns_exceeded` 日志 + 标记 + kill。消除 poll 循环错过最后一轮累加的窗口。
- 测试侧（test_subtask.py）：新增 `_ListHandler`（直接 `addHandler` 到 logger，handler 生命周期与 logger 一致，无窗口问题）替代 `caplog` 抓取线程日志；`TestGoalWatchdog` 4 个测试全部改用此模式。
- 验证：连跑 30 次 `TestGoalWatchdog` 0 失败；全量连跑 5 次 0 失败。

**根因教训**：`caplog` 与守护线程的组合是已知 pytest 陷阱；线程日志断言应优先用自定义 handler 直接挂到 logger。生产代码的"事件累加在子线程、阈值检查在主线程"模式需在 join 后补终检，避免 sleep 窗口漏检。

---

### ISSUE-25 三处测试与实现漂移（关键词列表 / 错误前缀 / metering 行数）

- **位置**：`tests/test_executor.py:1191` + `tests/test_tool_executor.py:189` + `tests/test_agent_loop.py:494`
- **状态**：✅ 已修复（2026-07-25）— 关键词对齐、错误前缀断言放宽、metering 行数随 ISSUE-24 自然消除
- **严重度**：P2（测试漂移让"已覆盖"代码实际未验证，覆盖率数字虚高）

**问题**：3 处测试断言与 working-tree 实现不一致：

1. **`_is_simple_task` 关键词漂移**：测试参数化含 `["理解","分析","研究","understand","analyze"]`，但 `executor.py:754` 实现已精简为 `["探索","调研","重构","迁移","refactor","migrate","explore"]` 并新增 agent_type / files_hint 两条规则。5 个已移除的关键词必然断言失败。
2. **`_bash` 错误前缀断言脆弱**：测试断言 `"禁止的命令" in result["error"]`，但 shell 操作符拦截路径返回 `"禁止的 shell 操作符: |"`（无"禁止的命令"前缀）。6 条含操作符的命令必然失败。前缀措辞是实现细节，不应锁死。
3. **`AgentLoop.run` metering 行数**：单独跑 PASS、全量跑偶尔失败——是 ISSUE-24 flaky 的连带的 caplog 抓取问题，非确定性漂移。

**修复**：
- A1：测试关键词列表对齐实现；补 agent_type（architect/reviewer/developer）、files_hint（多通配符/单通配符）新规则的参数化用例。
- A2：Bash 拦截断言放宽为 `result["success"] is False` 且 `result["error"]` 非空（核心行为契约），不再锁死前缀措辞。
- A3：随 ISSUE-24 的 `_ListHandler` 改造自然消除（不再用 caplog）。

**根因教训**：覆盖率统计无法发现"测试跑了但断言错了"的漂移——CI 红绿门禁 + "改实现必须同步改测试"的纪律才是根本。错误前缀等措辞不应硬编码进断言，应断言行为契约（success=False + 有错误信息）。

---

### ISSUE-26 `analyze_cost` 计价失真：$/pass rate 被低估 11-22 倍，gate 假性通过

- **位置**：`agent_go/eval.py:374-382`（旧实现，无条件 token 重算）+ `agent_go/subtask.py:370`（写 `actual_model="claude-code-executor"`）
- **状态**：✅ 已修复（2026-07-25）— `analyze_cost` 改为优先用真实 `cost_usd`，token 重算仅补缺；未知模型不再兜底 deepseek
- **S12 加固（2026-08-07）**：✅ 运行前模型-价格预检（`bench._probe_actual_model` 探测实际后端 + `pricing.resolve_price` 校验定价覆盖，缺定价告警/中止）；✅ 智谱后端定价补全（glm-4.7/5.1/5.2/4.5-air，消除 1.8x 虚高：claude-* 路由 → glm-4.7 按 $0.5556/$2.2222 重算 vs claude 报 Anthropic 价 $0.0429）
- **严重度**：**P0**（北极星指标 $/pass rate 失真，门禁永远绿，违背存在意义）

**问题**：`subtask.py:370` 默认写 `actual_model="claude-code-executor"`（Claude Code 子进程的真实标识），该字符串不在 `MODEL_PRICES`（仅 7 个模型）。旧 `analyze_cost:377` 对未知模型兜底 `MODEL_PRICES["deepseek-chat"]`（$0.27/$1.1 per Mtok），而真实 Claude 是 $3-15/Mtok——**成本被低估 11-22 倍**。`dollar_per_pass_rate` 因此被严重拉低，gate 永远通过。

**附带问题（D2 双数据源）**：`by_role` 读真实 `cost_usd`（line 348），总 cost 却用 token 重算——同一批事件两份成本对不上，报告自相矛盾。

**修复**：
- `analyze_cost` 改为双轨：每条事件优先累加真实 `cost_usd` 到 `cost_from_metering`；仅当事件缺 `cost_usd` 且模型在 `MODEL_PRICES` 时，才按 token 重算补到 `cost_from_rebuild`。`estimated_cost_usd = metering + rebuilt`。
- 未知模型（既无 `cost_usd` 又不在价目表）不再兜底 deepseek，而是计为 `unknown_model_events`（可观测字段，监控价目表覆盖度）。
- 新增可观测字段：`cost_source_breakdown`（{metering, rebuilt}）、`unknown_model_events`、`fallback_events`（PRD §line 173 留痕字段终于被读）。
- `by_role` 与总 cost 现都以 `cost_usd` 为主，自洽。

**验证**：构造混合场景（claude-code-executor 真实 cost + sonnet + opus），新逻辑 $/pass=0.125（真实），旧逻辑会算成 ~0.005（低估 25 倍）。20 个 E2E 场景覆盖（`tests/test_e2e_scenarios.py`）。

**根因教训**：北极星指标的计价不能依赖"价目表全覆盖"的假设——新模型/provider 持续涌现（claude-code-executor、custom 本地模型），兜底为最便宜模型会让门禁变摆设。必须优先用 provider 自报的真实 `cost_usd`，token 重算仅作兜底，且对未知模型要有可观测告警。

---

### ISSUE-27 `evaluator.py` 硬编码 token 导致重复记账 + cost 低估

- **位置**：`agent_go/evaluator.py:225-226`（旧硬编码）+ `agent_go/api.py:82-96`（call_api 内部记账）
- **状态**：✅ 已修复（2026-07-25）— evaluator 抑制 call_api 内部记账，自写带估算 token 的 metering
- **严重度**：P1（重试越多 cost 越低估；角色标错）

**问题**：`evaluator.py` 调 `call_api` 时传入含 `_metering_path` 的 config，`call_api` 内部会写一条 `role="planner"` 的 metering（api.py:82-96，含真实 token）。随后 evaluator 又写第二条 `role="evaluator"` 的 metering（硬编码 `prompt_tokens=1000, completion_tokens=200`）。后果：
1. **重复记账**：同一 API 调用写两条 metering，cost 翻倍。
2. **角色标错**：evaluator 调用被 call_api 标成 planner。
3. **token 硬编码**：重试越多，evaluator cost 越被相对低估（真实评估可能消耗 10k+ token，却记 1k）。

**修复**：
- evaluator 传给 call_api 的 config 移除 `_metering_path`（`eval_config.pop("_metering_path", None)`），抑制 call_api 的内部记账。
- evaluator 用 prompt/response 长度估算真实 token（~3 字符/token，中英混合保守值），替代硬编码 1000/200。
- 角色正确标为 `evaluator`。

**根因教训**：`call_api` 硬编码 `role="planner"` 是设计债（不接受 role 参数），导致所有非 planner 调用方都必须手动抑制其内部记账。根治应给 `call_api` 加 `role` 参数，但影响面大（planner/evaluator/router 多处），本次用最小侵入修复（evaluator 抑制 + 自写）。

---

### ISSUE-28 PRD "不劣化"语义 vs 绝对阈值门禁；K5 中断恢复成功率无派生计算

- **位置**：`agent_go/eval.py:gate_cost`（绝对阈值）+ `analyze_reliability`（无 K5 派生）
- **状态**：✅ 已修复（2026-07-25）— 新增 `gate_cost_regression`（基线对比）+ `--check-regression` CLI flag + K5 `resume_success_rate`
- **严重度**：P1（PRD 铁律无执行力 + KPI 覆盖不全）

**问题**：
1. **PRD §line 184 "发布门禁「$/pass rate 不劣化」"** 是相对语义（vs S1 基线），但 `gate_cost` 用绝对阈值（actual > baseline）。后果：成熟仓库从 0.02 劣化到 0.04 仍"通过"；新 fork 仓库永远过不了 0.05 绝对门禁。
2. **PRD K5（中断恢复成功率，年度 ≥99.9%）** 的原始数据已在 execution.log（task_paused/subtask_resume 事件，`analyze_reliability:661-664` 已读），但无"恢复后是否最终成功"的派生计算。

**修复**：
- 新增 `gate_cost_regression(tasks_dir, tolerance=0.10, update=False)`：对比当前 rate 与 `.agent_go/cost_baseline.json` 基线，劣化 >10% 即失败（PRD "不劣化"语义）。首次运行自动建立基线；`--update-baseline` 强制重置（模型升级等场景）。
- CLI 新增 `--check-regression` / `--update-baseline` flag，与 `--baseline`（绝对阈值）互斥。
- `analyze_reliability` 新增 `interrupted_tasks` + `resume_success_rate`：被中断过的任务中最终 status=="completed" 的比例。

**未覆盖（记录为后续）**：K2（安全零事故）、K6（可观测性可回答率）、K7（首次上手时间）需全新数据采集，超出本次范围。

**根因教训**：PRD 的"铁律"和"KPI"必须有可执行的代码闭环（gate + 派生指标），否则只是文档承诺。"不劣化"这种相对语义需要状态（基线文件），不能只用无状态函数实现。



**修复建议**：统一为 0.8/1.2 区间公式；删除不可达的秒级分支。

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
- `api.py:116`：except 顺序依赖（`URLError` 是 `OSError` 子类，依赖 HTTPError→URLError→OSError 的声明顺序正确分派，当前行为正确但顺序改动会静默改变行为）；`api.py:387-389` `load_cached_plan` 末尾 `enabled` 判断为死代码（头部已在禁用时 return None）
- `executor.py:67-72`：argv 解析失败与超时两个分支产出的 result dict 不可区分（均 `exit_code=-1`、`duration_ms=0`），下游只能靠日志文本分辨（可观测性改进建议）

---

### ISSUE-29 LLM 生成的验证命令语法错误导致正确代码误判失败

- **位置**：`executor.py` 验证循环（验证命令直接 `subprocess.run` 执行，无语法预检）
- **状态**：✅ 已修复（2026-08-09，阶段 B 收敛）— `_is_safe_verification_command` 增加 `compile()` 单行语法预检
- **严重度**：P2（偶发，但会把正确代码判失败，消耗 retry 预算）

**问题**：LLM 生成的 `python -c` 单行验证命令可能包含 Python 语法错误。E2E 实测（2026-08-09 多子任务依赖链）：
- 验证命令：`python -c "from calculator import divide; assert divide(10,2)==5.0; assert divide(0,5)==0.0; try: divide(1,0); print('Divide by zero raised ValueError as expected'); except ValueError: pass"`
- **语法错误**：`try: ...; print(...); except ValueError: pass` 在单行 `python -c` 中 `try` 块内分号拼接非法 → `SyntaxError: invalid syntax`
- 后果：sub-2 代码完全正确（divide 含 b=0 抛 ValueError），但验证命令 exit_code=1 → 误判 failed，下游被阻断

**影响**：LLM 生成的复杂单行断言（尤其含 try/except）语法错误率较高；验证失败后走 retry 也消耗成本（`python -c` 内嵌错误不可自动修复）。

**建议修复方向**（M2）：
1. 验证命令执行前做**语法预检**（`python3 -m py_compile` 或 AST parse）。
2. 验证失败时把 stderr 注入 retry 上下文，让 LLM 修复验证命令本身（当前只注入代码 diff）。
3. 引导 LLM 用多行脚本文件或多行 `-c`（`\n` 分隔）替代单行分号拼接。

---

### ISSUE-30 无子任务结果的提前终止路径缺失 failure_class 契约

- **位置**：`cli.py` Plan 预检阻断路径（`plan_quality["blocking_issues"]` 非空时）
- **状态**：✅ 已修复（2026-08-09）
- **严重度**：P0（数据完整性，违反阶段 A 收敛门禁"所有终态失败/阻断任务必须有 failure_class"）

**问题**：Plan 预检发现 `blocking_issues`（如 `verification_command_rejected`）时，代码只写 `meta["status"]="BLOCKED"`，未写 `failure_class`、`failure_reason`、`plan_quality_status`。任务处于终态但无任何 results 子任务，无法推断模型/验证失败，`failure_class=None` 违反 Failure Class 契约。

**实测**（阶段 A 数据清理发现 2 个任务）：
- `task-20260808-213257-262-5d9c`、`task-20260809-003938-252-c644`
- 均因 `verification_command_rejected` 被 BLOCKED，`results=[]`、`failure_class=None`

**修复**：
1. **运行时**（`cli.py:759-769`）：提前终止路径补写 `failure_class="infrastructure_failure"`、`failure_reason="plan_quality_blocked"`、`blocked_without_result=True`、`plan_quality_status`、`blocking_issues`。
2. **迁移工具**（`metadata_migration.py`）：`results=[]` + 终态（BLOCKED/VERIFICATION_FAILED/DELIVERY_FAILED）+ 无 `failure_class` → 保守补 `system_error` + `blocked_without_result=True` + `root_failure_class="system_error"`（幂等，已标记则跳过）。
3. 历史任务已修复并保留 `status=BLOCKED`。

**验收**：全库 1170 任务检查——终态缺 failure_class=0、accepted 无 delivery=0、blocked 无 root=0。

---

## P0 — 必须修复

### ISSUE-31 验证命令沙箱环境与真实环境不一致，正确代码被误判失败

- **位置**：`executor.py` 验证循环（`_build_sandbox_env`）+ `run_subtask` 环境透传
- **状态**：✅ 已修复（2026-08-09）— 移除 `_apply_resource_limits` 的 `RLIMIT_NPROC`
- **严重度**：P0（正确代码误判失败，验证层成为误判源）

**问题**：agent_go 执行验证命令时使用 `_build_sandbox_env()`，该环境可能清理/隔离了部分环境变量（如 `AGENT_GO_METERING_PATH`、`AGENT_GO_CLAUDE_MODEL` 等透传逻辑）。当验证命令包含 E2E 类测试（真实 spawn claude 子进程）时，在沙箱环境中**立即失败**（<1 秒），但手动 shell 全部通过。

**实测**（2026-08-09 dogfooding task-20260809-105447-149-d67a sub-2）：
- 验证命令：`python -m pytest tests/test_executor.py tests/test_planning.py -q`
- agent_go 沙箱执行：**验证失败**（<1 秒），触发 fix-1，最终 sub-2 标 failed
- 手动 shell 执行：**155 passed**（全部通过）
- 后果：正确代码被误判 failed，sub-3 级联阻断，整个任务失败

**影响**：验证环境失真导致能力误判——这是验证层最严重的问题，直接削弱验证门禁的可信度。

**根因与修复**（2026-08-09）：
- **根因**：`_apply_resource_limits` 设置 `RLIMIT_NPROC=64`。macOS 上 RLIMIT_NPROC 是 **per-user 语义**，限制的是"该用户所有进程总数"（含 agent_go 多任务 + 后台进程累积，实测 455+），而非验证命令的子进程树。用户已有进程数超限时，任何 `fork_exec` 都触发 `BlockingIOError[Errno 35]`，导致 `git init`/`git commit` 子进程失败 → 正确代码误判 failed。
- **修复**：移除 `RLIMIT_NPROC` 设置（fork 炸弹防护交给 `RLIMIT_CPU`，CPU 时间耗尽即杀）。实测沙箱环境下整批 152 项测试从 5 失败变为全过。
- **回归测试**：`test_apply_resource_limits_no_nproc`（断言不再设置 RLIMIT_NPROC）。

---

## P1 — 功能受损

### ISSUE-32 Claude 子任务超范围改动，无 scope 约束

- **位置**：`executor.py` TASK.md 生成 + 验证范围
- **状态**：✅ 已修复（2026-08-09）— 验证通过时审计超范围改动（scope_compliance 记录）
- **严重度**：P1（改动范围不可控，验证范围随之扩大）

**问题**：子任务 Claude 会**顺手修改与任务描述无关的代码**。dogfooding 实测 sub-2（任务："rejected 不消耗 retry"）除核心改动外，还修改了 `run_subtask` 的 `AGENT_GO_METERING_PATH`/`AGENT_GO_CLAUDE_MODEL` 环境透传清理逻辑，并新增 2 个对应测试——超出任务范围。

**影响**：
- 改动范围不可控，验证命令需覆盖被意外触碰的模块。
- 失败时难以判定是核心逻辑问题还是超范围改动引入。
- 潜在引入与任务目标无关的行为变化。

**现状评估**：
- ✅ TASK.md 已有"范围约束"（`_build_architecture_context`，基于 files_hint 注入"你只能修改以下文件"）
- ✅ 验证失败分支已有 `_check_scope_compliance` + `scope_violation` 注入修复 prompt（撤销越界改动）
- ⚠️ 缺口：验证**通过**时超范围改动静默通过，无审计

**修复**（2026-08-09）：
- 验证通过分支（`all_pass`）新增 `_check_scope_compliance` 调用，违规时记录 `scope_compliance` 审计到 `verification_results`（out_of_scope/missing），供 review/交付检查发现。
- 回归测试：`test_scope_violation_recorded_when_verify_passes`、`test_scope_compliant_no_audit`。

### ISSUE-33 Skill 自动匹配误命中无关 skill

- **位置**：`skills.py` 自动发现 + `skill_backfill`
- **状态**：✅ 已修复（2026-08-09）— `_tokenize_words` CJK 分词从单字符改为 bigram
- **严重度**：P1（无关 skill 注入污染 TASK.md，干扰 Claude 注意力）

**问题**：Python 后端修复任务两次自动匹配 `security-review` + `frontend-react` skill，其中 `frontend-react` 与任务完全无关。`skill_backfill` 对无 skill 的子任务回填默认 skill，进一步放大误匹配。

**影响**：注入无关 skill 的指令会污染 TASK.md，浪费 Claude 上下文，可能引入错误方向。

**根因与修复**（2026-08-09）：
- **根因**：`_tokenize_words` 对 CJK 拆单字符（`状态管理` → `状`+`态`+`管`+`理`），丢失语义。高频单字（管理/组件/网络/请求）与任何中文任务都易重叠 ≥2 个字符，导致 `frontend-react` 等无关 skill 误配，并被 `skill_backfill` 放大到所有无 skill 的子任务。
- **修复**：CJK 改为 **bigram**（相邻两字符对）分词。`状态管理` → `状态`+`态管`+`管理`，保留语义，跨类型任务无重叠。
- **验证**：Python 修复任务不再匹配 frontend-react；安全任务匹配 security-review；前端任务匹配 frontend-react。
- **回归测试**：`test_no_cross_type_mismatch_issue33`。

---

## P1 — 功能受损

### ISSUE-34 python -c 装饰器检测误判 email 地址中的 @

- **位置**：`utils.py` `_is_safe_verification_command` Stage 2.5
- **状态**：✅ 已修复（2026-08-10）— 移除 `@\w+` 正则，仅依赖 `compile()` 预检
- **严重度**：P1（合法验证命令被拒，任务被误判 infrastructure_failure）

**问题**：装饰器检测用 `re.search(r"@\w+", content)` 判断"含装饰器(@)"。但 email 地址（如 `test@example.com`、`'a@b.c'`）中的 `@example`/`@b` 匹配该正则 → 含 email 地址的合法验证命令被误判为含装饰器而拒绝。

**实测**（阶段 E decision bench email-validator 0/3 全 infrastructure_failure）：
- 验证命令：`python3 -c "from solution import validate_email, batch_validate; ... r=batch_validate(['a@b.c','']); ..."`
- `@b.c` 匹配 `@\w+` → 命令被拒 → sub-1 failed → `kill_reason=interrupted_or_unknown` → infrastructure_failure
- 3 次 repeat 全部失败，全部因同一误判

**修复**：移除 `@\w+` 装饰器检测，完全依赖 `compile()` 预检（单行 `-c` 中真正的装饰器 `@dec def f()` 必然 SyntaxError，compile 已精确拦截；email 地址命令 compile 通过）。

**回归测试**：`test_accepts_email_address_not_decorator`、`test_rejects_single_line_decorator`。

### ISSUE-35 _generate_context 引用未定义的 _extract_key_constraints（NameError）

- **位置**：`executor.py` `_generate_context`
- **状态**：✅ 已修复（2026-08-10）— 还原到 HEAD 版本，移除半成品"改进 D"调用
- **严重度**：P0（成功子任务也会 NameError → failed → system_error）

**问题**：工作区存在未提交的半成品改动，给 `_generate_context` 增加了 `_extract_key_constraints(summary, diff_stat)` 和 `_extract_files_changed(summary)` 调用，但**这两个函数从未实现**，且 `diff_stat` 变量未定义。任何子任务（无论成功失败）生成 context 时触发 `NameError: name '_extract_key_constraints' is not defined` → 子任务 failed → system_error。

**实测**（阶段 E email-validator 重跑 rep1）：
- `串行异常 sub-1: NameError: name '_extract_key_constraints' is not defined`
- 非 email-validator 任务本身问题，而是执行器通用 bug

**修复**：还原 `executor.py` 到 HEAD 版本（移除未实现的"改进 D"调用），`_generate_context` 恢复可工作状态。

**教训**：未实现的函数引用（半成品改动）会在执行路径随机触发崩溃，且不在单元测试覆盖内。任何新增代码必须实现完整或先 commit 基线。

### ISSUE-36 eval_suite/fixtures/ 被主仓库与 fixture 独立仓库双重跟踪

- **位置**：`eval_suite/fixtures/`（fp-sandbox / task-mgr / data-pipeline / django-blog）
- **状态**：⏳ 待处理（结构性问题，2026-08-10 记录）
- **严重度**：P2（状态冲突，但不阻塞 bench 运行）

**问题**：4 个 fixture 项目都是独立 git 仓库（各有 `.git`），但主仓库也通过 `git ls-files` 跟踪了其中 75 个内容文件（历史遗留 f92d218 提交时一并纳入）。双重管理导致状态冲突：
- 阶段 E 期间 fp-sandbox 的 `solution.py`：主仓库跟踪的是旧版本，fixture 仓库初始状态（3ecb2b9）无此文件 → 主仓库工作区显示删除，实际是运行时产物混入主仓库。
- django-blog 的 `tests/test_performance.py` 同理（db 任务运行时被修改）。

**处理**：本次仅 `git rm --cached eval_suite/fixtures/fp-sandbox/solution.py` 移除该冲突点（fixture 运行时产物不应由主仓库跟踪）。未做大规模重构。

**建议修复方向**（M2+ 结构重构时）：
1. 主仓库 `.gitignore` 忽略 `eval_suite/fixtures/*/`（内容由 fixture 独立仓库管理）。
2. 或 `git rm --cached` 全部 fixture 内容文件，改为 bench/CI 运行时单独 clone/复制 fixture 仓库。
3. 需要确认 CI（git clone 主仓库）是否依赖 fixture 工作区文件；若依赖，需在 CI 中单独检出 fixture 仓库。

### ISSUE-37 eval gate 扫描全库 AGENT_GO_DIR，无法隔离到 bench batch

- **位置**：`eval.py` `cmd_eval` gate 分支（`gate_cost` / `gate_cost_regression`）
- **状态**：⏳ 待修复（2026-08-10 阶段 E 收尾发现）
- **严重度**：P2（发布门禁语义与 batch 化基准不匹配，误报劣化）

**问题**：`eval gate` 的两种模式（`--baseline` 绝对阈值 / `--check-regression` 回归）都硬编码扫描 `AGENT_GO_DIR`（~/.agent_go/ 全部历史任务 metering），**不接受 `--results`**。而 bench 的 `metric-freeze` / `batch-manifest` 是基于 results.jsonl 的 batch 数据。

**实测**（阶段 E 收尾）：
- 阶段 E decision baseline（48 条，冻结于 decision-20260809）：$/pass = **$0.0167**（32/34 valid，$0.536 成本），远低于 $0.05 阈值，达标
- `agent_go eval gate --baseline 0.05`：扫描全库 1976 个历史子任务，$/pass = **$0.20** → 判定"不通过"
- 差异根因：全库含早期高成本模型 + 大量探索性任务，噪声淹没 batch 真实值

**影响**：发布门禁无法针对"本次冻结的基线"做准确判断，会误报劣化。阶段 E 数据本身达标，但 gate 工具无法体现。

**建议修复方向**：
1. `eval gate` 支持 `--results <results.jsonl>`，从 batch 数据计算 $/pass（排除 timed_out 右删失），而非 AGENT_GO_DIR 全库。
2. 或 `--source-batch <batch>` 筛选 AGENT_GO_DIR 中匹配 source_batch 的任务。
3. 与 `metric-freeze` 的 `valid_cost_usd / valid_task_count` 语义对齐。

### ISSUE-38 fixture 仓库 worktree 泄漏（bench 运行残留）

- **位置**：`agent_go/bench.py` + `executor.py` worktree 管理
- **状态**：⏳ 待修复（2026-08-10 历史数据清理发现）
- **严重度**：P2（磁盘泄漏，长期运行累积 GB 级残留）

**问题**：agent_go bench 复制 fixture 到临时目录运行（bench.py:246 不复制 `.git`），但 `git worktree add` 时 worktree 仍注册到 **fixture 源仓库**的 `.git/worktrees/`，且 bench 清理逻辑不覆盖 → 每次运行在 fixture 源仓库累积一个 worktree 注册项，长期泄漏。

**实测**（2026-08-10 清理前）：
- 4 个 fixture 源仓库共残留 **2198 个 worktree**：
  - task-mgr 1250、data-pipeline 616、django-blog 194、fp-sandbox 140
- 路径全部指向 ~/.agent_go/task-*/sub-*/work（bench 子任务 worktree）
- 已清理 1323 个（3 天前），剩余 865 个为近期保留

**影响**：
- fixture 仓库 `.git/worktrees/` 无限膨胀（本次清理前 ~/.agent_go 总 18GB，其中 fixture worktree 相关占大头）
- `git worktree list` / `git gc` / fixture 操作变慢
- 长期运行每个 bench 任务泄漏一个注册项

**建议修复方向**：
1. **bench 完成时统一清理**：`_run_one_task` 结束后，删除注册到 fixture 源仓库的 worktree（当前只清理 task 目录内的 worktree，不清理 fixture 源仓库的注册）。
2. **复制 fixture 时不带 .git**：确保临时目录的 worktree 注册在临时仓库，不写回源仓库。
3. 或 executor 用 `--force` 方式在任务结束时 `git worktree remove`，并 `git worktree prune` 清孤立注册。
4. 提供一次性清理脚本（如 `agent_go clean --fixture-worktrees`）兜底历史残留。
