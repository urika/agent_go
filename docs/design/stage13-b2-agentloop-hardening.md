# 阶段十三 B2：AgentLoop 能力补齐 + 修复路径 Backend 分发（B0 并入）

> 状态：已实现（2026-09-03）
> 关联：roadmap §7.14（B0 并入 B2 的决策记录）、PRD §4.6 F-BACKEND-1/2
> 代码：`agent_go/backends/dispatch.py`、`agent_go/backends/base.py`、`agent_go/tool_executor.py`、`agent_go/agent_loop.py`、`agent_go/executor.py`

## 1. 背景与决策

- **B0 并入 B2**（2026-09-03）：B1（标准 backend 接口，73cfcea）落地证明 backend 抽象不依赖 executor.py 全量拆分。executor 侧真正需要拆的是三条修复执行路径（fix/replan/reload）——它们恰好是 B2「修复循环纳入 backend 分发」要改的代码，拆与改一次完成，避免两轮扰动同一区域。`web_server.py` 拆分不随本阶段，挂到下一个 Web 需求触发（NFR-8）。
- **目标**：AgentLoop 从「只能跑简单任务的实验路径」补齐到「可独立承担简单任务全生命周期（执行→修复）」的最小可信能力集，为 B5 bench A/B 提供可测对象。

## 2. 修复路径 Backend 分发（B0 部分）

### 2.1 问题

B1 只迁移了初始执行路径；`_verify_changes` 内三处修复执行仍直调 `subtask._run_headless`：

| 位置（迁移前） | 用途 |
|---|---|
| `_try_replan` reload 分支 | AG-4/5 带恢复上下文重试 |
| `_try_replan` split 分支 | 局部重规划拆分修复执行 |
| `_verify_changes` fix 循环 | 验证失败修复重试 |

后果：agent_loop 执行的子任务一旦进入修复就静默退回 claude（混搭），且三处超时计算逻辑重复。

### 2.2 设计

新增 `agent_go/backends/dispatch.py`：

- `repair_timeout(cfg, difficulty, env)`：三处内联超时逻辑收口（retry_timeout × 难度倍数封顶 600/900/1500，本地模型 ×2 封顶 3000），数值与迁移前逐一相等。
- `run_repair(ctx, is_simple)`：与初始执行同一解析策略（`resolve_backend_name`）；agent_loop 异常回退 claude 并 warning。修复路径**不重复**初始路径的 worktree reset——修复后必经 git add/commit，由 executor 完成边界兜底。

关键约束：

- **`tag_name` 强制留空**：AgentLoop 拿到非空 tag_name 会自行 git add/commit/tag；修复路径的完成边界（commit/tag、nothing-to-commit 合法态）由 executor 修复循环独占。
- **`progress=False`**：`BackendContext` 新增开关，ClaudeBackend headless 路径不起 ticker 线程、不打印结束行——保持修复路径控制台安静的既有行为。
- `backend_ctx` 模板复用：run_subtask 把初始执行的 `BackendContext` 传入 `_verify_changes`，三处修复用 `dataclasses.replace` 派生（换 prompt/sub_id/timeout/progress）；直接调用 `_verify_changes` 的场景（测试）缺省重建等价上下文。
- **claude 默认路径行为零变化**：同超时、同 kill_reason 捕获、同 commit/tag 逻辑、同控制台输出。

## 3. AgentLoop 能力补齐（B2 本体）

### 3.1 ACI 工具集（tool_executor.py）

新增三个导航工具，向 SWE-agent ACI / Claude Code 工具面看齐：

- `Grep(pattern, path?, glob?)`：正则内容搜索，返回 `相对路径:行号: 内容`，跳过 `.git`/`node_modules` 等目录，200 条截断。
- `Glob(pattern)`：文件名 glob 列出，同样跳过依赖目录，200 条截断。
- `View(file_path, offset?, limit?)`：长文件按行号范围读取，返回带行号内容（SWE-agent `view` 语义）。

### 3.2 explore 只读模式

