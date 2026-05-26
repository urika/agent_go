# PRD: agent_go 项目评估体系设计

> 版本: v1.0
> 日期: 2026-05-26
> 作者: Product
> 状态: Draft

---

## 一、评估维度总览

从 6 个维度评估 agent_go 系统质量：

| 维度 | 评估问题 | 核心指标数 |
|------|---------|-----------|
| 执行质量 Quality | "任务完成得好不好？" | 8 |
| 成本效率 Cost | "花了多少钱？值不值？" | 7 |
| 可靠性 Reliability | "系统稳定吗？" | 8 |
| 性能 Performance | "快不快？瓶颈在哪？" | 8 |
| 用户体验 UX | "好不好用？" | 7 |
| 改进效果 Improvement | "改完了真的变好了吗？" | 6 |

---

## 二、执行质量（Quality）

| # | 指标 | 定义 | 数据来源 | 当前 |
|---|------|------|---------|------|
| Q1 | 任务成功率 | `completed / (completed + failed)` | meta.json.status | ✅ |
| Q2 | Subtask 成功率 | `completed_subtasks / total` | meta.results | ✅ |
| Q3 | 首次通过率 | 无需重试的比例 | result.retry_count | ❌ |
| Q4 | 验证通过率 | `verify_ok / has_changes` | result.verify_ok | ✅ |
| Q5 | 新文件遗漏率 | 有新文件但 no_changes 占比 | result.status + summary | ❌ |
| Q6 | 产物传递成功率 | 下游 merge 无冲突占比 | merge 日志 | ❌ |
| Q7 | 计划准确性 | 实际 files vs plan.files | plan.steps[].files vs result | ❌ |
| Q8 | 变更规模 | 每 subtask 文件数/行数 | result.summary 结构化 | ⚠️ |

**数据来源增强**：result.json 扩展 `change_stats`、`verification_results`、`plan_accuracy` 字段。

**功能**: `agent_go eval quality [task-id|--all]` — 输出评分报告。

---

## 三、成本效率（Cost）

| # | 指标 | 定义 | 数据来源 | 当前 |
|---|------|------|---------|------|
| C1 | API 调用次数 | 会话/累计/按任务 | api_call event | ⚠️ 仅 log |
| C2 | Token 消耗 | prompt + completion tokens | API response usage | ❌ |
| C3 | 预估费用 | tokens × 模型单价 | 计算 | ❌ |
| C4 | 缓存命中率 | hits / (hits + calls) | plan_cache | ❌ |
| C5 | 缓存节省费用 | 命中次数 × 预估 API 费用 | 计算 | ❌ |
| C6 | 每任务成本 | 总费用 / 任务数 | 计算 | ❌ |
| C7 | 每 Subtask 成本 | 总费用 / subtask 总数 | 计算 | ❌ |

**数据来源增强**: `call_api()` 返回 token usage；新增 `session_stats.json`。

**模型单价**: claude-sonnet $3/$15 per 1M, gpt-4o $2.5/$10, deepseek-chat $0.27/$1.1。

**功能**: `agent_go eval cost` — 成本统计 + 节省建议。

---

## 四、可靠性（Reliability）

