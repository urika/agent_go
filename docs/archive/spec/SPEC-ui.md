# ui 模块规格说明

## 概述

`agent_go/ui.py` 是 Plan Mode 编排流程中面向终端用户的交互层，负责两件事：一是把 LLM 生成的 Plan 渲染为终端展示文本或 Markdown 文档；二是驱动用户确认流程——Plan 确认（含补充输入、挂载参考文档、编辑步骤、重新生成）、子任务拆解与确认、以及每个子任务执行后的人工验证。它位于 `cli.py`（编排主流程）与 `api.py`（LLM 调用）之间，只在 `cli.py` 中被引用（`agent_go/cli.py:10`）。模块纯 stdlib，无第三方依赖。

## 公共接口

模块通过 `__all__` 显式导出 7 个符号（`agent_go/ui.py:9-13`）。

### `plan_to_md(plan: dict[str, Any]) -> str`（`agent_go/ui.py:15`）
将 Plan dict 渲染为 Markdown 文档字符串，包含概述、预估工作量、共享资源清单（Git 远程/分支/目录/配置文件/环境变量，均按需输出）、执行步骤（含文件、验证命令、风险）及依赖关系段落。缺失字段回退为 `N/A` 或空。无副作用。

### `print_plan(plan, config) -> None`（`agent_go/ui.py:47`）
在终端打印 Plan 详情。读取 `config["behavior"]` 的两个开关：`show_resource_map`（默认 `True`）控制是否展示共享资源清单；`show_agent_prompt`（默认 `True`）控制是否展示每步 `agent_prompt` 预览（截断至 200 字符）。仅打印，无返回值。

### `confirm_plan(plan, config, repo: Path, logger, iteration: int = 1, task: str = "") -> tuple[Optional[dict], Optional[list[str]]]`（`agent_go/ui.py:113`）
Plan 确认主循环。返回三态：
- `(plan, reference_doc_paths)`：用户确认（Y 或默认同意模式下空 Enter）；
- `(None, reference_doc_paths)`：用户选择 R 重新生成（由调用方重跑）；
- `("__FALLBACK__", None)`：连续 API 失败后用户选择降级到规则拆解。

交互选项：Y 确认 / S 补充输入重新生成 / D 挂载参考文档重新生成 / E 编辑步骤 / R 重新生成 / N 取消（`sys.exit(0)`）。副作用：终端 IO、日志记录（`log_event`）、可能调用 `generate_plan` 触发 LLM 请求、N 时直接退出进程。

### `plan_to_subtasks(plan, logger, repo: Optional[Path] = None) -> list[dict]`（`agent_go/ui.py:248`）
把 Plan 的每个 step 转换为子任务 dict：拼接 description（原始描述 + Agent 执行指令 + 共享资源清单 + 验证命令 + 风险提示）、由 `dependencies` 生成 `depends_on`（`sub-<id>` 形式），并调用 `role_skill_map.apply_rules` 做角色-Skill 规则兜底匹配。最后记录 `plan_decomposed` 事件。子任务结构见下文「数据结构」。

### `print_subtasks(subtasks, config) -> None`（`agent_go/ui.py:308`）
打印子任务列表：Agent 角色（按 `_agent_type_source` 加 `[规则匹配]`/`[自动推断]` 标签）、Skill 列表、描述预览（截断 200 字符）、涉及文件、Agent Prompt 预览（截断 150 字符，受 `show_agent_prompt` 开关控制）。

### `confirm_subtasks(subtasks, config, logger) -> list[dict]`（`agent_go/ui.py:334`）
子任务确认循环。读取 `behavior.auto_confirm_subtasks`（默认 `False`）实现默认同意模式。选项：Y 全部确认 / N 取消（`sys.exit(0)`）/ E 编辑 / A 添加（新 id 为 `sub-{len+1}`）/ D 删除（删除后重排所有 id 为 `sub-1..n`）。返回（可能被修改的）子任务列表。

### `verify_subtask(current: int, total: int, summary: str, logger, config: Optional[dict] = None) -> str`（`agent_go/ui.py:407`）
单个子任务完成后的人工验证提示。返回 `"next" | "retry" | "modify" | "abort"`，接受大小写及全词（CONTINUE/RETRY/MODIFY/ABORT）。`config["behavior"]["auto_verify_subtask"]` 为 `True` 时空 Enter 视为继续。

