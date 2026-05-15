# agent_go 代码审查 — 发现的问题清单

> 日期: 2026-05-15  
> 审查范围: `agent_go.py`（约 1250 行）+ 4 份设计/需求文档  
> 审查方法: 深度代码阅读 + 架构分析 + 边界情况推演

---

## 问题总览

| 编号 | 类别 | 严重程度 | 标题 |
|------|------|----------|------|
| [Q1](#q1-plan-mode-交互中降级路径缺失) | 流程缺陷 | 🔴 高 | Plan Mode 交互中降级路径缺失 |
| [Q2](#q2-verify_subtask-自动确认配置错误) | Bug | 🔴 高 | `verify_subtask` 自动确认配置错误 |
| [Q3](#q3-git-merge-冲突处理缺失) | 流程缺陷 | 🟡 中 | `git merge` 冲突处理缺失 |
| [Q4](#q4-无头模式交互检测不够鲁棒) | 可靠性 | 🟡 中 | 无头模式交互检测不够鲁棒 |
| [Q5](#q5-tasksmd-路径替换存在误匹配风险) | Bug | 🟡 中 | TASK.md 路径替换存在误匹配风险 |
| [Q6](#q6-shelltrue-安全风险) | 安全 | 🔴 高 | `shell=True` 安全风险 |
| [Q7](#q7-shared_contextmd-并发写入不安全) | 并发安全 | 🟡 中 | SHARED_CONTEXT.md 并发写入不安全 |
| [Q8](#q8-任务-id-时间戳可能碰撞) | 可靠性 | 🟢 低 | 任务 ID 时间戳可能碰撞 |
| [Q9](#q9-commit-消息类型判断仅支持中文) | 功能缺陷 | 🟢 低 | commit 消息类型判断仅支持中文 |
| [Q10](#q10-greywall--claude-code-版本无检测) | 可靠性 | 🟢 低 | Greywall / Claude Code 版本无检测 |
| [Q11](#q11-subprocessrun-普遍未检查返回码) | 可靠性 | 🟡 中 | `subprocess.run` 普遍未检查返回码 |
| [Q12](#q12-单文件规模接近可维护性上限) | 可维护性 | 🟡 中 | 单文件规模接近可维护性上限 |
| [Q13](#q13-无键盘中断处理) | 可靠性 | 🟢 低 | 无 KeyboardInterrupt 处理 |

---

## 问题详情

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

**严重程度**: 🔴 高  
**位置**: `run_subtask()` 中验证执行（约第 910 行）

**问题描述**:  
```python
vr = subprocess.run(verification, shell=True, cwd=str(worktree),
                    capture_output=True, text=True, timeout=120)
```

`verification` 字段来自外部 LLM API 的 JSON 响应。在以下场景中存在命令注入风险：
1. LLM API 被中间人攻击
2. 使用不受信任的自定义 API 端点
3. 本地模型输出被篡改
4. Plan 被用户 S/E 键编辑时插入了恶意命令

**攻击示例**: 如果 verification 字段为 `echo ok && rm -rf /`，shell=True 会执行整个命令。

**建议修复**:
1. **去掉 `shell=True`**，使用 `shlex.split(verification)` 解析命令
2. 如果验证命令需要 shell 特性（管道、重定向等），在执行前打印完整命令让用户确认
3. 维护一个安全命令白名单（如 `go test`, `npm test`, `pytest` 等常见验证命令）
4. 至少禁止包含 `&&`、`;`、`|`、反引号等 shell 元字符的验证命令

---

### Q7: SHARED_CONTEXT.md 并发写入不安全

**严重程度**: 🟡 中  
**位置**: `run_subtask()` 中共享上下文写入（约第 920-930 行）

**问题描述**:  
```python
existing = shared_ctx.read_text(encoding="utf-8") if shared_ctx.exists() else ""
shared_ctx.write_text(existing + "\n".join(ctx_parts) + "\n", encoding="utf-8")
```

这是典型的 TOCTOU（Time-of-check to time-of-use）竞态条件。当前串行执行安全，但 `REQUIREMENTS.md` 的 P2 已规划「多任务并行执行」。一旦并行化：
1. 子任务 A 读取文件
2. 子任务 B 读取文件
3. 子任务 B 写入
4. 子任务 A 写入 → **B 的上下文被覆盖丢失**

**建议修复**:
1. 使用文件锁（`fcntl.flock` 或 `portalocker`）
2. 每个子任务写独立的上下文文件（如 `SHARED_CONTEXT_sub-1.md`），最后汇总
3. 改用 SQLite 数据库（与 P3 的持久化层统一）
4. 追加写入模式（`open(a)`）+ 原子重命名

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

**严重程度**: 🟡 中  
**位置**: `agent_go.py` 整体

**问题描述**:  
当前 1250 行单文件已出现隐式耦合：
- `cmd_run()` 和 `run_subtask()` 通过 `worktree_map` 字典共享状态
- 日志/配置/API/执行/PR 生成等关注点混在一个文件
- 测试友好性差：所有函数依赖全局状态或执行环境

**建议修复**:
在 P1/P2 迭代中考虑拆分：
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

## 问题统计

| 严重程度 | 数量 | 编号 |
|----------|------|------|
| 🔴 高 | 3 | Q1, Q2, Q6 |
| 🟡 中 | 6 | Q3, Q4, Q5, Q7, Q11, Q12 |
| 🟢 低 | 4 | Q8, Q9, Q10, Q13 |

---

## 修复优先级建议

### 第一优先级（安全 + 正确性，应立即修复）

1. **Q6** — 去掉 `shell=True`，防止命令注入
2. **Q1** — Plan 交互中 API 失败时提供降级路径
3. **Q2** — 修复 `verify_subtask` 的配置键

### 第二优先级（可靠性，下个迭代修复）

4. **Q5** — 路径替换使用正则边界匹配
5. **Q3** — git merge 冲突检测与处理
6. **Q11** — 统一 git subprocess 错误处理
7. **Q4** — 增强无头模式交互检测

### 第三优先级（工程质量，P2 阶段修复）

8. **Q7** — 共享上下文并发安全
9. **Q12** — 代码模块化拆分
10. **Q13** — KeyboardInterrupt 清理

### 第四优先级（优化，可延后）

11. **Q8** — 任务 ID 防碰撞
12. **Q9** — commit 类型英文支持
13. **Q10** — 工具版本检测
