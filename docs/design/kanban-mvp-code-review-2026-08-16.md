# Web 看板（Kanban）MVP 落地 Code Review

> 日期：2026-08-16
> 审查范围：`agent_go/kanban.py`（数据层）+ `agent_go/web_server.py`（看板 API + 前端视图）+ 关联测试
> 代码基线：`main` 分支 HEAD
> 关联文档：[kanban-board.md](kanban-board.md)（设计 v1.0）
> 测试状态：`tests/test_kanban.py` + `tests/test_web_kanban.py` 共 47 passed（10.2s）；`agent_go/lint.py` AST 检查无循环体截断告警

## 审查范围

| 文件 | 内容 |
|------|------|
| `agent_go/kanban.py` | 单文件数据层：mtime 缓存 + 原子写 + 进程内锁；create/update/move/archive/delete/link |
| `agent_go/web_server.py:1178-1221` | `api_kanban` 只读端点（卡片按 stage 分组 + latest_task 实时派生） |
| `agent_go/web_server.py:1398-1400, 1557-1578, 1814-1922` | GET/POST 路由、写操作、dispatch 派发流程 |
| `agent_go/web_server.py:2040-2054` | `_tasks_signature` SSE 签名（含 kanban.json mtime:size） |
| `agent_go/web_server.py:2979-3268` | 前端看板 JS：渲染/拖拽/表单/操作/详情 |
| `tests/test_kanban.py`、`tests/test_web_kanban.py` | 数据层单测 + 端点集成测试 |

## 结论摘要

**通过（有条件）**。实现与设计文档 v1.0 高度一致，正交性设计（看板 stage ⊥ status.py 执行状态机）执行到位，安全与审计细节扎实，测试覆盖充分。存在 1 个中等问题（dispatch 非原子）与 2 个低危输入卫生问题，均不阻塞 MVP 交付。

| # | 严重度 | 位置 | 问题 | 状态 |
|---|--------|------|------|------|
| M1 | 🟡 MED | `web_server.py:1915-1918` | 派发流程非原子：start_run 成功后 link_task/move_card 失败 → 任务在跑但卡片无链接 | ✅ 已修复 |
| L2 | 🟢 LOW | `web_server.py:1850-1851` | update 用 `str(body[k])`，JSON `null` 被存成字面量 `"None"`（create 路径安全） | ✅ 已修复 |
| L3 | 🟢 LOW | `web_server.py:1874` | `bool(body.get("archived", True))` 对字符串 `"false"` 强转为 True | ✅ 已修复 |

---

## 🟡 M1：派发流程非原子 —— ✅ 已修复

**位置**：`agent_go/web_server.py:1915-1918`（`_op_kanban_dispatch`）

```python
task_id = task_runner.start_run(repo, task_text, parallel=parallel,
                                confirm_mode=confirm_mode)   # ① 任务已启动
kanban.link_task(card_id, task_id)                          # ② 独立加锁
card = kanban.move_card(card_id, "implementation", note=...) # ③ 独立加锁
```

**问题**：
1. ① 成功、②③ 抛错（磁盘满 / kanban.json 损坏 / KanbanError）时，任务已在后台运行但卡片未链接、未流转，且客户端收到 422 错误 —— 实际已启动的"幽灵任务"。
2. ② 与 ③ 是两次独立的 `with _lock` 临界区，并发 GET `/api/kanban` 可能观察到"已链接 task_id 但仍在原列"的中间态。

**失败场景**：卡片 A（implementation, repo 存在）→ 派发成功 → `start_run` 返回 task_id → 紧接着 `link_task` 内 `_save_board` 写盘失败 → 422 回给前端"卡片不存在"类错误，但 agent_go 子进程已 spawn，用户不知情。

**修复方向**：在 `kanban.py` 增加原子操作 `dispatch_card(card_id, task_id, to_stage="implementation")`，单锁内完成 link + move + history（`link` + `move` 两条），由 web 层单次调用；这样即使 task 启动后卡片写入失败，也至少保持"卡片已链接"或整体失败的一致性可判定。

> **修复**（2026-08-16）：已实现 `kanban.py:dispatch_card`（单锁内读-改-写，link+move 两条 history），`_op_kanban_dispatch` 改用之；测试 `TestDispatchCard`（原子性/去重/非法参数）+ `test_dispatch_ok` 补 history 断言。

---

## 🟢 L2：update 输入卫生与 create 不一致（JSON null → "None"）—— ✅ 已修复

**位置**：`agent_go/web_server.py:1850-1851`

```python
fields = {k: str(body[k]) for k in
          ("title", "description", "repo", "cron", "spec_path") if k in body}
```

**问题**：create 路径（`web_server.py:1839-1842`）用 `str(body.get(x) or "").strip()`，JSON `null` → `""`，安全；update 路径用 `str(body[k])`，`str(None)` → `"None"`（实测确认）。对 implementation 卡发送 `{"repo": null}` 会通过 `_validate_repo`（`"None".strip()` 非空）存成字面量 `"None"`，直到派发时才发现路径不存在。

