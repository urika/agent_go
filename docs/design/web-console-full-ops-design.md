# Web 操作台全功能扩充——设计 + 需求文档（v2）

> 状态：需求评审（v2，已并入产品经理 review 意见）
> 日期：2026-08-12
> 关联：[web_server.py](../../agent_go/web_server.py)、[config.py](../../agent_go/config.py)、[mcp_server.py](../../agent_go/mcp_server.py)
> v2 变更：补用户画像/成功指标/golden path；R5 拆分解决计划确认交互矛盾；写操作复用 MCP 任务管理层（去双轨状态）；P0 收敛至 5 项；D1-D6 细节入验收标准。

---

## 1. 背景与目标

### 1.1 背景

agent_go 目前通过 CLI 驱动。纯本地模式（无网络、仅本地 LLM）已跑通全套流程（4816bd2），但：

1. **配置切换依赖手工编辑**：云端 ⇄ 本地切换需改 `config.json` 多个字段，无 UI 无校验——**这是第一痛点**；
2. **观测与处置割裂**：Web 平台只有只读 GET API，看到问题无法操作；
3. **无 LLM 辅助环境**：纯本地场景没有"agent 帮你操作"，用户必须能在界面上自助完成。

### 1.2 用户画像

| 画像 | 特征 | 核心诉求 |
|------|------|---------|
| **P-1 安全敏感环境工程师**（主） | 内网/离线机房，仅有本地 LLM；可能是运维而非开发者 | 配置不能改错；操作留痕（审计）；界面引导 |
| **P-2 个人开发者**（次） | 本机跑本地模型省成本；熟悉 CLI | 快速切换；少打字 |

> P-1 决定 P0 范围（配置+健康+审计随行），P-2 由同一方案自然覆盖。

### 1.3 成功指标

- **北极星**：纯本地用户**零 CLI** 完成首个任务（从打开浏览器到 merge 完成）；
- 配置切换一次成功率 = activate 后 health 全绿比例（目标 ≥95%）；
- web 操作占比 = web 发起的处置数 / 总处置数（M2 后观察，目标 >50%）；
- R5a 启动成功率（目标 ≥90%，失败必须给可读错误）。

### 1.4 Golden Path（新手第一小时，需求检验器）

```
装本地代理 → agent_go web → 健康页见红 → 点「一键本地」→ 变绿
→ 粘贴 repo + 任务描述 →（auto 模式直接跑 / web 模式确认计划）
→ SSE 看进度 → 失败子任务点 resume → 审批台看 diff
→ approve → merge → 完成
```

**旅程中任何一步断掉，对应需求才配进 P0。** 用此尺量：计划确认界面（R5b）缺失时旅程在"确认计划"处断——故 R5a 提供 auto 模式保底，R5b 后续补齐。

### 1.5 目标

在 `agent_go web` 单一界面完成：配置管理（切换/健康/编辑）、任务生命周期（run/resume/cancel/clean）、交付处置（review/merge/PR）、观测增强（TCO/偏差/操作事件）。

### 1.6 非目标

- 不做多用户/权限体系（单机工具，localhost + 可选 token）；
- 不做 Plan 编辑器（只读展示 + 确认/否决决策）；
- 不做 Skill/Agent 管理界面（CLI 已有）。

---

## 2. 现状分析

### 2.1 Web 平台现状（web_server.py，1784 行）

| 能力 | 现状 | 缺口 |
|------|------|------|
| 任务清单/详情/子任务 | ✅ 17 个只读 GET API + 8 视图 | 无操作入口 |
| 实时推送 | ✅ SSE /api/events | 只推状态，不推操作结果 |
| 成本/模型/基准分析 | ✅ overview/cost/models/bench-results | 无 TCO 视图 |
| 配置展示 | ✅ /api/config（只读、key 脱敏） | 无切换/编辑 |
| **处置能力** | ❌ WebHandler 只有 do_GET | 需 POST/PUT/DELETE |

### 2.2 已有任务管理能力（mcp_server.py，**必须复用，禁止重造**）

MCP 层已实现后台任务管理：run_task / resume_task / cancel_task / inspect_task / review_task，含"后台运行 + inspect 轮询 + cancel"语义（返回值直接引导轮询）。

**v2 架构约束**：web 写端点**直接复用该任务管理层**（抽共享 service 模块或直接调用其函数），**不另写 subprocess.Popen + PID 文件**。web 侧不存任务状态——**meta.json + status.py 8 状态机是唯一事实源**，web 只缓存"启动参数"供展示。

### 2.3 其他可行性基础

