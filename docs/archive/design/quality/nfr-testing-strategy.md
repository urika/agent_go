# agent_go 非功能需求 (NFR) 测试策略分析

> 基于对 18 个源文件 (4980 行) 和 27 个测试文件 (6620 行) 的系统审查

---

## 1. 性能 (Performance)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 并发执行 | `pipeline.py:65-117` | `ThreadPoolExecutor` + 拓扑 Wave 调度，`--parallel N` 控制并发度 |
| 阶段耗时采集 | `metrics.py:10-18` | `collect_timing()` — 5 个阶段精确到 ms |
| Plan 缓存 | `api.py:286-340` | SHA256 去重，TTL 86400s，最大 100 条，避免重复 API 调用 |
| git gc.auto 禁用 | `pipeline.py:29-36` | 并发 worktree 操作时禁用，结束恢复，防止竞态 |
| 超时控制 | `api.py:45,269` / `subtask.py:230` | API 60s，本地模型 10s，headless 120s + 2 次重试 |
| 变更规模统计 | `metrics.py:21-58` | git diff --numstat，文件数/增删行数/new files |

### 已有测试

- `test_metrics.py` — `collect_timing` 精度/边界/舍入 (4 用例)
- `test_integration.py::TestConcurrentExecution` — 并发 vs 串行耗时对比 (2 用例)
- `test_eval.py::TestAnalyzePerformance` — P1-P6 指标 + 百分位计算 (4 用例)

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无并发压力测试** | 高 | 没有测试 `--parallel 8` + 100 subtask 场景下 `ThreadPoolExecutor` 的正确性 |
| **无 gc.auto 竞态测试** | 高 | 未验证并发 worktree 下禁用 gc.auto 的实际效果 |
| **无真实超时行为测试** | 中 | `subprocess.run(timeout=120)` 超时后的子进程清理未覆盖 |
| **无 Plan 缓存命中率测试** | 中 | 相同 task+repo 二次调用是否命中缓存未验证 |
| **无大文件截断性能测试** | 低 | `read_reference_docs` 中 15000 字符截断的性能特征 |

### 测试策略

```python
# 1. 并发正确性：多线程同时执行不产生死锁/数据竞争
class TestConcurrencyCorrectness:
    def test_no_race_on_worktree_map(self): ...
    def test_all_subtasks_get_unique_worktrees(self): ...
    def test_meta_lock_prevents_corruption(self): ...

# 2. gc.auto 生命周期：禁用→执行→恢复 的完整性
class TestGcAutoLifecycle:
    def test_gc_disabled_before_concurrent_worktree_ops(self): ...
    def test_gc_restored_after_completion(self): ...
    def test_gc_restored_after_interrupt(self): ...

# 3. 超时处理
class TestTimeoutBehavior:
    def test_api_timeout_triggers_fallback(self): ...
    def test_headless_timeout_triggers_retry_with_urgency_prompt(self): ...
    def test_orphan_process_cleanup_after_timeout(self): ...

# 4. Plan 缓存
class TestPlanCachePerformance:
    def test_identical_task_hits_cache(self): ...
    def test_cache_bypasses_api_call(self): ...
    def test_cache_ttl_expiry(self): ...
```

---

## 2. 可靠性 (Reliability)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 中断/恢复 | `pipeline.py:38-134` | SIGINT/SIGTERM 注册 → 标记 `_interrupted` → kill 子进程 → 保存 meta → 恢复 gc.auto → sys.exit(0) |
| 失败重试 | `subtask.py:241-260` | headless 超时最多重试 2 次，注入 `RETRY_SUFFIX` 催促指令 |
| 三层降级 | `api.py:253-283` | 外部 API → 本地模型 (localhost:8000) → `DECOMPOSE_RULES` 规则兜底 |
| 验证重试 | `executor.py:276-345` | 验证失败自动重试后仍失败则标记 failed |
| worktree 降级 | `executor.py:94-99` | `worktree add` 失败→回退到 `git clone` + `git checkout -b` |
| 并发异常隔离 | `pipeline.py:101-106` | 单个 subtask 异常不影响同 wave 其他任务 |
| 无依赖 = stdlib only | 全项目 | 消除外部依赖导致的安装失败风险 |

