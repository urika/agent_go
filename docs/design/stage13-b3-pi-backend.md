# 阶段十三 B3：Pi Backend PoC

日期：2026-09-04。状态：PoC 完成（canonical 批量对比随 B5）。

## 目标

验证开源 agent CLI（pi，`github.com/earendil-works/pi`，本机实测 0.84.4）能否作为第三种 worker backend 接入 B1 标准接口，不改动 pipeline/executor 主流程。

## pi CLI 输出契约（实测确认）

命令：`pi -p --mode json --no-session [--tools a,b] [--model provider/id] <prompt>`，cwd=worktree。

- stdout 为 NDJSON 事件流，每行一个 JSON 事件：
  - `session`：首个事件，携带 id / cwd / version；
  - `message_end`（role=assistant）：携带 `usage`（input/output/cacheRead/cacheWrite/totalTokens + `cost.total` 精确美元值）与 `stopReason`（`toolUse` / `stop` / `error`）；
  - `tool_execution_start/end`：携带 toolName / args / result / isError；
  - `agent_end` 携带完整消息列表，`agent_settled` 为结束标志；
  - `message_update` 为增量流（thinking_delta/toolcall_delta），解析时跳过。
- 进程退出码 0 = 流程正常结束（不代表验证通过；验证仍归 executor 既有通道）。
- 内置工具：read/bash/edit/write 默认开，grep/find/ls 只读默认关；`--tools` 白名单可显式启用。

## 落地内容

- `agent_go/backends/pi_backend.py`：`PiBackend`（name="pi"）。
  - NDJSON 解析（`_parse_events`）：最终 assistant 文本（最后一个非 toolUse 的 message_end）、tokens/cost 聚合、tool 统计、stopReason=error 告警，对非 JSON 行容错跳过；
  - 计量：pi 事件流自带精确 cost，直接写一条聚合 `meter_event`（virtual_model=`agentgo-worker-pi`），不经 pricing 估算；
  - readonly（ctx.extra.readonly）→ `--tools read,grep,find,ls`（pi 的 bash 非只读，不放行）；
  - ctx.routed_model → `--model` 透传；
  - hard_timeout：communicate(timeout) 超时 kill，kill_reason="hard_timeout"；active_pids 登记/清理与 claude 路径一致；
  - pi 未安装 → returncode=127（由 executor 既有「非零退出码即正常失败结果」语义处理；加载/执行抛异常才回退 claude）；
  - 零产出错误映射（批量实测发现）：pi 对 API 级错误（402 余额不足等）也退出 0——
    `stopReason=error` 且 0 tokens / 0 工具调用 / 无最终文本时显式返回 returncode=1 并附 errorMessage，
    避免 executor 把「什么都没做」当成功结果进入验证循环。
- `backends/registry.py`：`resolve_backend_name` 新增显式声明优先——`subtask.backend` 或 `config.worker_backend`；非 claude 显式 backend 仅 headless 生效（pi 是非交互 CLI），交互模式回退 claude。默认空，不改变 B1/B2 既有行为。
- `executor.py`：初始执行分发新增非 claude 显式 backend 分支，异常回退 claude（与 agent_loop 分支容错一致，但不做 worktree reset——pi 路径未被标记为会产生半改状态，且后续 commit 边界由 executor 兜底）。
- `backends/dispatch.py` 修复（批量实测发现）：`run_repair` 原先只特例 agent_loop，显式 pi 会静默落回 claude；现统一为「解析结果非 claude 则分发，异常回退 claude」。
- `bench.py` / `cli.py`：`--worker-backend` 开关，注入 bench 临时 config 并在 record 标记 backend 臂（B5 对照实验基础设施）。
- 配置：`worker_backend: ""`（DEFAULT_CONFIG + config.example.json）。
- 测试：`tests/test_backends.py` +21（命令构造/readonly/模型透传/超时 kill/未安装 127/容错/计量事件/显式解析/修复路径 pi 分发与回退/零产出错误映射），全量 41 通过。

## 小规模批量（2026-09-04，golden 套件 6 任务 × repeat 1）

**deepseek 臂**（pi + deepseek-v4-pro，付费按量；`eval_suite/results_pi_poc_20260904.jsonl`）
对照基线：golden-20260812（claude backend + deepseek-v4-flash，repeat 2）：