- **profile 机制完整**（config.py:247-272）：AGENT_GO_PROFILE/--profile → profiles/<name>.json；
- **纯本地配置模板已验证**（见 5.3）；
- **CLI 处置能力已存在**：resume/clean/review/merge 均有 cmd_* 实现；
- **安全边界清晰**：默认 127.0.0.1；写操作复用 --token 鉴权。

### 2.4 必要性（同 v1，略）

---

## 3. 功能架构设计

### 3.1 架构总览（v2：共享 service 层）

```
┌─────────────────────────────────────────────────────┐
│                浏览器（单文件 SPA）                    │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP (JSON) + SSE
┌──────────────────────┴──────────────────────────────┐
│          agent_go web（web_server.py）               │
│   只读 GET（17 现有） + 写 POST/PUT/DELETE（新增）      │
└──────────────────────┬──────────────────────────────┘
                       │ 调用
┌──────────────────────┴──────────────────────────────┐
│      task_service.py（新，从 mcp_server 抽出）         │
│  run / resume / cancel / inspect / review-decision   │
│  状态永远读 meta.json + status.py（唯一事实源）         │
└──────────┬───────────────────────────┬──────────────┘
           │                           │
    cli.py cmd_* 复用            mcp_server.py 复用
```

**设计原则**：
- 写操作 = task_service / cmd_* 复用，web 只是参数包装；
- run 由 task_service 后台执行（沿用 MCP 已有机制），SSE 推 meta.json 状态变化；
- 配置切换 = 写 profile 文件 + **web 进程自动重载 config 单例**（D2）；
- 所有写操作写审计行（`~/.agent_go/web_audit.jsonl`），**审批决策必写**（D4）。

### 3.2 API 设计（新增；v2 调整）

#### 3.2.1 配置管理

| 方法 | 路径 | 功能 | 备注 |
|------|------|------|------|
| GET | `/api/profiles` | profile 列表 + 当前生效 | - |
| POST | `/api/profile/local` | 一键生成并激活纯本地 profile | 探测代理 /v1/models 自动填模型 |
| POST | `/api/profile/cloud` | 恢复云端（备份当前） | - |
| POST | `/api/profile/activate` | 切换任意 profile | 激活后 web 重载 config（D2） |
| GET | `/api/health` | plan/worker/evaluator 端点 + 本地代理探测 | 模型名 ≠ local_models 时返回 `mismatch: true` + 建议动作（D6） |
| GET | `/api/config/diff` | 当前 vs 目标 profile 差异 | P2 |
| PUT | `/api/config` | 白名单字段编辑 | P2，api_key 永不回显 |

#### 3.2.2 任务生命周期

| 方法 | 路径 | 功能 | 备注 |
|------|------|------|------|
| POST | `/api/tasks/run` | 启动任务（task_service） | 入参含 `confirm_mode: auto\|web`（见 3.4） |
| POST | `/api/tasks/<id>/resume` | 恢复任务 | 运行中任务返回 409 + 可读错误（D3） |
| POST | `/api/tasks/<id>/cancel` | 取消任务 | 复用 MCP cancel 语义 |
| DELETE | `/api/tasks/<id>` | 清理任务 | 需 `{confirm: true}` |
| POST | `/api/tasks/clean-old` | 按天数清理 | 二次确认 |
| GET | `/api/tasks/<id>/worktrees` | 保留 worktree 清单 | P2 |

#### 3.2.3 交付处置

| 方法 | 路径 | 功能 | 备注 |
|------|------|------|------|
| POST | `/api/tasks/<id>/review` | 触发聚合审查 | `{deep: bool}` |
| POST | `/api/tasks/<id>/review/decision` | 审批决策 | **必写审计行**（D4） |
| POST | `/api/tasks/<id>/merge` | 合并交付分支 | 确认弹窗须展示目标分支+remote+commit 数（D5） |
| POST | `/api/tasks/<id>/pr` | 创建 PR | 返回 PR url 或错误 |

#### 3.2.4 观测增强

| 方法 | 路径 | 功能 | 备注 |
|------|------|------|------|
| GET | `/api/tasks/<id>/deviation` | 偏差记录聚合 | P1 |
| GET | `/api/local-tco` | 本地 TCO 视图 | **界面必须标注"估算成本"**（D1） |
| SSE | `/api/events` | 追加 operation_* 事件 | - |

### 3.3 前端视图扩展