### 已有测试

- `test_integration.py::TestResume` — 中断恢复 + 幂等检测 (2 用例)
- `test_integration.py::TestFallback` — API 失败 → fallback 执行 (2 用例)
- `test_subtask.py` — `_run_headless` 超时/重试/交互检测 (6+ 用例)
- `test_pipeline.py` — SIGINT 信号处理 + 状态保存 (5+ 用例)

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无 worktree 降级测试** | 高 | `worktree add` 失败→`git clone` 路径未覆盖 |
| **无并发异常隔离测试** | 高 | 一个 subtask 抛异常时同 wave 其他任务是否正常完成 |
| **无规则兜底覆盖完整性测试** | 中 | `DECOMPOSE_RULES` 只覆盖 JWT/test 两种模式，其他任务类型降级质量未知 |
| **无多次中断恢复测试** | 中 | 中断→恢复→再中断→再恢复的循环 |
| **无磁盘满/权限不足场景** | 低 | meta.json/result.json 写入失败时的行为 |

### 测试策略

```python
# 1. worktree 降级
class TestWorktreeDegradation:
    def test_git_worktree_add_failure_falls_back_to_clone(self): ...
    def test_clone_fallback_preserves_branch_naming(self): ...
    def test_both_git_and_fallback_fail_reports_error(self): ...

# 2. 并发异常隔离
class TestConcurrentFaultIsolation:
    def test_one_subtask_exception_does_not_block_others(self): ...
    def test_all_subtasks_fail_results_in_correct_meta_status(self): ...

# 3. 降级覆盖
class TestFallbackCoverage:
    def test_every_decompose_rule_produces_valid_subtasks(self): ...
    def test_unknown_task_pattern_returns_single_fallback_subtask(self): ...
    def test_local_model_timeout_falls_back_to_rules(self): ...

# 4. 循环中断恢复
class TestInterruptResumeCycle:
    def test_double_interrupt_and_resume_preserves_state(self): ...
    def test_resume_with_missing_worktree_skips_cleaned_subtasks(self): ...
```

---

## 3. 安全性 (Security)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 验证命令白名单 | `utils.py:44-118,145-257` | 4 阶段验证：shlex 解析→shell 注入扫描→命令查找→逐 token 正则校验 |
| shell 注入防御 | `utils.py:138-143` | 6 种模式检测：命令链、命令替换、管道执行、危险删除、输出重定向、输入重定向 |
| 审计日志 | `utils.py:260-289` | `_log_rejected_command` → WARNING + JSON event + `verification_audit.jsonl` |
| 沙箱环境 | `executor.py:64-74` | `_build_sandbox_env` — 剔除敏感环境变量 (KEY/SECRET/TOKEN/PASSWORD) + 强制删除 `AGENT_GO_API_KEY` |
| 路径穿越防御 | `utils.py:12` | `read_reference_docs` — `startswith(repo.resolve())` 检查 |
| worktree 隔离 | `git_utils.py:41-49` | 每个 subtask 在独立 worktree + 分支执行，tag 命名空间隔离 |
| 配置权限 | `config.py:93` | `os.chmod(..., 0o600)` 配置文件仅 owner 可读写 |
| 锁文件机制 | `utils.py:292-310` | `_safe_append_to_file` — 原子锁文件 + 指数退避 + 原子追加写入 |

### 已有测试

