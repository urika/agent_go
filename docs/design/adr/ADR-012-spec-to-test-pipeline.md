# ADR-012: Spec-to-Test 验收测试管线（需求+架构 → AI 起草 → 人审冻结 → 可执行验证）

## 状态

**Accepted（2026-10-05 落地；默认关，opt-in）**——草案由 swe-eval 侧对齐起草（EXP-12 轮 2 tdd 臂证据迁移），同日按本 ADR 四条护栏实现并测试（40 例新测试）。启用：`spec_test.enabled=true` 或 `agent_go run ... --accept-tests`。落地不改变默认行为。

## 背景

### 痛点根因

ISSUE-29（LLM 生成的验证命令语法错误误判正确代码）、ISSUE-31（验证沙箱与真实环境不一致）、ISSUE-40（schema 混淆致 0 子任务真空 DELIVERY_READY 假成功）——三者同根：**验证循环没有可信的 oracle**。现状基线不是"无测试"，是"误判的验证"（比没有更糟：假成功摧毁 Accepted Delivery 语义，误判杀死正确产出）。

### 证据输入（swe-eval EXP-12 轮 2，2026-10-05）

tdd 臂（验收契约 test_patch 预置可见）对 plain 臂的配对差异（r1 单遍，n=6 对，方向性信号）：

| 实例 | plain | tdd |
|---|---|---|
| 1a4644ff（深上下文） | 触顶 180min 有界中止 | resolved 14min |
| 42355d18（马拉松/硬位） | failed 14min | resolved 21min |
| 379058e1（边界） | failed（空 patch） | resolved 8min |
| 40ade1f8（题面误导+大新模块） | failed | failed（唯一未破格——信息缺口修复≠能力缺口修复） |

关键定性：tdd 破格 patch 的 `gold_identity=0.0`（与 gold 零重合、独立实现）——**测试给出的是验收标准而非实现方案**。诚实校准：以上是 gold test 上界；本 ADR 的产线草稿测试质量取决于人审，效应会打折，但对照现状基线（验证误判）方向不变。

## 决策（骨架——四条护栏为必须项，缺一不可）

1. **管线**：需求+架构设计 → LLM 起草验收测试（每任务一次，Plan 阶段）→ **人审并入现有 Plan 确认门**（不新增人工例外点）→ 冻结 → worker 子任务以"契约可见"条件实现 → verify 循环**重放冻结版测试**判定。
2. **护栏①（冻结先于执行）**：测试冻结先于 worker 启动；worker 对冻结测试**只读**，verify 前/评测前剥除 worker 对测试文件的改动并重放冻结版（防"改测试凑通过"；swe-eval 评测侧同款机制可移植）。
3. **护栏②（生成时机前移）**：验证命令/测试从 per-subtask 现场生成改为 per-task 预生成+人审——把 ISSUE-29 的暴露面从 N×retry 收缩到 1×人工审。
4. **护栏③（与语义评估分层）**：可执行冻结 oracle 优先于 `evaluator.enabled` 的 LLM 语义评估；后者降级为补充（覆盖不可执行面）。
5. **护栏④（生产/评测两口径）**：bench 模式下测试 oracle 独立性沿用 swe-eval 规则（**出题人≠解题人**，冻结件来自任务定义而非 solving agent）；生产交付模式下人审冻结即真值锚。

## 原因

对北极星三支柱：**工程闭环**（Accepted Delivery Rate 受验证可信度直接约束）；**智能闭环**（首次验证通过率——实证：同一任务无反馈回路烧穿 180 分钟、有验收测试 14 分钟解出）；**人机信任**（ISSUE-40 假成功即"虚假控制感"实例；人审冻结测试把虚假控制感换成真控制，且不新增人工例外点）。

## 约束

- 需求质量是上界：错误/误导需求经 AI 起草会被**冻结成更难纠正的错误测试**（40ade 教训）——人审门不可省。
- AI 起草有 happy-path 偏置：边界/对抗覆盖是人审增值点。
- 冻结测试的执行必须与 verify 同沙箱（ISSUE-31 教训直接适用）。
- 需求/架构文档缺位或过期的任务：管线降级为现状行为（不阻塞主交付链路）。

## 验收口径（预注册，拍板时钉定）