| 视图 | 内容 |
|------|------|
| **配置中心**（新，P0） | 当前模式徽标 / 一键 local-云切换 / 健康面板（红绿+模型名+mismatch 引导按钮） |
| **任务操作栏**（详情页扩，P1） | run 表单（含 confirm_mode 选择）/ resume / cancel / clean |
| **计划确认卡片**（新，P2/R5b） | Plan 渲染 + Y/S/D/E/R/N 决策按钮 |
| **交付审批台**（新，P1） | review 报告 + diff 摘要 + 三决策按钮 + merge/PR |
| **偏差视图**（详情页扩，P1） | 类型/根因分布 + 记录列表 |
| **TCO 面板**（成本页扩，P1） | 累计估算成本（显著标注"估算"） |

### 3.4 计划确认交互方案（B1 的解）

CLI 的 confirm_plan/confirm_subtasks 是 input() 阻塞。抽象为**可注入确认函数**：

```python
# ui.py 现有 confirm_plan(...) 抽象接口：
def confirm_plan(plan, *, mode: str, task_id: str = "") -> str:
    # mode="cli"：现有 input() 交互（不变）
    # mode="auto"：直接返回 "y"（--yes 等价）
    # mode="web"：写 pending_confirmation.json（plan + 上下文），
    #             阻塞轮询 / SSE 等 web POST /tasks/<id>/confirm 的决策
```

- **R5a（P1）**：run 入参 `confirm_mode=auto`，前端明示"跳过计划确认"；
- **R5b（P2）**：`confirm_mode=web`，Plan 生成后任务状态 → `awaiting_confirmation`，SSE 推送，前端渲染计划确认卡片，POST 决策后续跑。subtasks 确认同机制。

### 3.5 安全设计

- 默认绑定 127.0.0.1；--token 时**所有**写端点 401 校验；
- DELETE/merge 二次确认；merge 确认展示分支+remote+commit 数；
- PUT /api/config 白名单；api_key 永不回显；
- run 端点 repo 路径绝对路径 + 存在性校验；
- 全部写操作落 `web_audit.jsonl`（时间/端点/入参摘要/结果/操作者 token 哈希）。

---

## 4. 需求明细（v2 重排）

### P0（M1，一周可交付，解决第一痛点）

| ID | 需求 | 验收标准 |
|----|------|---------|
| R1 | `agent_go config local` 一键生成纯本地 profile 并激活（备份当前） | 执行后 run 走本地；探测代理 /v1/models 自动填模型；代理不可达时给可读错误并中止 |
| R2 | `agent_go config cloud` 恢复云端 | 原配置完整恢复；备份保留 |
| R3 | Web 配置中心：profile 列表 + 当前模式徽标 + 一键切换 | 与 CLI 等价；**切换后 web 自动重载 config，/api/config 立即反映新值**（D2） |
| R4 | Web 健康检查面板 | 每端点 ok/fail + 实际模型名；**模型名 ≠ local_models 时显示"重新生成 profile"引导按钮**（D6） |
| R8 | 写端点 token 鉴权 | 无 token 写请求 401 |

**P0 Exit Criteria**：纯本地用户打开浏览器，5 分钟内从"健康页全红"到"全绿 + local 生效"，全程零 CLI、零手工编辑 JSON。

### P1（M2，任务操作 + 交付闭环）

| ID | 需求 | 验收标准 |
|----|------|---------|
| R5a | Web 任务启动（confirm_mode=auto） | 返回 task_id；SSE 进度；**前端明示"跳过计划确认"**；代理不可达时启动即报错（D3） |
| R6 | Web resume / cancel | 与 CLI 一致；**resume 运行中任务返回 409 + 可读提示**（D3） |
| R7 | Web clean（单任务 + 按天数） | 二次确认；清理统计回显 |
| R9 | Web review + 审批决策 | 与 CLI review 输出一致；**决策必写 review.json + 审计行**（D4） |
| R16 | 操作审计 web_audit.jsonl | 覆盖全部写端点；含时间/端点/入参摘要/结果 |
| R10 | Web merge（可 --push） | **确认弹窗展示目标分支+remote+commit 数**（D5）；冲突时回显冲突文件列表（D3） |

### P2（M3，体验完整化）

| ID | 需求 | 验收标准 |
|----|------|---------|
| R5b | Web 计划确认界面（confirm_mode=web） | Plan 渲染 + Y/S/D/E/R/N 决策 + 续跑；subtasks 确认同机制；超时策略明示 |
| R11 | Web PR 创建 | 返回 PR url 或错误 |
| R12 | Web 偏差视图 | 类型/根因分布 + 记录列表 |
| R13 | Web TCO 面板 | Σ(调用数 × local_model_cost)；**显著标注"估算成本，非真实账单"**（D1） |
| R14 | Web 配置编辑（白名单 PUT） | 校验错误前端回显；保存后新任务生效 |
| R15 | config diff 视图 | 字段级差异展示 |
| R17 | worktree 清单视图 | 路径/branch/状态表格 |