- `test_safe_verification_command.py` — 4 阶段验证 + shell 注入防御 + 参数白名单 (30+ 用例)
- `test_is_safe_verification_command.py` — 额外 shell 注入模式 (10+ 用例)
- `test_p0_p1_fixes.py::TestSandboxEnvApiKey` — AGENT_GO_API_KEY 剔除 (2 用例)
- `test_read_reference_docs.py` — 路径穿越防御 (1 用例)
- `test_safe_append_to_file.py` — 锁文件并发写入 (若干用例)

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无完整审计日志链路测试** | 高 | 从 `_log_rejected_command` → `verification_audit.jsonl` 的端到端 |
| **无沙箱环境完整性测试** | 高 | 多种敏感变量同时存在时的剔除行为 (CI/CD 环境中 GITHUB_TOKEN 等) |
| **无 worktree 隔离逃逸测试** | 中 | subtask 是否可以访问/修改其他 worktree 的文件 |
| **无配置注入测试** | 中 | 恶意构造的 config.json 是否会导致意外行为 |
| **无 resource limit 测试** | 低 | `_apply_resource_limits` (setrlimit) 是否真的生效 |

### 测试策略

```python
# 1. 审计日志完整性
class TestAuditTrail:
    def test_rejected_command_logged_to_jsonl(self): ...
    def test_audit_file_contains_timestamp_command_reason(self): ...
    def test_audit_writes_persist_across_multiple_rejections(self): ...

# 2. 沙箱环境
class TestSandboxEnvironment:
    def test_ci_environment_variables_stripped(self): ...
    def test_agent_go_api_key_always_removed_regardless_of_prefix_rules(self): ...
    def test_non_sensitive_agent_go_vars_preserved(self): ...

# 3. worktree 隔离
class TestWorktreeIsolation:
    def test_subtask_cannot_access_other_worktree_via_filesystem(self): ...
    def test_subtask_cannot_modify_parent_repo_directly(self): ...
    def test_tag_namespace_prevents_cross_task_collisions(self): ...

# 4. CMD_ARG_RULES 完整性
class TestCmdArgRulesIntegrity:
    def test_every_rule_has_flags_and_positionals_regex(self): ...
    def test_rules_cover_all_supported_languages_and_tools(self): ...
    def test_no_ambiguous_or_too_permissive_rules(self): ...
```

---

## 4. 可用性 (Usability)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 交互式确认 | `ui.py` | `confirm_plan` (Y/S/D/E/R/N)、`confirm_subtasks` (Y/N/E/A/D)、`verify_subtask` |
| 无头模式 | `cli.py` | `--yes` 跳过所有交互，`safe_input` EOF 返回空字符串 |
| Console 抽象 | `console.py` | `--quiet` 抑制输出 / `--verbose` 调试 / semantic methods / table/data |
| 任务状态 | `cli.py` | `cmd_status --watch` 实时监控 + `cmd_list` / `cmd_show` |
| 帮助信息 | `cli.py` | `_build_parser` — argparse 全子命令 + epilog |
| TUI 仪表盘 | `tui.py` | curses 实时状态面板 (199 行) |

### 已有测试

- `test_console.py` — 全语义方法 + 模式切换 + 结构化输出 (24 用例)
- `test_ui.py` — `confirm_plan` / `confirm_subtasks` / `verify_subtask` (若干用例)
- `test_cli.py` — CLI parser 解析验证 (若干用例)
- `test_p0_p1_fixes.py::TestCmdEvalSignature` — cmd_xxx 签名一致性 (2 用例)
- `test_config_helpers.py::TestSafeInput` — EOFError/正常输入 (4 用例)

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无 TUI 集成测试** | 高 | `tui.py` 只在 `test_tui.py` 中有基础 import 测试，curses 渲染逻辑完全未覆盖 |
| **无 --yes 模式完整路径测试** | 中 | 自动确认+并行的端到端行为 |
| **无错误消息可读性测试** | 中 | 各种异常场景下的用户提示是否清晰 |
| **无 CLI 参数组合测试** | 低 | `--parallel 3 --remote origin --skill xxx --agent-type yyy` 组合 |

### 测试策略

