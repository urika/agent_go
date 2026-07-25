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
