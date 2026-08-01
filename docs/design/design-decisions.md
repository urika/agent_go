# agent_go UI/UX 设计决策记录 (ADR)

| 字段 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 状态 | Active（各 ADR 落地状态见下表） |
| 关联文档 | `interaction-design-spec.md`（规范）、`interaction-roadmap.md`（路线图） |

> 本文档记录 UI/UX 优化过程中的关键设计决策及其依据。每条决策均标注：**背景 → 决策 → 理由 → 代码验证**。决策变更时追加修订记录，不删除历史。

### ADR 落地状态速查（2026-08-01 核验）

| ADR | 决策 | 落地状态 |
|-----|------|---------|
| ADR-001 | 验证上下文卡片 | ✅ 已实现（验证卡片复用 executor 数据） |
| ADR-002 | `--json` 提前到 Phase 0.5 | ✅ 已实现（全局 `--json`） |
| ADR-003 | TUI 降级为 `--interactive` 可选 | ✅ 已实现（`--interactive`） |
| ADR-004 | stream-json 事件为进度主数据源 | ✅ 已实现（`current_activity` + shared_activity） |
| ADR-005 | 删除 emoji 标准化 | ✅ 已落地（不实现） |
| ADR-006 | 多方案对比降级 backlog | ✅ 已落地（不实现） |
| ADR-007 | 验证分层 + 失败分类 | ❌ 未实现（无 fast/full 字段） |
| ADR-008 | shell 链分解 | 🔶 部分（仅 `&&` 拆链，未按 `\|\|`/`;`/`\|` 拆分） |
| ADR-009 | 可验证引用进入报告 | ✅ 已实现 |
| ADR-010 | STEERING.md 运行中引导 | ❌ 未实现 |
| ADR-011 | 两轴安全（allowedTools 分级） | ✅ 已实现（agents.py reviewer/architect 只读集 + `--allowedTools`） |
| ADR-012 | role_skill_map 四态激活矩阵 | ❌ 未实现（无 activation 字段） |
| ADR-013 | worker env 密钥剥离 + evaluator fail-closed | 🔶 部分（`evaluator.fail_closed` 已实现；`sensitive_env_prefixes` 密钥剥离未实现） |

---

## ADR-001：验证上下文卡片复用已有 executor 数据

**状态**：Accepted
**日期**：2026-07-26

### 背景
PM 计划 P1-2"验证失败上下文展示"估时 2 人天，标注"中风险——需要改 evaluator 返回结构"。假设需要重构 `evaluator.py` 的返回值才能获取失败上下文。

### 决策
**不重构 evaluator，直接复用 `executor.py` 验证循环中已收集的数据。** 估时下调至 0.5 人天。

### 理由
代码验证发现 `executor.py:607-636` 的验证循环**已经在收集**完整的失败上下文：

```python
# executor.py:610-636（已存在）
failed_cmds: list[str] = []
failed_outputs: list[str] = []
for vcmd in cmds:
    vr_entry = _run_verification_cmd(...)  # 返回结构化条目
    if vr_entry["exit_code"] not in (0, 127):
        failed_cmds.append(vcmd)
        out_parts = [f"exit_code={vr_entry['exit_code']}"]
        if vr_entry.get("stdout_tail"):    # ← stdout 尾部已收集
        if vr_entry.get("stderr_tail"):    # ← stderr 尾部已收集
```

实际工作仅为：
1. 修改 `verify_subtask()` 签名，接收 `failed_outputs` 参数
2. 在 `ui.py` 格式化为卡片输出（IDS §4.3.2）

### 代码验证
- `executor.py:580-708`：验证循环完整结构
- `executor.py:688-691`：交互模式首次失败即 break，`failed_outputs` 在作用域内但未传递给 UI 层
- `_run_verification_cmd()`：返回含 `stdout_tail`/`stderr_tail`/`exit_code`/`rejected` 的结构化字典

---

## ADR-002：`--json` 模式从 Phase 4 提前到 Phase 0.5

**状态**：Accepted
**日期**：2026-07-26

### 背景
PM 计划将 `--json` 全局输出模式列为 P4-3（Phase 4，低优先级），归类为"高级功能"。

### 决策
**将 `--json` 提前到 Sprint 2（Phase 0.5），与输出统一化并行。**

