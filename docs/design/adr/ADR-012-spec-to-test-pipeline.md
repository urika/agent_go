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

## 开放问题（拍板前须答）

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

跨仓对齐：swe-eval 评测侧孪生（受控信息供给/任务定义冻结纪律）；EXP-12 轮 2 H2 判读件为验收口径的终稿证据源。