- 首次验证通过率提升；ISSUE-29/31 类验证误判率下降；
- `Cost per Accepted Delivery` 不升；`Human Intervention Minutes` 不增（以"并入 Plan 确认门"为前提）；
- 草稿质量度量：人审采纳率 / 编辑距离（作为拍板依据采集）。

## 整体流程与自动化 / 留痕边界

| # | 阶段 | 执行者 | 自动化 | 留痕件 | 事后可查（追溯） |
|---|---|---|---|---|---|
| 0 | 任务输入 | 人 / 上层 agent | — | Task Spec、`tasks.json`、`spec_snapshot.md` | `requirement_ids` / `acceptance_criteria_ids` |
| 1 | 起草验收测试 | LLM（`draft_role`，默认 planner 档位） | ✅ 自动（enabled 时，一次/任务） | `acceptance/DRAFT.json`（原始草稿 + `model`/`latency`/`cost_usd`/`drafted_at`） | 草稿全文、模型档位、起草成本（metering 差分）、起草时刻 |
| 2 | **人审**（护栏①前置） | **人**：CLI tty `[T]/[K]`／web 控制台确认卡片（编辑/跳过）／MCP 由宿主代人工 | ❌ **必须人审**（`require_review=true`） | `confirmation_decision.json`（`acceptance` 回执）+ DRAFT.json 保留原文 | 决策（approved/skipped）、编辑文件数、渠道（cli/web/mcp/provided）、采纳率、起草→冻结耗时 |
| 3 | 冻结 | 自动 | ✅ | `acceptance/manifest.json` + `files/`（逐文件 sha256） | 冻结哈希、`frozen_dir`、`reviewed`、冻结时刻 |
| 4 | 注入（先于 worker） | 自动（每子任务） | ✅ | worktree `<frozen_dir>/` + 冻结提交 | 注入文件清单 + commit（`result.json.acceptance`） |
| 5 | worker 实现（契约可见） | claude / 本地 backend | ✅ | TASK.md「验收测试（冻结·只读契约）」章节 | 契约是否随任务下发 |
| 6 | 重放验证（护栏①②③） | 自动 | ✅ | `verification_results`：`acceptance` / `acceptance_restore` / `semantic_advisory` | 每次尝试的命令与退出码、护栏①拦截次数与被恢复文件、advisory 判定理由 |
| 7 | 判定 / 修复重试 | 自动 | ✅ | `verify_state.json`、`verification_history` | 首次验证通过率、重试轮次、失败命令、修复后是否收敛 |
| 8 | 交付 | 自动 | ✅ | `meta.json`（`acceptance` 段 + 交付字段） | Cost per AD、Accepted Delivery 原因码 |
| 9 | **事后分析 / 校准** | 人 + 工具 | 半自动（数据已全，聚合待补） | 上述全部 + `metering.jsonl` | 四项预注册口径（见下）+ 误判复盘 |

**自动化边界（判据）**

- **可全自动**：内容生成（起草）与状态机推进（冻结→注入→重放→判定→重试）。每步 fail-open + 降级留痕（`degraded`/`DRAFT.json`），失败不阻塞主交付链。
- **必须人工**：冻结前的**采信决策**——ADR「约束」节：错误需求会被起草成更难纠正的错误测试（40ade 教训），人审是生产口径的真值锚。落地为「不新增人工停点」：审阅并入既有 Plan 确认门。
- **仅评测口径免人审**：`provided_dir` 用任务定义冻结件（出题人≠解题人），人审由"任务定义已冻结"这一事实替代。
- **可代理但不自动**：MCP 由宿主（Claude Code 等）征询其用户后代提交人审回执（`review_task(action=acceptance_review)`），留痕 `review_channel=mcp`。

**追溯入口（人 / agent 两用）**

- 人：web 控制台任务详情「验收测试」区（冻结状态、人审渠道与采纳率、起草→冻结留痕、文件全文、护栏①拦截与 advisory 运行结果）；CLI 场景直接看 `<task_dir>/acceptance/`。
- Agent：MCP Resource `agent_go://tasks/{task_id}/acceptance`（同一数据组装 `spec_test.task_acceptance_view`）；`review_task(action=acceptance)` 返回同视图。

**四项预注册口径 → 数据源**

