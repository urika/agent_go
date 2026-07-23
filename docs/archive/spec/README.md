# agent_go 模块规格文档总览

> 本文档是 `docs/spec/` 下各模块 spec 的索引与架构总览，面向维护者。
> 各 spec 基于 v2.0.0 源码（2026-07）逆向整理，函数签名、行号、默认值均与代码逐一核对。
>
> **产品文档 (PRD)**: [../prd/PRD.md](../prd/PRD.md) — 产品定位、功能优先级、NFR KPI、路线图

## 项目定位

agent_go 是一个 Plan Mode 编排工具：包装 Claude Code CLI，实现 `Plan -> Decompose -> Execute` 结构化工作流。LLM 生成执行计划，拆分为 2–5 个独立子任务，每个子任务在独立 git worktree 中由 Claude Code 执行，结果通过 git tag/merge 向下游传递，支持拓扑波次并发调度、中断恢复与 Plan 缓存。纯 Python stdlib，无第三方依赖。

## 分层架构

```
┌─────────────────────────────────────────────────────────┐
│ 入口层      agent_go.py → cli.py（argparse 分发）        │
├─────────────────────────────────────────────────────────┤
│ 编排层      ui.py（Plan/子任务确认交互）                  │
│             pipeline.py（拓扑波次调度、中断/清理）        │
├─────────────────────────────────────────────────────────┤
│ 执行层      executor.py（子任务端到端执行）               │
│             subtask.py（claude 调用原语、上游 merge）     │
├─────────────────────────────────────────────────────────┤
│ 能力层      api.py（LLM Plan 生成 + 缓存）                │
│             agents.py / skills.py / role_skill_map.py     │
│             （角色、技能、匹配规则）                       │
├─────────────────────────────────────────────────────────┤
│ 基础设施    config.py（配置/日志） console.py（输出抽象） │
│             git_utils.py / utils.py / metrics.py          │
├─────────────────────────────────────────────────────────┤
│ 旁路功能    eval.py（评估报表） tui.py（状态面板）        │
│             workflow_gen.py（CI 生成）                    │
└─────────────────────────────────────────────────────────┘
```

## 核心数据流（`agent_go run`）

1. `cli.cmd_run` 初始化 Console/logger/任务目录 `~/.agent_go/task-<id>/`
2. `api.generate_plan` 生成 Plan（缓存命中则直接返回；失败走三级降级：本地模型 → 规则匹配 → 单任务）
3. `ui.confirm_plan` → `ui.plan_to_subtasks` 拆解并经 `role_skill_map.apply_rules` 补齐角色/技能
4. `ui.confirm_subtasks` 确认后交 `pipeline._run_pipeline`
5. pipeline 按依赖拓扑分波次，串行或线程池调用 `executor.run_subtask`
6. executor：建 worktree → `subtask._git_merge_upstream` 合并上游产物 → 生成 TASK.md → `subtask._run_headless` 调 Claude → 验证命令 → commit + tag → 写 meta.json
7. pipeline 收尾：远程推送、worktree/tag 清理、结果落盘

## 模块索引