```python
# 1. TUI
class TestTuiDashboard:
    def test_task_list_renders_correctly(self): ...
    def test_watch_mode_refreshes_periodically(self): ...
    def test_no_tasks_shows_empty_state(self): ...

# 2. 无头模式
class TestHeadlessEndToEnd:
    def test_yes_flag_skips_all_interactive_prompts(self): ...
    def test_yes_with_parallel_3_completes_without_hanging(self): ...

# 3. 错误消息
class TestErrorMessageQuality:
    def test_missing_api_key_shows_actionable_message(self): ...
    def test_invalid_repo_path_shows_help(self): ...
    def test_failed_subtask_report_is_human_readable(self): ...
```

---

## 5. 可观测性 (Observability)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 双格式日志 | `config.py:99-116` | INFO=人类可读 + DEBUG=结构化 JSON (`log_event`) |
| 阶段耗时 | `metrics.py:10-18` | 5 阶段到 ms: worktree_create / merge_upstream / claude_execute / verification / git_commit |
| 变更统计 | `metrics.py:21-58` | 文件数 / 增删行数 / new files / modified files |
| 产物传递 | `metrics.py:62-66` | merge 成功/冲突 + 冲突文件列表 |
| API 用量 | `metrics.py:69-76` | prompt/completion tokens + model + provider |
| 分析引擎 | `eval.py` | quality (Q1-Q8) / performance (P1-P6) / cost / reliability / UX |
| 审计日志 | `utils.py:278-289` | `verification_audit.jsonl` 持久化 |
| subtask result | `executor.py:484-496` | 每个 subtask 输出完整 result dict + per-file result.json |

### 已有测试

- `test_metrics.py` — 4 类采集函数全覆盖 (10 用例)
- `test_eval.py` — 5 种分析引擎覆盖 (20+ 用例)
- `test_cmd_eval.py` — cmd_eval 路由 + helper 函数 (38 用例)
- `test_config_helpers.py::TestLogEvent` — 结构化事件日志 (5 用例)

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无日志格式一致性测试** | 高 | log_event 的 JSON 能否被 eval.py `_read_log_events` 正确解析 |
| **无 metrics 端到端测试** | 高 | 从一个完整 run_subtask 返回中提取所有 metrics 字段的完整性 |
| **无 eval 跨任务聚合正确性测试** | 中 | 多个任务的 aggregate 数据是否真实反映源数据 |
| **无执行日志完整性测试** | 中 | execution.log 是否包含所有关键阶段的事件 |

### 测试策略

```python
# 1. 日志-分析闭环
class TestLogToEvalRoundtrip:
    def test_log_event_json_matches_read_log_events_pattern(self): ...
    def test_all_required_event_types_in_execution_log(self): ...

# 2. Metrics 完整性
class TestMetricsCompleteness:
    def test_run_subtask_result_contains_all_metrics_fields(self): ...
    def test_timing_sum_equals_subtask_duration(self): ...
    def test_api_usage_logged_for_every_plan_call(self): ...

# 3. 聚合一致性
class TestAggregationConsistency:
    def test_quality_aggregate_averages_match_individual_reports(self): ...
    def test_cost_aggregate_sums_match_individual_calls(self): ...
```

---

## 6. 可移植性 (Portability)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 零外部依赖 | `pyproject.toml` | 纯 Python stdlib (`urllib`, `subprocess`, `json`, `logging`, `pathlib`) |
| 跨 shell | `utils.py:161` | `shlex.split` 而非 `shell=True` |
| 多 API 提供商 | `api.py:20-50` | Anthropic / OpenAI / DeepSeek / custom — 统一 `call_api` 抽象 |
| 跨平台 git | `git_utils.py` | `subprocess.run(["git", ...])` 而非 shell 脚本 |

### 已有测试

