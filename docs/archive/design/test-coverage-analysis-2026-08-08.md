# 测试覆盖分析（对照 PRD + 设计文档）

> 日期：2026-08-08
> 视角：测试工程师，对照 `docs/prd.md` + `docs/design/` 评估覆盖、补充缺失
> 基线：1761 测试通过；本次新增 22 个覆盖测试

## 方法论

两条线交叉比对：
- **PRD 线**：逐条 KPI（K1–K13）/ P0-P1 功能 / NFR → 查 `tests/` 是否有对应测试。
- **设计文档线**：`timeout-kill-strategy`（G1–G8）、`model-eval-routing-fixes`（G6/G1/G2/G4/G5/G3）、`model-eval-routing-mechanism` → 逐机制查覆盖。

分类：**COVERED / PARTIAL / MISSING**。区分 _可直接补测试_（代码已有）与 _需先实现再测_（代码缺失）。

## 覆盖良好（确认无需补）

成本控制 L1/L2/L3 强制（test_cost_control）、熔断器 + fallback_reason、K8 zero-retry 数学、bench v2 schema、安全验证命令白名单、cmd_review --deep、M3 质量看板、3 通道通知、SSE、Bearer 鉴权、cross_judge 自评阻断、plan cache、agent_loop、--profile、compute_plan_diff、L1.5 AST 冲突、LLM evaluator、MCP 消费端故障隔离、recover 状态分类、级联阻断（M6）、保留 worktree。

## 本次已补充（22 个测试，均已通过）

| # | 缺口 | 测试 | 文件 |
|---|------|------|------|
| 1 | `_parse_cpu_time` S3 macOS/Linux 解析无回归守护（CR-H2） | 提升到模块级 + 4 单测 | test_subtask.py |
| 2 | L3 读路径 `over_budget_l3` 不重试（G8，仅 L2 被测） | `test_over_budget_l3_skips_fix` | test_executor.py |
| 3 | degrade 模式降档本身未测（只测了安全阀/max_retries） | `test_degrade_mode_downgrades_model` | test_executor.py |
| 4 | L1 默认预算值未 pin 到 PRD 冷启动契约（防静默误杀） | `TestCostControlDefaults`（3 测试） | test_config.py |
| 5 | `budget_mode=ignore` 三态分支未测 | `TestBudgetModeIgnore` | test_cost_control.py |
| 6 | G3 Spec `task_type` override 优先于关键词检测 | 2 测试（override 胜出 / 无匹配=None） | test_plan_to_subtasks.py |
| 7 | `_collect_result` over_budget_l3 分类分支未测 | `test_kill_reason_runtime_over_budget_l3` | test_bench.py |
| 8 | `hard_timeout` kill_reason 值未断言 | `test_kill_reason_runtime_hard_timeout` | test_bench.py |

> 顺带重构：`_parse_cpu_time` 从 `_run_headless` 闭包提升到模块级（无闭包依赖），解锁单测——也修了它原本不可单测的可测性阻塞。

## 剩余缺口（按优先级，建议后续）

### P0 — 正确性关键，代码已有可直接测

| 缺口 | 说明 | 建议测试 |
|------|------|---------|
| **L2 成本熔断写路径** | executor 设 `kill_reason=over_budget_l2` 的分支（_verify_changes 累计 cost≥limit）无测试；只测了读路径。回归 = 预算控制静默失效 | drive `_verify_changes` 真实 metering 超限 → 断言 kill_reason=over_budget_l2 且不重试 |
| **`--yes` 仍跑 L1 准入（headless 信任契约）** | cli.py:525 实现但无集成测试。回归 = 畸形 Spec 在 CI 静默通过 | cmd_run + 缺 §5 的 Spec + --yes → 断言非零退出、不调 generate_plan |
| **planner_api 隔离** | api.py:25 override 实现但零测试。回归 = planner 流量走 worker proxy、成本膨胀 | generate_plan + planner_api 配置 → 断言请求 URL/model 用 planner_api |
| **cmd_pr gh CLI 调用** | PR 创建（最显眼交付物）零 mock 验证 title/body/branch | mock subprocess.run → 断言 `gh pr create` argv + body 含看板 |

### P1 — 需先实现再测（代码缺失，KPI 不可测）

| 缺口 | PRD 来源 | 说明 |
|------|---------|------|
| **low_confidence（样本<5 不决策）** | §模型分级策略铁律 | `low_confidence` 在 bench/eval/cross_judge 全无实现 → 小样本噪声进自动路由 |
| **任务级 $/pass 分母** | §分母概念缺陷 | 当前 sum(cost)/sum(pass_rate) 偏乐观；任务级成功计数未实现 → K4 不可有效度量 |
| **K12 MCP 工具成功率** | §KPI 表 | 指标聚合未实现 |
| **K13 产物完整率 100%** | §KPI 表 | 缺"声明产物缺失即报不全"的契约测试（现状是静默跳过） |

### P2 — 大件/基础设施

| 缺口 | 说明 |
|------|------|
| **checkpoint.py 零测试** | 整个 SnapshotManager（8 函数 + CLI）无测试；被 executor 回滚 + SIGINT handler 使用，bug = 中断/回滚静默丢数据。需新建 test_checkpoint.py |
| **verify_state.json resume 续跑** | 只测了坏 JSON 容错；未测"崩溃在第 2 次重试 → resume 从第 2 次续"的契约 |
| **线程安全竞态** | test_concurrent_with_mixed_results 不够强；未真正 race meta_lock（CLAUDE.md 头号风险模式） |
| **kill_state 落盘顺序** | 未断言 metering 写在 proc.kill() 之前（SIGKILL 丢事件风险） |

## 技术债（非缺口，建议清理）

- **`on_exceed` 是死配置**（config.py:90）：只在 YAML/文档出现，Python 无读者。真正开关是 `budget_mode`。建议删字段或补测试防漂移。
- **`stuck_confirmed` vs `stuck` 文档/代码分歧**：设计 §8 区分两值，代码只发 `"stuck"`。统一后再写测试。
- **Spec 预算字段未实现**：设计 G3 要求 budget 作为 Spec 字段，目前只有 CLI flag。
- **`STUCK_GRACE_SEC=120` 硬编码**：测试靠魔数时间步，建议参数化。

## 结论

最近交付（S12 + 模型评价 gap 修复）**广度覆盖强**；本次补的是**深度缺口**——成本控制关键写路径/读路径、degrade 实际降档、CR-H2 回归守护、PRD 默认值 pin、三态开关完整、G3 优先级链。剩余 P0 多为"代码已有、缺集成测试"（建议尽快补），P1 是"代码未实现导致 KPI 不可测"（需先实现），checkpoint 是最大的一块未测模块。