| # | 指标 | 定义 | 数据来源 | 当前 |
|---|------|------|---------|------|
| R1 | 中断恢复率 | 成功恢复 / 中断次数 | meta.status + resume | ❌ |
| R2 | 降级率 | fallback / (fallback + API) | plan_generate event | ✅ |
| R3 | API 错误率 | 4xx/5xx / 总调用 | api_call event | ❌ |
| R4 | 僵尸任务率 | zombie / 总运行任务 | cmd_status zombie | ⚠️ |
| R5 | Worktree 泄漏率 | 残留 worktree 数 | git worktree list | ❌ |
| R6 | Tag 泄漏率 | 残留 agent_go tag 数 | git tag -l agent_go/* | ❌ |
| R7 | Greywall 使用率 | greywall / (greywall + native) | sandbox_type | ✅ |
| R8 | 重试率 | 触发重试的 subtask 占比 | result.retry_count | ⚠️ |

**数据来源增强**: `session_stats.json` 记录可靠性事件。

**功能**: `agent_go eval reliability` — 健康检查报告。

---

## 五、性能（Performance）

| # | 指标 | 定义 | 数据来源 | 当前 |
|---|------|------|---------|------|
| P1 | 端到端耗时 | Plan 开始 → 最后完成 | meta + log 计算 | ⚠️ |
| P2 | Plan 阶段耗时 | generate_plan 耗时 | plan_generate event | ❌ |
| P3 | Subtask 平均耗时 | 所有 subtask duration_sec 均值 | result.duration_sec | ✅ |
| P4 | Subtask 耗时分布 | P50/P95/P99 | result 聚合 | ❌ |
| P5 | 各阶段占比 | worktree/claude/verify/commit | 细粒度计时 | ❌ |
| P6 | 并发效率 | 实际耗时 / 串行预估 | 计算 | ❌ |
| P7 | Claude 思考时间 | 首次 tool call 前耗时 | tool_calls 分析 | ❌ |
| P8 | 等待时间 | Wave 间空闲 | 计算 | ❌ |

**数据来源增强**: result.json 扩展 `timing` 字段（各阶段 ms）。

**功能**: `agent_go eval perf [task-id|--all]` — 耗时分析 + 瓶颈定位。

---

## 六、用户体验（UX）

| # | 指标 | 定义 | 数据来源 | 当前 |
|---|------|------|---------|------|
| U1 | Plan 迭代次数 | 确认前重新生成次数均值 | plan_generate.iteration | ✅ |
| U2 | 默认同意使用率 | --yes 使用占比 | 参数统计 | ❌ |
| U3 | 人工编辑率 | Plan/Subtask 编辑操作占比 | user_plan_choice event | ⚠️ |
| U4 | 文档挂载率 | --docs 使用占比 | meta.reference_docs | ✅ |
| U5 | Skill 使用率 | 有 Skill 的 subtask 占比 | subtask.skills | ✅ |
| U6 | Agent 多样性 | 非 developer 角色占比 | result.agent_type | ✅ |
| U7 | 参数使用分布 | --parallel/--remote/--issue 频率 | meta | ❌ |

**功能**: `agent_go eval ux` — 使用习惯分析 + 优化建议。

---

## 七、改进效果（Improvement）

| # | 指标 | 对比方式 |
|---|------|---------|
| I1 | 成功率变化 | vA vs vB |
| I2 | 耗时变化 | 平均 subtask 耗时前后对比 |
| I3 | 成本变化 | 每任务平均费用 |
| I4 | Skill 命中率变化 | role_skill_map 引入前后 |
| I5 | 新文件遗漏率变化 | P0 修复前后 |
| I6 | 中断恢复率变化 | P1 修复前后 |

**功能**: `agent_go eval compare v0.3 v0.4` — 版本对比雷达图（文本） + 综合评分。

---

## 八、技术方案

### 8.1 新增模块

| 文件 | 功能 |
|------|------|
| `agent_go/metrics.py` | **新增** — 指标采集（executor/pipeline 调用） |
| `agent_go/session_stats.py` | **新增** — 会话统计收集与持久化 |
| `agent_go/eval.py` | **新增** — 评估命令入口 + 指标计算 |

### 8.2 CLI 命令

```bash
agent_go eval quality [task-id|--all]
agent_go eval cost
agent_go eval reliability
agent_go eval perf [task-id|--all]
agent_go eval ux
agent_go eval compare <v1> <v2>
agent_go eval all
```

### 8.3 配置

```json
{
  "eval": {
    "enabled": true,
    "session_stats_retention_days": 30,
    "cost_model_prices": {
      "claude-sonnet-4-20250514": {"prompt": 3.0, "completion": 15.0}
    }
  }
}
```

---

## 九、实施优先级

| 阶段 | 内容 | 新增 | 工作量 |
|------|------|------|--------|
| **Phase 1** | 质量 + 性能指标采集 + `eval quality/perf` | metrics.py, eval.py | ~1 天 |
| **Phase 2** | 成本 + 可靠性 + session_stats | session_stats.py | ~1 天 |
| **Phase 3** | UX + 改进对比 + `eval compare` | eval.py 扩展 | ~0.5 天 |

**Phase 1 最小可行**：result.json 扩展 `timing`/`change_stats` + 两个 eval 命令。

---

*文档结束*
