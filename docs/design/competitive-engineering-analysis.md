# 竞品工程机制分析：工作流与交互设计借鉴

| 字段 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 状态 | Active |
| 关联文档 | [`interaction-design-spec.md`](./interaction-design-spec.md)、[`interaction-roadmap.md`](./interaction-roadmap.md)、[`design-decisions.md`](./design-decisions.md)（ADR-007~013）、[`mcp-server-interface-design.md`](./mcp-server-interface-design.md) |
| 调研对象 | Claude Code、OpenAI Codex（CLI + Cloud）、Aider、Cursor、Windsurf Cascade、OpenClaw、Hermes Agent |
| 分析方法 | 官方文档/源码级调研 + agent_go 代码现状逐点验证（避免"借鉴已有能力"的重复建设） |

> 本文回答两个问题：(1) 竞品的工作流机制与交互设计点，哪些值得借鉴到 agent_go；(2) OpenClaw/Hermes 能否实现或作为基础改造实现 agent_go 的目标。结论：**agent_go 的编排骨架不落后，借鉴集中在单点机制；OpenClaw/Hermes 与本项目不在同一层，应采取"反向嵌入"而非"基础改造"。**

---

## 1. 定位光谱：agent_go 是编排层，不是对话 agent

agent_go 的本质是**编排器（orchestrator）**——把任务拆解后，将 agent 内循环外包给 `claude -p` 执行。这决定了借鉴方向：**学各家如何设计"控制点"和"数据流"，而非如何写 agent 内循环**。

```
对话 agent 层（Claude Code / Codex CLI / Aider / Hermes / OpenClaw）
   └── 单 agent 内循环：思考→工具→观察→再思考
编排层（agent_go / Devin 的多任务面 / Codex Cloud 的任务面）
   └── 任务 → Plan → 子任务 DAG → 隔离执行 → 验证 → 汇总
```

| 维度 | agent_go 现状 | 对标的最佳实践 |
|---|---|---|
| 任务编排 | ✅ wave 拓扑调度 + worktree 隔离（独有，领先） | — |
| 单点执行 | 外包 `claude -p`（stream-json 实时事件） | Claude Code 内循环 |
| 验证回路 | ✅ 重试 + 失败上下文注入 | Aider edit→lint→test→fix |
| 安全模型 | ⚠️ 白名单 + bypassPermissions（过宽） | Codex 两轴模型 |
| 扩展点 | ❌ 无 hook 体系 | Claude Code hooks |
| 上下文注入 | ⚠️ role_skill_map（3 种匹配） | Cursor/Windsurf 四态矩阵 |

---

## 2. 工作流机制借鉴（7 项）

每项格式：**竞品做法 → agent_go 现状（代码验证）→ 借鉴落地**。决策细节见对应 ADR。

### 2.1 验证分层（Tiered Verification）★★★ — Aider → ADR-007

- **竞品**：Aider 用 `--lint-cmd`（每文件编译级检查，便宜）+ `--test-cmd`（全量测试，贵）分层，便宜先跑，失败即回。
- **现状**：`executor.py:592-636` 单一 verification 串行，无分层。语法错也要等 5 分钟全量测试。
- **落地**：Plan schema 的 verification 分 `fast`/`full` 两段；fast 失败直接进修复不跑 full。成本低，失败定位更快。

### 2.2 失败分类处理（Failure Taxonomy）★★★ — Aider → ADR-007

- **竞品**：Aider 区分"patch 无法应用"（格式问题）与"lint/test 失败"（逻辑问题），用不同修复 prompt。
- **现状**：`executor.py:610-636` 的 `vr_entry` 已含 `exit_code`（0/127/其他）、`rejected` 等分类信号，但未利用——所有失败走同一重试路径。
- **落地**：`exit_code=127`→环境问题（修命令而非代码）；`rejected`→安全拦截（不重试直接 blocked）；其余→现有注入重试。与验证卡片（ADR-001）合并呈现类别。

