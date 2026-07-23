# agent_go 第二次代码审查报告

> **审查日期**: 2026-07-19  
> **审查范围**: 18 个源文件（~4950 行）+ 19 个测试文件（278 条测试）  
> **版本**: v2.0.0  
> **审查方法**: 逐文件静态分析 + 对比上次审查修复状态  

---

## 📊 概要：上次修复验证

上次审查（2026-05-27, CODE_REVIEW.md）报告了 **3 个 CRITICAL + 3 个 HIGH + 6 个 MEDIUM + 4 个 LOW** 问题。

| ID | 问题 | 上次状态 | 本次验证 |
|----|------|---------|---------|
| C1 | `active_pids` 并发竞态 | ✅ 已修复 | ✅ `active_pids_lock` 已实现 |
| C2 | `shell=True` 降级路径 | ✅ 已修复 | ✅ `shell=True` 已移除 |
| H1 | `call_api()` 异常捕获不足 | ✅ 已修复 | ✅ HTTPError/URLError/JSONDecodeError/KeyError 全覆盖 |
| H2 | Git 操作返回码未强制 | ✅ 已修复 | ✅ `(bool, str)` 元组返回 |
| H3 | Plan Prompt 无 Token 预算 | ✅ 已修复 | ✅ system 6K / user 12K 字符上限 |
| M1 | `_safe_append_to_file` 非原子 | ⏳ 待修复 | ⚠️ 锁文件无 stale lock 检测 |
| M2 | CLI 参数解析脆弱 | ✅ 已修复 | ✅ 已迁移至 argparse |
| M3 | 信号处理器执行 I/O | ⏳ 待修复 | ❌ **仍然存在** - `_on_interrupt` 直接写 `meta.json` |
| M5 | `run_subtask()` 过长 | ✅ 已修复 | ✅ 已拆分为 6 个子函数 |
| L1 | 通篇重复 import | ⏳ 待修复 | ❌ 仍然存在 |
| L4 | 缺少 CI 配置 | ⏳ 待修复 | ✅ `.github/workflows/test.yml` 已添加 |

---

## 🔴 新发现——严重/高优先级问题

### N1. `workflow_gen.py:cmd_ci()` 签名不匹配 + 引用未定义变量

| 属性 | 值 |
|------|-----|
| **文件** | `agent_go/workflow_gen.py` + `agent_go/cli.py` |
| **类型** | **运行时崩溃** |

**bug 1 — 参数不匹配**：  
`workflow_gen.py` 中定义为 `def cmd_ci() -> None:`（无参数），但 `cli.py:main()` 中调用为：
```python
elif args.command == "ci":
    cmd_ci(args)                    # 传入了 args 参数！
```
Python 将抛出 `TypeError: cmd_ci() takes 0 positional arguments but 1 was given`。

**bug 2 — 引用未定义变量**：  
`workflow_gen.py:37-40`：
```python
def cmd_ci() -> None:
    import sys
    if args and hasattr(args, 'dry_run'):    # args 未定义！
```
即使修复了参数传递，`args` 在函数作用域中也不存在，会抛出 `NameError`。

**影响**：`agent_go ci` 命令完全不可用，任何调用都会崩溃。

**修复建议**：将签名改为 `def cmd_ci(args=None) -> None:`。

---

### N2. 信号处理器仍然直接执行 I/O —— `_on_interrupt`

| 属性 | 值 |
|------|-----|
| **文件** | `agent_go/pipeline.py:33-38` |
| **类型** | **POSIX 信号安全违规** |

```python
def _on_interrupt(signum, frame):
    meta["status"] = "paused"
    (task_dir / "meta.json").write_text(...)    # I/O 操作！
    ...
```

POSIX `signal.signal` 处理器中只允许调用 **async-signal-safe** 函数（如 `write()`、`_exit()`）。`json.dumps()` + `write_text()` 既非可重入，也可能在持有锁时被中断导致死锁。

**影响**：在信号密集场景（如并发 `os.kill`）下可能导致程序挂起或数据损坏。

**修复建议**：
```python
_interrupted = False

def _on_interrupt(signum, frame):
    global _interrupted
    _interrupted = True

# 主循环末尾检测标志
if _interrupted:
    meta["status"] = "paused"
    (task_dir / "meta.json").write_text(...)
    # ... 安全地 kill 子进程 ...
    sys.exit(0)
```

