# 运维与故障排查 Runbook

> 状态：As-Built 操作手册
> 更新日期：2026-08-08
> 关联：[architecture.md](../architecture.md) 任务状态机与数据持久化路径、[ISSUES.md](../ISSUES.md) 已知问题目录

本文档是 agent_go 日常运维和故障排查的单一入口，整合了状态恢复、日志排查、worktree 清理、成本复盘等操作流程。

---

## 1. 日志与数据路径速查

所有任务数据存放在 `~/.agent_go/` 下：

```
~/.agent_go/
├── config.json              ← 用户配置（API provider/key/model）
├── role_skill_map.json      ← 角色-Skill 映射规则
├── skills/<name>/SKILL.md   ← 已安装的 Skill
├── agents/<type>.md         ← Agent 类型定义
├── cache/plans/<sha256>.json ← Plan 缓存（24h TTL）
├── verification_audit.jsonl ← 验证命令审计日志
└── task-<id>/               ← 每个任务的独立目录
    ├── meta.json            ← 任务元数据 + results 数组
    ├── execution.log        ← 双格式日志（INFO 人读 + DEBUG JSON）
    ├── metering.jsonl       ← 每次 API 调用的计量记录
    ├── assessment.jsonl     ← 语义评估事件
    ├── plans/v{version}.json ← Plan 版本快照
    ├── review.json          ← 结果审查聚合（review 命令生成时）
    └── sub-<n>/
        ├── work/            ← git worktree（完成后清理；失败/阻塞保留）
        ├── .preserved       ← 保留标记（含 branch/status/failure_reason）
        ├── verify_state.json ← 验证循环状态（resume 检查点）
        └── result.json      ← 子任务结果
```

### 快速定位

| 需要查看 | 路径 |
|---|---|
| 任务整体状态 | `meta.json` → `status` / `results[]` |
| 子任务执行日志 | `execution.log`（人读格式） |
| 机器可读事件 | `execution.log`（DEBUG JSON 行） |
| API 调用成本 | `metering.jsonl` |
| Plan 变更历史 | `plans/v*.json` + `plan-history` 命令 |
| 验证失败详情 | `result.json` → `verification_results` / `kill_reason` |
| 保留的 worktree | `sub-*/.preserved` 文件存在 → worktree 未清理 |

---

## 2. 任务中断后恢复流程

### 2.1 判断中断类型

```bash
# 查看任务状态
agent_go show <task-id>
```

`meta.json` 的 `status` 字段决定恢复策略：

| status | 含义 | 恢复操作 |
|---|---|---|
| `Completed` | 正常完成 | 无需操作 |
| `Failed` | 任务失败（有子任务 failed） | `agent_go resume <task-id>` 重跑失败子任务 |
| `Interrupted` | 被 SIGINT/SIGTERM 中断 | 先 `recover`，再 `resume` |
| `Recovering` | recover 进行中或未完成 | `agent_go recover <task-id>` |

### 2.2 SIGKILL / 异常崩溃恢复

进程被 SIGKILL 或异常退出时，`meta.json` 可能与 worktree 实际状态不一致。

```bash
# Step 1: 扫描 worktree 状态（不修改文件）
agent_go recover <task-id> --dry-run

# Step 2: 重建 meta.json
agent_go recover <task-id>
```

**recover 状态分类规则**：

| worktree 状态 | 判定结果 | resume 行为 |
|---|---|---|
| 有 commit + 验证通过 | `completed` | 跳过 |
| 有 commit + 验证失败 | `failed` | 重跑 |
| 有 commit + 无验证记录 | `committed_unverified` | **重跑验证**（不直接判完成） |
| 无 commit + 有改动 | `reset`（清理 orphan） | 重跑（干净 worktree） |
| 无 commit + 无改动 | `no_changes` | 跳过 |

**重要**：recover **永不替你 commit 孤儿改动**——commit 是唯一完成边界。如果 orphan reset 失败，状态标记为 `reset_failed`，需人工处理。

### 2.3 resume 重跑

```bash
# 重跑未完成的子任务
agent_go resume <task-id>
```

