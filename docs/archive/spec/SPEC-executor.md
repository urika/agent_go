# executor 模块规格说明

## 概述

`agent_go/executor.py` 是 Plan Mode 工作流中**单个子任务（subtask）的完整执行器**。它负责从创建隔离的 git worktree、合并上游产物、构建 TASK.md 提示文件、调用 Claude Code（headless 或交互模式）、提交变更并打 tag、执行验证命令（含失败自动修复重试），到生成供下游子任务读取的上下文文件的端到端流程。模块对外的唯一入口是 `run_subtask`，由 `pipeline.py` 的拓扑波次调度器（串行或 `ThreadPoolExecutor` 并发）调用。本模块不感知全局调度，只负责"一个子任务的一次执行"。

## 公共接口

模块通过 `__all__` 仅导出一个符号，其余均为内部函数（下划线开头）。

### `run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref="", active_pids=None, active_pids_lock=None)`

`agent_go/executor.py:376`

子任务执行主入口，全流程编排。

- 参数：
  - `task_id` (str)：任务 ID，用于 worktree 分支名 `agent_go/{task_id}/{sub_id}` 和 tag 名 `{task_id}/{sub_id}`。
  - `subtask` (dict)：子任务定义，关键字段见下文「数据结构」。
  - `repo` (Path)：源仓库路径。
  - `task_dir` (Path)：任务工作目录（由 pipeline 提供，形如 `~/.agent_go/tasks/<task_id>`，详见 config 模块）。
  - `logger` (logging.Logger)：任务日志器。
  - `upstream_worktrees` (dict | None)：`{上游子任务 id: 上游 worktree Path}`，用于产物合并。
  - `headless` (bool)：True 走 `claude -p` 无头模式，False 走交互模式（greywall 包装或原生 claude）。
  - `issue_ref` (str)：关联 issue 引用，进入 commit message。
  - `active_pids` / `active_pids_lock`：并发执行时活动子进程 PID 集合与锁，透传给 `_run_headless` 用于中断清理。
- 返回值 (dict)：执行结果，字段见下文「数据结构」；由 pipeline 写入 `<task_dir>/<sub_id>/result.json`。
- 副作用：
  - 创建目录 `<task_dir>/<sub_id>/` 及 worktree `<task_dir>/<sub_id>/work/`；
  - 写入 `TASK.md`、`context.md`；
  - 在 worktree 内执行 `git add/commit/tag`、git worktree/clone、git merge；
  - 启动 `claude` 子进程；
  - 通过 `log_event` 记录 `subtask_start` / `subtask_complete` 结构化事件。

### 内部函数（承担关键职责）

| 函数 | 位置 | 职责 |
| --- | --- | --- |
| `_create_worktree(task_id, sub_id, repo, task_dir, logger)` | `executor.py:77` | 创建/复用 worktree，返回 `(worktree_path, create_time_ms)`；worktree add 失败回退 git clone，非 git 仓库回退 `shutil.copytree` |
| `_build_task_md(subtask, repo, task_dir, worktree, logger, headless, merge_conflicts=None)` | `executor.py:107` | 构建 TASK.md 内容，返回 `(task_md, verification, skill_names, unresolved_skills)`；注入合并冲突、上游 context、skill 知识，并将源仓库路径重写为 worktree 路径 |
| `_run_claude(task_md, worktree, env, headless, agent, sub_id, active_pids, active_pids_lock, logger)` | `executor.py:191` | 按模式调用 Claude，返回 `(result, sandbox_type, claude_time)`；sandbox_type ∈ `headless` / `greywall` / `native` |
| `_verify_changes(task_id, sub_id, subtask, worktree, headless, task_md, env, tag_name, active_pids, active_pids_lock, logger, issue_ref="", allowed_tools=None)` | `executor.py:224` | 统计变更、`git add/commit/tag`、逐条执行验证命令（headless 下失败注入修复 prompt 重试一次），返回验证结果 dict |
| `_run_verification_cmd(vcmd, worktree, attempt, env, logger, task_id="", sub_id="")` | `executor.py:20` | 单条验证命令执行：安全门禁 → `subprocess.run`（120s 超时 + rlimit 沙箱 + 脱敏环境），返回结果 dict |
| `_generate_context(subtask, task_dir, sub_id, logger, headless, result, verify_ok, summary, verification)` | `executor.py:350` | 写入 `<task_dir>/<sub_id>/context.md`，供直接下游子任务注入 TASK.md |
| `_apply_resource_limits()` | `executor.py:52` | 验证命令子进程 `preexec_fn`，设置 ulimit（CPU 60s / 文件 50MB / fd 256 / 进程 64） |
| `_build_sandbox_env()` | `executor.py:64` | 构建验证命令的脱敏环境：剔除含 `API_KEY/SECRET/TOKEN/PASSWORD/CREDENTIAL/PRIVATE_KEY` 的环境变量（`AGENT_GO_` 前缀除外，但 `AGENT_GO_API_KEY` 单独强制剔除） |