### 2.3 shell 链分解加固白名单 ★★★ — Codex → ADR-008

- **竞品**：Codex 用 tree-sitter 把 `git add . && rm -rf /` 按 `&&`/`||`/`;`/`|` 拆成逐命令评估，防止危险命令藏在安全命令后。
- **现状**：`_is_safe_verification_command`（`utils.py`）是整串判断，组合命令可能穿透白名单。
- **落地**：白名单检查前先拆链，每个子命令独立过检，任一不过则整条拒绝。一个函数的成本，纯安全收益。

### 2.4 两轴安全模型（Capability × Approval）★★★ — Codex → ADR-011

- **竞品**：Codex 把"sandbox 能做什么"（OS 内核强制）与"approval 何时问"（软策略）解耦，打包为 Auto/ReadOnly/FullAccess 三预设。
- **现状**：`subtask.py:112` 一律 `--permission-mode bypassPermissions`（等于 `--yolo`）。`agents.py` 有工具白名单但仅是"建议"；`subtask.py:118` 的 `--allowedTools` 硬约束能力存在却默认不收紧。
- **落地**：不复制 Seatbelt/seccomp，借"两轴"概念——轴 1：按 agent_type 收紧 `--allowedTools`（reviewer/architect 只读）；轴 2：按 subtask 风险分级 `--permission-mode`。只读型子任务从机制上无法误改代码。

### 2.5 可验证引用（Verifiable Citations）★★★ — Codex → ADR-009

- **竞品**：Codex Cloud 每个结论附终端日志/测试输出片段，可追溯"测试真过了吗"。
- **现状**：`verification_results`（`executor.py:618`）已存每条验证命令的结构化结果，但报告/review 未作为证据呈现。
- **落地**：报告与验证卡片中"验证通过 ✓"附命令+关键输出片段。数据已在 `results_map`，纯展示层，近零成本。

### 2.6 密钥剥离 + fail-closed 语义评估 ★★ — Codex → ADR-013

- **竞品**：Codex Cloud 两阶段运行时——setup 阶段有网有密钥，agent 阶段密钥已删除；guardian 审批 fail-closed。
- **现状**：`executor.py:919` `os.environ.copy()` 把全部环境变量（含密钥前缀）透传 claude；`evaluator.py` 语义评估结果仅记录不阻断。
- **落地**：(a) 配置 `sensitive_env_prefixes`，构建 worker env 时剥离；(b) evaluator 高置信"未达成"时 fail-closed 标 failed。

### 2.7 预算化仓库地图（Repo Map）★★ — Aider（P3，未立 ADR）

- **竞品**：Aider 用 tree-sitter 提符号 + PageRank，固定 token 预算内给 LLM 全局感知。
- **现状**：`git_utils.analyze_project` 只给 planner 文件清单，无符号级信息。
- **落地**：planner 上下文增加每文件顶层符号摘要，控制 token 预算。成本中，P3 排期。

---

## 3. 交互设计点借鉴（6 项）

### 3.1 Steering 通道（不阻塞引导）★★★ — Cursor/Windsurf → ADR-010

- **竞品**：Cursor queued messages / agent ask-questions 工具；Windsurf real-time continue——运行中送引导不打断主流程。
- **现状**：只有粗粒度 SIGINT 暂停+resume。子任务跑 10 分钟时用户只能干等或全停。
- **落地**：每 worktree 放 `STEERING.md`，`_run_headless` 事件循环定期检查，变更即作为下一条 user message 注入 claude。利用现有 stream-json 循环，改动集中在 `subtask.py`。**长任务体验的最大单点跃升。**

### 3.2 检查点快照 + plan 联动回滚 ★★ — Cursor/Claude（P3，未立 ADR）