| 口径 | 数据源 |
|---|---|
| 首次验证通过率 | `verification_results` 中 `type=acceptance` 且 `attempt=1` 的通过比例 |
| ISSUE-29/31 类误判率 | 验证命令 `rejected`/`exit_code=127` 计数（安全门禁拒绝、命令不可执行）+ 冻结 oracle 与语义评估冲突（`semantic_advisory`）复盘 |
| Cost per Accepted Delivery | `metering.jsonl`（含起草差分 `draft_cost_usd`）+ 既有 `eval` 成本口径 |
| 人审干预分钟数 | `drafted_at` → `frozen_at` 差值（合并进既有 Plan 确认门，故只计增量） |

**聚合命令（2026-10-05 落地）**：`agent_go eval acceptance [--window-days N] [--json]`——纯读 `~/.agent_go/task-*/`，一次出四项口径 + 队列对比（启用验收 vs 其余）与支持量（护栏①拦截、adoption、渠道分布）。实现=`metrics.compute_acceptance_metrics`。

**口径诚实性**：`draft_cost_usd` 依赖 metering 差分（起草发生在 Plan 阶段、无并发子任务，差分安全）；无 metering 通道时记 `None` + `draft_cost_source=unavailable`，聚合单列为「成本不可得任务数」，不冒充 0。

**独立性闸（MCP 代审）**：`spec_test.mcp_review`（默认 `allow`｜`deny`）。`deny` 时 `review_task(acceptance_review)` 直接拒收（结构化错误指引人工走 CLI/web），适用于对外交付/评测等要求独立性的场景；无论 allow/deny，回执都留 `review_actor`（`web:<token哈希8>` / `mcp:<token哈希8>` / `cli`），事后可归因「谁批的」——留痕能审计，但拦不住放宽策略下的自审自批，评测口径仍必须用 `provided_dir`。

## 开放问题（落地后收敛）

1. 冻结测试文件的落点与命名约定 → **已定**：canonical 冻结件在 `<task_dir>/acceptance/`（`files/` + `manifest.json` 含逐文件 sha256）；注入 worktree 的仓库内相对目录由 `spec_test.frozen_dir` 配置（默认 `tests/acceptance`），注入即先行提交进子任务 base（worker 只读契约）。
2. 起草调用的模型档位与 difficulty 路由关系 → **已定**：`spec_test.draft_role`（默认 `planner`，经 `router.resolve_role` 走既有角色路由；不按 difficulty 细分——起草质量受模型档位影响，先用 planner 档位采集采纳率/编辑距离再定）。
3. bench 模式是否接入 → **已定（机制层）**：`spec_test.provided_dir` 从任务定义冻结件（`manifest`/`commands.json`）取 oracle，`source="task"`、不经 LLM ⇒ 出题人≠解题人；bench 自带 verification 仍为权威，本管线不改变其判定。
4. 与 §8 扩展能力决策门其他项的优先序 → 未决（设计/roadmap 维护者定）；本 ADR 落地不阻塞其他项。

## 实现

（2026-10-05 落地，默认关）

