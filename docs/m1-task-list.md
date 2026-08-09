# M1 交付闭环任务清单

> 阶段：M1 交付闭环
> 状态：进行中（M1-1/M1-2/M1-3 已实现 + E2E 验证，2026-08-09）
> 更新日期：2026-08-09
> 关联：[roadmap.md](roadmap.md) §5 · [prd.md](prd.md) §M1 · [delivery-design.md](design/delivery-design.md)

## 1. M1 目标

解决“代码做出来但没有可靠到达用户目标分支”的最高优先级问题——让用户拿到**完整、正确、可合并的 PR**。

```text
M0 之后
  -> M1 交付闭环（当前）
  -> M2 核心可靠性
  -> M3 真实任务验证
```

M1 完成后，`accepted_delivery_rate` 才能从当前 0 提升到有效值。

## 2. 当前状态基线

| 能力 | 现状 | 缺口 |
|---|---|---|
| `base_commit`/`base_branch`/`target_branch` 记录 | ✅ 已实现（cli.py:724-750） | 无 |
| Accepted Delivery 判定 | ✅ 已实现（delivery.py `evaluate_accepted_delivery`） | 依赖 M1 交付字段 |
| `delivery_branch` 创建与 commit 汇总 | ✅ 已实现（M1-1，delivery.py `create_delivery_branch`） | 需多子任务 E2E 验证 |
| `cmd_pr` | ✅ 已修复（M1-2：推 delivery branch，--head/--base 显式指定） | 需 E2E 验证 |
| 显式 merge 命令 | ✅ 已实现（M1-2，`cmd_merge`） | 需 E2E 验证 |
| 交付失败重试 | ✅ 已实现（PR/merge 失败标 delivery_failed，可从 delivery branch 重试） | 需 E2E 验证 |
| 交付状态与恢复 | ⚠️ 部分（recover 已用 base_commit） | 三层状态分离 |
| traceability / 架构审查 | ❌ | M1.4 全新 |

## 3. P0 必须完成（M1.1-M1.3）

### M1-1 交付分支模型（M1.1）

- [x] 创建 `delivery_branch`：命名规则 `agent_go/{task_id}/delivery`，从 `base_commit` 分支。
- [x] pipeline 完成后将所有成功子任务的 commit 汇总到 delivery branch（`git merge --no-ff`，临时 worktree 隔离）。
- [x] 上游 artifact merge（子任务间 tag merge）与最终交付 merge（delivery branch 聚合）语义分离。
- [x] 验证：非 `main` 默认分支（master/develop）端到端正确。（E2E 通过，develop 分支）
- [x] 验证：不依赖提交时间窗口判断 worker 是否产生 commit（用 base_commit 对比）。

**交付物**：`delivery_branch` 创建 + 聚合逻辑；`meta.delivery_branch` 正确填充。

**验收**：
- [x] 单子任务真实 Git 仓库端到端通过。（E2E 通过）
- [x] 多子任务依赖链真实 Git 仓库端到端通过。（E2E 通过 2026-08-09）
- [x] 非 `main` 默认分支可以正确执行。（E2E 通过，develop 分支）
- [x] 上游 artifact merge 和最终交付 merge 语义分离。

### M1-2 PR 交付（M1.2）

- [x] 修复 `cmd_pr` 误推 bug：`git push HEAD:{base_branch}` → `git push {remote} {delivery_branch}:{delivery_branch}`（禁止推 HEAD 到 base）。
- [x] `gh pr create` 增加 `--head {delivery_branch} --base {base_branch}` 明确 head/base。
- [x] `pr_url`、`pr_head`、`pr_base` 写入 meta。
- [x] PR 创建失败归类为 `delivery_failure`（写 `delivery_failed=true`，不能报告 completed）。
- [x] 新增 `agent_go merge <task-id>` 显式交付命令（本地 merge 到 target branch 或推送）。
- [x] 交付失败时从 delivery branch 重试，不需要重新执行 Claude。

**交付物**：修复后的 `cmd_pr` + `cmd_merge`；PR 失败独立标记。

**验收**：
- [x] 生成的 PR head/base 正确。（E2E 通过：pr_head=delivery, pr_base=main/develop）
- [ ] PR 包含全部已接受子任务变更（需真实远程 PR 验证）。
- [x] 交付失败时可以从 delivery branch 重试，不重跑 Claude。

### M1-3 交付状态与恢复（M1.3）

- [x] commit / verification / delivery 三层状态分离（在 `meta` 中明确区分）。
- [x] recover 使用 `base_commit` + `commit_hash` 判定（已部分实现，补齐 delivery 维度）。
- [x] 已提交但未验证的任务 → `committed_unverified`（subtask 级），不得直接进入下游。
- [x] recover/resume 使用 task lock（已实现，验证与 delivery 操作兼容）。

**交付物**：三层状态分离 + recover 交付维度判定。