- **竞品**：Claude checkpoint 按 prompt 快照可回滚；Cursor 官方建议"改偏了→回滚代码→改 plan→重跑"。
- **现状**：每次重试是独立 git commit/tag，天然就是检查点（比内存快照更可靠），但缺"回到某检查点重跑"操作与 plan 联动。
- **落地**：`agent_go rewind <task-id> <sub-id> [--to <tag>]` 级联重置下游 tag；resume 时检测 plan 版本变化提示从受影响子任务重跑。P3，依赖 $EDITOR Plan（P2-1）。

### 3.3 四态激活矩阵 ★★ — Cursor/Windsurf → ADR-012

- **竞品**：两家不约而同收敛到 Always / Intelligently / Glob / Manual 四态 + 成本标注。
- **现状**：`role_skill_map.py` 支持 agent_type/keywords/file_patterns 三种匹配 + 全局/项目两层（已对标分层配置），缺 `always_on` 态与注入成本提示。
- **落地**：规则加 `activation` 字段并新增 `always`（如安全规范每个子任务必带）；`skills.py` 渲染时标注 token 占用。增量增强非重构。

### 3.4 状态栏 JSON-on-stdin ★ — Claude Code（P4，未立 ADR）

- **竞品**：statusline 是用户脚本，stdin 收 JSON session 数据，stdout 渲染。
- **落地**：允许配置 `status_script`，agent_go 把任务状态 JSON 传给脚本，输出接入 TUI 状态栏或 `--json` 补充。开放定制，P4。

### 3.5 有界自主到点后的 Continue 语义 ★ — Windsurf（P3，未立 ADR）

- **竞品**：20 次工具调用到点弹 Continue 而非判死。
- **现状**：`max_retries` + `MAX_GOAL_TURNS` 已有等价的有界自主，但超限是 kill+failed。
- **落地**：goal 超限改为"暂停+提示用户加轮次"，贴合 Continue 语义。P3。

---

## 4. 已有等价物清单（禁止重复建设）

| 竞品特性 | agent_go 等价物 | 结论 |
|---|---|---|
| Claude `isolation: worktree` | `git worktree add -b agent_go/{task}/{sub}` | 已有，任务级更强 |
| Claude Stop Hook + `/goal` | `goal_injector.py` | 已直接借鉴 |
| Aider edit→lint→test→fix | `executor.py` 验证重试循环 | 基础版有，补分层（ADR-007） |
| Codex `/review` 独立审查 | `review --deep` | 已有 |
| Cursor plan 版本 | `plan-history`/`plan-diff` | 已有 |
| Codex sessions as files | `task_dir/`（meta.json+result.json） | 已有，天然可 resume |
| Windsurf 20-tool 上限 | `max_retries`+`MAX_GOAL_TURNS` | 已有 |
| Claude subagent 工具白名单 | `agents.py`+`--allowedTools` | 机制有，默认太宽（ADR-011） |
| Aider 自动 commit | 每 subtask commit+tag | 已有，且即检查点 |
| Codex/Claude 分层配置 | 全局 `~/.agent_go/` + 项目 `.agent_go/` | 方向一致，保持 |

---

## 5. 借鉴优先级总表（→ ADR / Roadmap）