### 理由
1. **CI/headless 是核心用例**：竞品分析（场景 S3）明确 CI 流水线是 agent_go 的主要使用场景之一。当前 `--quiet` 不静音（ui.py 绕过 Console），使得 agent_go **无法可靠地在 CI 中使用**。

2. **`--json` 是 CI 集成的根本方案**：不是锦上添花的"高级功能"，而是让 agent_go 能被 IDE 插件、CI 步骤、数据分析工具消费的基础能力。

3. **与 Console 层同类**：`--json` 本质是 Console 的**第三种输出模式**（终端/紧凑/JSON），与输出统一化（Phase 0）是同一架构层改动，应一并完成。

### 代码验证
- `console.py`：Console 类已有 `quiet`/`verbose` 属性，增加 `json_mode` 是同层扩展
- 当前无任何命令支持结构化输出——`--json` 是从零搭建，但 Console 层是唯一注入点

---

## ADR-003：TUI 从"默认视图"降级为"`--interactive` 可选"

**状态**：Accepted（修订 IDS §4.4）
**日期**：2026-07-26

### 背景
IDS §4.4 设计 TUI 为 `run` 命令的默认执行视图，`--no-tui` 降级。PM 计划 P3-1 据此估时 3 人天。

### 决策
**TUI 改为 `--interactive` 显式触发，默认保持增强后的文本模式。** IDS §4.4 需相应修订。

### 理由

**理由 1：用户接受度未验证，且有反证。**

agent_go 的核心用户是在终端工作的工程师。大量 CLI 重度用户**反感强制 TUI**（类比反感强制 `less` 分页的工具）：
- TUI 输出不可 grep、不可重定向
- SSH 不稳定连接下 TUI 体验差
- 与现有 shell 管道/脚本工作流冲突

竞品 Devin 用 Web UI 是因为它面向非终端用户；agent_go 面向终端原住民，假设不同。

**理由 2：主线程争用是架构级风险（R1）。**

Python `curses.wrapper()` 必须在主线程运行。当前调用链：

```
main() [主线程] → cmd_run() [主线程] → _run_pipeline() [主线程]
```

`_run_pipeline` 阻塞主线程。要让 TUI 同时运行，必须将 `_run_pipeline` 移入后台线程，但这导致：
- `signal.signal(SIGINT)` **只能在主线程注册** → 移到后台后 SIGINT 处理失效
- 需重新设计中断传递机制

这是 5 人天的架构改动，不是 3 人天的 UI 增强。

**理由 3：增强后的文本模式已能满足"执行可见"需求。**

进度行（P1-1）+ 验证卡片（P1-2）+ stream-json 进度（P1-5）组合后，文本模式已提供：
- 实时进度（claude 在做什么）
- 失败上下文（为什么失败）
- 成本可见（$/pass）

TUI 的增量价值（多面板布局）不足以抵消默认强制它的成本。

### 修订影响
- IDS §4.4：标题改为"可选 TUI 仪表盘"，触发方式从默认改为 `--interactive`
- IDS §4.4.5："退出 TUI"语义简化——TUI 本就是显式启动的
- Roadmap P3-1：估时 3d → 5d（即使作为可选，主线程争用仍需解决）

---

## ADR-004：stream-json 事件作为进度展示的主数据源

**状态**：Accepted（新增任务 P1-5，改诚实表述）
**日期**：2026-07-26

### 背景
IDS §4.3.1 的进度行设计为"独立守护线程 + 5s 计时器"，仅显示耗时。PM 计划 P1-1 据此估时 1 人天。

### 决策
**新增 P1-5 任务：将 `_run_headless` 已解析的 stream-json 事件作为进度展示的主数据源。** 进度行/TUI 日志面板均消费此数据流。

### 理由（经复核修正——原声明部分夸大）
代码验证发现 `subtask.py:108-120` 的 `_run_headless` 已经在逐事件解析 claude 的 stream-json 输出，但**实际可用数据与事件设计文档表述不完全一致**：

**现状（经复核验证）**：
- ✅ **工具名称**已正确捕获：`current_tool[0]` 在 `content_block_start` 事件中精确记录（line 183: `tool_name = cb.get("name", "")`），已知 `Read`/`Edit`/`Write`/`Bash`。
- ⚠️ **文件路径/命令参数未提取**：`tool_input[0]` 只是把 `partial_json` 分片**字符串拼接**（line 202: `tool_input[0] += delta.get("partial_json", "")`），从未从拼接结果中解析 `file_path` 字段。当前仅 `logger.debug` 200 字符预览。
- ❌ **无共享进度状态**：`current_tool`/`tool_input` 是 `parse_and_log` 闭包中的可变容器，局限在 `read_stdout` 线程内，没有任何锁保护的共享变量供外部渲染器读取。