- `READONLY_TOOLS = {Read, Grep, Glob, View, Bash}`（Bash 自身已禁写入/删除/网络）。
- `ToolRegistry.definitions(readonly=True)` 只暴露只读工具；`execute(readonly=True)` 对写工具直接拒绝——**双保险**：LLM 看不到写工具，强行调用也被拦。
- 触发：subtask 级 `readonly: true`，经 `BackendContext.extra` 透传，默认关。

### 3.3 stuck 检测（agent_loop.py）

- 签名 =（工具名 + 参数 JSON），连续重复计数。
- 达阈值（`agent_loop.stuck_repeat_threshold`，默认 3）→ 注入一条提醒消息（一次机会）。
- 提醒后仍重复 → 判定卡死：`exit_code=1` 终止，计量事件带 `stuck_detected=true`。
- 终态判定仍归 wrapper 验证循环——stuck 只是提前止损，不替代验证。

### 3.4 no-progress 信号

连续 `agent_loop.no_progress_turns`（默认 8）轮无成功 Write/Edit → 记 warning + 计量 `no_progress=true`。**只记信号不终止**（只读探索类任务合法地不写文件）。

### 3.5 scope advisory

subtask `files_hint` 经 `BackendContext.extra` 透传为 `scope_hint`；Write/Edit 成功但路径不在声明范围内 → 工具结果追加 advisory 警告 + 日志。**advisory 不硬阻断**（planner 的 files_hint 是提示性声明，硬阻断会误伤合理越界）；阻断级 scope 判定仍由 wrapper 侧 `classify_verification_scope` 负责。

### 3.6 显式不做

- 不在 AgentLoop 内重复实现 wrapper 级验证（shell/语义评估是 `_verify_changes` 职责，backend 无关）。
- 不改变默认路径：所有增强仅在 `agent_loop.enabled=true` 时生效；`readonly`/`files_hint` 默认关/空。
- 不为 agent_loop 修复路径做 worktree reset（见 §2.2）。

## 4. 配置面（config.example.json 已同步）

```json
"agent_loop": {
  "stuck_repeat_threshold": 3,
  "no_progress_turns": 8
}
```

## 5. 验收状态

- 新增/更新测试：`test_backends.py`（dispatch 分发/容错/timeout/progress）、`test_tool_executor.py`（Grep/Glob/View/只读）、`test_agent_loop.py`（stuck/no-progress/只读/scope）。
- 既有双 patch 测试回退为单 patch（修复循环现走 backend 分发，`agent_go.executor._run_headless` 已删除）。
- 全量 pytest 通过；~~B5 bench A/B（通过率/成本证据）是本阶段 accepted 的剩余门槛~~ → 已于 2026-09-05 执行（T11），结果见 §6。

## 6. B2 bench A/B：AgentLoop vs Claude Code（T11，2026-09-05）

口径完全复用 B5：golden 6 任务 × repeat 2，glm-5.3-flash 同端点（`api.z.ai/api/anthropic`，
bench `--bench-endpoint` 注入 planner/evaluator/worker），两臂只剩 backend 一个变量；
AgentLoop 臂经 `--worker-backend agent_loop` 显式分发（含修复路径）。
对照臂复用 B5 claude 臂数据（`results_b5_claude_glm_20260904.jsonl`，09-04 同口径产出）。

- 结果文件：`eval_suite/results_b2_agentloop_glm_20260905.jsonl`（+ `.proxy_context.json` 口径快照）；
  补跑：`eval_suite/rerun_t11_agentloop/results_rerun.jsonl`，经
  `tools/recompute_bench_results.py --merge --apply` 替换 4 条（备份 `.bak_prerecompute`）。

| 指标 | claude 臂（B5） | agent_loop 臂（T11） |
|---|---|---|
| pass_rate | 10/12（83%） | **8/12（67%）**；首跑原始 4/12（33%） |
| first-pass | 10/12 | 8/12 |
| easy 子集 | 4/4 | 4/4 |
| medium 子集 | 4/4 | 3/4 |
| hard 子集 | 2/4 | 1/4 |
| elapsed 均值/中位 | 488s / 408s | 537s / 513s |
| lint 均值 | 6.7 | 4.5 |
| 语义评估通过 | 9/11 | 6/7 |
| token 归一化成本¹ | $0.2283（$/pass $0.0228） | $0.1352（$/pass $0.0169，-26%） |

