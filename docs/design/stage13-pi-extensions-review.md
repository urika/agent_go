# 阶段十三附：Pi 插件生态调研与借鉴评估

日期：2026-09-05。来源：知乎剪藏（超级东哥CyberFD，2026-09-03）推荐的 6 个 pi 扩展，
均为公开仓库（注意：**context-mode 为 Elastic License 2.0 源码可用，非严格开源**；
其余 5 个为 MIT）。

关联：[ADR-010 轨迹平台化三层切分](adr/ADR-010-trajectory-layering.md)、
[stage13-b3-pi-backend](stage13-b3-pi-backend.md)。

## 总览

| 插件 | 作者/许可 | 核心机制 | 借鉴价值 |
|---|---|---|---|
| pi-web-access | nicobailon / MIT | 网页搜索/抓取/GitHub 克隆/PDF/视频理解，多搜索后端 | ★☆☆ 低 |
| pi-memory | jayzeng / MIT | 三层 markdown 记忆 + qmd 语义检索 + KV cache 稳定快照注入 | ★★★ 高 |
| rpiv-todo | juicesharp / MIT（已归档，并入 rpiv-pi） | 模型可用 todo 工具，实时 overlay，跨 /reload 与压缩存活 | ★★☆ 中 |
| pi-subagents | tintinweb / MIT | Claude Code 式子代理编排 + worktree 隔离 + 确定性工作流脚本 | ★★★ 高（设计验证） |
| pi-mcp-adapter | nicobailon / MIT | 单一代理工具（~200 token）动态接入 MCP，不塞爆上下文 | ★★★ 高 |
| context-mode | mksglu / ELv2（非严格开源） | 工具结果外置沙箱 + FTS5/BM25 检索，98% 上下文削减 | ★★★ 高（代理层） |

## 逐个分析

### 1. pi-mcp-adapter（[nicobailon/pi-mcp-adapter](https://github.com/nicobailon/pi-mcp-adapter)）——最直接可借鉴

不把 MCP 服务器的全量工具 schema 注入上下文，而是注册**一个 ~200 token 的
代理工具**（`mcp`/`mcpScript`），模型按需动态发现与调用。agent_go 的
`mcp_client.py` 目前以 `mcp__{server}__{tool}` 命名空间注入子任务，若注入全量
schema 同样存在上下文膨胀。**候选增强：mcp_client 增加代理工具模式（单工具 +
动态发现）**，对 agent_loop 路径（直接 API、上下文最贵）收益最大。

### 2. pi-memory（[jayzeng/pi-memory](https://github.com/jayzeng/pi-memory)）——对谦逊层/C4 最有启发

- **KV cache 稳定快照**（默认 `stable` 模式）：记忆注入块仅在
  session_start / compact / 长期写入 / 跨天时刷新，其余每轮字节不变——避免
  逐轮 invalidate 前缀缓存（llama.cpp/vLLM/MLX 从首个分歧 token 起重算全部）。
  直接命中 C4 KnowledgeStore 注入设计盲区：注入内容逐轮变化 = 本地模型每轮
  重算前缀。C4 应采「检查点快照」而非「逐轮重建」。
- **三层记忆**：MEMORY.md 长期 / daily 日志 / scratchpad（待回来处理的事）。
  agent_go 现有 problems.jsonl 为单一层级；scratchpad 是谦逊层盲区观测的天然
  载体。
- **recoverable deletion**：`memory_forget` 先存 `recovery/` 再删，
  `memory_restore` 可恢复——跨任务记忆的纠错机制。
- 注入预算：scratchpad 2K + 今日日志 3K + MEMORY.md 4K + 昨日日志 3K，
  上限 16K 字符——分级截断策略可参考。

### 3. pi-subagents（[tintinweb/pi-subagents](https://github.com/tintinweb/pi-subagents)）——对 agent_go 架构的独立验证

本质是在 pi 内重建 agent_go 核心：并行后台子代理（并发队列，默认 10）+
worktree 隔离（配套 pi-subagents-worktrees）+ 自定义 agent 类型（YAML
frontmatter 定义 system prompt/模型/工具限制 ≈ role-skill 映射）+ 嵌套委派
深度上限（≈ dsh delegationDepth）。**社区独立造出同构轮子，佐证 DAG+worktree
架构方向**。可借鉴：

- **确定性工作流脚本**（`agent()/parallel()/pipeline()` JS，兼容 Claude Code
  Workflow）：编排不应由 LLM 即兴时给确定性脚本——对重复性任务可考虑
  「plan 模板固化」（长期候选）。
- **mid-run steering**：运行中给子代理发消息纠偏；agent_go headless 路径暂无，
  候选增强。

### 4. context-mode（[mksglu/context-mode](https://github.com/mksglu/context-mode)，ELv2）——代理层压缩的参照系

机制：MCP 工具原始输出不进上下文，进沙箱子进程 + SQLite/FTS5 索引，模型按需
BM25 检索（315KB→5.4KB，98% 削减）；「think in code」范式（模型写脚本处理数据
而非读入上下文：47 次 Read 700KB → 1 次 execute 3.6KB）；会话连续性跨压缩
（事件索引，检索恢复而非回灌）。证明「工具结果外置 + 按需检索」路线在 17 个
客户端成立，是代理层（llama.cpp 系）semantic/bm25 压缩的成熟参照。**符合
ADR-010 边界：协议/输出层关注点，归代理层**。仅理念借鉴——ELv2 许可 +
agent_go 零依赖约束，不引入代码。

### 5. rpiv-todo（juicesharp，已归档并入 rpiv-pi）——一个设计点可取

todo 状态跨 /reload 与上下文压缩存活（持久 overlay）。headless 场景 UI overlay
无意义，但「进度状态必须在压缩/重启后存活」原则与 agent_go resume/recover 同构；
agent_loop 路径 stuck 检测（B2）可参考「模型自维护 todo」。优先级低。

### 6. pi-web-access（nicobailon）——基本无关

搜索/抓取属 worker 工具面增强（日常使用 pi 臂可装），对平台层无架构借鉴。

## 落地优先级

1. **mcp_client 代理工具模式**（pi-mcp-adapter）——改动小、上下文收益直接。
2. **C4 知识注入采用 KV-cache 稳定快照**（pi-memory）——影响 C4 设计正确性。
3. **代理层压缩参照「外置+检索」路线**（context-mode 理念，不引入代码）。
4. pi-subagents 作设计验证归档；workflow 脚本固化为长期候选。

## Bench 口径注意

这些插件改变 worker 行为：bench 跑 pi 臂必须保持**无扩展环境**（对照 B5/B6 的
`--pure`/干净环境原则），与生产使用区分，否则四臂口径不可比。