---

### N3. `executor.py` 中验证命令多次重复的 shlex.split/安全门禁逻辑

| 属性 | 值 |
|------|-----|
| **文件** | `agent_go/executor.py:200-280` |
| **类型** | **DRY 违规 / 维护风险** |

验证命令执行逻辑在主路径和重试路径中几乎完整重复了两遍（~80 行重复代码），包括：
- `shlex.split()` + try/except
- `_is_safe_verification_command()` 安全门禁
- `subprocess.run()` + capture_output
- `verification_results.append()` 构建

若后续需要修改验证行为（如超时调整、日志格式变更），必须在两处同步修改。

**修复建议**：将验证命令执行逻辑抽取为独立函数：
```python
def _run_verification_cmd(vcmd, worktree, attempt, env, logger):
    ...
```

---

### N4. `cli.py:cmd_show()` 中 `results` 索引越界风险

| 属性 | 值 |
|------|-----|
| **文件** | `agent_go/cli.py:459-463` |
| **类型** | **潜在 IndexError** |

```python
for i, st in enumerate(meta.get("subtasks", [])):
    r = meta["results"][i] if i < len(meta.get("results", [])) else None
```

如果 `meta["results"]` 不存在（`meta.get("results", [])` 但使用了 `meta["results"]`），会触发 `KeyError`。`meta.get("results", [])` 更安全。

---

### N5. 僵尸检测只改元数据，未实际清理进程

| 属性 | 值 |
|------|-----|
| **文件** | `agent_go/cli.py:634-637` |
| **类型** | **逻辑缺陷** |

```python
if status == "running" and log_path.exists():
    log_mtime = log_path.stat().st_mtime
    if time.time() - log_mtime > ZOMBIE_TIMEOUT:
        zombie = True
        meta["status"] = "failed"       # 只改 meta 状态
```

检测到僵尸进程后仅将 `meta.json` 中的状态改为 `failed`，但不会实际终止可能仍在运行的 `claude` 进程。残留进程可能持续消耗资源。

---

## 🟡 中优先级

### N6. `workflow_gen.py` 中 `cmd_ci` 的 `args` 引用与导入逻辑混乱

`cli.py` 通过 `from .workflow_gen import cmd_ci` 导入，但 `workflow_gen.py` 中 `cmd_ci()` 引用了未定义的 `args`。此外，`cmd_ci` 既可通过 `agent_go ci` 调用，也可被 `cli.py` 导入后用于 `ci` 子命令，但两者逻辑互斥。

---

### N7. `executor.py:_build_task_md` 中正则路径替换的 `_boundary_chars` 字面量定义在两处

`executor.py` 中 `_build_task_md()` 和 `run_subtask()` 都定义了一模一样的：
```python
_boundary_chars = r'\s"\'\(\):/：，。、'
```
不仅重复定义，而且如果修改一处忘了另一处，会导致路径替换行为不一致。

---

### N8. `pyproject.toml` 中 `target-version = "py39"` 但代码使用了 Python 3.9 之后特性？

检查 `str.removeprefix` / `str.removesuffix`（Python 3.9+ 有）、`dict` union operator（3.9+ 没有）、`match/case`（3.10+）等。当前代码看起来兼容 3.9，但需要确认 CI 测试的 Python 版本。

---

### N9. `config.py` 模块级副作用

```python
AGENT_GO_DIR = Path.home() / ".agent_go"
AGENT_GO_DIR.mkdir(exist_ok=True)    # import 时自动创建目录
```

任何 `import agent_go` 都会创建 `~/.agent_go` 目录。如果用户只是做非交互式引用（如类型检查、文档生成），这个副作用是不必要的。

---

## 🟢 低优先级

### N10. 测试断言强度不均衡

- `test_format_commit.py`（36 测试）覆盖非常细致
- `test_pipeline.py`（7 测试）仅覆盖了基础的信号和移除逻辑，没有并发/中断/恢复的测试
- `test_executor.py`（24 测试）主要是 mock 测试，缺乏对 `_verify_changes` 重试逻辑和冲突恢复的集成测试
- `test_tui.py`（13 测试）覆盖了基本 TUI 渲染，但 `_cmd_status_text` 的僵尸检测、verbose 模式等无测试

### N11. `agent_go/__init__.py` 过度导出