模块级常量（`executor.py:15-17`）：`_BOUNDARY_CHARS` / `_BOUNDARY_BEFORE` / `_BOUNDARY_AFTER`，用于路径替换正则的边界匹配，避免误替换路径子串。

## 关键逻辑与流程

`run_subtask` 的执行分 7 步：

1. **创建 worktree**（`executor.py:389`）：已存在则复用；git 仓库走 `_worktree_create`（分支 `agent_go/{task_id}/{sub_id}`），失败回退 `git clone` + `git checkout -b`；非 git 目录直接 `copytree`。
2. **上游产物合并**（`executor.py:395-410`）：对每个 `upstream_worktrees` 调用 `_git_merge_upstream`（合并 tag `{task_id}/{up_id}`）。冲突通过 `subtask.py` 写入的 `<worktree>/.MERGE_CONFLICT` 标记文件检测：读取内容存入 `merge_conflicts` 后删除该文件，并用 `collect_merge_result` 记录。
3. **构建 TASK.md**（`executor.py:414`）：`_build_task_md` 组装任务描述、agent_prompt、执行要求、验证命令；注入合并冲突说明（含"解决后 `git add . && git commit`"提示）与直接上游的 `context.md` 内容；按 `subtask["skills"]` 逐个 `load_skill` + `render_skill_for_execution` 注入 skill 知识，未找到的记入 `unresolved_skills`；最后用边界正则把文本中源仓库路径替换为 worktree 路径，保证 LLM 在隔离目录内操作。TASK.md 落盘到 `<task_dir>/<sub_id>/TASK.md`（`executor.py:420`）。
4. **环境变量与 Agent 类型**（`executor.py:433-446`）：env 注入 `AGENT_GO_TASK_ID` / `AGENT_GO_SUBTASK_ID` / `AGENT_GO_WORKTREE` / `AGENT_GO_SKILLS`；`load_agent_type` 加载 agent 配置（默认 `"developer"`），未注册时 warn 降级并继续（agent=None，走默认 claude 命令）。注意 verification 命令中的仓库路径在 `_build_task_md` 之外单独重写（`executor.py:425-430`），而 context.md 用的是重写前的 `original_verification`。
5. **运行 Claude**（`executor.py:449`）：headless 走 `_run_headless`（强制透传 agent 的 `allowed_tools` 白名单）；交互模式优先 `get_claude_command(agent, ...)`，无 agent 时 `(greywall --)? claude <worktree>`，`FileNotFoundError` 时降级原生 `claude`。
6. **验证与提交**（`executor.py:455`）：`_verify_changes` 先 `git status --porcelain` 判定有无变更，生成摘要文本（`git diff --stat` + `??` 新文件列表），`collect_change_stats` 采集结构化统计；有变更则 `_format_commit` 生成 Conventional Commits 消息并 `git add -A && git commit`，随后**无条件** `git tag -f {task_id}/{sub_id}`（无变更也打 tag，供下游 merge）。验证命令支持 str 或 list，逐条执行；`exit_code` 为 0 或 127（命令不存在）视为通过；被安全门禁拒绝直接判失败不重试；headless 模式下失败时注入"修复指令"prompt 调 `_run_headless` 重试一次，重新 add/commit/tag 后全量重跑验证命令；交互模式遇失败即停。
7. **生成上下文 + 状态判定**（`executor.py:470-482`）：`_generate_context` 写 `context.md`（状态、变更摘要、验证结果、risks、从 Claude stdout 正则提取的"关键决策"最多 3 条）。状态三值：`result.returncode == 0 and verify_ok` → `completed`（有变更）/ `no_changes`；否则 `failed`。

## 依赖关系

内部模块（`executor.py:4-10`）：

