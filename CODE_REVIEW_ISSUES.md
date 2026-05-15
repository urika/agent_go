# agent_go 代码审查 — 发现的问题清单

> 版本: v2（迭代后复查）  
> 日期: 2026-05-15  
> 审查范围: `agent_go.py`（约 1400 行）+ 4 份设计/需求文档  
> 审查方法: 深度代码阅读 + 架构分析 + 边界情况推演 + 迭代变更对比

---

## 迭代回顾（v1 → v2 改进）

| # | 问题 | v2 状态 | 本次变化 |
|---|------|---------|---------|
| Q2 | `verify_subtask` 配置键错误 | ❌ 未修复 | 仍使用 `auto_confirm_plan` |
| Q5 | 路径替换误匹配 | ❌ 未修复 | 仍是 `str.replace()` |
| Q6 | `shell=True` 安全风险 | ✅ **部分修复** | 已优先使用 `shlex.split()`，但保留了 shell=True 降级 |
| Q7 | SHARED_CONTEXT 并发安全 | ⚠️ **风险升级** | 新增 `--parallel` 后问题从潜在变为实际 |
| Q8 | 任务 ID 碰撞 | ❌ 未修复 | 仍是秒级精度 |
| Q9 | commit 类型仅中文 | ❌ 未修复 | 仍是 `"实现"/"新增"` |
| Q13 | 无 KeyboardInterrupt | ❌ 未修复 | 无 try/except |

### v2 迭代新增改进

| 改进 | 说明 |
|------|------|
| ✅ `--parallel N` 并发执行 | 拓扑排序列 wave + ThreadPoolExecutor，重大架构升级 |
| ✅ stream-json 事件解析 | 从纯文本升级为解析 `content_block_start/delta/stop`、`tool_use`、`tool_result` 等细粒度事件 |
| ✅ `plan_to_md()` + PLAN.md 持久化 | Plan 方案保存为 Markdown 文件 |
| ✅ `degraded` 状态 | 区分「正常完成有变更」vs「正常完成无变更」 |
| ✅ 路径穿越防护 | `--docs` 路径解析后校验须在 repo 范围内 |
| ✅ 配置文件 chmod 600 | API Key 文件仅 owner 可读写 |
| ✅ 配置深层合并 | JSON 序列化深拷贝替代浅拷贝 |
| ✅ 验证命令优先 shlex | `shlex.split()` + shell=True 降级 |
| ✅ 超时放宽至 600s | 更容忍 LLM 长思考周期 |
| ✅ 空输入检测 | 连续 5 次空输入自动退出 |
| ✅ 异常类型精细化 | `(FileNotFoundError, subprocess.SubprocessError)` 替代裸 `Exception` |

---

## 问题总览

### 原始问题（Q1-Q13）

