# 产品功能价值优先级分析

> 版本: v1.0
> 日期: 2026-07-24
> 基准: [PRODUCT_VISION.md](PRODUCT_VISION.md) — 核心用户 + 一句话场景 + 一句话价值
> 方法: 遍历全部 18 个模块、80+ 功能点，逐一判定对核心承诺的贡献等级

---

## 一、判定框架

核心价值链条 (6 步):

```
① 用户说任务 → ② Plan(拆解) → ③ Execute(执行) → ④ Verify(验证) → ⑤ Commit → ⑥ PR
```

判定标准:

| 等级 | 定义 | 判定问题 |
|------|------|----------|
| **P0** | 核心链路 — 没有它，承诺崩塌 | 去掉这个功能，用户还能"说完就走、回来收 PR"吗？ |
| **P1** | 信任增强 — 让用户敢用、愿意用 | 去掉这个功能，用户还敢关掉终端吗？ |
| **P2** | 体验完善 — 让用户用得舒服 | 去掉这个功能，体验打折扣但不影响核心承诺 |
| **P3** | 偏离核心 — 做对了但不急 | 这个功能服务于核心场景吗？还是服务于另一个假设场景？ |

---

## 二、全功能优先级矩阵

### P0 — 核心链路（18 项）：没有这些，产品不成立

| # | 功能 | 模块 | 对应步骤 | 理由 |
|---|------|------|----------|------|
| 1 | `cmd_run` — 任务入口 | cli.py | ① | 用户启动任务的唯一方式 |
| 2 | `generate_plan` — LLM 生成执行方案 | api.py | ② | "说一遍任务"→Plan 的转换核心 |
| 3 | `decompose_fallback` — 三层降级 | api.py | ② | Plan API 不可用时的保障 |
| 4 | `plan_to_subtasks` — Plan→子任务转换 | ui.py | ② | Plan 到可执行单元的桥梁 |
| 5 | `_run_pipeline` — 拓扑调度引擎 | pipeline.py | ③ | 多子任务的有序执行 |
| 6 | `run_subtask` — 单子任务端到端 | executor.py | ③④⑤ | 每个子任务的生命周期 |
| 7 | `_run_headless` — Claude 无头调用 | subtask.py | ③ | "不需要你盯着屏幕"的关键实现 |
| 8 | `_git_merge_upstream` — 产物传递 | subtask.py | ③ | 子任务间代码传递，多步骤正确性的基础 |
| 9 | `_verify_changes` — 变更验证 | executor.py | ④ | "验证已通过"的直接实现 |
| 10 | `_run_verification_cmd` — 验证命令执行 | executor.py | ④ | 每个验证命令的实际运行 |
| 11 | `_format_commit` — Conventional Commits | utils.py | ⑤ | "代码已提交"的规范性 |
| 12 | `cmd_pr` — PR 生成 | cli.py | ⑥ | "PR 已生成"的直接实现 |
| 13 | `cmd_resume` — 中断恢复 | cli.py | ③ | "关掉终端"后进程可能被中断，不能恢复=承诺崩塌 |
| 14 | `_build_sandbox_env` — 沙箱环境净化 | executor.py | ③④ | 验证命令在净化环境中执行，安全零事故的基础 |
| 15 | `_is_safe_verification_command` — 命令白名单 | utils.py | ④ | LLM 生成的验证命令必须经白名单校验 |
| 16 | `_create_worktree` — 隔离环境创建 | executor.py | ③ | 每个子任务独立 worktree，不污染主仓库 |
| 17 | `load_config` + `get_api_key` — 配置与鉴权 | config.py | ①② | 没有 API Key 一切免谈 |
| 18 | `setup_logger` — 日志基础设施 | config.py | 全部 | "回来看结果"的前提是有东西可看 |

### P1 — 信任增强（17 项）：让用户"敢关终端"

