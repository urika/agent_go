# 测试覆盖补缺批次总结（2026-08-08）

> 范围：测试覆盖分析 + 逐项补测。起点是 S12/模型评价 gap 修复后的"代码已有但无测试"与"KPI 不可测"两类缺口。
> 任务清单：#7–#17（P0×4 / P1×4 / P2×2 / TD×1）全部闭环。
> 验证：全量测试 **1671 → 1826 passed**；改动源文件零新增 ruff 错误；关键测试有鉴别力（改代码即 FAIL）。

## 一、新增测试（~60 个，12 个测试文件）

| 类别 | 测试 | 验证契约 |
|------|------|---------|
| P0 正确性 | L2 成本熔断写路径、L3 读路径不重试、`--yes`+L1 信任、planner_api 隔离、cmd_pr gh body | 预算/信任/路由/交付内容 |
| S12 深度 | `_parse_cpu_time` macOS/Linux 回归、degrade 降档、budget_mode=ignore、over_budget_l3/hard_timeout 分类 | CR-H2 回归、三态开关、kill_reason 归因 |
| KPI 可信 | 任务级 $/pass（all-or-nothing）、K12 成功率、K13 完整率、low_confidence | 北极星/PRD KPI 可测 |
| 基础设施 | verify_state resume 续跑、线程竞态 per-subtask 持久化、kill_state 落盘顺序、checkpoint 整模块（17） | 崩溃续跑/并发/顺序/回滚 |
| TD | Spec budget 解析、STUCK_GRACE env 覆盖 | 新功能参数化 |

## 二、实现的缺失功能（5 项，均带测试）

1. **low_confidence**（PRD"样本<5 不决策"）：`analyze` 标 flag + `cmd_recommend` 排除小样本噪声。
2. **任务级 $/pass**：`_task_delivered`（全部子任务通过才算 1 交付）+ `task_level_dollar_per_pass`（证明 legacy 分母低估）。
3. **K12** `compute_mcp_tool_success_rate`（排除用户配置错误，≥95% 阈值）。
4. **K13 产物完整率**：`export` 加 `completeness/missing`，summary 明示不完整（反静默跳过）。
5. **Spec budget 字段**：`TaskSpec.budget` + 解析 + 模板 + cmd_run 注入（CLI `--budget` 优先）。

## 三、修复 / 改进

- **checkpoint bug**：`take()` 无匹配文件时 `snap_dir.rmdir()` 对非空目录抛 OSError（此前零测试潜伏）→ `shutil.rmtree(ignore_errors=True)`，优雅返回 None。
- **cmd_run console 泄漏**（测试侧）：调 cmd_run 泄漏 quiet console 到模块全局，致全量套件 45 个 test_review 集体失败 → try/finally 恢复；存入记忆 `feedback-cmdrun-console-leak.md`。
- **`_parse_cpu_time` 提升模块级**：解锁 CR-H2 单测。

## 四、测试更新（策略对齐）

- **L1 默认开关**：config.py 2026-08-08 有意改 `l1_enabled: False`（Claude CLI 2.1.224 预算语义"接近上限即拒绝"致任务无法启动）→ 更新 2 个过期测试 + 补显式开启测试。

## 五、遗留债 / 待办

- **口径重叠**：metrics.py 已有 `compute_frozen_metrics`（M0-6、`cost_per_accepted_delivery`）——与 `task_level_dollar_per_pass` 语义重叠，建议对齐或去重。
- **K12 记录 hook 未做**：`compute_mcp_tool_success_rate` 是纯聚合，工具派发点需接事件记录才能端到端可测。
- **cmd_pr** 只补了 body 内容（argv 已有测试）；`on_exceed` 死配置仅加注释未删。