P1-5 所需的新建工作：
1. 从分片的 `partial_json` 重组合法 JSON（需处理流式片段边界），提取 `file_path`（Edit/Write 工具）或 `command`（Bash 工具）。
2. 写入线程安全的共享变量（`threading.Lock` + 最新活动快照）。
3. 渲染器按 1-5 秒间隔轮询该快照。

### 代码验证（复核修正）
- `subtask.py:146-209`：`parse_and_log` 函数正确捕获工具名（line 183）并累加 tool_input 分片（line 202），但**从未提取 file_path**，仅 `logger.debug` 200 字符预览（line 207-208）。
- `subtask.py:255-266`：stdout/stderr 在独立 daemon 线程中读取，状态不与外部共享。
- 无加锁的"当前活动"变量供 P1-1 渲染器消费。

**原声明「agent_go 实时知道 claude 在编辑哪个文件」不准确**——知道"编辑工具"已打开，但文件路径需从流式 partial_json 重组，该提取逻辑待新建。

### 实现（修正版）
1. P1-5：新建 `_run_one()` 到渲染器的共享状态通道（线程安全 + 语义化映射：tool_name + 文件/命令摘要）。
2. P1-1 进度行消费该通道：`➜ sub-2: [修改路由配置] → 正在编辑 src/routes.py (45s)`
3. P3-2 TUI 日志面板消费同一通道。

### PoC 前置 gate（新建——Sprint 3 必须验证）
P1-5 的可行性依赖以下假设，**必须在 Sprint 3 以 ≤50 行 PoC 验证**：
- **假设 1**：`partial_json` 分片能在流式边界下稳定重组为合法 JSON（Edit/Write 的 `file_path` 字段永不跨分片拆分？）
- **假设 2**：重组后能可靠提取标准化 `file_path`（相对路径、绝对路径、或被编辑器重写前的路径？）
- **假设 3**：claude 的 stream-json 事件 schema 在版本升级后保持向后兼容（或降级方案是否行得通？）

PoC 不通则 P1-5 降级为"工具名级别进度"（`➜ 编辑中...`），P1-1 回退到原始计时器方案。估时相应回调。

### 代价
- P1-1 估时 1d → 1.5d（需共享 `_run_one` 解析状态）
- P1-5 新增 2d（事件→语义化映射 + 共享状态 + PoC）
- 但解锁两个任务的真正价值——从"知道它在跑"到"知道它在做什么"（需 PoC 确认可行性）

### 风险（R4）
claude 版本升级可能变更 stream-json 事件 schema。缓解：解析层做版本检测（`_detect_tool_versions` 已存在），未知格式降级为工具名级别进度。

---

## ADR-005：删除 emoji 标准化任务（原 P0-3）

**状态**：Accepted
**日期**：2026-07-26

### 背景
PM 计划 P0-3"标准化 emoji 字符集"估时 0.5 人天，定义 `ICONS` 字典并替换 18 个文件中 ~50 种 emoji 字面量。

### 决策
**删除 P0-3。** ICONS 字典定义保留在 IDS §4.1.2 作为规范，但不作为独立任务强制全局替换。

### 理由
1. **用户零感知**：`print("✅")` 和 `console.success()` + ICONS 字典在用户端完全等价。这是内部代码整洁度问题，不是 UX 问题。

2. **投入产出比极低**：替换 18 个文件的 emoji 字面量是机械劳动，易引入 typo，却不改变任何用户可见行为。

3. **真正的输出一致性问题已在 P0-1 解决**：`print()` vs `console.*` 的双系统才是 `--quiet` 失效的根因。emoji 来源（字面量 vs 字典）不影响功能。

4. **自然收敛**：P0-1 消灭 `print()` 时，新增的 `console.success()` 等调用天然使用语义方法（已内置 emoji），旧字面量在后续迭代中逐步替换即可，无需专项。

---

## ADR-006：多方案对比（原 P2-3）降级到 backlog

**状态**：Deferred
**日期**：2026-07-26

