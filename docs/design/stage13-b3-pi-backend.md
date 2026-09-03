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
  - pi 未安装 → returncode=127（由 executor 既有「非零退出码即正常失败结果」语义处理；加载/执行抛异常才回退 claude）。
- `backends/registry.py`：`resolve_backend_name` 新增显式声明优先——`subtask.backend` 或 `config.worker_backend`；非 claude 显式 backend 仅 headless 生效（pi 是非交互 CLI），交互模式回退 claude。默认空，不改变 B1/B2 既有行为。
- `executor.py`：初始执行分发新增非 claude 显式 backend 分支，异常回退 claude（与 agent_loop 分支容错一致，但不做 worktree reset——pi 路径未被标记为会产生半改状态，且后续 commit 边界由 executor 兜底）。
- 配置：`worker_backend: ""`（DEFAULT_CONFIG + config.example.json）。
- 测试：`tests/test_backends.py` +15（命令构造/readonly/模型透传/超时 kill/未安装 127/容错/计量事件/显式解析），全量 2941 通过。

## 实测 smoke（2026-09-04，本机 pi 0.84.4 + deepseek-v4-pro）

- `tmp/pi-poc` 下经 PiBackend 执行「创建 answer.txt 内容 42 并回复 DONE」：rc=0，1 次工具调用，21349+105 tokens，$0.0003，3.2s，文件真实创建，stdout 解析为 "DONE"。
- 注意：pi 单轮 input tokens 显著高于 claude/agent_loop（默认 system prompt + 完整工具 schema 较重），简单任务成本主要来自 prompt cache 未命中场景——canonical 批量成本对比留待 B5。

## 边界与后续

- 不做：pi 的 session 复用（--continue/--resume）、extension/skill 体系、rpc 模式；这些属于定制化深度集成，等 B5 数据证明价值后再评。
- B4 路由配置：目前显式选择是全局/单子任务粒度；按 difficulty/task_type 的声明式路由在 B4 落地。
- B5 验收门槛不变：5-10 个 canonical 任务 A/B，pass_rate 不劣化 + 成本可解释，才允许扩大默认启用范围。
