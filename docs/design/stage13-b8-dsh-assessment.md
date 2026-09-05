# 阶段十三 B8：DeepSeek Harness（dsh）评估

日期：2026-09-05（调研）。状态：**调研完成，待冒烟决策**。

关联：[ADR-010 轨迹平台化三层切分](adr/ADR-010-trajectory-layering.md)
（dsh 调研直接促成了该分层决策）。

## 基本信息

[dsh](https://github.com/deepseek-ai/deepseek-harness) 是 DeepSeek 官方开源的
agent harness（MIT），基于 Cordis「一切皆插件」架构，文档站
[deepseek-harness.github.io](https://deepseek-harness.github.io/deepseek-harness/)。
当前为 **developer preview，官方明示存在破坏性变更**。

模型接入：DeepSeek 原生 + Anthropic/OpenAI + 任意 OpenAI 兼容 / Responses /
Anthropic-Messages 自定义端点（可复用 glm/kimi/deepseek 凭证）。

## Backend 契合度（B1 标准接口）

| 集成点 | 现状 | 契合度 |
|---|---|---|
| headless 单次执行 | `dsh --profile headless "<task>"`：新 session、跑完打印最终文本即退出 | ✅ 与 `claude -p`/`opencode run` 同构 |
| 工作目录 | 无 `--dir`，cwd 即工作区根（agent_go 以 cwd=worktree 拉起） | ✅ |
| 退出码 | 0=turn completed / 1=失败 / 130=SIGINT 优雅退出 | ✅ 映射干净 |
| stdout/stderr | stdout 仅最终助手文本；**无 JSON 事件流** | ⚠️ 简单但无流式计量 |
| token 计量 | 需读 session 持久化日志（`assistant/message` 事件自带 `usage`） | ⚠️ 多一层间接 |
| 权限/审批 | headless 下审批**失败闭合**——编辑/跑命令需预配置 permission/approval 补丁，否则任务 stall | ⚠️ 批量前置条件 |
| 模型选择 | 配置驱动，未见 per-run 模型标志（同 zcode 约束） | ⚠️ 可接受 |
| 运行时 | CLI 需 Node.js（`npx @deepseek-ai/dsh`）；PyPI Python SDK 自带运行时为备选路径 | ✅ |

## 轨迹回放与日志（重点调研结论）

**核心架构**：Session = append-only 类型化 `SessionEvent` 日志，是唯一真源；
LLM 消息历史从日志派生，从不单独存储。架构约束：模型能看见的任何东西必须能
从 Session Log 重建（[session.zh.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.zh.md)）。

- 事件词汇：`turn/*`、`step/*`、`user/message`、`assistant/message`（含原始
  模型流 + `usage`）、`assistant/attempt`（失败尝试无损保留）、`tool/*`、
  `compaction/*`、`hook/*`；插件可 declaration merging 扩展。
- **持久化**（[persistence.zh.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.zh.md)）：
  JSONL 后端（Zstd 帧 + checksum，可配原始行）、原子实体化、逐批 fsync、
  单写者句柄租约、撕裂尾部截断；崩溃恢复**不截断**中断轮次，resume 时补
  合成 `turn/end{interrupted}`。
- **元数据头**：格式版本、cwd、`parentSession` 谱系、`origin:'subagent'`、
  `delegationDepth`（委派深度持久化）、`agentPreset`。

**「回放」能力分层**：

| 能力 | 归属 |
|---|---|
| Resume / Fork（seed + 精确 inheritedEventCount） | ✅ 核心 |
| 轨迹查看（Web UI Trajectory 视图，事件级时间线） | ✅ 核心 |
| 轨迹**重放执行**（记录级切点重跑、原轨迹 vs 新执行对比） | ❌ 核心没有，社区插件（apoexia/dsh-trajectory-replay）基于核心 seed/fork 原语实现 |

## 对 agent_go 的价值

1. **第五个 backend**：DeepSeek API 便宜，符合「deepseek 兜底」优先级链。
2. **最好的轨迹数据源**：事件溯源 + 无损 attempt 保留 + 崩溃不丢数据，
   可观测性架构在五臂中最扎实——是 ADR-010 阶段 1 的首个 full-fidelity
   harvester 数据源。
3. **架构参照**：其「日志即真源 + 能力 seam + 插件化」三原则已吸收进
   ADR-010 的平台层设计；fork 原语启发 fork-retry（验证失败从断点续跑）。

## 风险

- **developer preview 破坏性变更** → 版本 pin（如 `@0.1.0-rc.7`）+ 契约漂移
  检查（复用 `tools/check_llama_contracts.py` 模式）。
- **审批失败闭合** → ✅ 已解决（T01）：`DSH_PERMISSION_MODE=danger-full-access`
  环境变量即审批模板（代价是关沙箱，隔离由 worktree 承担）。
- **计量读 session 日志**（Zstd/v2 内部契约）→ ✅ 可行性已验证（T01）：
  `zstd -dc` 可读、`assistant/message` 带 usage；实现时仍需版本探测 +
  失败降级（读不到记未知，不阻塞任务）。
- **轨迹重放执行靠社区插件** → 不作为依赖项；agent_go 的 fork-retry 走核心
  原语即可。
- ~~批量并发时会话目录按 cwd 隔离预计无冲突~~ → ✅ 已确认按 cwd 分目录（T01）。

## 下一步（B8 实施条件）

1. ~~手工冒烟~~ ✅ 已完成（2026-09-05，见下节）。
2. ~~实现 `DSHBackend` + `harvest_trajectory` 钩子~~ ✅ 已完成（T02+T04，见下节）。
3. ~~bench golden 6×2~~ ✅ 已完成（T03，见下节）。

## T02/T04 实现记录（2026-09-05）

- `agent_go/backends/dsh_backend.py`（~380 行）：命令 `npx -y @deepseek-ai/dsh@0.1.2-rc.1
  --profile headless`（版本 pin）；非 readonly 注入 `DSH_PERMISSION_MODE=danger-full-access`，
  readonly 显式移除该变量（防用户 shell 继承全放开值）；rc=0 空 stdout → 零产出失败；
  hard_timeout 进程组 kill；`available()` = npx + ~/.dsh/settings.yaml 存在性。
- 计量真源：session 日志 `assistant/message.data.usage`（含 cacheReadTokens）；
  actual_model 从日志 message.source 回读（dsh 无 per-run 模型标志，日志即真源）；
  找不到日志仍写零值 metering 事件（管道可见性），全链 fail-open。
- cwd 编码：dsh 源码 `projectKey()` 移植（分隔符折叠、`~XXXX` 转义、首尾 `--`），
  有回归测试；编码不命中兜底按 header.cwd 扫描比对。
- **T04（ADR-010 阶段 1）**：`BaseBackend.harvest_trajectory()` 可选钩子（默认 []）；
  DSHBackend 首个实现——白名单词汇防腐翻译（chunk 流丢弃、参数截断 300 字符、
  header version≠0 降级）；executor 接线落盘 `<task_dir>/trajectory/{sub_id}.jsonl`。
- 测试：TestDSHBackend 16 例；全量非集成 2994 绿；ruff 干净。

## T03 批量：dsh × glm-5.3-flash 臂（2026-09-05，golden 6×2，并行 4）

结果文件：`eval_suite/results_b8_dsh_glm_20260905.jsonl`（12 条，全部真实记录——
无秒退、无人工 kill）。

**首跑 11/12 通过**（binary_pass 口径，与 B5-B7 一致），平均 elapsed ~420s
（快于 claude ~488s、zcode ~638s，慢于 pi ~368s），成本字段未知
（dsh 不报告成本 + glm-5.3-flash 缺定价覆盖，记录为 0 仅占位）。

- 唯一失败：add-simple-caching r1，`plan_gate_blocked`——规划门拦下 planner
  臆测 API 的 plan（agent_prompt 引用不存在的 `kwargs.items`/`clear_cache`），
  是**规划层质量事件（planner=glm-5.3-flash）而非 dsh worker 失败**，且门禁行为
  本身符合设计；r2 同任务通过。
- **T04 轨迹采集首次实战验证**：每子任务落盘 trajectory jsonl（实测一例 151
  事件：26 step / 34 次工具调用 / 逐步 usage 含 cacheReadTokens）——dsh 臂是
  首个带完整执行轨迹的 bench 臂。

**五臂对比（golden 6×2，binary_pass，同模型 glm-5.3-flash）**：

| 臂 | 通过 | 平均耗时 | 备注 |
|---|---|---|---|
| claude（B5） | 10/12 | ~488s | 默认路径基线 |
| pi（B5） | 10/12 | ~368s | 最快 |
| opencode（B6） | 12/12（dedup 后，首跑真实 7/8） | ~447s | 含补跑偏向 |
| zcode（B7） | 10/12（首跑零补跑） | ~638s | 最慢；$0 |
| **dsh（B8）** | **11/12（首跑零补跑）** | **~420s** | 唯一失败为 planner 臆测被门禁拦下 |

## T01 手工冒烟记录（2026-09-05，版本 pin `@0.1.2-rc.1`）

环境：/tmp/dsh_smoke 一次性仓库；模型路由 **z.ai 国际站直连**
（anthropic-messages 协议，glm-5.3-flash）——本地代理 :4000 的
chat/completions 与 /v1/messages 均挂起（HTTP=000，30-45s 无响应，
models 列表正常），疑似昨晚批量后 wedge，未深查。

配置落地（全部非交互）：

- `~/.dsh/settings.yaml`：`llm-pi-ai.providers.zai-glm`（apiKeyEnv=ZAI_API_KEY，
  api=anthropic-messages，baseURL=https://api.z.ai/api/anthropic，
  models=[glm-5.3-flash]）。
- `~/.dsh/profiles/headless/cordis.patch.yml`：id-targeted 覆盖
  `agent-default-model` → `{provider: zai-glm, model: glm-5.3-flash}`。
  （默认是 deepseek-official/deepseek-v4-flash；本机 DEEPSEEK_API_KEY 实测为
  本地代理专用 key，api.deepseek.com 直连 401。）
- 审批：`DSH_PERMISSION_MODE=danger-full-access`（approval policy 变为
  never）。**注意此模式同时关沙箱**——隔离边界由 agent_go worktree 承担；
  只读任务在默认 workspace-write 模式下无需审批即可跑通。

冒烟结果：

| 项 | 结果 |
|---|---|
| 只读任务（默认模式） | ✅ exit 0，stdout=最终答复，stderr=reasoning 流 |
| 编辑+执行验证任务（danger-full-access） | ✅ exit 0，calc.py 真实新增 subtract 并自验打印 2 |
| 会话日志位置 | `~/.dsh/sessions/--<cwd编码>--/session-<uuid>/session.jsonl.zstd`（按 cwd 分目录，与 bench worktree 隔离天然契合） |
| 日志结构 | 首行 header（type=session/version/cwd/delegationDepth），后续 seq 事件 |
| **计量** | ✅ `assistant/message` 事件带 `usage`（input/output/total/**cacheReadTokens**），zstd -dc 可读——harvester 数据源确认 |
| 事件完整性 | turn/step/tool/call/result/chunk 全词汇实测在场（6 step、5 工具调用、36 chunk） |
