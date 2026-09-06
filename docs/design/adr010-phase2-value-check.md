# ADR-010 阶段 2 价值验证评估（2026-09-06）

> 门禁依据：ADR-010「阶段 2 必须由阶段 1 的价值验证背书」。
> 评估问题：轨迹（trajectory）能否提供传统工件缺失的失败归因因果信息？
> 案例来源：opencode/Zen 免费臂 golden 6×1 批量（adr010-p2-oczen-20260906，
> mimo-v2.5-free，$0）+ dsh 臂 golden 批量（2026-09-05）的边界失败。

## 结论（先行）

**阶段 1 价值验证：通过。** 轨迹在两个 worker 级失败案例中提供了传统工件完全
缺失的决定性因果信息，并直接促成 ISSUE-58（PWD 泄漏，P1）的根因确认与修复。

**阶段 2：背书启动，但缩小切入面。** 优先做 TaskEvent 骨架事件覆盖规划级归因
盲区（边界案例证明这是轨迹覆盖不到的部分）；meta.json 投影化维持 ADR 原节奏
（双写一个版本周期后切换）。

## 评估协议

每个案例两组对比：A 组 = 传统工件（execution.log + meta.json + result.json +
metering.jsonl）；B 组 = A 组 + `trajectory/<sub>.jsonl`。判定标准：轨迹是否
提供 A 组缺失的**因果信息**（哪一步开始错、模型实际操作了什么），而非同义重复。

## 案例 1：implement-done-command（task-20260906-120214-027-a875）— worker 级失败

- **A 组可见**：子任务结束 `no_changes`；shell 验证通过；语义评估 fail
  （「未检测到文件变更」）；「收到 SIGTERM，跳过第 1 次修复」。归因止步于
  「模型没写代码」——**为什么没写，完全不可见**：模型没干活？写到别处？
  worktree 挂载错？三种假设无法区分。
- **B 组（轨迹 139 事件）**：第一个 tool/call 起全部操作
  `/Users/jinsongwang/workspace/agent_go/` 绝对路径——read 主仓库 src/storage.py
  （不存在，error）→ glob 探索 → seq 57 把 fixture 实现写进**主仓库根 src/**、
  seq 102/107 edit 同一文件 → 自验「通过」→ stop。worktree 全程零改动。
  根因链完整：**PWD 泄漏**（父进程 cwd=主仓库，`PWD` 经 env 遗传进 opencode
  子进程，被当作项目根暴露给弱模型）。
- **差值**：从「不知道为什么没改动」到「定位到环境变量级根因 + 可复现证据」，
  直接促成 `executor._backend_env` 修复（PWD 统一改写为 worktree）。

## 案例 2：security-hardening-taskmgr（task-20260906-120803-593-ce40）— infra kill + 重试

- **A 组可见**：kill_reason=infra、verification_failure、重试 1 次后仍 0/1。
- **B 组（轨迹 151 事件）**：同一 PWD 泄漏模式——worker 直接 edit
  `eval_suite/fixtures/task-mgr/src/`（fixture 主仓库）。这同时解释了 B6
  （2026-09-05）以来反复记为「snapshot 污染」的 fixture 写入：snapshot:false
  早已禁用影子仓库写回，真正通道是绝对路径直写。
- **差值**：把跨批次的「fixture 污染」悬案归因到同一根因。

## 案例 3：conditional-branching-datapipeline（task-20260906-120849-249-5893）— 重试→恢复

- 正面案例：验证失败 → 重试 1/5 → 最终 pass。轨迹 133 事件可复盘首次失败步骤。
- **暴露的设计缺口**：重试会**覆盖**同名轨迹文件（`<sub>.jsonl` 每 attempt 重写），
  只能看到最后一次 attempt 的轨迹。失败 attempt 的轨迹即最有价值的部分却丢失——
  与 dsh「无损失败 attempt」原则相悖。阶段 2/3 应考虑 per-attempt 命名
  （`<sub>.attempt-N.jsonl`）。

## 边界案例：dsh 臂 plan_gate_blocked（add-simple-caching r1，2026-09-05）

- worker 未执行（planner 臆测 API 被规划门拦下）→ **无轨迹产生**。规划级失败的
  归因（哪个 gate、哪条规则、planner 输出是什么）在轨迹覆盖范围之外，需要平台
  编排事件——这正是阶段 2 TaskEvent 词汇（plan/decompose/subtask_start/…）的
  正面动机：轨迹补执行级，TaskEvent 补编排级。

## 附带异常（登记，不在本评估下结论）

- 批量中 add-format-helper / fix-missing-default 两条结果的 task_dir 指向
  **2026-09-05 的旧任务目录**（task-20260905-153438-689-*，kill_reason=
  cleanup_race、failure_class=timeout 但 binary_pass=True）——疑似 bench 复用了
  昨日任务目录，且 fixture 当时已被污染（正确实现可能已存在），pass 真实性存疑。
  需复查 bench 任务目录分配/去重逻辑。

## 判定与后续

| 项 | 判定 |
|---|---|
| 阶段 1 轨迹采集价值 | ✅ 通过（决定性因果信息，促成 P1 修复） |
| 阶段 2 启动 | ✅ 背书通过——切入面收窄为 TaskEvent 骨架事件（规划级归因盲区） |
| meta.json 投影化 | 维持 ADR 节奏（双写一个版本周期），不提前 |
| per-attempt 轨迹命名 | 列入阶段 2/3 待办（失败 attempt 轨迹丢失问题） |
| bench 目录复用异常 | 登记待查（见上） |
