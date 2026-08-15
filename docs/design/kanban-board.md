# Web 看板（Kanban）任务管理设计

> **版本**：v1.0（MVP 已落地）
>
> **日期**：2026-08-15
>
> **状态**：已实现（`agent_go/kanban.py` + `agent_go/web_server.py` 看板视图）

---

## 一、定位与决策背景

本文档**显式反转** `docs/design/project-management-tool-interaction.md`（2026-08-01）
中"agent_go 不往上延伸做需求管理"的决策。反转理由：

- 原决策假设存在一个独立的外部项目管理工具；实际使用中该工具并未落地，
  需求/讨论/任务流转分散在会话和文档里，缺少项目视角的统一视图。
- 内置 web 操作台（`agent_go web`）已具备任务观测 + 处置能力，看板是其自然延伸，
  增量成本低（零新依赖，stdlib only）。
- **边界保持不变**：agent_go 只做**轻量看板**（卡片 + 阶段流转 + 派发执行）；
  重量级项目管理（PRD 管理、Roadmap 排期、人员分工、跨项目依赖）仍留给外部工具，
  通过 Task Spec / MCP 对接。

## 二、核心设计：看板状态与执行状态正交

```
需求管理层（看板）          执行层（agent_go 既有）
─────────────────          ─────────────────────
brainstorm     💡 头脑风暴
requirements   📝 需求生成
design         🎨 设计方案讨论
implementation 🔨 落地实现  ──dispatch──▶  agent_go run（meta.json 唯一事实源）
operations     📈 运营优化                    │
                                ◀──task_ids[] 软链接，执行状态实时派生
                                （status.py 8 态状态机，不在卡片上冗余）
```

- 看板 stage 是**需求管理**状态，与 `status.py` 的**执行**状态机完全正交，互不改写。
- 卡片通过 `task_ids[]` 软链接执行任务；卡片上的执行状态徽章由 `api_kanban()`
  实时从 meta.json 派生，卡片本身不存执行状态（避免双写不一致）。
- **meta.json 不动**：看板数据独立存 `~/.agent_go/kanban.json`，
  避免污染执行层唯一事实源（cli/pipeline/recover/resume 多处整体读写 meta.json）。

## 三、数据模型

存储：`~/.agent_go/kanban.json`（单文件，`kanban.py` mtime 缓存 + 原子写 + 锁串行化）。

卡片 schema：

```json
{
  "id": "card-<12位小写字母数字>",
  "title": "str",
  "stage": "brainstorm|requirements|design|implementation|operations",
  "type": "discussion|implementation|periodic",
  "repo": "str（implementation/periodic 必填）",
  "description": "str（markdown，沉淀人+AI 讨论内容）",
  "spec_path": "str（可选，关联 Task Spec 文件）",
  "cron": "str（periodic 专用，展示用 cron 表达式）",
  "task_ids": ["task-..."],
  "archived": false,
  "created": "iso", "updated": "iso",
  "history": [{"ts": "iso", "action": "create|update|move|archive|link", "from": "?", "to": "?", "note": "?"}]
}
```

三类卡片：

| type | 含义 | 可派发 |
|---|---|---|
| `discussion` 💬 讨论 | 人+AI 讨论文档任务，description 沉淀讨论内容 | 否 |
| `implementation` 🤖 实施 | AI 落地实施任务，一键派发 agent_go 执行 | 是 |
| `periodic` 🔁 周期 | 周期性任务，cron 字段展示周期，外部触发派发 | 是 |

## 四、阶段流转工作流

MVP 允许**任意方向自由流转**（PM 灵活性优先），每次流转记 history（from/to/note/ts）。
流转门禁（如"需求生成 → 落地实现必须经过设计讨论"）留待二期按实际使用数据再议。

派发执行（dispatch）是唯一的自动流转：卡片派发给 agent_go 后自动流转到
implementation 列并链接 task_id。

## 五、API 端点

读：

- `GET /api/kanban` → `{stages, card_types, cards（按 stage 分组）, total}`，
  每张卡片带 `latest_task: {task_id, status, task}`（实时派生）；archived 卡片不返回。

写（全部走 token 鉴权 + `web_audit.jsonl` 审计，op 名 `kanban.*`）：

| 端点 | body | 说明 |
|---|---|---|
| `POST /api/kanban/cards` | `{title, type, stage?, repo?, description?, cron?, spec_path?}` | 建卡 |
| `POST /api/kanban/cards/<id>/update` | `{title?, description?, repo?, cron?, spec_path?}` | 改卡（白名单字段） |
| `POST /api/kanban/cards/<id>/move` | `{stage, note?}` | 阶段流转 |
| `POST /api/kanban/cards/<id>/archive` | `{archived?}` | 归档/取消归档 |
| `POST /api/kanban/cards/<id>/delete` | `{}` | 物理删除（仅未派发过任务的卡片） |
| `POST /api/kanban/cards/<id>/dispatch` | `{parallel?, confirm_mode?}` | 派发执行 → link + 自动流转 implementation |

错误映射：参数错 400 / 卡片不存在 404 / 业务规则（KanbanError）422。

SSE 联动：`_tasks_signature()` 包含 kanban.json 的 mtime:size，
看板数据变化触发前端自动刷新（任务视图与看板视图均响应）。

## 六、周期任务：外部触发（MVP）

agent_go 无内置调度机制，MVP 不做调度线程。周期卡片存 cron 表达式（展示/提醒用），
由系统 crontab / launchd 定时调用 dispatch API：

```cron
# 每天 09:17 触发周期卡片派发
17 9 * * * curl -s -X POST http://127.0.0.1:8091/api/kanban/cards/card-xxx/dispatch \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{}'
```

注意：该方式要求 `agent_go web` 进程在触发时点处于运行状态。

## 七、前端交互

web 操作台新增 `🗂 看板` 首个 tab：

- 横向 5 列 flex 布局，列头 = 阶段名 + 卡片计数 + "＋ 新建"内联表单。
- 卡片：标题 + 类型标签 + repo 短名 + cron 标签 + 最新执行任务状态徽章 + 更新时间。
- 流转：HTML5 drag&drop 拖拽到目标列；◀▶ 按钮作无拖拽 fallback。
- 卡片详情（点击展开）：description（pre 原文，MVP 不做 markdown 渲染）、
  history 时间线、关联 task_id 跳转任务列表、操作（编辑/派发执行/归档/删除）。
- 顶部 repo 文本筛选（客户端过滤）。

## 八、二期路线（明确未做）

- 内置周期调度线程（web server daemon 线程扫描到期卡片自动派发）
- description markdown 渲染 / 在线编辑器
- 多看板 / 项目维度切换（当前单全局板 + repo 筛选）
- 阶段流转门禁规则
- 讨论文档 → Task Spec 一键生成（`spec template` 集成）
- 卡片与 Task Spec 准入门禁联动（dispatch 时可选 `--spec`）