| 面 | 位置 | 说明 |
|---|---|---|
| 模块 | `agent_go/spec_test.py` | 起草（LLM）→ 清洗（路径/命令安全面）→ 冻结（sha256 manifest）→ 注入 → 重放 → meta 段；全链 fail-open |
| 起草+人审 | `cli.py` `_prepare_acceptance_draft` / `_freeze_acceptance_after_review`；`ui.py` `confirm_plan(acceptance=...)` + `_review_acceptance_draft_interactive` | 草稿随 Plan 确认门展示（[T] 逐个编辑 [K] 跳过 [B] 返回）；Y=按草稿冻结（reviewed=True）；不新增人工停点 |
| 起草+人审（web） | `web_confirm.py`（payload 带 `_acceptance_draft`；决策回执带 `acceptance`）／`web_ops.py` `_op_confirm`（回执校验，非法 400 不落盘）／`cli.py` `_confirm_plan_channel`（回执合并进草稿）／`web_frontend.py` `renderAcceptanceDraft`（每个文件可展开编辑 + 跳过勾选） | web 确认门与 CLI 门**同审同冻结**：`--confirm-mode web` 不再是"无人审"降级路径；回执非法或不带 → 按未人审处理（require_review 降级） |
| 只读观察面（web） | `web_data.py` `api_task_acceptance` + `_acceptance_summary`；`web_handler.py` `GET /api/tasks/<id>/acceptance`；`web_frontend.py` `renderAcceptancePanel` | 任务详情页「验收测试」区：冻结状态/来源/人审/sha256/命令 + 文件全文 + 运行结果（验收通过与否、护栏①拦截恢复、advisory 语义评估）；列表加冻结/降级标识。仅读，viewer 角色可用 |
| 护栏① | `executor.py` `run_subtask`（启动前注入+提交）+ `_verify_changes`（提交前/每轮重放前/修复提交前恢复） | 恢复以 canonical 冻结件为准（内容比对，幂等）；注入提交从"worker 自提交"判定中排除（防空转误判 completed） |
| 护栏② | `executor.py` `_verify_changes`（验收命令合入 `cmds`，`type=acceptance`） | 复用既有安全门禁/沙箱/超时/失败回修链 |
| 护栏③ | `executor.py` 语义评估分支（`spec_test.oracle_priority`，默认 true） | oracle 通过 ⇒ 语义评估失败降级 `semantic_advisory`（不阻断）；oracle 未跑/未过 ⇒ 原判定不变 |
| 护栏④ | `spec_test.freeze_from_provided` + `spec_test.provided_dir` | 评测口径：冻结件来自任务定义，不调 LLM |
| 配置 | `config.py` `spec_test` 段 + `config.example.json` | enabled/frozen_dir/require_review/draft_role/provided_dir/oracle_priority/max_files/max_file_bytes/max_commands |
| CLI | `--accept-tests` / `--no-accept-tests` | 覆盖 config；`--yes`/headless + `require_review=true` ⇒ 自动降级（留档草稿，不启用 oracle） |
| 测试 | `tests/test_spec_test.py`（40 例） | 含真实 git + 真实 pytest 子进程的集成用例（注入/重放/验收命令执行/护栏①拦截）、CLI 决策路径、安全清洗 |

未接入（登记为后续）：e2e 模式起草（无 Plan 确认门）；验收指标的自动采集（采纳率/编辑距离需人审数据积累；web 面已提供人审回执字段 `edits` 可先行采集）。

### 端到端冒烟（真链路）

`tools/spec_test_smoke.py`：真 CLI + 真 worker + 真 verify 重放（与 `tests/` 的离线集成互补）。

| 口径 | 说明 | 本地引擎占用 |
|---|---|---|
| `--mode provided` | 评测口径：provided_dir 冻结件；零 planner/draft LLM 调用 | 不占（worker 走 claude CLI 自身端点） |
| `--mode draft-web` | 全链路：真 LLM 起草 + web 确认门人审（脚本代人工提交回执，可附编辑验证回执链路） | 占（plan+draft 两次调用） |
| `--mode draft-unreviewed` | 真起草 + `require_review=false` | 占（同上） |

安全闸：draft* 模式检测外部批次锁（swe-eval 等 `.batch.lock`）并**默认拒绝**（`--allow-engine-share` 放行），
避免与他人评测批次争用同一本地引擎。结果落档 `eval_suite/spec_test_smoke/results.jsonl`。
断言=四护栏 + 追溯：冻结件在交付 commit 内且 sha256 一致、验收命令来自冻结件、首次尝试即通过、
worker 未动测试（commit 只含实现文件）、聚合命令能数到本任务、人审编辑随冻结件入 commit（draft-web）。

**首轮实录（2026-10-05，provided）**：PASS 12/12，1.21 min，`DELIVERY_READY`；独立复核交付 commit
（`a59795f`：只改 `calc/__init__.py`，冻结件哈希一致，实现 `return a + b`）。该轮抓到两个真 bug 并已修：
① `meta.acceptance` 缺 `frozen` 键（web 列表标识与 `eval acceptance` 队列归类失真）；
② 聚合把评测口径 `source=task` 误归 `drafted`。`draft-web` 口径待批期窗口执行（会占本地引擎）。

跨仓对齐：swe-eval 评测侧孪生（受控信息供给/任务定义冻结纪律）；EXP-12 轮 2 H2 判读件为验收口径的终稿证据源。