resume 从 `meta.json` 读取已完成子任务列表，只重跑 `failed` / `interrupted` / `committed_unverified` / `reset` 状态的子任务。

### 2.4 并发安全

recover 和 resume 使用 task 级文件锁（`.task.lock`，fcntl.flock），不会与正在运行的 pipeline 冲突。如果锁文件残留，pipeline 会检测 heartbeat 判断任务是否活跃。

---

## 3. 失败子任务排查

### 3.1 kill_reason 归因表

`result.json` 的 `kill_reason` 字段指示子任务被终止的原因：

| kill_reason | 含义 | 是否计入模型能力失败分母 |
|---|---|---|
| `cleanup_race` | 清理竞态（正常路径） | 否（标 completed） |
| `stuck` / `hard_timeout` | Claude 卡住或超时 | **是** |
| `over_budget_l2` | 子任务累计成本超 L2 | 否（成本问题） |
| `over_budget_l3` | 任务级成本超 L3 | 否（成本问题） |
| `metering_unavailable` | metering 写入失败，保护性停止 | 否（基础设施） |
| `system_error` | 内部异常 | 否（基础设施） |
| `infra` | API 故障、网络错误（cost=0） | 否 |
| `plan_gate_blocked` | plan 质量门拦截（planner 计划未过确定性预检，未进入执行；bench 记录级） | 否（planner/harness 侧，能力观测未发生） |

### 3.2 排查步骤

```bash
# 1. 查看保留的 worktree（失败子任务自动保留）
agent_go inspect <task-id>

# 2. 查看全部子任务（包括已清理的）
agent_go inspect <task-id> --all

# 3. 机器可读格式
agent_go inspect <task-id> --json
```

inspect 输出包括：
- worktree 路径（如保留）
- git 分支名（`agent_go/{task_id}/{sub_id}`）
- `failure_reason` 和 `kill_reason`
- `.preserved` 标记内容

### 3.3 手动检查保留的 worktree

```bash
# 进入保留的 worktree
cd ~/.agent_go/task-<id>/sub-<n>/work/

# 查看 Claude 的实际改动
git log --oneline -5
git diff HEAD~1

# 查看验证失败详情
cat verify_state.json
```

### 3.4 手动清理保留的 worktree

确认排查完成后，可手动清理：

```bash
# 方法 1：用 inspect 提供的路径
cd <repo-path>
git worktree remove ~/.agent_go/task-<id>/sub-<n>/work/ --force
git branch -D agent_go/<task_id>/<sub_id>
git tag -d <task_id>/<sub_id>

# 方法 2：清理全部任务数据（谨慎！）
agent_go clean

# 方法 3：只清理旧任务
agent_go clean --older-than 7
```

---

## 4. 成本超支排查

### 4.1 从 metering.jsonl 复盘

```bash
# 查看任务总成本
python3 -c "
import json
total = 0
with open('$HOME/.agent_go/task-<id>/metering.jsonl') as f:
    for line in f:
        rec = json.loads(line)
        if rec.get('event') != 'cost_censored':
            total += rec.get('cost_usd', 0)
print(f'Total cost: \${total:.4f}')
"

# 按子任务统计
python3 -c "
import json, collections
by_sub = collections.defaultdict(float)
with open('$HOME/.agent_go/task-<id>/metering.jsonl') as f:
    for line in f:
        rec = json.loads(line)
        if rec.get('event') != 'cost_censored':
            by_sub[rec.get('sub_id', '?')] += rec.get('cost_usd', 0)
for sid, cost in sorted(by_sub.items(), key=lambda x: -x[1]):
    print(f'{sid}: \${cost:.4f}')
"
```

### 4.2 cost_censored 事件

L2/L3 熔断触发时写入 `cost_censored` 事件。该事件的 `cost_usd` 是累计花费的**下界快照**，**不会**被重复计入总成本。如果看到 metering 中有多条 `cost_censored`，不代表成本翻倍。

### 4.3 调优成本控制