### 内部：`_prompt_fallback(logger) -> str`（`agent_go/ui.py:93`）
API 重新生成连续失败后的降级询问，返回 `"fallback"` 或 `"retry"`；选 N 则 `sys.exit(0)`。仅供 `confirm_plan` 使用。

## 关键逻辑与流程

### Plan 确认循环（`confirm_plan`）
1. 每次迭代先 `print_plan` 全量展示（`agent_go/ui.py:130`）。
2. 默认同意模式：`auto_confirm_plan=True` 且 `iteration == 1` 时提示"按 Enter 直接确认"，空输入即返回（`agent_go/ui.py:133-142`）；环境变量 `AGENT_GO_INTERACTIVE=1` 强制关闭该模式（`agent_go/ui.py:125-126`）。
3. S（补充输入）：多行读取（连续两个空行结束，`agent_go/ui.py:182-186`），与已挂载的参考文档一起调用 `generate_plan` 重新生成，`iteration` 递增；原任务文本经 `plan["_original_task"]` 在多次再生成间传递（`agent_go/ui.py:195-197`）。
4. D（挂载文档）：逗号分隔路径，去重（`dict.fromkeys`，`agent_go/ui.py:216`）后经 `read_reference_docs` 读取内容再重新生成。
5. API 失败计数：`plan_api_failure_count` 达到 `max_plan_api_failures = 2`（硬编码，`agent_go/ui.py:122`）时进入 `_prompt_fallback`；用户选"重试"则计数清零（`agent_go/ui.py:208`）。
6. 非交互保护：连续空输入超过 5 次判定为非交互环境，`sys.exit(1)`（`agent_go/ui.py:239-243`）。

### Plan → 子任务转换（`plan_to_subtasks`）
- description 分段拼接：原始描述 → `【Agent 执行指令】` → `【共享资源清单】`（空行剔除）→ `【验证命令】` → `【风险提示】`（`agent_go/ui.py:259-278`）。
- 依赖换算：`dependencies` 中 step id 转字符串后映射为 `sub-<id>`（`agent_go/ui.py:280-282`）。
- 角色/Skill 兜底：`role_skill_map` 与 `skills` 为函数内延迟 import（`agent_go/ui.py:285-286`）；`apply_rules` 返回 `{skills, agent_type, required_skills, matched_rules}`，其中 `agent_type` 优先级为 step 自带 > 规则匹配 > role_map 默认值（`agent_go/role_skill_map.py:132`）。
- `_agent_type_source` 标记来源：`llm`（step 自带）/ `rule`（规则命中）/ `default`（`agent_go/ui.py:302`），`print_subtasks` 据此打标签。

### 子任务编辑/删除
- 删除后重排 id（`agent_go/ui.py:394-395`）——注意这会使既有 `depends_on` 引用悬空，见「维护注意事项」。

## 依赖关系

内部模块（均为顶层 import，除注明外）：
- `.config`：`safe_input`（包装 `input()`，非交互时返回空串）、`log_event`（JSON 结构化 debug 日志）。
- `.utils`：`read_reference_docs(doc_paths, repo, logger)`，读取挂载的参考文档内容。
- `.api`：`generate_plan(task, repo, config, logger, supplement, reference_docs, iteration, ...)`，S/D 选项重新生成 Plan 时调用。
- `.role_skill_map`：`load_role_skill_map`、`apply_rules`（`plan_to_subtasks` 内延迟 import）。
- `.skills`：`list_skills`（同上，延迟 import）。

外部依赖：
- 环境变量 `AGENT_GO_INTERACTIVE`：值为 `1`（不区分大小写）时强制交互，关闭所有 auto_confirm（`agent_go/ui.py:125`、`agent_go/ui.py:339`）。
- 无直接 CLI 命令调用，无直接文件系统读写（文件读写均委托给上述内部模块）。

## 数据结构与持久化

无持久化：模块本身不读写任何文件。