导出所有 CLI 命令函数和 `run_subtask`、`load_config` 等实现细节。这导致 `from agent_go import *` 暴露了过多的内部 API。建议只导出 `main`, `__version__` 等用户级接口。

### N12. 文档中测试数过时

`CLAUDE.md` 写着 "163 tests"，但实际已有 **278 条测试**（19 个文件）。需同步更新。

### N13. 版本号不一致

- `agent_go/__init__.py`: `__version__ = "2.0.0"`
- 上次 CODE_REVIEW.md 提到 "v0.9" 已修复
- 代码中没有 `VERSION` 或版本变更记录文件

---

## 📋 测试覆盖分析

### 充分覆盖 ✅
| 模块 | 测试数 | 覆盖内容 |
|------|--------|---------|
| `_is_safe_verification_command` | 63+18=81 | 白名单、shell 注入、边界条件 |
| `_format_commit` | 36 | 中英文前缀检测、scope 提取、issue 引用 |
| `_slugify` | 9 | Unicode、边界截断 |
| `role_skill_map` | 18 | 规则匹配、合并逻辑 |
| `skills` | 17 | 加载、解析、渲染、发现 |
| `plan_to_subtasks` | 12 | Plan→子任务转换、规则应用 |

### 薄弱或未覆盖 ⚠️
| 模块 | 覆盖情况 |
|------|---------|
| `workflow_gen.py` | `cmd_ci` 无测试，且存在运行时崩溃的 bug |
| `tui.py` | 仅测试渲染输出，未测试键盘交互、resize、filter 切换 |
| `pipeline.py` | 无并发/中断恢复/远程推送的集成测试 |
| `executor.py` | 无 `_verify_changes` 重试逻辑的端到端测试 |
| `subtask.py` | `_run_headless` 的 stream-json 解析、交互检测、超时重试无测试 |
| `cli.py` | `cmd_run`, `cmd_resume`, `cmd_review`, `cmd_cache` 等命令函数无单元测试 |

---

## 🔍 架构观察（新增）

### 优点
1. **对比上次审查，大部分严重问题已修复**，代码质量有显著提升
2. **argparse 迁移**使 CLI 更规范，参数解析不可靠的问题已解决
3. **`run_subtask()` 拆分为 6 个子函数**极大改善了可读性和可测试性
4. **`Console` 输出抽象层**贯穿全项目，quiet/verbose 模式贯穿实现了干净的输出控制
5. **`collect_timing/change_stats/merge_result`** 等指标采集函数为质量评估提供数据基础
6. **测试从 132 增长到 278**（+110%），安全门禁测试尤其充分（81 条）

### 需关注的设计决策

1. **`_safe_append_to_file` 的锁文件机制** —— 虽比无锁好，但缺乏 stale lock 检测 + 竞争窗口仍然存在。建议在并发场景中评估是否真正需要（当前所有调用点均为串行 write + append）
2. **`_run_headless` 内嵌两个读取线程 + 事件循环** —— 函数体约 150 行，复杂性已接近重构前的 `run_subtask`。建议将 stream-json 事件解析抽为独立模块
3. **`_verify_changes` 返回 dict**（约 15 个字段）—— 已接近"贫血 DTO"模式，考虑使用 `dataclass` 增强类型安全

---

## 🎯 修复优先级建议

| 优先级 | ID | 修复项 | 难度 | 影响 |
|--------|-----|--------|------|------|
| 🔴 P0 | N1 | `cmd_ci` 参数签名 + 未定义变量 | 低 | 功能完全不可用 |
| 🟠 P1 | N2 | 信号处理器 I/O → 延迟保存 | 中 | 潜在死锁/数据损坏 |
| 🟠 P1 | N3 | 验证命令执行逻辑去重 | 低 | 维护风险 |
| 🟡 P2 | N5 | 僵尸检测 → 实际终止进程 | 中 | 资源泄漏 |
| 🟡 P2 | N4 | `cmd_show` results 安全访问 | 低 | IndexError |
| 🟡 P2 | N7 | 重复正则常量去重 | 低 | 维护风险 |
| 🟢 P3 | N10 | 补充薄弱测试 | 中 | 测试充分性 |
| 🟢 P3 | N12 | 文档同步更新 | 低 | 误导 |

---

*报告结束 — 由 agent_go 项目第二次代码审查生成*
