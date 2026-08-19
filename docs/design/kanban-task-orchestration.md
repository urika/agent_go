# 看板驱动的智能任务编排工作流（设计）

> 状态：设计评审（Draft）
> 日期：2026-08-18
> 关联：[kanban-board.md](kanban-board.md)、[harness-driving-architecture.md](harness-driving-architecture.md)、[decision-assistant-design.md](decision-assistant-design.md)、[model-entity-config-design.md](model-entity-config-design.md)
> 需求来源：本地模型能力边界（27B 模块工厂定位）→ 任务分类编排：系统架构走云端+人工，明确模块走本地后台队列

---

## 1. 目标

基于看板把任务按「可自动化程度」分类，让不同类型走最优执行路径：

- **系统架构/hard（L3 跨文件/全局设计）** → 云端强模型 + 人工把关（能力边界决定）
- **明确 spec 的模块实现（L1/L2）** → 本地模型后台队列异步批量执行（零边际成本）
- 全流程：及时通知验证 + 差错自动兜底（降级链）+ 人工介入点

**核心定位**：本地模型是"模块工厂"，不是"系统架构师"——工作流把任务精准分流到各自的成本-能力最优路径。

## 2. 核心架构

```
需求输入（看板 brainstorm/requirements 卡片）
   ↓
任务分类器（自动判定 + 人工可覆盖）
   ├─ 系统架构/L3/hard  → design 列（云端 planner + 人工评审 plan）
   └─ 明确 spec 模块     → implementation 列（本地后台队列）
   ↓
执行编排（按列路由）
   ├─ design：agent_go run --spec（云端 K3/GLM planner，人工确认 plan）
   └─ implementation：后台队列 worker（本地模型，goal force，自动执行）
   ↓
验证 + 通知
   ├─ evaluator 语义评估（GLM，防假阳性）
   ├─ 通过 → operations 列 + webhook/桌面通知（完成/成本/产物）
   └─ 失败 → 降级链兜底 → 仍失败 → 标 blocked + 通知人工介入
   ↓
人工介入点：design 列评审 / blocked 卡片处理 / operations 交付审批
```

## 3. 任务分类规则（自动化判定 + 人工覆盖）

### 3.1 自动分类器（复用 _should_e2e 逻辑扩展）

| 判定信号 | 分类结果 | 依据 |
|---------|---------|------|
| 含架构级关键词（refactor/架构/并发/race/系统设计/端到端/性能优化/跨文件） | **系统架构**（design 列，云端+人工） | L3 能力边界（实测 hard 拆分 0/6） |
| difficulty=hard（Spec/输入） | 系统架构 | L1 显式输入 |
| 明确 spec + 单/少文件 + 无架构关键词 | **模块实现**（implementation 列，本地队列） | L1/L2 能力范围（实测 100%） |
| 其余（默认） | 模块实现（保守偏自动化） | 误判成本低（可人工覆盖） |

### 3.2 看板卡片分类标记

卡片加 `automation` 字段：
- `auto`（implementation 列自动执行）
- `manual`（design 列人工+云端）
- `review`（执行后需人工验收）

分类器在卡片创建/移入 design 时自动标记；用户可手动覆盖。

## 4. 执行路径

### 4.1 系统架构路径（design 列，云端+人工）

```
卡片(design) → dispatch → agent_go run --spec <spec> --e2e?
  - planner: 云端强模型（K3/GLM，router.roles.planner）
  - 计划确认：web 确认卡片（人工评审 plan，R5b）
  - worker: opus-4-7 → 云端（deepseek-v4-pro）
  - evaluator: GLM（router.roles.evaluator）
  - 完成 → operations 列（人工审批 merge/PR）
```

### 4.2 模块实现路径（implementation 列，本地后台队列）

```
卡片(implementation, spec 明确) → dispatch → 后台队列
  - 模式：拆分（plan 拆分多子任务，worker_models 本地 haiku/sonnet）
  - worker：本地 27B（local-mlx，worker_backends→localhost:4000）
  - goal force（验证死守，max_turns 35 防兔子洞）
  - evaluator：GLM 云端（评估质量）
  - 并发：看板队列串行（资源保护，bench-parallel 1）
  - 完成 → operations 列 + 通知
  - 失败 → 降级链（本地→云端 v4-pro）→ 仍失败 → blocked + 人工介入通知
```

**dispatch 执行模型（PoC 修正）**：dispatch 必须**异步**——`POST /api/kanban/cards/<id>/dispatch` 立即返回 `{task_id, status: "started"}`，任务在后台队列执行（复用 task_runner 后台机制），状态经 SSE/轮询更新。**PoC 发现同步阻塞实现会导致 HTTP 超时**（任务执行分钟级）。这是 W2 后台队列的前提。

## 5. 通知与验证（及时性）

- **完成通知**：任务 DELIVERY_READY → webhook/桌面通知（含任务名/成本/产物摘要/报告链接）
- **失败通知**：blocked/failed → 立即通知（含 failure_class/failure_reason/Worktree 现场链接）
- **等待人工通知**：design 列待评审、blocked 待处理 → 通知（明确"需要你"）
- **验证自动**：evaluator 语义评估（GLM）+ shell 验证命令（双保险）