---

## 5. 技术方案要点

### 5.1 写操作复用（v2：去双轨）

```python
# task_service.py（新模块，从 mcp_server.py 抽出后台任务管理）
def run_task(repo, task, options) -> dict      # 后台执行 + 返回 task_id
def resume_task(task_id) -> dict               # 状态校验：running → 409
def cancel_task(task_id) -> dict
def inspect_task(task_id) -> dict              # 状态永远读 meta.json

# web_server.py 写端点 = task_service 薄包装 + 审计行
```

**禁止**新增 `web_tasks/<id>.json` PID 文件；web 重启后状态从 meta.json 重建。

### 5.2 配置切换实现

```python
def activate_profile(name: str) -> dict:
    # 1. 备份当前 → profiles/backup-<ts>.json
    # 2. 生成/读取 profiles/<name>.json（local 模板见 5.3）
    # 3. 写 ~/.agent_go/.current_profile
    # 4. 校验（local：local_models 非空、worker_backends 指向本机）
    # 5. web 进程重载 config 单例（D2）
```

### 5.3 纯本地 profile 模板（同 v1）

```json
{
  "plan_api": {"provider": "openai", "base_url": "http://localhost:4000/v1/chat/completions", "model": "claude-sonnet-4-6", "api_key": "", "worker_base_url": "http://localhost:4000", "local_models": ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-7"]},
  "planner_api": {"provider": "openai", "base_url": "http://localhost:4000/v1/chat/completions", "model": "claude-sonnet-4-6"},
  "worker_models": {"easy": "claude-haiku-4-5", "medium": "claude-sonnet-4-6", "hard": "claude-opus-4-7"},
  "worker_backends": {"claude-sonnet-4-6": "http://localhost:4000", "claude-haiku-4-5": "http://localhost:4000", "claude-opus-4-7": "http://localhost:4000"},
  "local_model_cost": {"unsloth/Qwen3.6-35B-A3B-UD-MLX-4bit": 0.0005},
  "evaluator": {"enabled": true, "provider": "openai", "base_url": "http://localhost:4000/v1/chat/completions", "model": "claude-sonnet-4-6"},
  "goal": {"enabled": true, "policy": "force", "max_turns": 50, "timeout_seconds": 3600},
  "fallback": {"local_model_url": "http://localhost:4000/v1/chat/completions", "local_model_name": "claude-sonnet-4-6", "enable_rules": true}
}
```

### 5.4 SSE 操作事件

```json
{"type": "operation", "op": "resume", "task_id": "t1", "status": "started|completed|failed", "detail": "..."}
{"type": "confirmation_required", "task_id": "t1", "stage": "plan|subtasks"}
```

---

## 6. 验收与发布

### 6.1 总验收清单

1. **纯本地闭环**：断网 + 仅本地代理，web 完成 golden path 全程（R5b 未交付前用 auto 模式）；
2. **云端兼容**：切回 cloud 原配置完整恢复；
3. **状态一致性**：web 重启后任务状态与 meta.json 完全一致（无双轨）；
4. **安全**：token 模式写操作 401；merge 确认展示分支明细；
5. **审计**：全部写操作落 web_audit.jsonl，审批决策可溯源。

### 6.2 发布计划（v2：小步快跑 + 价值验证）

| 阶段 | 内容 | Exit |
|------|------|------|
| **M1** | P0：R1/R2/R3/R4/R8 | P0 Exit Criteria（5 分钟配置闭环） |
| 观察期 | 收集配置切换一次成功率、web 使用率 | ≥95% 才进 M2，否则修配置体验 |
| **M2** | P1：R5a/R6/R7/R9/R16/R10 | web 操作占比 >50% |
| **M3** | P2：R5b/R11/R12/R13/R14/R15/R17 | 北极星：零 CLI 首任务（含计划确认） |

### 6.3 风险（v2 更新）

| 风险 | 影响 | 对策 |
|------|------|------|
| ~~写操作与 CLI 漂移~~ | 已解 | task_service 单一实现，CLI/MCP/web 三方复用 |
| ~~后台进程状态双轨~~ | 已解 | meta.json 唯一事实源，禁 PID 文件 |
| confirm_mode=web 阻塞超时 | 任务挂起 | 确认卡片设超时（默认 30min）+ 超时自动 cancel + SSE 提醒 |
| 本地代理模型名变化 | 配置失效 | health mismatch 检测 + "重新生成 profile"引导（D6） |
| 绑定非 localhost | 未授权操作 | 默认 127.0.0.1；--host 文档警示；token 必选 |