**验收**：
- [x] SIGTERM、SIGKILL、PR 创建失败、merge 冲突等场景均可区分。（E2E 通过：PR 失败→DELIVERY_FAILED，merge 冲突→DELIVERY_FAILED+现场保留）
- [x] recover 不会破坏运行中的 task。
- [x] resume 不会重复提交或混入旧 worktree 改动。

## 4. P1 按需完成（M1.4 SDD 最小治理闭环）

> 仅建设最小可追踪和可审查闭环，不建设完整 KnowledgeStore、活文档或自动架构决策。

### M1-4 Spec 追踪

- [ ] 为 Spec requirement 和 acceptance criterion 分配稳定 ID。
- [ ] Plan step、subtask、verification 和 delivery record 支持引用 requirement ID。

### M1-5 架构审查

- [ ] 执行前生成最小 Architecture Decision（边界、依赖方向、关键约束）。
- [ ] Architecture Review 产生 `approved` / `rejected` / `changes_requested` 决策。
- [ ] 未通过的架构审查不得进入执行，除非用户明确覆盖并留下审计记录。

### M1-6 追踪输出

- [ ] 生成任务级 `traceability_matrix` 和 `architecture_compliance` 摘要。
- [ ] 缺少 requirement/acceptance criterion 映射的任务标记为追踪不完整，而不是静默通过。
- [ ] 架构审查结果持久化，并在 CLI、MCP 和报告中可见。

**验收**：
- [ ] 一个真实任务可以从 requirement 追踪到 Plan、测试和 PR。
- [ ] 架构审查结果持久化到任务产物，CLI/MCP/报告可见。
- [ ] 该能力不自动替代人工做复杂架构决策。

## 5. P2 测试与文档

### M1-7 端到端测试

- [x] 真实 Git 仓库单子任务 E2E：delivery branch 创建 → PR head/base 正确。（E2E 通过）
- [x] 真实 Git 仓库多子任务依赖链 E2E：commit 汇总 → PR 完整。（E2E 通过 2026-08-09：3 子任务依赖链 sub-1→sub-2→sub-3，delivery branch 汇总全部 3 commit，cmd_merge 合并到 main）
- [x] 非 `main` base branch（master/develop）E2E。（E2E 通过，develop）
- [x] PR 创建失败 → `delivery_failed` → 从 delivery branch 重试成功。（E2E 通过）
- [x] merge 冲突 → 明确阻断并保留现场（worktree 保留）。（E2E 通过：DELIVERY_FAILED+现场保留）
- [x] SIGTERM / SIGKILL 中断后 recover/resume 不重复提交。（E2E 通过）

### M1-8 单元测试

- [x] delivery branch 创建/聚合逻辑单测（mock git）。（test_delivery.py）
- [x] `cmd_pr` head/base 逻辑单测（禁止 HEAD→base 误推）。（test_delivery_cli.py）
- [x] `cmd_merge` 交付命令单测。（test_delivery_cli.py）
- [x] 交付失败状态分类单测（`delivery_failure`）。（test_cli_commands.py）

### M1-9 文档同步

- [x] 更新 `delivery-design.md`：记录实际实现（delivery branch 命名/聚合/merge 命令）。
- [ ] 更新 `architecture.md`：交付数据流 + `meta` 字段。（待补）
- [x] 更新 `spec.md`：`cmd_pr`/`cmd_merge`/delivery 接口签名。
- [x] 更新 `module-catalog.md`：delivery.py 职责。
- [ ] 更新 `m0-accepted-delivery-contract.md`：M1 后判定条件实际生效说明。（待补）
- [ ] 更新 runbook：交付失败排查流程。（待补）

## 6. M1 完成门禁

- [x] 单子任务真实 Git 仓库端到端通过（delivery branch + PR）。（E2E 通过）
- [x] 多子任务依赖链真实 Git 仓库端到端通过（commit 汇总 + PR 完整）。（E2E 通过 2026-08-09）
- [x] 非 `main` 默认分支可正确执行。（E2E 通过，develop 分支）
- [ ] PR head/base 正确，包含全部已接受子任务变更。（需真实远程 PR 验证）
- [x] 交付失败可从 delivery branch 重试，不重跑 Claude。
- [x] SIGTERM/SIGKILL/PR 失败/merge 冲突可区分；recover 不破坏运行中任务。（E2E 通过）
- [x] 全量测试通过。（1948 tests，CI green）
- [ ] 在 smoke suite 上重新生成基线，`accepted_delivery_rate` 由 0 变为有效值。

## 7. 依赖与顺序

```text
M1-1 交付分支模型（必须先，所有后续依赖 delivery_branch）
  -> M1-2 PR 交付（依赖 M1-1）
  -> M1-3 交付状态与恢复（可与 M1-2 并行）
  -> M1-4/M1-5/M1-6 SDD 治理（可最后）
  -> M1-7/M1-8 测试（每完成一个 P0 任务即补测试）
  -> M1-9 文档同步（随实现更新）
```

> **建议**：M1-1 与 M1-2 是核心交付能力（P0），M1-4~M1-6 是 SDD 治理（P1），可在交付闭环稳定后按需推进。