通知通道复用 notify.py（desktop/webhook/command），按事件类型路由（完成/失败/待人工）。

## 6. 差错处理（兜底与人工介入）

| 差错场景 | 处理 |
|---------|------|
| 验证失败 | 验证循环自动重试（goal force）→ 仍失败 → 降级链（本地→云端）→ 仍失败 → blocked + 通知人工 |
| 环境漂移（key 过期/后端被改） | R8 归因 + 健康检查（配置中心 mismatch 提示）→ 人工修复 |
| 评估假阳性 | evaluator 低置信度仲裁（双评估） |
| 卡片误分类 | 人工拖动改列（automation 字段可覆盖） |
| 长会话失效 | goal max_turns=35 看门狗 + 会话超时强制新会话 |

## 7. 智能化推进（决策辅助集成）

- **分类器自学习**：决策辅助（M6）定期分析 blocked/失败卡片的分类准确率 → 改进分类规则
- **成本-质量自适应**：insight 分析"本地队列 vs 云端"的成本-通过率权衡 → 动态调整分类阈值
- **自动降级建议**：blocked 卡片自动生成 insight 建议（换模型/降级链/人工介入）

## 8. 落地计划（分阶段）

| 阶段 | 内容 | 依赖 |
|------|------|------|
| **W1** 分类器 + 看板标记 | 自动分类规则 + 卡片 automation 字段 + dispatch 按列路由 | kanban.py + cli.py run 分类参数 |
| **W2** 后台队列执行 | implementation 列 dispatch → 后台队列（串行 worker，goal force）+ 完成/失败通知 | task_runner.py + notify.py |
| **W3** 人工介入点 | design 列 web 确认（R5b）+ blocked 通知 + operations 审批 | web_confirm.py + notify.py |
| **W4** 智能化 | 分类器自学习 + 成本自适应 + 自动降级建议 | M6 决策辅助 |

## 9. 验收标准

1. 创建"系统架构类"卡片 → 自动标 manual → design 列 → 云端 planner + 人工确认 → 交付
2. 创建"明确 spec 模块"卡片 → 自动标 auto → implementation 列 → 后台本地队列执行 → 完成通知 → operations 交付
3. 模块任务失败 → 自动降级云端 → 仍失败 → blocked + 通知（含现场链接）
4. 夜间批量：排 5 个模块卡片 → 后台队列完成 ≥4 个（本地 27B 吞吐）
5. 看板可视化：各列实时状态 + 通知历史

## 10. 风险

| 风险 | 对策 |
|------|------|
| 分类误判（架构任务进本地队列） | 保守偏自动化（默认模块实现）+ 人工覆盖 + blocked 通知兜底 |
| 本地队列资源耗尽（GPU/内存） | 串行队列 + bench-parallel 1 + 磁盘告警（M5.3） |
| 通知淹没 | 分级通知（失败/待人工=即时，完成=聚合） |
| 降级链成本失控 | 降级链只在 blocked 后触发 + 成本熔断（cost_control） |

## 11. PoC 验证记录（2026-08-19）

端到端验证通过（fp-sandbox safe_json_load 模块任务）：

| 环节 | 验证 |
|------|------|
| 建卡 → 分类 → 流转 | ✅（brainstorm→design→implementation，SSE 联动 + 审计落 kanban.*） |
| dispatch 派发执行 | ✅（任务 task-20260819-200901 DELIVERY_READY，sub-1 completed + verify_ok） |
| 产物 | safe_json.py(14 行) + test_safe_json.py(17 行)（merge 到 delivery 分支） |
| 流转 operations | ✅ |

**PoC 发现并修正的偏差**：
1. **dispatch 同步阻塞** → 改为异步（见 §4.2 修正）
2. **API 契约**：move 端点参数 `stage`；GET /api/kanban 返回 `{stages, card_types, cards: {stage: [cards]}, total}`
3. **交付产物**：任务完成产物在 delivery 分支，merge 后入主分支（见 §5）

## 12. API 契约（看板端点）

| 端点 | 方法 | 参数 | 返回 |
|------|------|------|------|
| `/api/kanban` | GET | - | `{stages, card_types, cards: {stage: [card]}, total}` |
| `/api/kanban/cards` | POST | `{title, stage, card_type, repo?, task?, description?, tags?, task_ids?}` | `{ok, card}` |
| `/api/kanban/cards/<id>` | POST | `{op: "update", ...fields}` | `{ok, card}` |
| `/api/kanban/cards/<id>/move` | POST | `{stage}`（目标阶段名） | `{ok, card}` |
| `/api/kanban/cards/<id>/archive` | POST | `{}` | `{ok}` |
| `/api/kanban/cards/<id>` | DELETE | `{}` | `{ok}` |
| `/api/kanban/cards/<id>/dispatch` | POST | `{}` | `{ok, task_id, status: "started"}`（**异步**，立即返回） |
