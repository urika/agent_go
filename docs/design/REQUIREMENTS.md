# agent_go 需求清单

> 版本: v0.1  
> 日期: 2026-05-15  
> 项目: agent_go — 单文件 Python 原型，LLM Plan Mode + Claude Code 无头执行编排器

---

## 一、项目现状

**代码规模**: 1129 行 Python (agent_go.py) + 3 份文档 (1255 行 md)  
**Git 历史**: 11 commits，从初始化到无头模式全自动执行  
**技术栈**: Python 3.11+, stdlib only，无外部 Python 依赖

**已实现的核心管线**:

```
Plan Mode (DeepSeek API) → Plan 确认 → 子任务拆解 → 
执行 (claude -p 无头模式) → Git commit + tag → 验证执行 →
失败自动重试 → 共享上下文生成 → 产物传递 (git merge) → 归档
```

**SnippetHub 验证**: 4 个子任务、42 个文件、1897 行代码、10 个 API 端点全部通过。

---

## 二、需求清单

优先级定义: P0=已实现 P1=下个迭代 P2=中期 P3=远期

### P0 — 已实现

| # | 需求 | commit |
|---|------|--------|
| 1 | Plan Mode：外接 LLM API 生成结构化执行方案 | `b2d7328` |
| 2 | 多供应商 API 支持 (Anthropic/OpenAI/DeepSeek/Custom) | `b2d7328` |
| 3 | 子任务拆解 + 依赖图 + 共享资源清单 | `b2d7328` |
| 4 | Git worktree 隔离执行 + 三层降级策略 | `b2d7328` |
| 5 | 结构化审计日志 (双格式) + 参考文档挂载 | `b2d7328` |
| 6 | --yes 一键自动确认 + 非 TTY safe_input | `5eaa5dd` `0993f04` |
| 7 | 路径隔离：TASK.md 源项目→worktree 路径替换 | `7355efc` |
| 8 | 产物传递：上游 worktree 代码 git merge 到下游 | `e34cfde` `9fa53ae` |
| 9 | 无头模式：claude -p + bypassPermissions + 流式输出 + 交互检测 + 超时重试 | `56b6cbe` `faf6a5e` `b251f4f` `76c400d` |
| 10 | 测试验证强制执行 + 失败自动修复重试 | `9fa53ae` |
| 11 | 共享上下文 SHARED_CONTEXT.md + 注入下游 TASK.md | `9fa53ae` |
| 12 | Git 代码管理：commit + tag + merge 替代文件拷贝 | `9fa53ae` |

### P1 — 高优先级 (建议下个迭代)

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| 13 | **--issue 参数**：关联 GitHub Issue 编号 | ✅ `948ec95` | commit 追加 "Refs: #N"，meta.json 记录 issue |
| 14 | **分支命名规范**：worktree 使用 feature/fix 前缀 | ✅ `948ec95` | `feature/{issue}-{slug}` 或 `agent_go/{task_id}/{sub_id}` |
| 15 | **Conventional Commits**：feat/fix/refactor 前缀 | ✅ `948ec95` | `_format_commit()` 自动前缀 + Issue 尾部引用 |
| 16 | **agent_go pr 命令**：生成 PR 描述 + gh pr create | ✅ `948ec95` | `cmd_pr()` 在线/离线双模式 |
| 17 | **PR 模板自动填充**：变更摘要、关联Issue、测试结果 | ✅ `948ec95` | 基于 meta.json + SHARED_CONTEXT.md |
| 18 | 中断恢复：任务重启后跳过已完成子任务 | 📋 | 读取 meta.json 恢复状态 |
| 19 | 验证命令数组支持：多个串行检查步骤 | 📋 | `["go vet", "go test ./...", "golangci-lint"]` |

### P2 — 中优先级

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| 20 | **GitHub Actions 工作流生成** | 📋 | 生成 `.github/workflows/test.yml` |
| 21 | **Projects 看板联动** | 📋 | `gh pr create --project --label --milestone` |
| 22 | 多任务并行执行 | 📋 | 端口分配器 + 多 worktree |
| 23 | Plan 结果缓存 | 📋 | 相同任务降低 API 成本 |
| 24 | `agent_go review` 命令 | 📋 | Claude 审查 PR 变更，输出审查报告 |
| 25 | TASK.md 文件覆盖检查 | 📋 | 验证 Planned files vs 实际产出 |
| 26 | Sandbox 增强：Greywall Incus 后端 | 📋 | Linux mini 主机容器级隔离 |

### P3 — 远期

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| 27 | FastAPI + SQLite 编排层 | 📋 | 多用户、任务队列、状态持久化 |
| 28 | Web 仪表盘 + TUI 观测终端 | 📋 | 实时状态可视化 |
| 29 | 多 Agent 并发协作 | 📋 | 分支隔离 + PR 合并 |
| 30 | IDE 插件 (VS Code / JetBrains) | 📋 | 在 IDE 中触发 agent_go |

---

## 三、GitHub 工作流整合需求详情

### 目标流程

```
agent_go run . "<task>" --issue 42 --yes
    │                     │
    │                     └── Issue 引用 → commit + TASK.md
    ├── Plan Mode (可引用 Issue 内容)
    ├── 子任务在 feature/42-slug 分支执行
    ├── commit: "feat(auth): add OAuth2\n\nFixes #42"
    ├── 验证通过 → git tag
    └── 全部完成
            │
            ▼
agent_go pr
    ├── 读取 meta.json + git log
    ├── 渲染 PR 模板
    ├── gh pr create --fill
    └── 输出 PR URL
            │
            ▼
GitHub Actions CI (agent_go 可生成 workflow 文件)
```

### 依赖条件

| 依赖 | 状态 | 详情 |
|------|------|------|
| gh CLI | ✅ 已安装 | v2.92.0 (Homebrew) |
| gh auth | ✅ 已登录 | github.com/urika |
| Token 权限 | ✅ 完整 | repo, workflow, admin:public_key, read:org, gist |
| GitHub 仓库 remote | — | 用户自行配置 git remote |

### 实现计划

| 阶段 | 需求编号 | 预估改动 | 涉及 |
|------|---------|---------|------|
| 阶段1 | #13 #14 #15 | ~20 行 | cmd_run(), commit 生成 |
| 阶段2 | #16 #17 | ~60 行 | 新增 cmd_pr() |
| 阶段3 | #19 #20 #21 | ~40 行 | 验证数组, CI 生成 |