- `.console.get_default_console`：控制台输出。
- `.config.log_event`：结构化事件日志（JSON 行，debug 级）。
- `.utils`：`_format_commit`（Conventional Commits 消息）、`_is_safe_verification_command`（验证命令安全门禁）、`_log_rejected_command`（拒绝命令审计，写 `~/.agent_go/verification_audit.jsonl`）。
- `.subtask`：`_git_merge_upstream`（上游 tag 合并，冲突时写 `.MERGE_CONFLICT` 标记文件——**跨模块文件协议**）、`_run_headless`（`claude -p` 无头执行）。
- `.agents`：`load_agent_type` / `get_claude_command` / `get_agent_env`，另在 `run_subtask` 内延迟导入 `list_agent_types`。
- `.git_utils._worktree_create`：返回 `(ok, err_msg)`。
- `.metrics`：`collect_timing` / `collect_change_stats` / `collect_merge_result`。
- `.skills`（函数内延迟导入，`executor.py:167`）：`load_skill` / `render_skill_for_execution` / `list_skills`。

外部依赖：

- CLI 命令：`git`（worktree/clone/checkout/status/diff/add/commit/tag/merge）、`claude`、`greywall`（可选，`shutil.which` 探测）。
- 环境变量：写入 `AGENT_GO_TASK_ID` / `AGENT_GO_SUBTASK_ID` / `AGENT_GO_WORKTREE` / `AGENT_GO_SKILLS`；验证沙箱剔除含敏感关键词的环境变量（`AGENT_GO_API_KEY` 显式剔除）。
- 文件系统：`<task_dir>/<sub_id>/{work/, TASK.md, context.md}`；worktree 内 `.MERGE_CONFLICT` 标记文件（由 subtask.py 写入，本模块消费并删除）；间接涉及 `~/.agent_go/agents/`、`~/.agent_go/skills/`（经 agents/skills 模块）。

## 数据结构与持久化

### `subtask` dict（入参，由 pipeline/plan 模块构造）

使用字段：`id`（必需）、`title`、`description`、`depends_on`（list，默认 `[]`）、`agent_prompt`（可选）、`verification`（str 或 list[str]，默认 `""`）、`skills`（list[str]）、`agent_type`（默认 `"developer"`）、`risks`（list[str]）、`_agent_type_source`（透传到返回值，默认 `"default"`）。

### `run_subtask` 返回 dict（持久化为 `<task_dir>/<sub_id>/result.json`，由 pipeline 写入）

```
subtask_id, status ("completed"|"no_changes"|"failed"), exit_code, summary,
worktree, sandbox_type ("headless"|"greywall"|"native"), verify_ok, duration_sec,
agent_type_source, skills_unresolved (list), retry_count,
timing (collect_timing 输出: worktree_create_ms/merge_upstream_ms/claude_execute_ms/verification_ms/git_commit_ms),
change_stats (collect_change_stats 输出: files_changed/insertions/deletions/new_files/modified_files/actual_files),
merge_results (list of {upstream, status: "success"|"conflict", conflict_files?}),
verification_results (list of {command, exit_code, duration_ms, attempt, rejected?, reject_reason?})
```

### 文件

- `<task_dir>/<sub_id>/TASK.md`：任务提示文件（Markdown，UTF-8）。
- `<task_dir>/<sub_id>/context.md`：子任务完成后的共享上下文，仅被**直接下游**子任务在 `_build_task_md` 中读取注入。
- `<worktree>/.MERGE_CONFLICT`：subtask.py 与本模块间的冲突标记文件协议，消费后即删。
- git 侧产物：分支 `agent_go/{task_id}/{sub_id}`、tag `{task_id}/{sub_id}`（强制 `-f` 覆盖）。

## 错误处理与边界情况

- **整体策略**：本模块内部不向外抛业务异常，各步骤失败以 warning 日志降级（git add/commit/tag 失败仅告警继续）；异常兜底在 `pipeline.py` 的 future 包装层（捕获后构造 `status="failed"` 结果）。
- **worktree 创建**：worktree add 失败 → `git clone`（`check=True`，clone 本身失败会抛 `CalledProcessError`，由调用方兜底）；checkout 分支失败仅告警；非 git 目录静默降级 copytree。
- **验证命令**：安全门禁拒绝 → 记审计、判失败、不执行不重试；argv 无法解析（`FileNotFoundError/OSError/ValueError`）→ 跳过，**不降级 shell=True**（安全策略，`executor.py:44`）；120s 超时 → `exit_code=-1` 判失败；`exit_code=127`（命令不存在）视为通过；rlimit 设置失败静默忽略。
- **验证重试**：仅 headless 模式、仅一轮（注入修复 prompt → 重新提交 → 全量重跑命令）；交互模式遇首个失败即 `break`。
- **greywall 降级**：交互模式下 `FileNotFoundError` 时降级原生 `claude`（`executor.py:214`）。
- **Agent 类型未注册**：warn 后以 `agent=None` 继续，不中断执行。
- **Skill 未找到**：warn 跳过并计入返回的 `skills_unresolved`，不中断。
- **无变更**：跳过 commit，但仍强制打 tag；状态记 `no_changes`。
- **中断处理**：本模块不处理信号；SIGINT/SIGTERM 由 `pipeline.py` 注册处理器，经 `active_pids`/`active_pids_lock` 清理 claude 子进程。

