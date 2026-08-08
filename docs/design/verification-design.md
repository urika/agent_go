# agent_go 验证设计

> 状态：当前 M2 可靠性基线
> 更新日期：2026-08-08

## 1. 验证层次

| 层次 | 目标 | 结果 |
|---|---|---|
| Shell | 命令、测试、退出码 | `verification_results` |
| Code Quality | lint/type/test regression | `lint_errors/tests_broken` |
| Semantic | Shell 无法覆盖的语义残差 | `semantic_pass` |
| Spec Compliance | 需求验收是否满足 | `spec_compliance` |
| Architecture Compliance | 是否违反架构约束 | `architecture_compliance` |
| Delivery | 是否形成可取得交付物 | `accepted_delivery` |

硬约束：Semantic、Spec 或 Architecture 审查不能绕过必要的 Shell 验证。

## 2. Retry 状态

每次 retry 应记录：

- attempt。
- failed commands。
- stdout/stderr tail。
- diff/stat 摘要。
- `diff_stat_hash`。
- `failure_pattern`。
- `effective_strategy`。
- `no_progress`。
- `failure_analysis`（如启用 Reflexion）。

## 3. 无进展控制

默认策略：

```text
连续两次 retry 没有实质 diff 变化
  -> 标记 no_progress
  -> 停止当前 retry loop
  -> failure_class=verification_failure
  -> 可选人工/局部重规划
```

无进展停止是止损机制，不代表任务一定不可修复。

## 4. 受控 Reflexion

- 仅在 retry 达到阈值后触发。
- 使用独立 evaluator 时优先不同 provider。
- 只生成失败分析和下一步策略建议。
- 不直接修改任务状态。
- 不绕过 Shell 验证。
- 受 token、次数和任务预算限制。
- evaluator 失败时降级为普通 repair。

## 5. 验证证据

每个验收标准应关联至少一种证据：

- 测试命令。
- lint/type 命令。
- 文件/符号检查。
- diff 片段。
- Spec Compliance Review 结果。
- Architecture Compliance Review 结果。