### 背景
IDS 未涉及，PM 计划 P2-3"多 Plan 方案对比"设计为在确认环节允许生成备选方案，估时 2 人天。

### 决策
**降级到 backlog，需用户研究验证需求后再排期。**

### 理由
1. **价值假设未验证**：没有证据表明用户需要"多个 Plan 方案对比"。这可能是设计师的假设，而非真实需求。

2. **成本不确定**：每次生成备选方案都需额外 API 调用（成本 + 延迟），用户是否愿意为此付费未知。

3. **优先级低于已验证需求**：验证卡片（P1-2）、--json 模式（P0.5）、stream-json 进度（P1-5）都有明确的用户痛点支撑，应优先。

### 重新评估条件
- 用户访谈中 ≥3 人主动提到"想看不同方案"
- 或 A/B 测试显示多方案对比提升确认后任务成功率

---

## ADR-007：验证分层 + 失败分类处理

**状态**：Accepted（P1）
**日期**：2026-07-26
**来源**：竞品工程分析 §2.1/§2.2（借鉴 Aider）

### 背景
Aider 用 `--lint-cmd`（便宜的每文件编译检查）+ `--test-cmd`（贵的全量测试）分层验证，并区分"patch 无法应用"与"测试失败"用不同修复 prompt。agent_go 当前是单一 verification 命令串行，失败统一走同一重试路径。

### 决策
1. **验证分层**：Plan schema 的 `verification` 支持 `fast`/`full` 两段。fast（如编译/语法检查）失败 → 直接进修复，不跑 full；full 失败 → 走现有重试。
2. **失败分类**：按 `vr_entry` 已有信号分类处理——`exit_code=127`（命令不存在）→ 环境问题，修复 prompt 指向命令本身而非代码；`rejected=True`（白名单拦截）→ 不重试，直接标 blocked；其他 → 现有注入重试。

### 理由
- 语法错不再浪费 5 分钟全量测试；失败类别帮助用户（与 ADR-001 验证卡片合并呈现）与修复 prompt 对齐。
- 分类信号**已存在**于 `executor.py` 的 `vr_entry`，只需加分支，不改数据流。

### 代码验证
- `executor.py:610-636`：`vr_entry` 含 `exit_code`/`rejected`/`reject_reason`/`stdout_tail`/`stderr_tail`
- `_run_verification_cmd()`：返回结构化条目，无需改签名

### 落地
`executor.py` 验证循环（fast/full 分支 + 分类处理）+ `api.py` planner prompt（输出 fast/full 两段）+ Plan schema 文档。估时 1.5d。

---

## ADR-008：shell 链分解加固验证命令白名单

**状态**：Accepted（P1）
**日期**：2026-07-26
**来源**：竞品工程分析 §2.3（借鉴 Codex 规则引擎）

### 背景
Codex 用 tree-sitter 把 `git add . && rm -rf /` 按 `&&`/`||`/`;`/`|` 拆成逐命令评估，防止危险命令藏在安全命令后面。agent_go 的 `_is_safe_verification_command` 是**整串判断**，组合命令可能穿透白名单。

### 决策
白名单检查前先拆链：按 `&&`/`||`/`;`/`|` 拆分验证命令，**每个子命令独立过 `_is_safe_verification_command`**，任一不过则整条拒绝。不引入 Starlark/tree-sitter，纯字符串拆分（验证命令场景无复杂 quoting 需求）。

### 理由
- 一个拆分函数的成本，消除"安全命令 && 危险命令"的穿透面。
- 影响面可控：`goal_injector.py` 与验证循环共用同一白名单函数，一处加固两处受益。

### 代码验证
- `utils.py:_is_safe_verification_command`：现整串判断
- `goal_injector.py:50`：调用同一函数过滤注入命令

### 落地
`utils.py` 新增 `_split_shell_chain()` + `_is_safe_verification_command` 改为逐段检查；补穿透用例测试（`pytest && curl evil.com` 应拒绝）。估时 0.5d。

---

## ADR-009：可验证引用进入报告与验证卡片

**状态**：Accepted（P1）
**日期**：2026-07-26
**来源**：竞品工程分析 §2.5（借鉴 Codex verifiable citations）

### 背景
Codex Cloud 每个结论附终端日志/测试输出片段。agent_go 的 `verification_results` 已存每条验证命令的结构化结果（命令/exit_code/stdout 尾部），但报告与 review 仅展示"验证通过 ✓"，不展示证据。