| # | 功能 | 模块 | 理由 |
|---|------|------|------|
| 19 | `--yes` 无头模式 | cli.py | 核心场景的本质要求：不需要交互确认 |
| 20 | Plan 缓存 (`get_cache_key`/`load_cached_plan`) | api.py | 相同任务不走 API：省钱 + 加速 + 结果稳定 |
| 21 | `_log_rejected_command` → 审计 JSONL | utils.py | 谁在什么时候拒绝了什么命令，事后可查 |
| 22 | `collect_timing` — 阶段耗时采集 | metrics.py | "这个任务跑了多久"是可观测性的基础 |
| 23 | `collect_change_stats` — 变更规模统计 | metrics.py | "改了多少文件/行"是 PR review 的第一印象 |
| 24 | `collect_merge_result` — 产物传递结果 | metrics.py | 子任务间 merge 是否成功直接影响最终结果 |
| 25 | `extract_usage` — Token 用量提取 | metrics.py | "花了多少钱"的前提 |
| 26 | `analyze_cost` — 成本分析 | eval.py | 用户需要知道成本才能放心用 |
| 27 | 并发调度 (`ThreadPoolExecutor` + 拓扑 Wave) | pipeline.py | 多子任务并行为用户节省等待时间 |
| 28 | SIGINT 中断处理 (`_on_interrupt`) | pipeline.py | 用户 Ctrl+C 后状态保存到 meta.json |
| 29 | `read_reference_docs` — 参考文档注入 | utils.py | Plan 质量提升的关键：让 LLM 看到项目文档 |
| 30 | `load_skill` / `render_skill_for_execution` | skills.py | Skill 注入提升 Claude 执行质量 |
| 31 | `load_agent_type` / `get_claude_command` | agents.py | 不同子任务用不同角色（架构师设计、开发者实现） |
| 32 | `--remote` 远程分支推送 | pipeline.py | 执行结果推送到 GitHub，PR 才能创建 |
| 33 | Console 抽象层 (`Console` class) | console.py | 输出质量直接影响"回来看结果"的体验 |
| 34 | `log_event` — 结构化 JSON 事件 | config.py | 机器可解析的日志，支撑分析报表 |
| 35 | `cmd_status --watch` — 实时监控 | cli.py | 用户"偶尔看一眼"时能快速了解进度 |

### P2 — 体验完善（15 项）：锦上添花

| # | 功能 | 模块 | 理由 |
|---|------|------|------|
| 36 | `cmd_show` — 任务详情查看 | cli.py | 看历史任务的具体信息 |
| 37 | `cmd_list` — 任务列表 | cli.py | 回顾执行过的所有任务 |
| 38 | `cmd_config` — 配置管理 | cli.py | 查看和编辑配置 |
| 39 | `cmd_clean` — 清理任务目录和 tags | cli.py | 维护磁盘空间 |
| 40 | `analyze_quality` — Q1-Q8 质量分析 | eval.py | 深度评估单次任务质量 |
| 41 | `analyze_performance` — P1-P6 性能分析 | eval.py | 深度分析耗时瓶颈 |
| 42 | `analyze_reliability` — 可靠性分析 | eval.py | sandbox 分布、重试统计 |
| 43 | `aggregate_quality` / `aggregate_performance` | eval.py | 跨任务趋势对比 |
| 44 | `cmd_eval` — eval CLI 报表 | eval.py | 用户主动查询统计 |
| 45 | `confirm_plan` / `confirm_subtasks` — 交互确认 | ui.py | 非无头模式下的人工审核步骤 |
| 46 | `apply_rules` — 角色-Skill 自动匹配 | role_skill_map.py | 按关键词/文件模式自动推荐角色和 Skill |
| 47 | TUI 面板 (`cmd_status_tui`) | tui.py | 更漂亮的实时监控界面 |
| 48 | `_set_gc_auto` — git gc 并发控制 | git_utils.py | 并发 worktree 操作的安全性保障 |
| 49 | `_safe_append_to_file` — 锁文件机制 | utils.py | 并发场景下的文件写入安全 |
| 50 | `cmd_cache list/clean` — 缓存管理 | cli.py | 手动管理 Plan 缓存 |

### P3 — 偏离核心（9 项）：做对了，但不是现在

| # | 功能 | 模块 | 理由 |
|---|------|------|------|
| 51 | `cmd_ci` — GitHub Actions 生成 | workflow_gen.py | 服务于"搭建 CI"场景，不是"异步任务委派" |
| 52 | `cmd_review` — Claude 代码审查 | cli.py | 服务于"审查已有代码"场景，不是"执行新任务" |
| 53 | `cmd_skills` — 列出已安装 Skill | cli.py | 管理型操作，核心用户不需要每天用 |
| 54 | `cmd_agents` — 列出 Agent 类型 | cli.py | 管理型操作，核心用户不需要每天用 |
| 55 | `list_skills` / `list_agent_types` | skills/agents | 浏览型功能 |
| 56 | `discover_skills` — 自动技能发现 | skills.py | 关键词匹配精度不够，价值存疑 |
| 57 | `analyze_ux` — UX 分析 | eval.py | 面向产品团队的自我评估工具 |
| 58 | `_detect_tool_versions` — 工具版本检测 | utils.py | 调试用途，非用户价值 |
| 59 | `plan_to_md` — Plan 排版输出 | ui.py | 纯展示用途 |

---

## 三、核心链路覆盖度评估