| 模块 | 职责 | Spec |
|------|------|------|
| `cli.py` | CLI 入口与命令分发 | [SPEC-cli.md](SPEC-cli.md) |
| `pipeline.py` | 拓扑波次调度器 | [SPEC-pipeline.md](SPEC-pipeline.md) |
| `executor.py` | 子任务端到端执行器 | [SPEC-executor.md](SPEC-executor.md) |
| `subtask.py` | claude 调用与上游 merge 原语 | [SPEC-subtask.md](SPEC-subtask.md) |
| `api.py` | LLM Plan 生成 + Plan 缓存 | [SPEC-api.md](SPEC-api.md) |
| `ui.py` | Plan/子任务终端交互 | [SPEC-ui.md](SPEC-ui.md) |
| `config.py` | 配置中心与日志基础设施 | [SPEC-config.md](SPEC-config.md) |
| `console.py` | 统一输出抽象层 | [SPEC-console.md](SPEC-console.md) |
| `git_utils.py` | git 交互与 worktree 管理 | [SPEC-git_utils.md](SPEC-git_utils.md) |
| `utils.py` | 共享工具（文档读取/shell 安全/commit 格式） | [SPEC-utils.md](SPEC-utils.md) |
| `agents.py` | Agent 角色类型系统 | [SPEC-agents.md](SPEC-agents.md) |
| `skills.py` | Skill 加载与注入 | [SPEC-skills.md](SPEC-skills.md) |
| `role_skill_map.py` | 角色-Skill 匹配规则 | [SPEC-role_skill_map.md](SPEC-role_skill_map.md) |
| `metrics.py` | 结构化指标采集（纯函数） | [SPEC-metrics.md](SPEC-metrics.md) |
| `eval.py` | 离线评估报表（Q/P/成本/可靠性/UX） | [SPEC-eval.md](SPEC-eval.md) |
| `tui.py` | curses 状态监控面板 | [SPEC-tui.md](SPEC-tui.md) |
| `workflow_gen.py` | GitHub Actions 工作流生成 | [SPEC-workflow_gen.md](SPEC-workflow_gen.md) |
| `__init__.py` | 包门面与公共符号 re-export | [SPEC-__init__.md](SPEC-__init__.md) |
| *(跨模块)* | 非功能需求 (NFR) 规格 | [SPEC-nfr.md](SPEC-nfr.md) |

## 持久化数据布局（`~/.agent_go/`）

```
~/.agent_go/
├── config.json            # 用户配置（config.py 自动创建）
├── role_skill_map.json    # 项目级角色-Skill 规则（可选）
├── agents/<type>.md       # 用户自定义角色
├── skills/<name>/SKILL.md # 全局 Skill
├── cache/plans/<sha256>.json  # Plan 缓存（24h TTL）
└── task-<id>/
    ├── meta.json          # 任务结果（pipeline 落盘，eval/tui 消费）
    ├── execution.log      # JSON 事件流（config.log_event）
    └── worktrees/         # 子任务 worktree（结束后默认清理）
```

## 跨模块共享约定

- **`.MERGE_CONFLICT` 文件协议**：`subtask._git_merge_upstream` 在 headless 冲突时写入标记文件，`executor` 据此让 Claude 解决冲突——跨模块文件协议，改名需同步两侧。
- **git 命名约定**：分支 `agent-go/<task-id>/<step>`、tag `<task-id>-step<N>-<slug>`，pipeline 与 executor 隐式耦合（见 SPEC-pipeline 维护注意事项）。
- **`matched_rules` 返回键**：`role_skill_map.apply_rules` 的输出键被 `ui.py` 直接消费。
- **Console 绑定时机**：~~`config.py`/`workflow_gen.py`/`eval.py` 在 import 时绑定 `get_default_console()` 结果~~ → 已修复，三个模块迁移为 `console = _LazyConsole()`，每次属性访问动态解析当前 Console（`console.py:150`）。

## 已知问题速查

各 spec 的「维护注意事项」章节记录了代码缺陷与隐患。完整 issue 清单见 [../ISSUES.md](../ISSUES.md)：

- ISSUE-1 ~ ISSUE-6：spec 梳理确认的 bug，已于 2026-07-23 全部修复（含 7 个回归测试，645 个测试通过）
- ISSUE-7 ~ ISSUE-14：2026-07-23 核实录入的待处理改进项，其中两个 P2 值得优先关注：
  - **ISSUE-7**：pipeline 依赖循环时未执行的子任务被跳过，meta.json 仍误标 `completed`（SPEC-pipeline）
  - **ISSUE-8**：`read_reference_docs` 路径穿越校验可被兄弟前缀目录绕过，安全相关（SPEC-utils）
  - 其余 P3：TUI 快捷键映射错位、`cache.enabled` 只禁写不禁读、全局 role_skill_map 死代码、Console import 时绑定、agent 列表覆盖不可见、`lstrip("./")` 误改文件名

## 使用建议

- 修改某模块前先读对应 spec 的「公共接口」与「依赖关系」，确认调用方。
- spec 中的行号会随代码演进漂移，以接口签名为准、行号为辅。
- 模块行为发生变化时，请同步更新对应 spec 与本文档的索引/约定章节。