### 决策
最终报告、验证卡片（ADR-001）、`inspect`/`show` 输出中，"验证通过/失败"附**可验证引用**：命令 + 关键输出片段（尾部 N 行，N=3）。完整输出给路径。

### 理由
- 数据已在 `results_map`/`verification_results`，纯展示层改动，近零成本。
- 直接强化"验证可信度"这一核心卖点——用户能追溯"测试真过了吗"，而非信任一个 ✓。

### 代码验证
- `executor.py:618`：`verification_results.append(vr_entry)` 结构化积累
- `pipeline.py:359-365`：报告循环处可访问 `r["verification_results"]`（需确认该字段随 result 持久化到 `result.json`——若未持久化，补充持久化是本 ADR 的一部分）

### 落地
`pipeline.py` 报告段 + `ui.py` 验证卡片 + `cli.py:cmd_show/cmd_inspect`。估时 0.5d。

---

## ADR-010：STEERING.md 运行中引导通道

**状态**：Accepted（P2）
**日期**：2026-07-26
**来源**：竞品工程分析 §3.1（借鉴 Cursor queued messages / Windsurf continue）

### 背景
Cursor/Windsurf 允许用户在 agent 运行中送引导而不打断主流程。agent_go 只有粗粒度 SIGINT 暂停+resume——子任务跑 10 分钟时，用户想补一句"记得兼容 Python 3.9"只能干等或全停。

### 决策
每个 worktree 下约定 `STEERING.md`：`_run_headless` 的事件循环定期检查其 mtime，发现变更即把内容作为下一条 user message 注入运行中的 claude 会话（经 stream-json stdin 或重启会话携带补充指令，实现细节在 PoC 确定）。用户在任何终端 `echo "兼容3.9" >> <worktree>/STEERING.md` 即可引导。

### 理由
- 利用现有 stream-json 事件循环，改动集中在 `subtask.py`，不动 pipeline/executor 骨架。
- 长任务体验的最大单点跃升：从"全停/干等"两态到"边跑边引导"。

### 代码验证
- `subtask.py:270-298`：`_run_headless` 主循环按事件轮询，有天然的检查点插入位
- ⚠️ 待 PoC 确认：claude `-p` 单次调用是否支持中途注入 user message；若不支持，降级方案为"当前 attempt 完成后，下一 attempt 的 prompt 携带 steering 内容"（价值仍在，粒度变粗）

### 落地
`subtask.py`（事件循环 + steering 注入）+ `executor.py`（worktree 初始化时创建空 STEERING.md + 提示）。估时 2d（含 PoC）。

---

## ADR-011：两轴安全模型（allowedTools 收紧 + 分级 permission-mode）

**状态**：Accepted（P2）
**日期**：2026-07-26
**来源**：竞品工程分析 §2.4（借鉴 Codex sandbox × approval）

### 背景
Codex 把"技术上能做什么"（sandbox）与"什么时候问"（approval）解耦为两个正交轴。agent_go 当前 `subtask.py:112` 一律 `--permission-mode bypassPermissions`（等于 `--yolo`）；`agents.py` 有工具白名单但仅是建议，`--allowedTools` 硬约束能力已存在（`subtask.py:118`）却默认不收紧。

### 决策
- **轴 1（能力）**：按 `agent_type` 收紧 `--allowedTools`——reviewer/architect 默认只读集（Read/Grep/Glob），tester 加 Bash，developer 全量。配置可覆盖。
- **轴 2（审批）**：按 subtask 风险分级 `--permission-mode`——只读型用 `default`，写操作型在 headless 下维持 `bypassPermissions`（无头场景无人可批）。

### 理由
- 只读型子任务（架构分析、代码审查）从机制上无法误改代码——比事后验证更可靠。
- 不复制 Seatbelt/seccomp（成本过高），只借"能力分级"概念，复用已存在的 `--allowedTools` 通路。

### 代码验证
- `subtask.py:118-119`：`allowed_tools` 参数已支持，只需 executor 传入分级值
- `agents.py`：agent 类型的工具白名单已有定义，需从"建议"升级为默认强制

### 落地
`agents.py`（类型→工具集映射默认值收紧）+ `executor.py`（按类型传 allowed_tools）+ config 覆盖口。估时 1d。

---

## ADR-012：role_skill_map 四态激活矩阵