**修复方向**：统一为 `str(body.get(k) or "").strip()`；如需区分"未传"与"置空"，用 `if k in body and body[k] is not None`。

> **修复**（2026-08-16）：`_op_kanban_update` 改为 `if k in body and body[k] is not None` 才纳入（null = 未传，跳过），title/repo/cron/spec_path strip、description 保留原文；测试 `test_update_null_is_ignored` + `test_update_repo_stripped`。

---

## 🟢 L3：archive 布尔强转 —— ✅ 已修复

**位置**：`agent_go/web_server.py:1874`

```python
archived = bool(body.get("archived", True))
```

**问题**：`bool("false")` → `True`（实测确认）。客户端传字符串 `"false"` 时卡片反而被归档。前端始终传真布尔，故当前无实际触发路径，但 API 契约应按 JSON 布尔校验。

**修复方向**：`v = body.get("archived", True); if not isinstance(v, bool): 400`；或显式接受 `True`/`"true"` 并拒绝其它。

> **修复**（2026-08-16）：`_op_kanban_archive` 校验 `isinstance(archived, bool)`，否则 400；测试 `TestArchiveBool`（字符串拒绝 + 真布尔取消归档）。

---

## 备注（均已解决，随二期一并落地）

- **归档不可逆于 UI**：`api_kanban` 新增 `include_archived`（`GET /api/kanban?archived=1`），前端新增「🗂 已归档」开关 + 已归档卡片灰显（`.archived` 样式）+ 详情面板「♻️ 恢复」按钮（`archived:false`）。✅ 已解决
- **并发/多进程**：`kanban.py` 新增 `_interprocess_lock`（`kanban.lock` flock LOCK_EX，非 Unix 平台降级 no-op），所有 7 个 RMW 函数改为 `with _lock, _interprocess_lock():`，防止多实例（双 `agent_go web`）并发读-改-写互相覆盖。已实测：无锁 4×30 并发仅剩 51/120 张（丢更新 + JSON 损坏），有锁 4×15=60 全保留。✅ 已解决
- **性能**：`api_kanban` 任务状态改为 `_task_status_snapshot()` 按 meta 签名（`AGENT_GO_DIR\n` + `mtime:size`）缓存，任务无变化时复用，消除每请求全量 open+json.loads（O(tasks) 解析 → O(tasks) stat）。✅ 已解决
- **派发幂等性**：`_op_kanban_dispatch` 前置检查卡片最新任务，`EXECUTING/PLANNING` 或托管句柄存活 → 409 拒绝重复派发；前端联动禁用派发按钮。✅ 已解决

---

## ✅ 做得好的部分

- **正交性设计执行到位**：卡片只存 `task_ids[]` 软链接，`latest_task` 由 `api_kanban()` 从 meta.json 实时派生（`web_server.py:1198-1214`），卡片不冗余执行状态，`meta.json` 不被污染 —— 与设计文档"看板状态与执行状态正交"完全一致。
- **错误映射与设计文档一致**：参数错 400 / 卡片不存在 404 / 业务规则（KanbanError/ProfileError/TaskRunnerError）422，写路由统一在 `_route_write_api` except 链处理（`web_server.py:1579-1590`）。
- **安全细节**：`_CARD_ID_RE` 正则（`kanban.py:59`）防路径穿越/注入；`update_card` 字段白名单（`kanban.py:64`）；写端点全部 token 鉴权 + `web_audit.jsonl` 审计（`kanban.*`）；派发任务文本 4000 字符截断防超长 argv（`web_server.py:1910-1914`）；仅未派发过任务的卡片可物理删除（`kanban.py:300-301`）。
- **数据层工程**：mtime 缓存 + tmp/`os.replace` 原子写 + 锁串行化（`kanban.py:89-132`），仿 `models_registry` 模式；损坏文件回退空看板不阻断 web（防御性）。
- **测试覆盖**：数据层（校验/历史/缓存/原子写/损坏回退）+ 端点集成（鉴权 401 / 分组 / 归档隐藏 / latest_task 派生与 unknown 兜底 / dispatch 全链路 mock `start_run` / SSE 签名联动）共 47 项，结构清晰。

## 优先级建议

| 优先级 | 项 | 理由 |
|--------|-----|------|
| **建议尽快** | M1 | 幽灵任务对用户认知破坏大；改动小（新增原子 `dispatch_card`） |
| **可顺带** | L2、L3 | 输入卫生加固，各一两行 + 对应用例 |

## 建议补测（均已随修复落地）

- `update` 传 `{"repo": null}` → 断言不存成 `"None"`（L2 回归）✅ `test_update_null_is_ignored`
- `archive` 传 `"false"` 字符串 → 断言 400 或按契约行为（L3 回归）✅ `TestArchiveBool`
- dispatch 原子性：mock `start_run` 成功后 `link_task` 抛错 → 断言状态一致（M1 回归）✅ `TestDispatchCard`（数据层原子性）+ `test_dispatch_ok` history 断言
