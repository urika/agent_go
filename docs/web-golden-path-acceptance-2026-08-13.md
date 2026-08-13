# 纯本地 Golden Path 真实验收报告（Web 操作台）

> 日期：2026-08-13
> 验证对象：Web 操作台全功能扩充（R1-R17，commit 75dff37/7102c9b/0ee1aa2/6dd3ce8）
> 验收依据：`docs/design/web-console-full-ops-design.md` v2 §6.1 验收清单第 1 条
> 任务记录：`~/.agent_go/task-20260813-102533-805-f4d6`（fp-sandbox fixture，email_validator 任务）
> 结论：**通过**

---

## 1. 验收环境

| 项 | 值 |
|----|----|
| 本地后端 | rapid-mlx :8081（`unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`，Qwen3.6-35B-A3B） |
| 本地代理 | `anthropic_proxy.py` :4000（`manage.sh start` 启动，PID 32695/32803） |
| 任务仓库 | `eval_suite/fixtures/fp-sandbox`（空沙箱 fixture，无验证压力） |
| 任务内容 | 创建 `email_validator.py`（validate_email）+ 3 个 pytest 用例 |
| 启动方式 | `agent_go web --port 8091`，全程经 HTTP API 驱动（零 CLI） |

## 2. 验收清单逐项结果

| # | 步骤（v2 §6.1） | 结果 | 证据 |
|---|----------------|------|------|
| 1 | 切 local | ✅ | `POST /api/profile/local` → 探测模型列表 + 自动备份 + 激活；profiles 显示 local |
| 2 | 健康全绿 | ✅ | `GET /api/health`：plan/worker/evaluator/local_proxy 四端点全 ✅，model=claude-sonnet-4-6，mismatch=False |
| 3 | 启动（web 确认模式） | ✅ | `POST /api/tasks/run {confirm_mode:"web"}` → 子进程 `--json run --yes --confirm-mode web`；pending(plan, 2 步) 生成 |
| 4 | 两级计划确认 | ✅ | `POST .../confirm {plan, Y}` → pending(subtasks, 2) → `{subtasks, Y}` → 进入执行 |
| 5 | 纯本地执行 | ✅ | metering 6 条记录**总成本 $0.000000**；worker `is_local=True` + `actual_model=unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit`（真实模型名）；无任何云端调用 |
| 6 | 观测 | ✅ | 状态轮询 / 子任务明细 / metering 实时可查 |
| 7 | 处置 | ✅ | resume 已交付任务安全收敛（无剩余子任务，状态保持 ACCEPTED_DELIVERY）；cancel 语义经单测覆盖 |
| 8 | 审批 | ✅ | `POST .../review` 生成报告 → `POST .../review/decision {approve}` → 写入 review.json，决策=approved |
| 9 | merge | ✅ | `GET .../merge-preview`（delivery=agent_go/task-*/delivery, target=main, ahead=4, mergeable=True, 无冲突）→ `POST .../merge` → 已合并到 main: `789dbb2` |
| 10 | 终态 | ✅ | 状态 **ACCEPTED_DELIVERY**；`GET /api/audit` 审计链完整：run → confirm×2 → review → review.decision → merge（全部 ok） |

## 3. 关键验证点：纯本地零云端

```
planner  | is_local: -    | claude-sonnet-4-6                    | $0.0
worker   | is_local: True | unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit | $0.0
evaluator| is_local: -    | claude-sonnet-4-6                    | $0.0
worker   | is_local: True | unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit | $0.0
evaluator| is_local: -    | claude-sonnet-4-6                    | $0.0
总成本: $0.000000
```

- worker 真实模型名正确进入 metering（成本重算链路生效）
- 路由名（claude-*）在代理 /v1/models 返回 → health mismatch 检测自洽（mismatch=False）

## 4. 端到端耗时

| 阶段 | 耗时 |
|------|------|
| plan 生成（本地模型） | ~2.5 分钟（pending 出现） |
| 子任务执行（sub-1 + sub-2） | ~13 分钟 |
| 总计（含两级确认） | ~15 分钟 |

## 5. 过程中发现（非阻塞，已记录）

1. **web 启动请求阻塞**（优化项）：task_runner 等待 `--json` 首事件（含 task_id）最多 30s；本地模型 plan 生成 >30s 时 HTTP 响应挂起（本次 curl 120s 超时，但任务实际正常启动）。建议后续改「立即返回启动中 + 列表 🔔 引导」（与 U1/U2 联动），或延长/异步化 task_id 等待。

## 6. 环境恢复

- 云端配置已恢复（`POST /api/profile/cloud`，备份 `backup-20260813-105317.json`）
- 本地代理保持运行（`/Users/jinsongwang/APP/llama.cpp/manage.sh stop` 可停止）

## 7. 相关命令备忘（文档化）

```bash
# 本地代理启动（Qwen3.6-35B + anthropic_proxy:4000）
/Users/jinsongwang/APP/llama.cpp/manage.sh start
# 状态检查
/Users/jinsongwang/APP/llama.cpp/manage.sh status
# 详见 docs/in/api-and-operations-guide.md
```