¹ 两臂 record 的 `cost_usd` 不可直接对比（claude 臂为 claude CLI 自报、按内部价目；agent_loop 臂因
`metrics.estimate_cost` 遗留表缺 `("anthropic","glm-5.3-flash")` 条目记录为 0——计量缺口，见下）。
故按 metering 原始 token 数统一以 pricing.py glm-5.3-flash 价目（$0.15/$0.50 每 Mtok）归一化；
claude 臂 prompt_tokens 含缓存读（实际费率更低），归一化高估其成本，即 AgentLoop 成本优势为保守下界。
两臂实际均为 GLM 套餐内零边际成本（同 B5）。

逐任务交叉（agent_loop 臂，括号 = 首跑失败经补跑通过）：

| 任务（难度） | claude | agent_loop |
|---|---|---|
| add-format-helper（easy） | 2/2 | 2/2 |
| fix-missing-default（easy） | 2/2 | 2/2（2 补） |
| implement-done-command（medium） | 2/2 | 2/2（2 补） |
| add-simple-caching（medium） | 2/2 | 1/2 |
| security-hardening-taskmgr（hard） | 1/2 | 0/2 |
| conditional-branching-datapipeline（hard） | 1/2 | 1/2 |

补跑说明（沿用 B5「planner 基础设施失败补跑替换」先例，含补跑偏向，已如实披露）：
4 条首跑失败判定为基础设施/口径污染后各补跑 1 次并全部通过——
1 条 `plan_gate_blocked`（GLM flash planner 非法 JSON，B5 已知风险）；
3 条 `stuck_or_hardtimeout`：首跑时段 z.ai 端点网络抖动明显（日志多次 SSL handshake timeout /
connection reset，AgentLoop 逐轮直连 API 对抖动敏感），且 bench 动态 timeout（easy 504s /
medium 960s）校准自 claude 速度历史，AgentLoop 尚未跑完即被 SIGTERM；补跑时 timeout 随臂内
P95 自愈（655s/1248s）+ 网络平稳，4 条全部通过（225-576s）。

终态 4 条失败均为 `verification_failure`（真实能力失败，不重跑）：AgentLoop 在
security-hardening（0/2，claude 臂 1/2 亦弱）与两个 medium/hard 任务的 1 个 repeat 上
验证不过且修复回收失败（「验证修复后的 Git 提交/tag 失败，停止重试」）。

**判定（roadmap §8 触发条件 1：通过率不劣化 + 成本下降）：不成立。**
成本下降成立（归一化 $/pass -26%，且为保守下界）；通过率劣化（67% vs 83%；
B2 口径的 easy/medium 子集 7/8 vs 8/8）。n=12 样本小，差距集中在 security-hardening
与 2 个方差级 repeat，但按字面门槛「不劣化」不满足。

**建议（AgentLoop 去留）：留，但维持现有保守定位，不扩大默认启用。**
数据恰好验证 B4 既有默认路由（`agent_loop` 自动规则仅 `is_simple` 生效）：easy 子集 4/4 持平
且 token 成本更低；medium/hard 上 glm-5.3-flash 直驱 AgentLoop 的验证通过率与修复回收率
不足。后续若要复测，先补：① worker 直连 API 的网络重试鲁棒性（抖动敏感是首跑 3 超时的主因）；
② bench 动态 timeout 对慢速臂的校准（首跑宜用臂内历史或放宽余量）；③ `metrics.estimate_cost`
遗留定价表补 `("anthropic","glm-5.3-flash")`（(0.15, 0.50)，与 T10 pricing.py 条目对齐），
否则 agent_loop 臂 record `total_cost_usd`=0 且 `_collect_result` 把真实失败误标 `kill_reason=infra`
（本臂 3 条 verification_failure 被误标）；④ 扩样（n≥20）降低方差。