| 步骤 | 核心功能 | 完成度 | 缺失 |
|------|----------|--------|------|
| ① 用户说任务 | `cmd_run` | ✅ | — |
| ② Plan 拆解 | `generate_plan` + `plan_to_subtasks` + fallback | ✅ | — |
| ③ Execute 执行 | `_run_pipeline` + `run_subtask` + `_run_headless` | ✅ | **进度通知**（用户关终端后如何知道完成了？） |
| ④ Verify 验证 | `_verify_changes` + 白名单 + 沙箱 | ✅ | **失败摘要**（失败了用户一眼看懂为什么） |
| ⑤ Commit | `_format_commit` + git tag | ✅ | — |
| ⑥ PR | `cmd_pr` | ⚠️ | **PR 质量信号**（PR 描述是否让我快速判断合并风险？） |

---

## 四、P0 缺失功能（应该做但还没做）

这些是核心场景的硬伤——当前产品因为缺它们，核心承诺打了折扣：

| # | 缺失功能 | 严重度 | 用户痛苦 |
|---|----------|--------|----------|
| M1 | **任务完成通知** | 🔴 致命 | "我关了终端，怎么知道它跑完了？"——用户只能手动 `agent_go list` 检查 |
| M2 | **失败原因摘要** | 🔴 致命 | "status=failed，summary='无文件变更'——这条信息告诉我什么？"——失败原因不可读，用户必须翻 `execution.log` |
| M3 | **PR 质量仪表** | 🟡 重要 | PR 正文列出了改了哪些文件、验证结果，但没有回答"我该不该 merge？"——缺少风险提示、测试覆盖变化 |
| M4 | **时间预估** | 🟡 重要 | "周五下午 4 点跑，能在我走之前跑完还是得挂着？"——没有预估，用户不敢关终端 |

**M1 和 M2 应该成为 Q3 的最高优先级功能需求。**

---

## 五、可以砍掉的功能（P3 清理建议）

这些功能代码量不小但服务于非核心场景，建议：

| 功能 | 建议 | 理由 |
|------|------|------|
| `workflow_gen.py` (CI 生成) | 移出 repo，作为独立脚本 | 80 行代码，完全独立，无耦合 |
| `cmd_review` | 保留但降优先级维护 | 170 行，是 Claude Code 已有能力的薄封装 |
| `discover_skills` | 标记为实验性 | 中文分词缺陷导致命中率低 |
| `analyze_ux` | 合并入 analyze_reliability | 独立价值有限 |
| `cmd_skills` / `cmd_agents` 列表命令 | 合并入 `cmd_config` | 3 个管理命令 = 认知负担 |

**节省出的工程精力，转向 M1-M4。**

---

## 六、优先级与工程投入矩阵

```
        高用户价值
            │
    P0 18项 │  P1 17项
    ┌───────┼────────┐
    │ 必须维护 │ 继续投入 │
    │ 不能劣化 │ 完善体验 │
    │         │          │
低投入 ──────┼─────────── 高投入
    │         │          │
    │ 保持运行 │ 考虑砍掉 │
    │ 不做增强 │ 或降级   │
    └───────┼────────┘
    P2 15项 │  P3 9项
            │
        低用户价值
```

**策略**: 
- P0 区域 — 守护。每个 PR 不得让成功率/安全/恢复性劣化。
- P1 区域 — 投入。Q3 重点：缓存 fix、成本可见、审计完善。
- P2 区域 — 维持。不新增功能，仅修 bug。
- P3 区域 — 降级。workflow_gen 独立、cmd_review 降频、管理命令合并。

---

## 七、"需求vs实现"的错位：最值得警惕的发现

| 发现 | 说明 |
|------|------|
| **eval 体系过度建设** | `eval.py` 606 行（占代码量 12%），5 个分析维度 + 聚合 + CLI 报表。但核心用户只需要 `analyze_cost`（知道花了多少钱）和基础的成功/失败计数。其余 Q/P/reliability/UX 分析是给产品团队的工具，不是给用户的功能。 |
| **TUI 是 P2，用了 P1 的工程资源** | 199 行 curses 面板、实时刷新、多面板布局。做得很漂亮，但对核心场景的贡献不如一个 `curl` 通知 webhook。 |
| **Agent/Skill 体系是 P1，但配置复杂度是 P3** | 4 种 Agent 类型 + Skill YAML + role_skill_map.json 三层规则 — 核心用户需要的是"默认就行"，不是"先配半小时"。**
| **安全白名单是 P0，但没人知道它存在** | `_CMD_ARG_RULES` 覆盖 28 种工具、6 类注入防御 — 这是产品最大的技术壁垒之一，但在用户文档里完全没有体现。 |

---

## 八、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-07-24 | 初始版本：80+ 功能点 P0-P3 分级、核心链路覆盖度、缺失分析、错位发现 |