| 编号 | 类别 | 严重程度 | v2 状态 | 标题 |
|------|------|----------|---------|------|
| [Q1](#q1-plan-mode-交互中降级路径缺失) | 流程缺陷 | 🔴 高 | ❌ 未修复 | Plan Mode 交互中降级路径缺失 |
| [Q2](#q2-verify_subtask-自动确认配置错误) | Bug | 🔴 高 | ❌ 未修复 | `verify_subtask` 自动确认配置错误 |
| [Q3](#q3-git-merge-冲突处理缺失) | 流程缺陷 | 🟡 中 | ❌ 未修复 | `git merge` 冲突处理缺失 |
| [Q4](#q4-无头模式交互检测不够鲁棒) | 可靠性 | 🟡 中 | ❌ 未修复 | 无头模式交互检测不够鲁棒 |
| [Q5](#q5-tasksmd-路径替换存在误匹配风险) | Bug | 🟡 中 | ❌ 未修复 | TASK.md 路径替换存在误匹配风险 |
| [Q6](#q6-shelltrue-安全风险) | 安全 | 🔴 高 | 🔶 部分修复 | `shell=True` 安全风险 |
| [Q7](#q7-shared_contextmd-并发写入不安全) | 并发安全 | 🟡 中 | ⚠️ 风险升级 | SHARED_CONTEXT.md 并发写入不安全 |
| [Q8](#q8-任务-id-时间戳可能碰撞) | 可靠性 | 🟢 低 | ❌ 未修复 | 任务 ID 时间戳可能碰撞 |
| [Q9](#q9-commit-消息类型判断仅支持中文) | 功能缺陷 | 🟢 低 | ❌ 未修复 | commit 消息类型判断仅支持中文 |
| [Q10](#q10-greywall--claude-code-版本无检测) | 可靠性 | 🟢 低 | ❌ 未修复 | Greywall / Claude Code 版本无检测 |
| [Q11](#q11-subprocessrun-普遍未检查返回码) | 可靠性 | 🟡 中 | ❌ 未修复 | `subprocess.run` 普遍未检查返回码 |
| [Q12](#q12-单文件规模接近可维护性上限) | 可维护性 | 🟡 中 | ⚠️ 规模扩大 | 单文件规模 ~1400 行 |
| [Q13](#q13-无键盘中断处理) | 可靠性 | 🟢 低 | ❌ 未修复 | 无 KeyboardInterrupt 处理 |

### 迭代新增问题（N1-N8）

| 编号 | 类别 | 严重程度 | 标题 |
|------|------|----------|------|
| [N1](#n1-并发模式下-shared_contextmd-仍有时序问题) | 并发安全 | 🔴 高 | 并发模式下 SHARED_CONTEXT.md 仍有时序问题 |
| [N2](#n2-并行模式下-git-worktree-分支命名冲突) | 并发安全 | 🟡 中 | 并行模式下 git worktree 分支命名冲突 |
| [N3](#n3-confirm_plan-闭包依赖外层-task-变量) | 代码质量 | 🟡 中 | `confirm_plan` 闭包依赖外层 `task` 变量 |
| [N4](#n4-无-keyboardinterrupt-处理严重性升级) | 可靠性 | 🟡 中 | 无 KeyboardInterrupt 处理（严重性升级） |
| [N5](#n5-degraded-状态命名存在语义歧义) | 代码质量 | 🟢 低 | `degraded` 状态命名存在语义歧义 |
| [N6](#n6-stream-json-事件日志过于详细) | 性能 | 🟢 低 | stream-json 事件日志过于详细 |
| [N7](#n7-并发模式与交互模式不兼容) | 流程缺陷 | 🟡 中 | 并发模式与交互模式不兼容 |
| [N8](#n8-read_reference_docs-目录文档不递归) | 功能缺陷 | 🟢 低 | `read_reference_docs` 目录文档不递归 |

---

## 问题详情 — 原始问题

### Q1: Plan Mode 交互中降级路径缺失

**严重程度**: 🔴 高  
**位置**: `confirm_plan()` 中 S/D/R 分支（约第 360-410 行）

**问题描述**:  
当 LLM API 正常返回第一个 Plan 后，用户在交互中按 S/D/R 触发重新生成时，如果 API 调用失败，当前只打印警告，`plan` 变量不更新，用户回到确认循环但看到的是旧版 plan。如果 API 持续不可用，用户会陷入死循环 — 降级路径 `decompose_fallback()` 只有在 `generate_plan()` 尝试 3 次全部失败时才会被触发，但交互中的重新生成调用的是独立的 `generate_plan()`，没有接入这个降级逻辑。

**相关代码**:
```python
# confirm_plan() 中
elif choice == "R":
    try:
        plan = generate_plan(...)
    except Exception as e:
        logger.error(f"重新生成失败: {e}")
        print(f"⚠️ 失败: {e}")
        # ❌ plan 未更新，用户继续循环，无法降级
```

**建议修复**:
1. 交互中 API 失败时，提供「降级到规则拆解」选项
2. 或在交互中累计失败次数，超过阈值自动降级
3. 至少打印更明确的提示，告诉用户当前使用的是旧版方案

---

### Q2: `verify_subtask` 自动确认配置错误

**严重程度**: 🔴 高  
**位置**: `verify_subtask()` 函数（约第 600 行）

**问题描述**:  
`verify_subtask()` 读取的自动确认配置项是 `auto_confirm_plan`，但语义上应控制的是子任务验证步骤的自动确认。虽然在 `--yes` 路径下两个配置项都被置为 `True` 而工作正常，但如果用户只想自动确认 Plan 而手动确认每个子任务的验证结果，当前无法实现。

**相关代码**:
```python
def verify_subtask(current, total, summary, logger, config=None):
    # ❌ 读取了 auto_confirm_plan，应为 auto_verify_subtask 或 auto_confirm_subtasks
    auto_verify = config.get("behavior", {}).get("auto_confirm_plan", False) if config else False
```

**建议修复**:
1. 新增独立配置项 `behavior.auto_verify_subtask`
2. 或改为读取 `auto_confirm_subtasks`（语义更接近）
3. 兼容方案：`--yes` 模式下所有自动确认项同时生效

---

### Q3: `git merge` 冲突处理缺失

**严重程度**: 🟡 中  
**位置**: `_git_merge_upstream()` 函数（约第 630 行）

**问题描述**:  
当上游和下游子任务修改了同一文件的同一区域时，`git merge` 会产生冲突。当前代码只记录 warning，不做任何冲突处理。下游子任务的 Claude Code 将面对一个含有 `<<<<<<<` / `>>>>>>>` 冲突标记的工作区，可能导致：
1. Claude Code 无法正常理解代码
2. 语法错误导致验证失败
3. 子任务静默产出错误代码

**相关代码**:
```python
if result.returncode == 0:
    logger.info(f"git merge {tag} 成功")
else:
    logger.warning(f"git merge {tag}: {result.stderr[:200]}")
    # ❌ 冲突未被处理，下游子任务继续执行
```

**建议修复**:
1. 检测到冲突时暂停执行，要求人工介入
2. 将冲突文件列表注入下游 TASK.md，让 Claude Code 自行解决
3. 在 Plan 阶段通过文件依赖分析避免可能冲突的子任务拆解
4. 使用 `git merge --no-commit` + 检查 `git diff --name-only --diff-filter=U` 来显式检测冲突

---

### Q4: 无头模式交互检测不够鲁棒

**严重程度**: 🟡 中  
**位置**: `_run_headless()` 中 `read()` 内部函数（约第 660 行）

**问题描述**:  
交互检测依赖对 Claude Code 输出文本的正则匹配：

```python
for pat in [r"waiting for input", r"approve\s+(Write|Edit|Bash|Read)",
            r"permission required", r"\[y/n\]", r"press.*to continue"]:
```

存在三个问题：
1. **语言假设**: 只匹配英文，中文环境下的「等待输入」「请确认」等文案无法检测
2. **版本耦合**: Claude Code 更新后交互提示文案可能变化，检测失效
3. **误报风险**: 如果 Claude Code 在代码注释或 Markdown 中输出这些模式，会触发误判

**建议修复**:
1. 以超时（无输出 N 秒）作为主要判断依据，正则作为辅助
2. 检测 Claude Code 的退出码（如 130 = SIGINT）来判定是否被中断
3. 增加中文交互文案的正则模式
4. 配合 `--no-session-persistence` 参数减少交互可能性

---

### Q5: TASK.md 路径替换存在误匹配风险

**严重程度**: 🟡 中  
**位置**: `run_subtask()` 函数（约第 935 行）

**问题描述**:  
```python
task_md = "\n".join(task_md_parts).replace(str(repo), str(worktree))
```

简单的字符串替换可能误匹配。例如：
- `repo = /Users/john/project`，worktree 路径为 `/Users/john/.agent_go/task-xxx/sub-1/work`
- 如果 TASK.md 中有文本 `参考项目 /Users/john/project-legacy`
- 会被替换为 `参考项目 /Users/john/.agent_go/task-xxx/sub-1/work-legacy`（错误）

另一个方向：如果 worktree 路径（如 `/tmp/work`）恰好出现在与路径无关的 Prompt 文本中，也会被意外替换。

**建议修复**:
1. 限定替换范围：仅替换出现在文件路径上下文中的 repo 路径（如前后为空格/引号/换行/冒号）
2. 使用正则 `\b` 边界匹配
3. 先替换为占位符 `{{WORKTREE}}` 再展开，避免交叉替换
4. 替换顺序：先替换长的（worktree），再替换短的（repo），确保不产生嵌套问题

---

### Q6: `shell=True` 安全风险

**严重程度**: 🔴 高 → 🔶 部分修复  
**位置**: `run_subtask()` 中验证执行（约第 1010 行）

**问题描述**:  
验证命令优先使用 `shlex.split()` 来避免 shell 注入，并在解析失败时降级为 `shell=True`：

```python
try:
    vr = subprocess.run(shlex.split(verification), cwd=str(worktree), ...)
except (FileNotFoundError, OSError):
    # shlex 解析失败时（如中文描述），尝试 shell=True
    vr = subprocess.run(verification, shell=True, cwd=str(worktree), ...)
```

**迭代变化**: ✅ **已部分修复**。`shlex.split()` 作为首选路径，覆盖了大多数标准验证命令（如 `go test ./...`、`npm test`）。但仍保留了 `shell=True` 降级，因为 LLM API 可能返回包含中文、管道、重定向等无法用 shlex 解析的验证命令。

**剩余风险**: 攻击者仍可通过构造一个可被 shlex 解析但包含恶意命令的 verification 字符串（如 `echo ok; rm -rf /` 不会被 shlex 拆分——它会作为三个参数传给可执行文件，实际不会执行 rm）来绕过。更危险的是 shell=True 降级路径，如果验证命令包含 `&&`、`|` 等 shell 元字符且无法被 shlex 解析，最终会走 shell=True。

**建议修复**:
1. 维护一个安全命令白名单（如 `go test`, `npm test`, `pytest`, `python -m pytest` 等常见验证命令前缀）
2. 对 verification 命令做安全检查：禁止包含 `&&`、`;`、反引号、`$()` 等 shell 元字符
3. 走 shell=True 降级路径时，先打印完整命令让用户确认

---

### Q7: SHARED_CONTEXT.md 并发写入不安全

**严重程度**: 🟡 中 → ⚠️ **风险升级**（因 `--parallel` 上线）  
**位置**: `run_subtask()` 中共享上下文写入（约第 1050 行）

**问题描述**:  
```python
existing = shared_ctx.read_text(encoding="utf-8") if shared_ctx.exists() else ""
shared_ctx.write_text(existing + "\n".join(ctx_parts) + "\n", encoding="utf-8")
```

这是典型的 TOCTOU（Time-of-check to time-of-use）竞态条件。

**迭代变化**: ⚠️ **风险已从潜在变为实际**。v2 新增 `--parallel N` 并发执行后，同一 wave 内的子任务会并行读写 `SHARED_CONTEXT.md`，但只有 `meta.json` 的写入受 `meta_lock` 保护，`SHARED_CONTEXT.md` 没有任何锁保护。

**并发写入场景**:
1. 子任务 A 读取文件（空）
2. 子任务 B 读取文件（空）
3. 子任务 B 写入内容
4. 子任务 A 写入内容 → **B 的上下文被覆盖丢失**

**建议修复**:
1. 将 `SHARED_CONTEXT.md` 的读写也纳入 `meta_lock` 保护
2. 或改为每个子任务写独立文件（如 `SHARED_CONTEXT_sub-1.md`），最后统一合并
3. 或使用追加写入模式 + 原子重命名

---

### Q8: 任务 ID 时间戳可能碰撞

**严重程度**: 🟢 低  
**位置**: `cmd_run()`（约第 990 行）

**问题描述**:  
```python
ts = datetime.now().strftime("%Y%m%d-%H%M%S")
task_id = f"task-{ts}"
```

秒级精度。同一秒内启动多个任务（手动快速执行两次、脚本批量调用、或并行执行 fork 子进程）会导致 ID 冲突，后启动的任务会覆盖先启动任务的目录。

**建议修复**:
1. 加入微秒：`%Y%m%d-%H%M%S-%f`
2. 或加入 4 位随机后缀：`f"task-{ts}-{random.randint(1000,9999)}"`
3. 或在创建目录时检测冲突并自旋重试

---

### Q9: Commit 消息类型判断仅支持中文

**严重程度**: 🟢 低  
**位置**: `_format_commit()`（约第 620 行）

**问题描述**:  
```python
prefix = "feat" if "实现" in title or "新增" in title else "chore"
```

只检查中文关键词。英文 title 如 `"Add OAuth2 support"` 永远被归类为 `chore:` 而非 `feat:`。
同理，`"fix: handle null pointer"` 不会被识别为 `fix:`。

**建议修复**:
1. 扩展关键词：`add|implement|create|feature|new` → `feat`，`fix|bug|patch|resolve|hotfix` → `fix`
2. 更好的方案：让 Plan API 在 `step` 中返回 `commit_type` 字段
3. 或遵循 Conventional Commits 的 `type(scope): description` 格式，让 Agent Prompt 自己填写 type

---

### Q10: Greywall / Claude Code 版本无检测

**严重程度**: 🟢 低  
**位置**: `run_subtask()` 执行引擎选择（约第 880 行）

**问题描述**:  
代码通过 `try/except FileNotFoundError` 来降级 Greywall → Claude → headless，但没有检查版本兼容性。如果 Claude Code 或 Greywall 升级后 CLI 参数变化、输出格式变化、或退出码语义变化，agent_go 会静默失败。

**建议修复**:
1. 启动时检查 `claude --version` 和 `greywall --version` 的最低版本
2. 在 `meta.json` 中记录执行时使用的工具版本，便于事后追溯
3. 对关键 CLI 参数做功能测试（如 `claude -p --help` 检查参数是否存在）

---

### Q11: `subprocess.run` 普遍未检查返回码

**严重程度**: 🟡 中  
**位置**: 全局多处

**问题描述**:  
大量 `git` 命令调用使用 `capture_output=True` 但忽略返回码。例如：
- `git add -A` 可能因权限问题失败
- `git commit` 可能因 pre-commit hook 失败
- `git tag -f` 可能因磁盘满失败
- `git clone` 可能因网络问题失败（虽然有 `check=True`）

这些失败在无头模式下尤其危险，因为用户不会看到错误输出，Agent 可能基于不完整的代码继续执行下一个子任务。

**建议修复**:
1. 所有关键 git 操作统一检查返回码
2. 封装 `_git_run()` 辅助函数，统一错误处理
3. 失败时将 git stderr 写入日志并中断当前子任务

---

### Q12: 单文件规模接近可维护性上限

**严重程度**: 🟡 中 → ⚠️ **规模扩大至 ~1400 行**  
**位置**: `agent_go.py` 整体

**问题描述**:  
v2 迭代新增 `--parallel` 并发执行、stream-json 事件解析、拓扑排序调度等功能后，代码从约 1250 行增长至约 1400 行。新增的隐式耦合点：
- `meta_lock`（`threading.Lock()`）作为模块级共享状态
- `worktree_map` 和 `results_map` 在 cmd_run 和 run_subtask 之间传递
- 并发调度逻辑与核心业务逻辑混在同一函数

**建议修复**:
在 v3 迭代中考虑拆分：
```
agent_go/
├── __init__.py
├── cli.py          # main / cmd_* 入口
├── config.py       # load_config / get_api_key / setup_logger
├── plan.py         # generate_plan / call_api / decompose_fallback
├── subtask.py      # run_subtask / _run_headless / _git_merge_upstream
├── ui.py           # print_plan / confirm_plan / print_subtasks / confirm_subtasks
├── pr.py           # cmd_pr
└── utils.py        # _slugify / _format_commit / read_reference_docs
```

---

### Q13: 无 KeyboardInterrupt 处理

**严重程度**: 🟢 低  
**位置**: `main()` / `cmd_run()` 入口

**问题描述**:  
用户按 `Ctrl+C` 中断执行时，程序直接退出，不执行任何清理：
1. 孤儿 git worktree 残留
2. 运行的 claude 子进程未终止
3. `meta.json` 状态未更新为 `aborted`

**建议修复**:
```python
def cmd_run():
    try:
        # ... existing code ...
    except KeyboardInterrupt:
        logger.warning("用户中断")
        meta["status"] = "aborted"
        (task_dir / "meta.json").write_text(...)
        print("\n⚠️ 任务已中断")
        sys.exit(1)
```

---

## 问题详情 — 迭代新增问题

### N1: 并发模式下 SHARED_CONTEXT.md 仍有时序问题

**严重程度**: 🔴 高  
**位置**: `run_subtask()` 中共享上下文写入（约第 1050 行）  
**引入版本**: v2（`--parallel` 并发执行）

**问题描述**:  
尽管 `threading.Lock()` 保护了 `meta.json` 的写入，但 `SHARED_CONTEXT.md` 的读-修改-写操作完全在锁外：

```python
# meta.json 的写入有锁保护
with meta_lock:
    meta["results"] = ...
    (task_dir / "meta.json").write_text(...)

# 但 SHARED_CONTEXT.md 的写入无任何保护
shared_ctx = (task_dir / "SHARED_CONTEXT.md")
existing = shared_ctx.read_text(encoding="utf-8") if shared_ctx.exists() else ""
shared_ctx.write_text(existing + "\n".join(ctx_parts) + "\n", encoding="utf-8")
```

同一 wave 中并行执行的子任务同时读写此文件，最终写入者的内容覆盖了其他人的内容。

**建议修复**:
1. 将 `SHARED_CONTEXT.md` 的写入纳入 `meta_lock` 保护
2. 或改为每个子任务写独立文件（如 `SHARED_CONTEXT_sub-1.md`），每个 wave 结束后合并
3. 或使用追加写入模式（`open("a")`）加行级锁

---

### N2: 并行模式下 git worktree 分支命名冲突

**严重程度**: 🟡 中  
**位置**: `run_subtask()` 中分支创建（约第 930 行）

**问题描述**:  
```python
branch = f"feature/{issue_ref}-{_slugify(subtask['title'])}"
```

`_slugify()` 将标题截断为 30 字符，不同子任务可能生成相同的 slug。例如：
- "Add unit tests for auth module" → `feature/42-Add-unit-tests-for-auth`
- "Add unit tests for API module" → `feature/42-Add-unit-tests-for-api`

虽然第二个不会完全相同，但如果并行执行的子任务标题高度相似（如 "Setup frontend"、"Setup backend"）、或 issue_ref 为空时，分支名可能非常接近甚至相同。`git checkout -b` 失败会导致整个子任务异常中止。

**建议修复**: 在分支名中确保加入 `sub_id`（如 `sub-1`），确保唯一性。

---

### N3: `confirm_plan` 闭包依赖外层 `task` 变量

**严重程度**: 🟡 中  
**位置**: `confirm_plan()` 中 S/D 分支

**问题描述**:  
```python
# confirm_plan() 内部使用了外层 cmd_run() 的 task 变量
# 通过 Python 闭包隐式捕获
elif choice == "S":
    original = plan.get("_original_task", task)  # task 来自外层作用域
```

`task` 不是 `confirm_plan()` 的参数，而是通过闭包从 `cmd_run()` 捕获。如果未来将 `confirm_plan` 移出 `cmd_run`（如模块化拆分），这行会抛出 `NameError`。

**建议修复**: 将 `task` 作为显式参数传入 `confirm_plan()`。

---

### N4: 无 KeyboardInterrupt 处理（严重性升级）

**严重程度**: 🟡 中（原 Q13 从 🟢 升级）  
**位置**: `main()` / `cmd_run()` 入口，`ThreadPoolExecutor` 并发执行

**问题描述**:  
v1 已有此问题。v2 新增并发执行后，`Ctrl+C` 的中断后果更严重：
1. `ThreadPoolExecutor` 不会优雅关闭，正在运行的子任务被硬中断
2. 孤儿 git worktree 残留磁盘
3. `meta.json` 不更新为 `aborted`
4. 正在执行的 claude 子进程变成僵尸进程

**建议修复**:
```python
try:
    # 主执行循环
except KeyboardInterrupt:
    logger.warning("用户中断")
    meta["status"] = "aborted"
    (task_dir / "meta.json").write_text(...)
    executor.shutdown(wait=False)  # 并发模式
    print("\n⚠️ 任务已中断")
    sys.exit(1)
```

---

### N5: `degraded` 状态命名存在语义歧义

**严重程度**: 🟢 低  
**位置**: `run_subtask()` 状态判定（约第 1070 行）

**问题描述**:  
```python
if result.returncode == 0 and verify_ok:
    status = "degraded" if summary == "无文件变更" else "completed"
```

"degraded" 的本意是「降级执行」（如 API 失败后走规则拆解），但这里用它表示「执行成功但没产生变更」。语义不匹配。有些子任务可能天然不需要变更（如只读分析、纯验证任务），不应被视为降级。

**建议修复**:
1. 改用 `no_changes` 或 `skipped` 状态名
2. 或在最终报告中用不同的图标和文案区分

---

### N6: stream-json 事件日志过于详细

**严重程度**: 🟢 低  
**位置**: `_run_headless()` 中 stream-json 事件处理

**问题描述**:  
stream-json 模式下，每段文本增量（`text_delta`）都产生一条 `logger.info()` 日志：

```python
elif it == "content_block_delta":
    ...
    if text.strip():
        logger.info(f"{PFX} [text] {text[:200]}")
```

对大型代码生成任务（生成数千行代码），可能产生数万行日志，且每行都被写入文件和终端。

**建议修复**:
1. `text_delta` 改用 `logger.debug()`，INFO 级别只记录工具调用事件（`tool_use`, `tool_result`）
2. 或在终端输出时聚合文本增量，每 5 秒输出一次而非每段增量

---

### N7: 并发模式与交互模式不兼容

**严重程度**: 🟡 中  
**位置**: `cmd_run()` 中并行调度

**问题描述**:  
```python
if actual_workers == 1:
    # 串行执行
    ...
else:
    # 并发执行当前 wave
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
```

当 `parallel > 1` 但没有 `--headless`（即使用 greywall 或原生 claude 交互模式）时，会同时打开多个终端窗口/进程，用户无法同时操作多个交互式 Claude Code 会话。当前没有检测或警告。

**建议修复**:
1. 当 `parallel > 1` 且非 headless 时，打印警告并自动降至串行
2. 或强制启用 headless 模式
3. 或在非 headless 时拒绝使用并发

---

### N8: `read_reference_docs` 目录文档不递归

**严重程度**: 🟢 低  
**位置**: `read_reference_docs()` 函数

**问题描述**:  
```python
elif path.is_dir():
    for md_file in sorted(path.glob("*.md")):
```

`path.glob("*.md")` 只读取目录顶层 `.md` 文件，不递归子目录。如果目录包含 `docs/sub/guide.md`，不会被读取。

**建议修复**:
1. 改为 `**/*.md` 实现递归读取
2. 或提供选项让用户选择是否递归

---

## 问题统计

### 按严重程度

| 严重程度 | 数量 | 编号 |
|----------|------|------|
| 🔴 高 | 3 | Q1, Q2 (Q6 部分修复降级), N1 |
| 🟡 中 | 9 | Q3, Q4, Q5, Q7, Q11, Q12, N2, N3, N4, N7 |
| 🟢 低 | 6 | Q8, Q9, Q10, N5, N6, N8 |

### 按修复状态

| 状态 | 数量 | 编号 |
|------|------|------|
| ❌ 未修复 | 12 | Q1, Q2, Q3, Q4, Q5, Q8, Q9, Q10, Q11, Q13, N7, N8 |
| 🔶 部分修复 | 1 | Q6 |
| ⚠️ 新增/升级 | 8 | Q7（风险升级）, Q12（规模扩大）, N1, N2, N3, N4, N5, N6 |

---

## 修复优先级建议

### S 级 — 紧急修复（并发安全 + 中断处理）

1. **N1 / Q7** — SHARED_CONTEXT.md 并发锁（`--parallel` 上线后已从潜在变为实际 bug）
2. **N4 / Q13** — KeyboardInterrupt 优雅关闭（并发执行后后果更严重）
3. **N7** — 并发+交互模式不兼容性检测（防止用户同时打开多个交互终端）
4. **Q1** — Plan 交互中 API 失败时提供降级路径

### A 级 — 安全 + 正确性

5. **Q6** — 完全消除 `shell=True` 降级路径，或添加命令白名单
6. **Q2** — 修复 `verify_subtask` 的配置键
7. **N2** — 分支命名加入 `sub_id` 确保唯一性
8. **Q3** — git merge 冲突检测与处理

### B 级 — 可靠性 + 可维护性

9. **Q5** — 路径替换使用正则边界匹配或占位符方案
10. **Q11** — 统一 git subprocess 错误检查
11. **N3** — 消除 `confirm_plan` 闭包依赖
12. **Q4** — 增强无头模式交互检测
13. **Q12** — 代码模块化拆分（~1400 行单文件）

### C 级 — 优化（可延后）

14. **N6** — stream-json 日志级别调优（`text_delta` 降为 debug）
15. **N5** — `degraded` 状态名改为 `no_changes` 等更准确的语义
16. **Q8** — 任务 ID 加入微秒/随机后缀
17. **Q9** — commit 类型支持英文关键词
18. **Q10** — 工具版本检测
19. **N8** — 目录文档递归读取