- `test_p0_p1_fixes.py::TestImportSmoke` — 包导入冒烟 + 全模块可独立导入 (3 用例)
- `test_api.py` — 多 provider API 调用测试 (约 40 用例)
- `test_format_commit.py` — 中英文关键词兼容 (20+ 用例)

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无多 Python 版本测试** | 高 | Python 3.10/3.11/3.12 兼容性未验证 |
| **无多 OS 测试** | 高 | Linux/macOS 差异 (如 `/tmp` vs `/private/tmp`, `resource` 模块) |
| **无 git 版本兼容性测试** | 中 | `git worktree add -b` 在老版本 git (< 2.5) 的行为 |
| **无多 provider 响应格式兼容性测试** | 中 | OpenAI/DeepSeek 的 usage 字段格式差异 |

### 测试策略

```python
# 1. Python 版本
# 通过 CI matrix: python-version: ["3.10", "3.11", "3.12"]

# 2. 平台兼容
class TestPlatformCompatibility:
    def test_resource_limit_graceful_fallback_on_macos(self): ...
    def test_tmp_path_resolves_correctly(self): ...

# 3. 提供商兼容
class TestProviderResponseParsing:
    def test_openai_usage_format(self): ...
    def test_deepseek_usage_format(self): ...
    def test_anthropic_usage_format(self): ...
```

---

## 7. 可伸缩性 (Scalability)

### 需求描述

| 维度 | 代码位置 | 机制 |
|------|----------|------|
| 并行上限 | `pipeline.py:65` | `--parallel N` 控制 ThreadPoolExecutor max_workers |
| Prompt 截断 | `api.py:207-214` | system MAX_SYSTEM_PROMPT_CHARS (48k), user MAX_USER_CONTENT_CHARS (24k) |
| Plan 缓存限制 | `api.py:286-340` | `max_entries=100`, `plan_ttl=86400` |
| 文件截断 | `utils.py:21-23` | 文档读取 15000 字符上限 |

### 测试缺口

| 缺口 | 严重度 | 说明 |
|------|--------|------|
| **无大量 subtask 场景测试** | 高 | 100 个 subtask + 依赖图深度 10 的调度正确性 |
| **无 Prompt 超限场景测试** | 中 | 超长 reference docs + skill 注入导致 prompt 截断时的行为 |
| **无缓存驱逐测试** | 中 | `max_entries=100` 满后的 LRU 行为 |

### 测试策略

```python
class TestLargeScaleBehavior:
    def test_deep_dependency_chain_schedules_correctly(self): ...
    def test_wide_fan_out_wave_schedules_concurrently(self): ...
    def test_prompt_truncation_preserves_essential_instructions(self): ...
    def test_cache_max_entries_enforced(self): ...
```

---

## 优先级排序

| 优先级 | NFR 维度 | 缺口项 | 影响 |
|--------|----------|--------|------|
| **P0** | 安全性 | 审计日志端到端 + 沙箱完整性 | 合规风险 |
| **P0** | 可靠性 | worktree 降级 + 并发异常隔离 | 生产稳定性 |
| **P1** | 性能 | 并发压力 + gc.auto 竞态 | 数据一致性 |
| **P1** | 可观测性 | 日志-分析闭环 + metrics 完整性 | 故障排查 |
| **P2** | 可用性 | TUI 测试 + 错误消息 | 用户体验 |
| **P2** | 可伸缩性 | 大规模场景 | 能力上限 |
| **P3** | 可移植性 | 多版本/多平台 CI | 覆盖面 |

---

## 实施建议

1. **CI 增强**：`.github/workflows/test.yml` 增加 Python 3.10/3.11/3.12 matrix + macOS runner
2. **安全测试独立套件**：增加 `tests/security/` 目录专门放置审计链路、沙箱完整性测试
3. **性能基准测试**：增加 `tests/benchmarks/` 使用 `pytest-benchmark` 记录并发执行基线
4. **混沌测试**：使用 `tox` 或自定义 harness 模拟 worktree 创建失败、磁盘满、网络超时