| 任务 | claude+flash pass/cost/elapsed | pi+pro pass/cost/elapsed |
|---|---|---|
| add-format-helper | 1.00 / $0.0052 / 58s | 1.00 / $0.0047 / 15s |
| fix-missing-default | 0.50 / $0.0226 / 235s | 1.00 / $0.0067 / 25s |
| implement-done-command | 1.00 / $0.0146 / 243s | 1.00 / $0.0115 / 65s |
| add-simple-caching | 1.00 / $0.0115 / 128s | 1.00 / $0.0145 / 180s |
| security-hardening-taskmgr | 1.00 / $0.0317 / 370s | 1.00 / $0.0271 / 330s |
| conditional-branching-datapipeline | 1.00 / $0.0436 / 453s | 0.00 / $0.0200 / 400s |
| **均值** | 0.92 / $0.0215 / 248s（n=12） | 0.83 / $0.0141 / 169s（n=6） |

- conditional-branching 失败链：pi 初始执行完成（24 次工具调用，$0.02）→ 验证失败 →
  修复路径因 dispatch bug 落回 claude（且 claude 侧未识别 `deepseek/deepseek-v4-pro` 模型名）
  → 修复 commit 失败终止重试。dispatch bug 已修（见上），重跑时恰逢 DeepSeek 余额耗尽（402）
  暴露了零产出 rc=0 缺陷（已修）。
- 口径警示：两臂模型不同（flash vs pro）、repeat 不对等（2 vs 1）、n=6——仅作 PoC 可行性证据，
  不作 backend 优劣结论；B5 需同模型双臂 + repeat≥2。

**模型选择策略**（2026-09-04 用户拍板）：优先 kimi / GLM 套餐额度，套餐耗尽才用 deepseek 按量；
同提供商内优先 flash 级模型控成本。实测探测：kimi-coding/kimi-for-coding 可用；
zai-coding-cn/glm-5.3-flash 当周额度尽（429，重置 15:17）；deepseek 余额 402。

**kimi 臂**（pi + kimi-coding/kimi-for-coding，套餐；`eval_suite/results_pi_kimi_20260904.jsonl`）：

| 任务 | pass | cost（刊例） | elapsed |
|---|---|---|---|
| add-format-helper | 1.0 | $0.0140 | 30s |
| fix-missing-default | 1.0 | $0.0234 | 40s |
| add-simple-caching | 1.0 | $0.0267 | 65s |
| implement-done-command | 1.0 | $0.0418 | 90s |
| security-hardening-taskmgr | 1.0 | $0.0619 | 240s |
| conditional-branching-datapipeline | 1.0 | $0.1027 | 290s |
| **均值** | **1.00（6/6）** | $0.0451 | 126s |

- 套餐臂「成本」为 pi 按刊例价记账，套餐内实际边际成本≈0，与 deepseek 按量臂不可直接比金额；
  可比的硬指标是 pass_rate（6/6）与 elapsed（均值 126s，两臂中最快）。
- 观察：security-hardening 与 conditional-branching 的 `failure_class=verification_failure`
  由 lint 告警触发（lint_errors=5/10，binary_pass=True、tests_broken=0）——pi 产出代码
  风格类 lint 告警偏多，不影响测试通过，但提示扩大使用前应在 B5 纳入 lint 口径对比。
- conditional-branching 在 kimi 臂一次通过（retries=0）；deepseek 臂的失败由 dispatch bug +
  402 余额叠加导致，非 pi 执行能力问题。

**两臂小结**：pi backend 在 golden 6 任务上累计 11/12 通过（deepseek 臂 5/6 + kimi 臂 6/6），
唯一失败可归因于已修复的基础设施 bug 与额度耗尽，未见 pi 执行能力本身的失败。
B5 正式验收仍需：同模型双臂、repeat≥2、lint/语义口径齐全。

## 实测 smoke（2026-09-04，本机 pi 0.84.4 + deepseek-v4-pro）

- `tmp/pi-poc` 下经 PiBackend 执行「创建 answer.txt 内容 42 并回复 DONE」：rc=0，1 次工具调用，21349+105 tokens，$0.0003，3.2s，文件真实创建，stdout 解析为 "DONE"。
- 注意：pi 单轮 input tokens 显著高于 claude/agent_loop（默认 system prompt + 完整工具 schema 较重），简单任务成本主要来自 prompt cache 未命中场景——canonical 批量成本对比留待 B5。

## 边界与后续

- 不做：pi 的 session 复用（--continue/--resume）、extension/skill 体系、rpc 模式；这些属于定制化深度集成，等 B5 数据证明价值后再评。
- B4 路由配置：目前显式选择是全局/单子任务粒度；按 difficulty/task_type 的声明式路由在 B4 落地。
- B5 验收门槛不变：5-10 个 canonical 任务 A/B，pass_rate 不劣化 + 成本可解释，才允许扩大默认启用范围。