关键 dict 结构：
- 输入 Plan：`{overview, estimated_effort, shared_resources: {git_remote, git_branch, directories, config_files, env_vars}, steps: [{id, title, description, files, verification, risks, agent_prompt, agent_type?, skills?}], dependencies: {<step_id_str>: [<id>...]}, _original_task?}`。`_original_task` 为本模块在 S/D 再生成时写入/读取的内部键（`agent_go/ui.py:195-197`）。
- 输出子任务（`plan_to_subtasks`，`agent_go/ui.py:291-303`）：`{id: "sub-<n>", title, description, files_hint, agent_prompt, verification, risks, depends_on: ["sub-<id>"...], skills, agent_type, _agent_type_source}`。`files_hint` 无文件时为 `"*"`。`confirm_subtasks` 手动添加（A）的子任务只含 `{id, title, description, files_hint, agent_prompt}` 五个键。

## 错误处理与边界情况

- 取消即退出：各确认流程选 N 直接 `sys.exit(0)`，无异常传播。
- 非交互环境保护：`confirm_plan`/`confirm_subtasks` 连续空输入 > 5 次后 `sys.exit(1)` 并提示使用 `--yes`；`safe_input` 本身在 EOF 时返回空串，配合该机制避免死循环。
- API 失败：`generate_plan` 抛出的所有异常被捕获、打印并计数（`agent_go/ui.py:200-208`、`229-237`），连续 2 次失败后询问降级/重试/取消。
- 字段缺失宽容：`plan_to_md`/`print_plan` 对缺失字段一律回退 `N/A`/空；但 `confirm_plan` 的 E 选项直接访问 `plan["steps"]` 及 `step["title"]`、`step["description"]`（`agent_go/ui.py:166-171`），缺键会抛 `KeyError`。
- `verify_subtask` 无空输入退出保护，非交互环境下若 `auto_verify_subtask` 未开启且输入流持续返回空串会无限循环（仅打印"无效输入"）。

## 测试覆盖

- `tests/test_ui.py`：覆盖 `plan_to_md`（基本渲染、最小 Plan、空 steps、缺 overview、文件/风险字段、无依赖时不输出依赖段落）和 `verify_subtask`（C/R/M/A、大小写、auto_verify 开关、无 config 场景），通过 patch `agent_go.ui.safe_input` 模拟输入。
- `tests/test_plan_to_subtasks.py`：覆盖 `plan_to_subtasks`（见该文件）。
- `confirm_plan`/`confirm_subtasks`/`print_plan`/`print_subtasks` 无直接测试（文件头注释说明交互流程不易测）。

## 维护注意事项

- **删除子任务后 `depends_on` 悬空**：`confirm_subtasks` 的 D 选项重排 id（`agent_go/ui.py:394-395`）但不重写其他子任务的 `depends_on`，可能导致依赖引用不存在的 id 或指向错误的子任务。下游调度器若按 id 解析需注意。
- **手动添加的子任务字段不全**：A 选项创建的子任务缺少 `depends_on`/`skills`/`agent_type`/`_agent_type_source`/`verification`/`risks`（`agent_go/ui.py:388`），下游若用 `st["depends_on"]` 而非 `.get()` 会 `KeyError`。
- **硬编码值**：`max_plan_api_failures = 2`（`agent_go/ui.py:122`）、空输入退出阈值 5（`agent_go/ui.py:241`、`agent_go/ui.py:401`）、预览截断长度 200/150 字符，均未走 config。
- **`"__FALLBACK__"` 哨兵耦合**：`confirm_plan` 用字符串哨兵与 plan dict 混在同一返回位（`agent_go/ui.py:207`），调用方（`cli.py`）必须按该精确字符串判断，属隐式协议。
- **延迟 import**：`role_skill_map`/`skills` 在 `plan_to_subtasks` 函数体内 import（`agent_go/ui.py:285-286`），每次调用都执行；改顶层 import 需确认无循环依赖。
- **`_original_task` 隐式键**：`confirm_plan` 在 plan dict 上写入 `_original_task` 以在多次再生成间保留原任务，该键会随 plan 流转到下游，修改命名需同步 `cli.py`。
- **`verify_subtask` 无非交互保护**：与其他确认循环不一致，若在管道环境运行且未开 `auto_verify_subtask` 会死循环，可考虑复用 empty_count 模式。
- **编辑路径的 KeyError 风险**：`confirm_plan` E 选项直接索引 `step['title']`/`step['description']`，对缺字段的 LLM 输出不健壮。