| 场景 | 建议 |
|---|---|
| 冷启动（无基线） | 保持 `cost_control.enabled=false`，仅靠 L1 防失控 |
| 频繁 L2 熔断 | 调高 `subtask_multiplier` 或关闭 L2（`enabled=false`） |
| 频繁 L3 熔断 | 调高 `max_budget_usd` 或使用 `budget_mode=degrade` |
| 想完全不限成本 | `budget_mode=ignore`（仅 L1/L2 生效） |
| 本地模型成本为 0 | 在 `plan_api.local_models` 中注册模型名 |

**关键约束**：开启 L2/L3 前**必须**先运行 `eval cost-baseline` 校准。未校准的基线会导致高误杀率。

> **2026-08-10 已校准并启用**：`~/.agent_go/config.json` 的 `cost_control.enabled=true`。
> 校准数据来源是**真实 worker metering**（`~/.agent_go/task-*/metering.jsonl`，生产路由
> claude-opus/sonnet/haiku + deepseek-pro），**不是** decision bench baseline——
> 后者用 deepseek-v4-flash（P90 easy $0.008）与生产成本差 10-25×，直接用于校准会大量误杀。
>
> 校准值（P90×1.5）：
> | 难度 | per_subtask_budget | L2 累计上限(×2.5) |
> |---|---|---|
> | easy | $0.64 | $1.60 |
> | medium | $0.88 | $2.20 |
> | hard | $2.27 | $5.67 |
>
> L3 `max_budget_usd=0` → 用动态默认（Σ per_subtask × 2.5 × 子任务数）。
> 验证：1262 easy / 145 hard 子任务 **0 误杀**；1643 medium 仅 4 个超限（跑飞异常任务）；
> 1106 真实任务 L3 熔断率 < 0.5%（单 medium 任务 $2.20 熔断 2.8% 均为异常）。
> 若更换模型路由（如切到更贵/更便宜的模型），需重新运行 `eval cost-baseline` 校准。

---

## 5. 常见问题与解决方案

### 5.1 API 认证失败（401）

```
Error: API request failed: 401 Unauthorized
```

**排查**：
```bash
# 检查 API key
echo $AGENT_GO_API_KEY

# 检查配置
cat ~/.agent_go/config.json | python3 -c "import json,sys; c=json.load(sys.stdin); print(c['plan_api']['api_key'][:10]+'...')"

# 测试连通性
curl -H "x-api-key: $AGENT_GO_API_KEY" https://api.anthropic.com/v1/messages -d '{}'
```

### 5.2 Claude CLI 未找到

```
Error: claude command not found
```

**排查**：
```bash
which claude
# 如果未安装：
npm install -g @anthropic-ai/claude-code
```

### 5.3 Worktree 创建失败

```
Error: git worktree add failed
```

**排查**：
```bash
# 检查是否有残留 worktree
cd <repo-path>
git worktree list
git worktree prune

# 检查分支冲突
git branch -a | grep agent_go

# 清理旧 agent_go 分支（谨慎）
git branch -D $(git branch -a | grep 'agent_go/')
```

### 5.4 任务目录权限错误

```
Error: Permission denied: ~/.agent_go/task-xxx/
```

**排查**：
```bash
chmod -R u+rw ~/.agent_go/
```

### 5.5 meta.json 损坏

如果 `meta.json` 被 SIGKILL 截断导致 JSON 解析失败：

```bash
# 方案 1：从 worktree 状态重建
agent_go recover <task-id>

# 方案 2：手动修复 JSON（如果只是截断）
python3 -c "import json; json.load(open('~/.agent_go/task-xxx/meta.json'))"
# 手动补全缺失的闭合括号
```

### 5.6 config.json 损坏

```bash
# 备份并重建
mv ~/.agent_go/config.json ~/.agent_go/config.json.bak
agent_go config  # 触发自动创建
```

### 5.7 gc.auto 未恢复

如果 pipeline 异常退出后 `gc.auto` 未恢复（git 操作变慢）：

```bash
cd <repo-path>
git config gc.auto 1
```

---

## 6. inspect / recover / resume / clean 决策树

