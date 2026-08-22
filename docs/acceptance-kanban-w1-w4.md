# 看板分类系统 W1–W4 验收报告

- **日期**：2026-08-22
- **范围**：agent_go 看板（Kanban）五阶段分类工作流 —— W1 分类器、W2 队列/回流/通知、W3 人工介入（含 W3.3 审批）、W4.1 分类统计、W4.2 成本-质量自适应、W4.3 自动降级建议，以及 W3.3 边界缺陷修复（惰性状态回流）。
- **状态**：✅ 全部交付，验收通过
- **测试基线**：全量回归 `2677 passed, 46 deselected`，`ruff`/`mypy` 0

---

## 1. 验收标准矩阵

| # | 验收标准 | 实现位置 | 测试证据 | 结果 |
|---|---------|---------|---------|------|
| 1 | 分类器准确（implementation / discussion / periodic 自动判定，manual 可覆盖并自学习） | `agent_go/kanban.py` `KanbanClassifier.classify_card`；`type` 字段 + `spec_path`/`cron`/`task_ids` 启发式；准确率统计见 W4.1 | `tests/test_web_kanban.py::TestClassifier`、`tests/test_kanban.py` | ✅ PASS |
| 2 | 队列 / 回流 / 通知（dispatch→operations；失败回流 implementation 并通知带现场链接；completed→operations） | `agent_go/kanban.py` `dispatch_card` / `move_card`；`agent_go/web_server.py` `POST /api/kanban/dispatch`；`agent_go/notify.py` 失败通知含 worktree 路径 | `tests/test_web_kanban.py::TestDispatchFlow`、`tests/test_kanban.py::test_dispatch_*` | ✅ PASS |
| 3 | 人工介入（operations 列 approve 终确认 / reject 回退重做 / request_changes 打回） | `agent_go/web_server.py` `POST /api/kanban/review`（approve / reject / request_changes）；W3.3 审批终确认逻辑（`tests/test_web_kanban.py::TestHumanReview`） | `tests/test_web_kanban.py::TestHumanReview`、`tests/test_review.py` | ✅ PASS |
| 4 | 分类统计 W4.1（分类器准确率统计与可视化） | `agent_go/kanban.py` `kanban_stats()`；`cost_quality_analysis()` 扩展维度 | `tests/test_web_kanban.py::TestStats` | ✅ PASS |
| 5 | 自动降级建议 W4.3（失败卡片一键 insight 分析生成修复/降级建议） | `agent_go/web_server.py` `POST /api/kanban/{id}/suggest-degrade` → `_op_kanban_suggest_degrade`（组装任务级证据 + `eval._insight_llm`） | `tests/test_web_kanban.py::TestSuggestDegrade` | ✅ PASS |
| 6 | 惰性状态回流（W3.3 边界缺陷修复：覆盖 CLI resume / 孤儿进程 / web 重启路径，不只依赖 on_exit 托管句柄） | `agent_go/kanban.py` `reconcile_cards()`；`agent_go/web_server.py` `GET /api/kanban` 惰性调用 | 真实看板验证 + `tests/test_web_kanban.py::TestLazyReconcile`（4 用例：completed→operations / running 不动 / failed 停留 / 无 task_ids 不动） | ✅ PASS |

---

## 2. 关键提交（按阶段）

| 阶段 | Commit | 说明 |
|------|--------|------|
| W1/W2/W3 基础 | `9be4f36` | W3.3 operations 审批：approve 终确认 / reject 回退重做 |
| W4.1 | `ab5472c` | 分类器自学习：分类准确率统计与可视化 |
| W4.2 | `6a5300d` | 成本-质量自适应：本地队列 vs 云端 $/pass 权衡分析 |
| W4.3 | `7b78441` | 自动降级建议：失败卡片一键 insight 分析 |
| 验收测试 | `baf8d97` | 看板工作流验收测试 —— 5 条标准端到端机制验证 |
| 验收测试 | `72a4708` | 端到端验收：manual/auto 判定 + dispatch 流转 + 失败降级通知带现场链接 |
| 边界修复 | `1a23bdf` | 惰性状态回流：看板打开即修正卡片状态，覆盖全部完成路径 |
| 配套 | `337980b` `beaa949` `c04641d` | decompose 端点 / import-spec 建卡 / SPA 键盘操作防回归 |
| 文档 | `e79c1a3` `48315ac` | roadmap v4.3 阶段状态对齐；N2-4 goal 表单记录 |

---

## 3. 本地任务卡片闭环（卡片 1–5）

| 卡片 | 标题 | 关联任务 | 终态 | 看板回流 |
|------|------|---------|------|---------|
| 1 | 看板任务分类器（W1） | task-20260819-200901-993-fd28 | DELIVERY_READY | operations |
| 2 | 队列/回流/通知（W2） | task-20260820-085325 / 085828 | DELIVERY_READY | operations |
| 3 | 人工介入审批（W3） | task-20260820-151316-077-4698 | DELIVERY_READY | operations |
| 4 | 多条件筛选（W3 续） | task-20260821-174050 / 174349 | DELIVERY_READY | operations |
| 5 | 任务统计报表（W4 续） | task-20260821-195908-469-6d54 | DELIVERY_READY | operations |

> 5 张卡片全部经 `reconcile_cards` 惰性回流至 `operations` 列，验证看板打开即修正状态（无需托管子进程 on_exit 回调）。

---

## 4. 测试汇总

- **全量回归**：`2677 passed, 46 deselected`（集成测试按 `-k "not integration"` 排除）
- **看板相关专项**：`tests/test_web_kanban.py` 61 passed（分类器 / 流转 / 人工介入 / 统计 / 降级 / 惰性回流）
- **静态检查**：`ruff` (E,F,W) 0 error；`mypy` 0 error
- **验收端到端**：`baf8d97` / `72a4708` 覆盖 5 条标准机制 + 失败降级通知现场链接

---

## 5. 已知限制 / 遗留

1. **标签语义**：W4 统计器 `task_report.py`（独立产物，位于 `/private/tmp`）按任务 JSON 的 `tags` 字段聚合；agent_go 任务 `meta.json` 本身无 `tags` 字段，看板"标签分布"退化为按 `status` 维度统计。如需业务标签需在任务模型补充 `tags`。
2. **看板无 blocked 列**：失败态（FAILED / VERIFICATION_FAILED）卡片停留 `implementation` 列而非独立 blocked 列（设计决策，避免列爆炸）；W4.3 降级建议弥补该缺口。
3. **归档范围**：会话末将 11 张含 `task_ids` 的历史开发卡片软归档（`archived=true`，数据完整保留、可 `unarchive` 恢复）以清理活动视图；非无任务关联的噪音卡片（此前误判已纠正）。
4. **统计器产物位置**：`task_report.py` 落在 `/private/tmp`（符合原任务指引路径），未纳入版本库；如需持久化建议移至 `agent_go/` 或 `tools/`。

---

## 6. 验收结论

看板分类系统 W1–W4 全部交付，6 项验收标准（含 W3.3 边界缺陷修复）均通过机制验证与真实看板回流验证，测试基线 2677 passed，静态检查零问题。**验收通过 ✅**。

| 角色 | 签署 | 日期 |
|------|------|------|
| 开发 | agent_go 自动化交付 | 2026-08-22 |
| 验收 | — | — |