**状态**：Accepted（P2）
**日期**：2026-07-26
**来源**：竞品工程分析 §3.3（借鉴 Cursor/Windsurf 规则激活模型）

### 背景
Cursor 与 Windsurf 不约而同收敛到四态激活：Always / Intelligently / Glob / Manual，并标注 token 成本。agent_go 的 `role_skill_map.py` 已支持 agent_type/keywords/file_patterns 三种匹配（≈Glob+keyword）与全局/项目两层，缺 `always_on` 态与注入成本可见性。

### 决策
1. 规则 schema 加 `activation` 字段：`always | keyword | file_pattern | agent_type`（现有 match 逻辑映射保留，新增 `always`）。
2. `always` 规则的技能无条件注入所有子任务（典型场景：安全规范、提交信息规范）。
3. `skills.py` 渲染时标注每个注入 skill 的 token 占用（按 body 长度估算），写入 PLAN.md 与日志，帮用户控制注入成本。

### 理由
- 增量增强非重构：现有 match 配置完全兼容。
- "always" 是当前无法表达的真实需求（用户只能往每个子任务描述里手抄规范）。

### 代码验证
- `role_skill_map.py:14-43`：DEFAULT_MAP 的 match 结构，加 activation 键向后兼容
- `skills.py:render_skill_for_plan`：渲染点可顺便计算并透出 token 估算

### 落地
`role_skill_map.py`（activation 字段 + always 分支）+ `skills.py`（token 标注）+ 配置文档。估时 1d。

---

## ADR-013：worker env 密钥剥离 + evaluator fail-closed

**状态**：Accepted（P2）
**日期**：2026-07-26
**来源**：竞品工程分析 §2.6（借鉴 Codex 两阶段运行时 + guardian）

### 背景
Codex Cloud 在 agent 阶段剥离密钥；guardian 审批 fail-closed。agent_go 当前 `executor.py:919` 用 `os.environ.copy()` 把全部环境变量透传 claude；`evaluator.py` 语义评估仅记录不阻断。

### 决策
1. **密钥剥离**：新增配置 `security.sensitive_env_prefixes`（默认 `["AWS_","PROD_","_SECRET","_TOKEN"]`），`executor.py` 构建 worker env 时按前缀剥离（`AGENT_GO_API_KEY` 等 agent_go 自身需要的例外白名单）。
2. **evaluator fail-closed**：`evaluator.evaluate_semantic` 返回高置信"未达成"时，fail-closed 标 failed（而非仅 warning）；加载/调用异常仍按既有 fail-open 原则降级为"评估跳过"（见 `executor.py:650` 注释），两者不冲突——**判定明确的失败才阻断，判定不可得的放行**。

### 理由
- 密钥剥离是安全敏感项目的硬需求，成本仅一个过滤函数。
- fail-closed 让"独立模型判定"从参考信息升级为质量门，与 Codex guardian 同思路但更轻（聚焦验证判定点）。

### 代码验证
- `executor.py:919`：`env = os.environ.copy()` 透传点
- `executor.py:650-661`：evaluator 异常 fail-open 的既有设计，决策只改"明确未达成"分支

### 落地
`executor.py`（env 过滤 + evaluator 分支）+ `config.py`（sensitive_env_prefixes 默认值）+ 文档。估时 1d。

---

## 风险登记（详细）

### R1：TUI 主线程争用

| 项 | 内容 |
|---|---|
| **影响任务** | P3-1 TUI |
| **严重度** | 高——可能需架构重构 |
| **描述** | `curses.wrapper()` 和 `_run_pipeline()` 都要求主线程。当前 `cmd_run → _run_pipeline` 在主线程同步执行并阻塞。 |
| **代码位置** | `cli.py:461`（`_run_pipeline` 调用）、`tui.py:276`（`curses.wrapper`） |
| **连锁影响** | `_run_pipeline` 内 `signal.signal(SIGINT)` 只能在主线程注册（Python 限制），移入后台线程后中断传递需重新设计 |
| **缓解** | Sprint 3 做 50 行 PoC：(a) `_run_pipeline` 移入 `threading.Thread` (b) 验证 SIGINT 通过 `Event` 传递。失败则改为子进程架构或放弃 TUI 集成。 |
| **状态** | 待 PoC 验证 |

### R2：PLAN.md 解析器健壮性