```
任务异常退出
    │
    ├─ 进程被 Ctrl+C (SIGINT)?
    │   └─ status=Interrupted → agent_go recover <id> → agent_go resume <id>
    │
    ├─ 进程被 kill -9 (SIGKILL)?
    │   └─ meta.json 可能不一致 → agent_go recover <id> --dry-run → agent_go recover <id>
    │
    ├─ 子任务 failed?
    │   └─ agent_go inspect <id> → 排查 → agent_go resume <id>
    │
    ├─ 想清理旧任务?
    │   └─ agent_go clean --older-than 7
    │
     └─ 想查看执行回放?
         └─ agent_go replay <id>
```

---

## 6.5 交付失败排查（M1 delivery / pr / merge）

交付环节（delivery branch 生成 → PR 创建 → merge）失败的排查路径：

```
交付失败
    │
    ├─ meta.delivery_attempted 为 False，且 DELIVERY_READY?
    │   └─ 交付尚未执行 → 查看 pipeline 收尾日志的 delivery_error
    │       ├─ "no base_commit"       → 执行前未记录 base_commit（cli.py:769）→ 检查目标仓库有效性
    │       └─ 其他                   → 读 meta.delivery_error / execution.log
    │
    ├─ agent_go pr --push 失败?
    │   ├─ "gh CLI 未安装 / 未登录"    → gh auth status；需 repo scope
    │   ├─ push 失败                  → 检查 origin 指向真实仓库、本地 main 是否同步
    │   ├─ check_mergeability 冲突     → delivery branch 与 base 有冲突，需人工解决后重试
    │   └─ PR 创建失败                → meta.delivery_failed=true，可修正后重新 agent_go pr --push
    │
    └─ agent_go merge 失败?
        ├─ "已走 PR 交付路径（互斥）"   → PR 与 merge 互斥：在 GitHub 合并 PR 或移除 meta.pr_url
        ├─ "PR 已合并，同步 merge commit" → 正常同步，meta.explicit_merge_commit 已写回（非失败）
        └─ 本地 merge 冲突            → worktree 保留现场，人工解决后重试
```

**交付状态速查**：

| 现象 | 含义 | 处理 |
|------|------|------|
| `status=DELIVERY_READY` | 全部子任务完成，交付待执行 | `agent_go pr --push` 或 `agent_go merge` |
| `status=ACCEPTED_DELIVERY` | 交付门通过（pr_url 或 explicit_merge_commit 生效） | 无需操作 |
| `meta.delivery_failed=true` | 交付尝试失败 | 读 `delivery_error`，修正后重试 |
| `meta.delivery_attempted=false` | 尚未尝试交付 | 按 pipeline 日志排查 base_commit / delivery_branch |

**交付链路三要素**（缺一即报错，不静默错误交付）：

1. 远程仓库：`origin` 指向真实 GitHub 仓库（可 push）。
2. GitHub 认证：`gh auth status` 已登录（含 `repo` scope）。
3. 同步的 base：本地 main 与 `origin/main` 对齐（PR base=main 反映最新代码）。

---

## 7. MCP Server 故障排查

MCP server（`agent_go mcp`）的专用排查见 [mcp-host-integration-guide.md](mcp-host-integration-guide.md) §8。

快速检查：

```bash
# stdio 模式健康检查（JSON-RPC initialize）
echo '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{}}' | agent_go mcp

# HTTP 模式健康检查
curl http://127.0.0.1:8090/health

# 查看 MCP server 日志
cat /tmp/agent_go_mcp.log 2>/dev/null
```

常见 MCP 错误码：

| 错误码 | 含义 | 排查 |
|---|---|---|
| `AGENT_GO_REPO_INVALID` | 仓库不在白名单 | 检查 `AGENT_GO_MCP_ALLOWED_REPOS` |
| `AGENT_GO_TASK_NOT_FOUND` | task_id 不存在 | 检查 `~/.agent_go/` 目录 |
| `AGENT_GO_TASK_RUNNING` | 对运行中任务执行 resume | 先 `inspect_task` 确认状态 |
| `AGENT_GO_CAPACITY` | 并发上限 | 提高 `AGENT_GO_MCP_MAX_CONCURRENT` 或等待 |
| `AGENT_GO_TIMEOUT` | wait=true 超时 | 非错误，任务仍在后台运行 |
