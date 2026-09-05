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
- **审批失败闭合** → 批量前必须落地 permission/approval 配置模板。
- **计量读 session 日志**（Zstd/v2 内部契约）→ 防腐层翻译 + 版本探测 +
  失败降级（读不到记未知，不阻塞任务）。
- **轨迹重放执行靠社区插件** → 不作为依赖项；agent_go 的 fork-retry 走核心
  原语即可。
- 批量并发时会话目录按 cwd 隔离预计无冲突，待冒烟验证。

## 下一步（B8 实施条件）

1. 手工冒烟（不做 backend）：`npx @deepseek-ai/dsh@<pinned> --profile headless`
   在 /tmp 临时仓库跑任务，验证审批配置、输出契约、会话日志位置。
2. 冒烟通过后实现 `DSHBackend`（工作量 ≈ B6）+ 同步落地 ADR-010 阶段 1 的
   `harvest_trajectory` 钩子（dsh 为首个 full-fidelity 数据源）。
3. bench 用 deepseek 官方 API 跑 golden 6×2，与现有四臂同口径对比。