| 项 | 内容 |
|---|---|
| **影响任务** | P2-1 $EDITOR Plan 编辑 |
| **严重度** | 中——可能导致超时 |
| **描述** | `plan_to_subtasks()`（`ui.py:308-368`）当前消费结构化 dict。从 Markdown 重建 dict 引入解析风险：中文 key、描述含 `###`、依赖环、ID 不连续、多值字段混用。 |
| **缓解** | Sprint 3 做 5 用例 PoC。交付分两步：先"只读 Markdown 展示"（零解析风险），再迭代"可编辑回写"。 |
| **状态** | 待 PoC 验证 |

### R3：`--quiet` 修复的连锁回归

| 项 | 内容 |
|---|---|
| **影响任务** | P0-1 ui.py 消灭 print |
| **严重度** | 中——CI 模式体验恶化 |
| **描述** | 将 `ui.py` 54 处 `print()` 改为 `console.*` 后，`--quiet` 下 Plan/子任务展示完全静默。但确认逻辑依赖用户看到 Plan 才能做 `[E]/[R]` 决策。用户会面对空屏+光标。 |
| **代码位置** | `ui.py:189-306`（confirm_plan 循环） |
| **缓解** | 确认菜单的提示行改用 `console.force()`（绕过 quiet）。非 TTY（stdin 管道）按 IDS §6.1 自动 `--yes`，不改变 `--quiet` 语义。 | Sprint 1 修复
| **状态** | Sprint 1 修复 |

### R4：stream-json 事件格式变更

| 项 | 内容 |
|---|---|
| **影响任务** | P1-5 stream-json 进度 |
| **严重度** | 低——可降级 |
| **描述** | claude CLI 版本升级可能变更 stream-json 事件 schema，导致 P1-5 的语义化映射失效。 |
| **缓解** | 解析层做版本检测（`_detect_tool_versions` 已存在），未知格式降级为纯计时器模式。 |
| **状态** | 监控 |

---

## 战略盲区记录

以下是深入分析中发现的原计划盲区及处理方式。

### 盲区 1：未区分"展示型"和"可编辑型"Plan

| 项 | 内容 |
|---|---|
| **问题** | Plan 紧凑展示（解决信息过载）和 $EDITOR 编辑（解决编辑低效）分散在不同 Phase，中间数周用户体验割裂 |
| **处理** | ADR 隐含决策：P0-5 紧凑展示移至 Sprint 4，与 P2-1 一起交付完整 Plan 交互 |
| **Roadmap 体现** | Sprint 4 包含 P0-5 + P2-1 |

### 盲区 2：TUI 默认化的用户接受度

| 项 | 内容 |
|---|---|
| **问题** | 计划假设 TUI 默认化是改进，但未验证终端用户是否接受强制 TUI |
| **处理** | ADR-003：降级为 `--interactive` 可选 |
| **IDS 修订** | §4.4 待更新 |

### 盲区 3：stream-json 数据利用缺失

| 项 | 内容 |
|---|---|
| **问题** | `_run_headless` 已解析 stream-json 事件，但进度展示完全未利用 |
| **处理** | ADR-004：新增 P1-5 作为进度数据源 |
| **Roadmap 体现** | P1-5 进入 Sprint 3 关键路径 |

---

## 决策修订历史

| 日期 | 决策 | 变更 | 原因 |
|---|---|---|---|
| 2026-07-26 | ADR-003 | IDS §4.4 TUI 从默认改为 `--interactive` 可选 | 主线程风险 + 用户接受度未验证 |
| 2026-07-26 | ADR-001 | P1-2 估时 2d → 0.5d | 代码验证数据已存在 |
| 2026-07-26 | ADR-005 | 删除 P0-3 emoji 标准化 | bikeshedding，用户无感知 |
| 2026-07-26 | ADR-007~009 | 新增 P1 借鉴项：验证分层+失败分类 / shell 链分解 / 可验证引用 | 竞品工程分析（Aider/Codex），见 competitive-engineering-analysis.md |
| 2026-07-26 | ADR-010~013 | 新增 P2 借鉴项：Steering 通道 / 两轴安全 / 四态激活 / 密钥剥离+fail-closed | 竞品工程分析（Codex/Cursor/Windsurf），同上 |

---

*新增决策请追加至末尾，按 ADR-NNN 编号。决策变更不删除原文，追加修订记录。*