## 测试覆盖

测试文件：`tests/test_executor.py`（约 903 行，集中在 `TestRunSubtask` 类，通过 mock `_worktree_create`、`subprocess.run`、`_run_headless`、`load_agent_type` 等隔离外部依赖）。覆盖场景：

- 三种状态判定：`completed` / `no_changes` / `failed`（含验证失败判 failed）；
- headless 与交互模式路径、sandbox_type 取值（headless/native）；
- TASK.md 生成（文件创建、上游 context 注入、merge 冲突注入、skill 注入）；
- context.md 生成（含 risks、verification 标注）；
- 环境变量注入（`AGENT_GO_*`）；
- 上游 merge 调用、tag 带 task_id 前缀命名空间；
- worktree 复用、clone 回退、非 git 目录 copytree 降级；
- 返回值结构完整性；
- 验证命令执行与失败处理。

## 维护注意事项

- **已修复（2026-07-23，docs/ISSUES.md ISSUE-5）**：`utils._is_safe_verification_command` 曾标注返回 `bool`、实际返回 `(bool, reason)` 元组；注解已修正为 `tuple[bool, str]`，`executor.py:26` 按元组解包不变。
- **`_run_verification_cmd` 的 `env` 参数未使用**（`executor.py:20`）：函数体内实际使用 `_build_sandbox_env()` 新建环境，`run_subtask` 传入的执行环境对验证命令不生效（这是有意的沙箱隔离，但签名具误导性，建议重命名或移除）。
- **重试验证的状态翻转逻辑脆弱**（`executor.py:313-327`）：重试循环中 `verify_ok = True` 在每条命令通过时被设置、`retry_verify_ok` 仅在拒绝/失败分支使用，语义依赖 `break` 时序；改动重试逻辑（如多轮重试）时需仔细推演。
- **硬编码值**：验证超时 120s（`executor.py:37`）；rlimit（CPU 60s / 文件 50MB / fd 256 / 进程 64，`executor.py:56-59`）；context.md 决策提取正则只匹配中文关键词（决策/选择/采用/改用/改为/降级/fallback，`executor.py:363`）且最多取 3 条；`result_entry["command"]` 截断 200 字符；敏感关键词列表硬编码在 `_build_sandbox_env`。
- **跨模块隐式耦合**：`.MERGE_CONFLICT` 标记文件是 `subtask._git_merge_upstream` 与本模块之间的文件协议，两边必须同步修改；tag 命名 `{task_id}/{sub_id}` 同时被本模块（打 tag）、subtask.py（merge）、pipeline（恢复逻辑）依赖。
- **验证命令重写不对称**：TASK.md 内的路径替换发生在 `_build_task_md` 末尾对全文生效，而 `verification` 字段的路径重写单独在 `run_subtask` 中进行（`executor.py:425-430`），两处使用同一 `_BOUNDARY_*` 常量；且 context.md 记录的是重写前的原始命令——排查"验证命令路径不对"问题时需分清三个阶段。
- **并发安全**：`run_subtask` 在多线程下并行调用，各子任务目录/tag 按 sub_id 隔离；共享状态仅 `active_pids`（有锁）和全局 console/logger。`context.md` 注释称"线程安全地追加"，但实际是整文件覆盖写（每个 sub_id 独立文件，无竞态），注释与实现有出入。
- **改进建议**：git add/commit/tag 失败仅告警后继续，可能导致 tag 指向旧 commit 而下游 merge 到错误产物，可考虑对 tag 失败提升为 failed；`shutil.copytree` 降级路径（非 git 仓库）下后续 git 命令（status/diff/commit/tag）必然失败，当前靠 warning 硬扛，可考虑显式跳过。