| 优先级 | 借鉴点 | 来源 | 落地模块 | 成本 | ADR | 落地状态（2026-08-01） |
|---|---|---|---|---|---|---|
| **P1** | 验证分层 + 失败分类 | Aider | `executor.py` | 低 | ADR-007 | ❌ 未实现 |
| **P1** | shell 链分解加固白名单 | Codex | `utils.py` | 低 | ADR-008 | 🔶 部分（仅 `&&`） |
| **P1** | 可验证引用进报告/卡片 | Codex | `pipeline.py`/`ui.py` | 低 | ADR-009 | ✅ 已实现 |
| **P2** | Steering 通道 | Cursor/Windsurf | `subtask.py` | 中 | ADR-010 | ❌ 未实现 |
| **P2** | 两轴安全模型 | Codex | `agents.py`/`subtask.py` | 中 | ADR-011 | ✅ 已实现（allowedTools 分级） |
| **P2** | 四态激活矩阵 | Cursor/Windsurf | `role_skill_map.py`/`skills.py` | 低 | ADR-012 | ❌ 未实现 |
| **P2** | 密钥剥离 + fail-closed | Codex | `executor.py`/`evaluator.py` | 低 | ADR-013 | 🔶 部分（`evaluator.fail_closed` ✅；密钥剥离 ❌） |
| P3 | rewind 检查点 + plan 联动 | Cursor/Claude | `cli.py`/`git_utils.py` | 中 | 待立 | ✅ 已实现（`checkpoint.py`） |
| P3 | goal 超限 Continue 语义 | Windsurf | `goal_injector.py` | 低 | 待立 | ❌ 未实现 |
| P3 | Repo Map 符号上下文 | Aider | `git_utils.py`/`api.py` | 中 | 待立 | ❌ 未实现 |
| P4 | 状态栏脚本契约 | Claude | `tui.py`/`console.py` | 中 | 待立 | ❌ 未实现 |

---

## 6. OpenClaw / Hermes 评估：不同层，不改造，反向嵌入

### 6.1 定位差异（事实核查）

| | agent_go | OpenClaw | Hermes Agent |
|---|---|---|---|
| 本质 | 工程任务编排器 | 个人助手网关（25+ 消息渠道） | 自改进对话 agent（学习循环+记忆） |
| 技术栈 | Python 零依赖 | TypeScript（pnpm monorepo） | Python 3.11 + 部分 JS |
| 隔离模型 | **git worktree + tag 产物传递** | 无（session 工作区） | 容器级（Docker/SSH/Modal 六后端） |

关键区分：Hermes 的隔离是**容器级进程隔离**，agent_go 是 **git 对象级隔离**——后者支撑上游产物 `git merge tag` 传递、每次重试可回放、人类可直接审查分支。容器隔离给不了这些。

### 6.2 核心能力对照

agent_go 十项核心能力中，两者缺失：Plan 分解、worktree 隔离、wave 调度、tag 产物传递、验证重试回路、任务级 resume、失败现场保留（7 项）；重叠仅有 skills 系统与多渠道通知（agent_go 亦已有 `skills.py`/`notify.py`）。

### 6.3 结论：改造不成立，嵌入成立

- **OpenClaw 路径 ✗**：TS 技术栈断层 + 核心编排骨架全要重写，其价值面（渠道/语音/伴侣 App）对本项目全冗余。
- **Hermes 路径 △**：Python 栈匹配，可借 terminal backends，但仍需新写 agent_go 一万行中的约七千行核心——换重壳写同样内核，不划算。
- **反向嵌入 ✅（推荐）**：agent_go 包成 **MCP server** 嵌入两者——它们做对话入口与渠道渲染，agent_go 做结构化执行。成本"几天" vs 改造"重写项目"。接口设计见 [`mcp-server-interface-design.md`](./mcp-server-interface-design.md)。

**附带发现**：两个 20 万+ star 项目中均无人做"git worktree + 依赖调度 + 验证回路"这一编排层——该层目前是空位，正是 agent_go 的护城河。

---

## 7. 三条核心结论

1. **编排骨架（wave 调度 + worktree + 检查点）是资产，不动**。借鉴集中在单点机制，不做骨架重构。
2. **最高性价比的借鉴都在验证回路**（ADR-007/008/009）——低成本、纯增量、直接强化"验证可信度"核心卖点。
3. **Steering 通道是最值得抄的交互**（ADR-010），MCP 反向嵌入是最划算的集成路径（§6.3）——前者补体验短板，后者补生态入口，都不动内核。

---

*调研来源：Claude Code 官方文档（hooks/subagents/permissions/checkpointing）、Codex 开发者文档与 codex-rs 源码分析、Aider 官方文档与博客、Cursor 文档、Windsurf/Devin 文档与 Wave 10 博客、openclaw/openclaw 与 NousResearch/hermes-agent 仓库 README。*
