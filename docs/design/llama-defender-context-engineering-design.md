# llama-defender 上下文工程改造设计（append-only + epoch 压缩 + 动作台账）

> 状态：设计（v0.3，review 合理性复核后修正台账布局）
> 日期：2026-08-19
> 修订记录：v0.1 初稿；v0.2 并入外部 review（Kimi）反馈；**v0.3 取消独立台账块**——复核发现 v0.2「台账置 L2 之后」仍违反布局不变式（L2 每轮增长位于台账之前 → 台账整体每轮被推移 ~15K 重算）。根本约束：每轮 append 增长区域必须唯一。台账改为三载体：原生流（epoch 间）+ 压缩区台账格式（epoch 时）+ 代理侧 canonical（复述块呈现聚合）
> 输入依据：公开集本地臂批跑形态学观察（搜索兔子洞 / 代理每轮语义改写 / ~3min/轮）+ 业界调研（Manus / Anthropic / llama.cpp 官方文档与 tutorial）
> 关联：[llama-defender-integration-requirements.md](llama-defender-integration-requirements.md)（接口需求基线）、[harness-driving-architecture.md](harness-driving-architecture.md)（智能化能力盘点）
> 目标项目：`/Users/jinsongwang/APP/llama.cpp`（llama-defender 代理侧改造，不涉及 llama.cpp 内核修改）
> 背景讨论：本设计同时回应两个问题——① 每轮语义改写导致响应时间随轮数恶化；② 改写销毁「重复行为」证据，削弱模型元认知（搜索兔子洞的放大器之一）。

---

## 0. 问题定义

### 0.1 现状行为

llama-defender 对每个会话轮次执行**语义压缩**：收到客户端全量历史 → LLM 改写为摘要版 → 发给 llama-server。每轮都改写。

### 0.2 三个症状

| 症状 | 机制 | 实证 |
|------|------|------|
| 延迟恶化 | 改写历史 → KV 前缀缓存全量失效 → 每轮全量 re-prefill | 批跑 ~3min/轮（66 轮 / ~100K 上下文） |
| 成本恶化 | 会话总 prefill 成本 O(n³)（见 §1 成本模型） | 41 任务批跑预期 7-12 天 |
| 元认知证据销毁 | 「我已搜过 8 次同一查询」的重复模式被摘要抹掉，模型无法自检 | 748f534 搜索兔子洞 45+ 分钟无自愈 |

### 0.3 核心判断

「不压缩 → 长度膨胀 → 平方上涨」是**伪两难**。真正的元凶是「每轮改写」导致的缓存击穿，而非「不压缩」。正确解法：

```text
平时 append-only（缓存命中，增量 prefill）
压缩从「每轮」改为「按 epoch」（到阈值才压一次）
压缩只作用于 observation 正文，永不作用于 action 轨迹
```

---

## 1. 成本模型（为什么是 O(n³) → 线性）

Agent 会话 prefill 主导（Manus 实测 input:output ≈ 100:1）。设会话 n 轮、epoch 上限 S tokens：

| 策略 | 每轮 prefill | n 轮总成本 | 说明 |
|------|-------------|-----------|------|
| A. 每轮语义改写（现状） | 全量 r | **O(n³)**（Σr² ≈ n³/3） | 改 1 个 token，缓存从该点起全失效 |
| B. 纯 append-only | 增量 δ | O(n²)（Σδ·r ≈ n²/2） | 缓存全命中，但长度无上界 |
| **C. append-only + epoch 压缩（本设计）** | 增量 δ + 尾部复述块 | **O(n·S) ≈ 线性** | 每 epoch 一次 O(S) 重算，可摊销 |

**验证手段**：llama-server 日志 `slot update_slots ... prompt processing progress, n_past = X, n_tokens = Y`。`n_tokens/n_past` 即增量占比——现状应为 ≈1.0（全量），改造后应 <0.1。**Phase 0 先测这个数，坐实诊断再动工。**

---

## 2. 调研结论（业界基线）

| 来源 | 结论 | 对本设计的映射 |
|------|------|---------------|
| Manus（生产 agent 一手经验） | KV-cache 命中率是生产 agent 第一指标（缓存价差 10x）；上下文 **append-only**，序列化确定性；压缩必须**可恢复**（丢内容留句柄：URL/路径）；**保留失败记录**（擦除失败 = 模型无法更新信念）；上下文同质化会 few-shot 锁定行为 | §3 五层架构；§4.3 写入期压缩；§4.4 句柄规则；§4.2 action 轨迹不压缩 |
| Anthropic（官方） | Context editing：自动清除旧 tool results（保留最近 3 条），~30K 阈值；Compaction：阈值触发、保护近期上下文；**清 observation，不清 action 轨迹** | §4.6 epoch 触发条件与 L2 保护窗口 |
| llama.cpp / llama-server | `cache_prompt: true`（默认开）按最长公共前缀复用；`id_slot` 显式固定 slot（避免 `-sps` 相似度抖动换 slot）；`--slot-save-path` 可持久化 slot KV；KV 量化（`--cache-type-k/v q8_0`）省约一半 KV 内存 | §4.8 服务端配合 |

参考：
- [Context Engineering for AI Agents: Lessons from Building Manus](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Tutorial: KV cache reuse with llama-server（llama.cpp #13606）](https://github.com/ggml-org/llama.cpp/discussions/13606)
- [Anthropic: Memory tool / Context editing](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)
- [Claude Platform Docs: Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)

---

## 3. 总体架构：五层上下文

物理顺序（由布局不变式决定，见 §4.7）：
```text
┌─ L0 固定前缀     system prompt + 工具定义              永不改动（改它 = 全量失效）
├─ L3 压缩区       老轮次的台账格式（动作行+摘要+句柄）    epoch 间静态
├─ L2 原生流       本 epoch 内全部轮次（原生消息格式）      唯一的每轮 append 增长区
├─ 尾部复述块      聚合状态（去重计数/材料清单/轮次预算）    每轮重算（有界，≤600 tokens）
└─ L4 档案库       完整原始转录落盘                        不在 prompt 内（harness/人可查）
```

**分层编号（L0-L4）是概念职责，物理顺序由缓存不变式决定**：每轮 append 增长区域必须**唯一**（原生流），有界重写区（复述块）只能位于其后；中段任何插入/改写只能作为 epoch 事件。> 布局勘误史：v0.1 台账在前缀区（L0 后）——中段增长每轮推移其后全部内容；v0.2 台账在 L2 之后——L2 每轮增长位于台账之前，台账仍每轮被整体推移（~15K 重算）；v0.3 取消独立台账块，见 §4.2。

三层职责分离是本设计的核心不变式：

> **「台账永不压缩、正文滚动保留、档案落盘可召回、压缩按 epoch 不按轮。」**

| 层 | token 预算（默认） | 压缩策略 | 缓存影响 |
|----|------------------|---------|---------|
| L0 | 客户端决定（~5-10K） | 永不 | 稳定前缀，缓存根基 |
| L1 台账（概念层，三载体见 §4.2） | 压缩区行 ~30-50/调用；原生流内即原生消息 | 原生流部分永不压缩；压缩区部分超长时保计数聚合 | 随所属物理区（L2/L3） |
| L2 原生流 | epoch 间无上限增长；重切时保留 K=8 轮（~8-16K） | epoch 内逐字 | 唯一每轮 append 区，缓存增量命中 |
| L3 压缩区 | 每轮 ≤80 tokens 摘要 + 句柄 | epoch 时一次性收编 | epoch 间静态；每 epoch 一次缓存重建 |
| 尾部复述块 | ≤600 | 每轮确定性重算（无 LLM 调用） | 每轮重发（有界重算成本） |
| L4 | 无限（磁盘） | 不压缩 | 无（不在 prompt 内） |

---

## 4. 详细设计

### 4.1 L0 固定前缀纪律

代理对 L0 区域的**唯一责任是不碰它**：

- 不注入时间戳、随机 id、会话计数等任何变化内容（Manus：秒级时间戳即可杀死命中率）
- 工具定义列表全程不变（中途增删工具 = 其后全部缓存失效，且模型易 schema 混乱）
- 若需要注入代理侧元信息（如版本标记），一律放**尾部复述块**

### 4.2 L1 动作台账（元认知证据层）

**目的**：让「重复行为」在上下文中显式可见——这是搜索兔子洞的对症结构，同时是轮级看门狗的数据源。

**v0.3 重定义：台账不是一个 prompt 内独立增长的块，而是三种载体**：

| 载体 | 位置 | 职责 |
|------|------|------|
| ① 原生轮次流 | prompt 中部（L2） | epoch 之间「台账」就是原生流本身——append-only，动作轨迹逐字在场（Manus "keep the wrong stuff in"） |
| ② 压缩区台账格式 | prompt 前部（L3） | epoch 压缩时，老轮次收编为台账行（动作+摘要+句柄+计数）——只在 epoch 重写 |
| ③ 代理侧 canonical 台账 | 不进 prompt（代理内存/落盘） | dup/last_dup_turn/材料清单的计算基础 + 看门狗数据源；聚合结果经复述块呈现给模型 |

> v0.3 勘误（连续第二次位置修正，本次根治）：独立台账块这个构造**本身不成立**。v0.1 置前缀区——中段增长每轮推移其后内容；v0.2 置 L2 之后——L2 每轮增长位于台账之前，台账（~15K）仍每轮被整体推移重算，每轮成本 ≈ 18-20K 而非宣称的 2-4K。根本约束（§4.7 布局不变式）：**每轮 append 增长区域必须唯一**——两个增长块无论谁先谁后，后者都被前者推移。取消独立块后，该约束由原生流单独满足。

台账行格式（用于压缩区 ② 与代理侧 ③，确定性序列化、字段定序）：

```text
[#42] search("github ansible pull 80376") → serper | 654 chars | top: "fix incorrect dnf..." | dup=4 | last=#48
[#43] fetch("https://raw.githubusercontent.com/.../pkg_mgr.py@devel") → 4310 chars | saved: work/upstream_pkg_mgr.py
[#44] read("pkg_mgr.py") → 1204 lines | local bug version
```

字段：`轮次 | 工具 | 目标（截断） | 结果规模 | 一句话摘要 | 重复计数 dup | 末次重复轮次 last_dup_turn`

**规则**：
1. 原生流（载体①）append-only，epoch 内任何压缩不得触碰（擦除失败证据 = 模型无法更新信念）
2. 重复计数 `dup` 与 `last_dup_turn` 由代理在 canonical（载体③）上确定性计算（查询规范化后 hash 对比），不是 LLM 生成——`last_dup_turn` 让模型区分「陈年重复」与「刚刚重复」；呈现路径：压缩区行内 + 复述块聚合
3. 超长会话（>1000 调用）压缩区自身膨胀时的**降级聚合**：同目标条目合并、保留计数与首次/末次轮次——仍然不丢「重复可见性」
4. 看门狗消费载体③（HTTP 端点），与 prompt 内容无关——鸭子洞检测不依赖模型看得见

### 4.3 写入期压缩（write-time compression）——替代「每轮改写」的关键机制

**原则：observation 在写入那一刻就定型为它的保留形态，之后永不改写。**

工具结果返回时，代理按类型应用保留模板（确定性规则，无 LLM 调用）：

| 工具类型 | 保留模板 | 默认预算 |
|---------|---------|---------|
| 搜索结果（serper/WebSearch/searxng） | top-3 organic 摘要 + 查询串句柄 | ≤1.5K tokens |
| 文件读取 | head + tail + 完整路径句柄 | ≤2K tokens |
| curl/HTTP body | 关键段 + URL 句柄 | ≤2K tokens |
| 命令输出 | stdout/stderr head+tail + 退出码 | ≤1.5K tokens |
| 错误/异常 | **全文保留**（负反馈是稀缺信号，不截断） | ≤1K tokens |

**最小阈值规则**：原始结果 ≤ 该类预算时**逐字保留、不做模板包装**——小结果包装后反而比原文长（句柄 + 模板开销），反复累积成为膨胀源。

**句柄（handle）定义**（Manus 可恢复原则）：能够重新取得该内容的充分信息——URL / 文件路径+offset / 规范化查询串。内容可丢，句柄必留。

### 4.4 L2 近期正文（保护窗口）

- 本 epoch 内的全部轮次（原生消息格式，observation 已过写入期压缩）逐字保留——这是**唯一的每轮 append 增长区**（§4.7 布局不变式）
- 原生流内不做任何二次处理（Anthropic compaction "保护近期" 同款语义）
- K 的语义（v0.3 澄清）：K 不限制 epoch 间原生流的增长，只在 **epoch 重切**时生效——原生流保留最近 K 轮，更老轮次收编入压缩区。K 的取值依据：模型「一轮记忆窗口」的实证（轮 58 自省、轮 59 复发 → 近期窗口是元认知的主要作用区）

### 4.5 L3 压缩区（epoch 压缩目标）

epoch 重切时，原生流中 K 窗口外的轮次收编入压缩区，表示为台账格式（§4.2 载体②）：

```text
[#12-#15] search("ansible 80376") ×4 无新信息（dup）
[#16] fetch devel pkg_mgr.py 成功，存 work/upstream_pkg_mgr.py（句柄：URL）
[#17] read 上游测试文件 成功（句柄：路径）
```

- **动作行是压缩区的主体**（台账行含动作+结果摘要+句柄+计数，单一格式无双写）；assistant 的推理文本不保留（老轮次的价值在动作轨迹与可恢复性）
- 摘要生成默认用**确定性抽取模板**（首行 + 状态 + 句柄），不调 LLM——零成本、零延迟、可复现；LLM 摘要作为可选开关（质量档）

### 4.6 尾部复述块（recitation，Manus todo.md 模式）

每轮在**最新消息之后、生成点之前**（即 prompt 最末），确定性重发一个有界状态块：

```text
--- proxy state ---
turn: 66/150 | ctx: 74K/128K | epoch: 3
search dedup: "github ansible pull 80376" ×8 (no new info since #40)
materials in hand:
  - work/upstream_pkg_mgr.py  (upstream FIXED version, fetched #43)
  - pkg_mgr.py                (local BUG version)
  - tests/test_pkg_mgr_upstream.py (reference tests, #45)
next-step hint: materials sufficient for diff-based fix
---
```

内容三部分，全部由代理从 L1 台账**确定性推导**（无 LLM 调用）：
1. **去重计数**：重复查询及次数（兔子洞显性化）
2. **材料清单**：已获取的关键工件（即「材料清单检查点」，对「把拿 PR diff 误设为必需条件」的直接解）
3. **轮次/上下文预算**：进度感知

预算 ≤600 tokens。这是本设计中**唯一每轮变化的内容**，其重算成本有界且不击穿前缀缓存（位于尾部）。

**合成负反馈（可选开关，默认关）**：当同一规范化查询 `dup ≥ 3` 时，在下一条 observation 头部注入：

```text
[proxy] 注意：查询 "github ansible pull 80376" 已执行 8 次，结果无变化。请勿再次搜索同一查询。
```

属 append-only 流的一部分，缓存安全。**开启时必须在转录中标注**（bench verdict 口径：干预臂 vs 观察臂分开统计）。

**注入位置作为 Phase 2 A/B 变量**：A = observation 头部（模型视作环境反馈，心理模型更「硬」）；B = 尾部复述块内（零额外成本、近因注意力更强，但模型可能视作「代理提示」而非环境反馈）。两臂各测兔子洞率与任务通过率。

### 4.7 序列化与缓存纪律

1. **布局不变式（缓存根基，v0.3 强化）**：每轮 append 增长区域必须**唯一**（原生流）；有界重写区（复述块 ≤600）只能位于其后。推论：任何「独立的每轮增长块」都不能存在于原生流之前——两个增长区无论谁先谁后，后者都被前者整体推移（v0.1 前缀台账、v0.2 尾部台账两次错误同源）。中段插入/改写（压缩区重建、K 窗口重切）只允许作为 epoch 事件发生
2. **JSON key 定序**（`json.dumps(..., sort_keys=True)` 或固定 schema 顺序）——库不保证 key 顺序稳定时会静默击穿缓存
3. 代理维护**规范历史（canonical history）**：**客户端历史是 ground truth，canonical 是代理的派生视图**。客户端（claude CLI）每轮发全量历史（无状态 API），代理与之做公共前缀 diff → 识别新增轮次 → 写入期压缩 → append 到 canonical → 按物理顺序发送 `L0 + L3压缩区 + L2原生流 + 尾部复述` 给 llama-server；代理注入块（复述块/负反馈）只在发送时附加、不进 canonical（或带标记，diff 时忽略）
4. 客户端历史与 canonical 前缀不匹配时（客户端侧自身做了裁剪/微压缩）：降级为全量重建该会话视图（正确性优先），并记指标 `canonical_mismatch` 监控发生频率
5. epoch 压缩点尽量选在**轮边界/工具序列间隙**（避免拆散同一轮的 action/observation 对；推理链保护见 §4.9）

### 4.8 llama-server 服务端配合

| 项 | 配置 | 说明 |
|----|------|------|
| 前缀缓存 | 请求带 `cache_prompt: true`（默认开，显式声明） | slot 保留 KV，按最长公共前缀复用 |
| slot 固定 | 请求带 `id_slot`，按会话 key 分配 | 避免 `-sps` 相似度抖动导致换 slot 全量重算；批跑场景 `-np` 按**并发会话数**配置，每会话独占一 slot |
| KV 量化 | `--cache-type-k q8_0 --cache-type-v q8_0` | 省约一半 KV 内存 → 同显存可支撑更大 epoch 上限 S |
| slot 持久化（可选） | `--slots --slot-save-path` | 长会话跨 backend 重启恢复（Phase 3） |

**⚠️ 前置验证（Phase 0 一并做）**：确认所用模型（Qwen3.8-27B）非 SWA 混合架构，或 SWA 下前缀缓存语义实测正常——有社区报告 SWA 模型缓存行为异常（误失效/错误复用）。精确验证流程：
1. 第 1 次请求：前缀 P + 尾部 T1 → 应见 `n_tokens = |P|+|T1|`（全量）
2. 第 2 次请求：前缀 P + 尾部 T2 → 缓存正常应见 `n_tokens ≈ |T2|`（仅增量）
3. 异常信号：`n_tokens` 包含部分/全部 P 的重算（SWA 窗口外前缀不可复用）
重复 10 次排除偶发。

### 4.9 epoch 压缩状态机

```text
运行态（append-only）：
  每轮：diff 客户端历史 → 写入期压缩新 observation → append → 重算尾部复述块 → 发送

触发检查（每轮发送前）：
  total_tokens > epoch_trigger（默认 min(65% × ctx_max, ctx_max - 8K)）？

压缩态（每 epoch 一次）：
  原生流中 K 窗口外的轮次 → 收编入压缩区（台账格式：动作行 + 摘要 + 句柄）
  保留：L0 + 压缩区 + 原生流重切为最近 K 轮 + 尾部复述块
  缓存重建范围（精确）：公共前缀止于 L0 → 需重算 = 新压缩区 + 原生流 + 复述块
  （v0.2 勘误：v0.1 只说「缓存全量重建」，实际范围是 L0 之后的全部；量级见 §6.3）
  记指标：epoch_count、压缩前后 token 数

回退保护：
  压缩后若 total_tokens 仍 > trigger（极端：台账/窗口自身超限）
  → 收紧：K=4，L3 摘要再减半
  → 硬上限：仍超限则停止截断，向客户端返回 context 超限错误——
    代理无权把历史压缩到失真来「假装放得下」；超长会话该结束的是任务，
    暴露给 harness/人决策（proxy 无 UI，错误返回是它的「拒绝」通道）

推理链保护（epoch 触发时机启发式）：
  最近 2 轮呈连续 thought → tool_use → thought（无稳定 observation 间隙）时，
  延迟 epoch 到下一个完整轮边界，避免切断连贯推理链
```

### 4.10 可行性边界：什么能/不能在代理层完成

代理的位置：`claude CLI（客户端）→ llama-defender（代理）→ llama-server（后端）`。代理**可见**：客户端每轮发来的全量历史（含 tool_use/tool_result）、自己发给后端的请求、后端的响应流。代理**不可见**：工具的实际执行（发生在客户端）、客户端内部状态。按此边界分三类：

| 类别 | 内容 | 说明 |
|------|------|------|
| **① 代理进程内可完成** | L1 台账、写入期压缩（§4.3）、L2/L3、尾部复述块、合成负反馈、去重计数、epoch 状态机、canonical diff、`cache_prompt`/`id_slot` 请求参数、看门狗 HTTP 接口 | 全部是「请求内容变换 + 请求参数附加」，与现有语义压缩同机制，仅策略不同 |
| **② 项目管辖、非代理进程** | KV 量化、`-np` slot 数、`--slot-save-path` | 属 llama-server **启动参数**，走 manage.sh / profiles 配置层（本项目本就管理服务生命周期）。注意 `-c` 是总量均分到各 slot（`-c 1024 -np 2` → 每 slot 512）：slot 数与单会话上下文上限是 tradeoff，批跑串行场景 `np=1-2` 即可，agent_go `--parallel 3` 场景需相应加大 `-c` |
| **③ 代理层做不到（需降级）** | 见下两条边界 | — |

**边界 1：模型可主动调用的档案召回工具（L4）做不了（除非架构升级）**
工具定义来自客户端；代理私自注入的工具若被模型调用，tool_use 会返回给 claude CLI 执行 → 客户端报未知工具错误。绕过需做「隐藏内循环」（代理拦截自己注入的工具调用、自行执行、喂回 llama-server 继续生成，对客户端呈现单一响应流）——涉及多轮内部请求、计量、流式语义，属架构升级，**明确推迟**。降级方案：
- 复述块被动携带（代理按启发式决定注入什么，模型不主动召回）；
- 看门狗/档案查询做成**给 harness 的 HTTP 端点**（人/批跑器消费，非模型消费）。

**边界 2：客户端行为只能监控、不能约束**
L0 内容（system prompt + 工具定义）由客户端决定，代理只能「不动它」，不能「修它」。若 claude CLI 自身往 system 塞变化内容或做自带 microcompaction，代理只能通过 `canonical_mismatch` 指标发现，无法阻止——此时缓存收益打折属客户端侧问题，记录并单独反馈。

**大前提：代理从「逐请求无状态压缩」升级为「有会话状态」**
现状每轮改写很可能是无状态的（每请求全量重压）。本设计要求代理维护 canonical history（客户端原始历史 + 压缩视图双份）与会话生命周期（TTL/驱逐/内存上限/并发安全）——这是 Phase 1 最大的架构改动点，不是策略参数。

**两个实现级注意**：
1. 「写入期压缩」在代理层的实际时机是**下一请求到达时**（工具结果随客户端下一次请求到达，代理无法在结果产生的瞬间截获）——对后端视角语义不变（发送历史前已定型），但 canonical 与客户端历史是两套账，diff 逻辑须以客户端原始历史为基准、忽略代理自身注入块（复述块/负反馈需带注入标记）。
2. token 精确计数依赖 llama-server `/tokenize` 端点（可缓存），或按 chars/4 估算留余量；epoch 触发的 `ctx_max` 从 `/api/status`（R11）或配置同步获取。

---

## 5. 实施计划

### Phase 0：诊断确认（0.5 天，先行）

> **状态：E1 完成（2026-09-06），E2/E3 工具就绪待静默窗口**。工具：`tools/phase0_cache_diag.py`（analyze / probe / probe-swa 三子命令，主动探针带批跑活跃守卫）。
>
> **环境修正**：后端实际为 **rapid-mlx（MLX 引擎）**，非 llama-server——§4.8 的 llama-server 配置表（`cache_prompt`/`id_slot`/KV 量化）不适用，MLX 侧为「条目式 prompt cache（LCP 对齐 + Metal 压力驱逐）」，语义不同但五层架构与 append-only 原则不变。SWA 验证对象改为 MLX 条目缓存语义（E3）。

**E1 结果**（`llama-server.log` 全量 17,787 请求 / 5.47 亿 prompt tokens，被动零干扰）：

| 指标 | 数值 | 解读 |
|------|------|------|
| 增量占比（token 加权） | **0.3712** | 63% 的 prefill 算力是重算——击穿真实存在，但非 100% 全坏 |
| ratio 分布 | **双峰**：36% 请求 ≤0.1（命中时 99%+ 复用），54% 请求 >0.9（整段全量）；P50=1.0 | 系统二态：对齐即近乎完美，发散即全损——问题不在缓存能力，在**对齐率** |
| LCP unavailable | 6,918 次，重算 1.74 亿 tokens，其中 **78.2%（1.36 亿）本可复用** | **头号击穿源**：条目与请求在消息内部发散 → 整条作废（all-or-nothing），99% 相同也全量重算；占总 prefill 算力 **67%** |
| 压力驱逐 | 5,207 事件 / 8,412 条目（cache_max 7.7GB vs 模型活跃 25-29GB） | 第二击穿源：可命中条目在下轮到达前被挤出 |
| 首 token 延迟 | P50 11.1s / P95 164.1s | 与批跑观测 ~3min/轮吻合（P95） |
| 上下文规模 | chars P50 59K / P95 164K / max 1.29M | 上下文膨胀真实且严重 |

**诊断修订**（替代 v0.1 §0.3 的「每轮改写 → ≈1.0 全击穿」单因假设）：
1. 引擎前缀缓存能力已被证明可用（36% 请求增量 ≤0.1）——「引擎/SWA 缓存坏了」假设基本排除（E3 受控验证待做，收尾确认）
2. 击穿主因是**对齐失败**：中段发散（改写/客户端微调）触发 LCP non-trimmable 整条作废 + Metal 压力驱逐。append-only 纪律（§4.7 布局不变式）仍是对的药，但 **Phase 1 头号优化目标更新为「部分条目复用」**（LCP trim：发散点截断复用而非整条丢弃）——仅此一项可把增量占比从 0.371 压到 ~0.12，叠加驱逐治理可望 <0.1 达标
3. 缓存预算（7.7GB）相对上下文规模偏小，驱逐治理需一并纳入 Phase 1（预算加大或条目粒度细化）

- llama-defender 记录每轮请求的 llama-server 日志指标：`n_past / n_tokens / prompt_eval_ms`（✅ 已由 rapid-mlx 原生日志覆盖，`[schedule]` 行含 prompt_tokens/tokens_to_prefill/cached）
- ~~输出：每轮增量占比曲线~~（✅ `analyze` 子命令输出 + `--json`）
- **隔离对照实验**：绕过代理直发 append-only（`probe` 子命令，⚠️ 批跑运行期禁用——探针会在 Metal 压力下驱逐在跑会话缓存条目）
- 同时验证 SWA 缓存语义（`probe-swa` 子命令；MLX 语义下改为验证「固定前缀 + 变尾不产生部分前缀重算」）

### Phase 1：核心改造（1-2 天）——append-only + 写入期压缩 + epoch

| 改动点 | 内容 |
|--------|------|
| llama-defender | canonical history + 前缀 diff + 写入期压缩模板（§4.3）+ epoch 状态机（§4.9）+ `cache_prompt`/`id_slot` 请求参数 |
| 不动 | llama.cpp 内核、客户端、模型配置 |

验收门禁：
1. 每轮增量 prefill 占比 >90%（`n_tokens/n_past < 0.1`）
2. 60+ 轮会话延迟分档（v0.2 修订，原 <15s 单目标不现实——epoch 轮重算 40-70K tokens 本身就需 20-70s）：非 epoch 轮 P90 <15s；epoch 轮 P90 <60s；整体 P90 <30s（对照现状 ~3min/轮，估算依据见 §6.3）
3. 压缩正确性：任意轮次可通过句柄重新取得原始内容（抽查）
4. 会话端到端时长对比报告（同任务、双臂）

### Phase 2：台账 + 复述块 + 看门狗数据源（1 天）

- L1 台账 + 尾部复述块（去重计数 + 材料清单）
- 看门狗接口：暴露台账查询（供批跑 harness 轮级 no-progress 检测消费——agent_go 任务级 `diff_stat_hash` 模式的轮级移植）
- 合成负反馈注入实现，**默认关**，开关进配置

验收门禁：
1. 探针任务集（含已知兔子洞场景）无效轮占比下降
2. 复述块 token 预算达标（≤600）
3. 注入开关状态在转录中可追溯

### Phase 3（可选）：档案召回 + slot 持久化

- L4 档案库访问：**先做降级形态**——`GET /api/session/<key>/archive` 查询端点（harness/人消费，§4.10 边界 1 的降级方案）；「模型可调用的召回工具」需隐藏内循环架构升级，明确不在本期
- `--slot-save-path` 持久化（跨 backend 重启）

---

## 6. 验证与指标

### 6.1 核心指标

| 指标 | 定义 | 目标 |
|------|------|------|
| **前缀命中率** | `1 - n_tokens/n_past`（llama-server 日志） | 运行态 >90% |
| 单轮 prefill 耗时 | prompt_eval_ms 分布（P50/P90，**epoch 轮与非 epoch 轮分档统计**） | 非 epoch 轮 P90 <15s；epoch 轮 <60s；整体 <30s |
| 会话端到端时长 | 任务开始到 done | 对照现状显著下降（预期量级见 §6.3） |
| epoch 数 | 每任务压缩次数 | 个位数（非每轮） |
| 无效轮占比 / 重复查询率 | 台账推导 | Phase 2 A/B 对比下降 |
| canonical_mismatch | 客户端历史与 canonical 前缀不匹配次数 | 监控项（频率高说明客户端自身在改历史） |

### 6.2 A/B 实验设计

```text
任务集：含已知兔子洞场景的探针集（10 个量级）+ 公开集抽样
臂 1（基线）：现状每轮语义改写
臂 2：Phase 1（append-only + epoch）
臂 3：Phase 2（+ 台账/复述块，注入关）
臂 4：Phase 2 + 合成负反馈（注入开）——单独口径，verdict 标注干预
双产出：延迟/成本指标 + 形态学指标（兔子洞率）
```

臂 2 vs 臂 1 分离**纯延迟收益**；臂 3 vs 臂 2 分离**元认知证据保留收益**（检验「压缩销毁元认知证据」假设）；臂 4 vs 臂 3 量化**主动干预收益**。

### 6.3 预期量级（估算，需实测校准）

- 修复前：~100K 全量 prefill × 每轮 ≈ 50-180s/轮（27B 本地 prefill ~500-2000 tok/s）+ 每轮压缩 LLM 调用 → 与 3min/轮观测吻合
- 修复后（非 epoch 轮）：增量 ~1-3K + 复述块 ≤600 ≈ 2-4s（**v0.3 起该估算才严格成立**——v0.2 的独立台账块每轮被原生流推移 ~15K，实际会是 8-20s）
- 修复后（epoch 轮）：重算范围 = 新压缩区 + 原生流 + 复述 ≈ 40-70K → 20-70s（按 1000-2000 tok/s）——这是验收分档的由来，也是 S 调优的动机（开放问题 1）
- 41 任务批跑：从 7-12 天量级压缩至 1-2 天量级（若兔子洞率同步下降还可能更短——无效轮本身是时长主贡献者）

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| SWA 混合架构缓存语义异常 | 前缀缓存收益不成立 | Phase 0 实测验证；异常则改用「窗口内 append-only」（等价退化，收益打折但仍优于每轮改写） |
| epoch 边界缓存重建 | 每 epoch 一次全量重算 | 固有代价；S 越大摊销越好，但受显存与长上下文性能退化约束——S 实测调优（开放问题 1） |
| 写入期压缩丢关键信息 | 模型缺材料 | 句柄可恢复（Manus 原则）+ L2 保护窗口 + 错误全文保留；A/B 观察通过率不劣化 |
| 台账膨胀（超长会话） | L1 自身成为大头 | §4.2 降级聚合（保计数合并）；台账预算上限告警 |
| 复述块扰动模型行为 | 个别任务变差 | 有界 ≤600 tokens；A/B 臂 3 单独验证；必要时按任务类型关闭 |
| 客户端自身微压缩 | canonical diff 失配 | §4.7 降级重建 + canonical_mismatch 监控 |
| 合成负反馈污染 bench 口径 | verdict 不可比 | 默认关；开时转录标注、单独臂统计 |
| L3 摘要信息丢失致任务失败 | 模型缺关键材料 | 句柄可恢复 + L2 保护 + 错误全文保留；A/B 加「摘要信息保留率」探针（随机抽查 L3 摘要，人工判定关键信息是否保留） |
| epoch 打断连贯推理链 | 压缩时机切断 thought 链 | §4.9 推理链保护启发式（连续推理中延迟到轮边界） |

---

## 8. 开放问题

1. **S（epoch 触发阈值）取值**：默认 `min(65% × ctx_max, ctx_max - 8K)`。灵敏度实验：固定任务，S=50%/65%/80% 三档，最小化「epoch 次数 × 单次重建耗时」乘积，同时观察通过率不劣化。
2. **K（L2 窗口）取值**：默认 8。4/8/12/16 网格；任务类型分层（代码修复 vs 查询类）最优 K 可能不同。
3. **复述块注入位置**：末尾（缓存友好 + 近因注意力）；L0 后固定区每轮击穿其后缓存，不推荐（仅记录）。负反馈的两种注入位置 A/B 见 §4.6。
4. **与 agent_go 侧 `/compact` 类机制的边界**：短期靠 `canonical_mismatch` 降级监控；长期方向是协商「客户端不做微压缩、压缩全权归代理」（单一责任方），需向 agent_go 侧提需求。
5. **LLM 摘要档**（L3 质量增强）：默认不开。确定性模板先行；每 epoch 一次 LLM 调用的延迟/成本很可能不抵质量收益。
6. **L3 与台账的冗余简化**：epoch 后的老轮次是否可仅由「台账 + 句柄」代表（L3 层取消）——台账已含动作 + 结果摘要 + 计数。若 A/B 显示通过率不劣化，可砍一层。

---

## 11. Phase 1 详细设计：LCP 边界检查点复用 + 驱逐治理

> 状态：**P1a 全链路已上线**（2026-09-06，llama.cpp `9ae90c8` + `d87b3f7` + `b77f643`；开关 `PROXY_CACHE_LCP_SNAPDOWN=1` 已进生产 conf）。**Gate A PASS**：模型级逐 token 一致，snap 9.4s vs cold 279.0s（**29.7×**），单请求复用 97.1%。剩余：真实负载 phase0 复测（Gate B1）、P1b 段链去重、E2/E3 探针（静默窗口被并行实验流量占用，活跃守卫正确拦截推迟）
> 实现位置：`vllm_mlx/memory_cache.py`（引擎侧；homebrew Cellar venv 内，补丁副本归档 llama.cpp 仓库）
> 验收工具：`tools/phase0_cache_diag.py`（before/after 同口径）

### 11.1 根因链（代码级实证，已核验至源码）

| # | 环节 | 证据 |
|---|------|------|
| 1 | Ornith（qwen3_5_moe）是**混合注意力架构**：线性注意力层用 `ArraysCache(size=2)` 递归状态，仅部分层用全注意力 `KVCache` | `mlx_lm/models/qwen3_5.py:304` `make_cache()` |
| 2 | `ArraysCache.is_trimmable()` 返回 False——线性递归态 S(L) 不可倒推 S(l<L)（数学上不可逆），拒绝 trim 是**正确行为** | `memory_cache.py:1414` `_RECURRENT_STATE_CACHE_CLASSES = {"ArraysCache", "MambaCache"}` |
| 3 | LCP 复用路径要求整条条目所有层可 trim → **该模型所有条目都含 ArraysCache → LCP 复用被结构性禁止** | `memory_cache.py:2096-2136`（`_layer_forbids_trim` 任一层 True 即整条拒绝） |
| 4 | 轮次重渲染天然产生尾部发散（generation header 摘除、tool_use→tool_result 重编码、代理 epoch 重写）→ 后继轮落入 LCP 路径 → 全量重算 | E1：6,918 次 LCP unavailable / 1.36 亿 tokens 可复用而未复用 |
| 5 | 纯 append 轮走 supersequence/prefix 路径不受影响 → 命中时近乎完美 | E1：46% 请求 HIT，命中桶增量 ≤0.1 |

**结论**：不是 trim 实现有 bug，是 **trim 这件事对混合架构不成立**。正确的机制不是「回卷」，而是「**检查点**」——线性态在消息边界 B 处的快照 S(B)，对共享前缀 0..B 的任何请求都是合法续算状态。

### 11.2 机制：边界检查点 + LCP 降档（snap-down）

**实测修正（2026-09-06，对 6,918 个 LCP 事件逐条量化，修正 v0.1 的两个错误假设）**：
1. **中位发散点在请求的 81% 处**（非尾部）——理想 snap-down（B=LCP）后 LCP 路径仍需重算 3,825 万 tokens（均值 5,489/次），但对比现状 1.74 亿已是 **4.6×**
2. **单边界检查点仅救 2.6%**——现有 boundary_snapshot 位置（条目末尾前 ~15 tokens 的 generation header 处）几乎总在发散点之后 → **多边界捕获是生产侧必做项**，不是优化项
3. **检查点只需存线性态**——KV 层可 trim，fetch 时从条目数组直接切片；每检查点 MB 级 vs KV 条目 GB 级（内存开销 ~10% 量级），v0.1「检查点 ≈ KV@B」的内存估算作废

**核心不变式**：`全注意力 KV 可 trim；线性递归态不可 trim 但可检查点。复用永远从检查点出发，绝不从条目末态回卷。`

**Store 侧（已实现，`memory_cache.py`）**：
1. 条目增加 `nontrim_layer_indices`（创建时算一次）+ `linear_checkpoints: {B → [nontrim 层状态快照]}`，检查点内存计入 `entry.memory_bytes`（驱逐核算诚实）
2. `store(..., linear_checkpoints=)` 新 kwarg；`snapshot_linear_states(cache)` 模块级函数供 scheduler 在 prefill 跨越消息边界时捕获（递归态不可事后回卷，只能现场存）
3. 快照 deepcopy 状态容器（防原地更新污染），MLX 数组不可变故引用共享安全

**Fetch 侧（已实现，LCP 分支 flag 门控）**：
```text
现路径：LCP 候选 → 任一层 forbids_trim → 整条拒绝（现状，全量重算）
新路径：LCP 候选 → 检查点表找 B ≤ lcp 的最大 B_i
        → _build_snapdown_view：KV 层 _trim_cache_offset 切片到 B_i
          + 递归层 _restore_recurrent_layer 恢复检查点态
        → 任一层失败 / 无 B ≤ lcp / 结构不一致 → 安全回退既有 MISS 路径
        → 成功：match_type=lcp_snapdown，snapdown_hits/tokens_saved 进 stats
```
- exact / supersequence / prefix 路径**零改动**（单元测试 T5 验证 prefix 路径含 boundary 条目场景不受影响；supersequence 对递归层的既有跳过是 issue #427 已知行为，非本补丁职责）

**正确性约束（Gate A，一票否决）**：
- 检查点复用 vs 全量 prefill，greedy 解码（temp=0）输出**逐 token 一致**（探针集 ≥10 个多轮上下文）
- 已知理论风险：线性态累积误差在「检查点快照 → 续算」与「全量重算」两条路径间可能有浮点级差异——若 Gate A 出现 token 分歧，降级为「检查点仅用于 KV 层、线性层从 B_i 重算」（仍省 ~99% 算力，因线性层算力占比小）
- 单元测试已覆盖机制层（15→7 项真实 mlx 层断言：flag 关逐字节一致、snap-down 命中、无候选/结构损坏回退、条目本体隔离）；**模型级 Gate A 待静默窗口**

### 11.3 驱逐治理（与 11.2 耦合，不可分开治）

E1 第二击穿源：5,207 次压力驱逐 / 8,412 条目（cache_max 7.7GB，条目为整上下文单拷贝，40K tokens/条 → 有效容量仅个位数条目）。

1. **链去重（主杠杆）**：条目结构从「每轮全量拷贝」改为「段链」——后继轮只存 delta 段 + 引用前驱检查点。会话内存 ≈ 1 份全量 + N 份增量，有效容量提升一个数量级
2. **链感知驱逐**：被链引用的前驱不可独立驱逐（引用计数）；优先驱逐「无引用的叶子 + 最久未命中链」；现有 LRU `move_to_end` 语义保留
3. **预算暂不动**：Metal 活跃 25.1GB / 峰值 29.2 / cap 28.1——7.7GB 上调空间极小，去重才是真杠杆；去重后重估 `cache_max`
4. 新指标：`evicted_reusable_entries`（被逐条目中「最近 N 轮命中过」的计数，健康应 ≈0）

### 11.4 与代理层的协同（依赖关系）

- append-only 纪律（§4.7）**减少发散频率**（少产生 LCP 事件）；边界检查点**降低残余发散的代价**。两层互补，各自独立有效
- **字节级确定性是硬依赖**：epoch 压缩区若跨 epoch 渲染不一致，发散点移到上下文前部 → snap-down 重算成本 = 全文 - 前部位置，重新爆炸。§4.7 确定性序列化（key 定序、模板固定）从「缓存友好」升级为「正确性前提」
- 压缩区内容跨 epoch 不变的属性（§3「epoch 间静态」）由此获得量化红利

### 11.5 验收（phase0 工具 before/after，同口径）

| 指标 | Before（E1 实测） | Gate B1（snap-down） | Gate B2（+去重/驱逐） |
|------|------|------|------|
| 增量占比（token 加权） | 0.3712 | **<0.15** | **<0.10** |
| LCP unavailable 浪费 tokens | 1.36 亿/全量日志 | ↓≥90% | ↓≥95%（事件本身应趋零） |
| 压力驱逐（evicted_reusable） | 8,412 条目 | 不劣化 | ≈0 |
| TTFT P95 | 164.1s | <60s | <30s |
| Gate A 输出一致性 | — | 逐 token 一致（或降级模式一致） | 同左 |

新指标埋点：`lcp_snapdown_events / snapdown_reused_tokens / checkpoint_overhead_bytes / evicted_reusable_entries`（进 `/metrics` 会话维度，即 R16 载体）。

### 11.6 风险与回退

| 风险 | 对策 |
|------|------|
| 检查点快照内存放大（M×每条目） | M=8 起步 + 只存线性态增量的最小快照；实测 checkpoint_overhead_bytes 后调 |
| 检查点复用的浮点路径差 → 输出分歧 | Gate A 一票否决；降级模式（线性层从 B_i 重算）兜底，仍省 ~99% |
| 链引用计数的并发竞态 | 单 worker（--max-num-seqs 1）内串行，风险低；引入并发时加锁 |
| engine 包为 homebrew Cellar 内第三方（升级被覆盖） | overlay/vendor 补丁 + 上游 PR 双轨；补丁带版本探测（0.12.12 实测） |
| 混合架构线性层行为随 mlx_lm 版本漂移 | Gate A 纳入回归（升级 mlx_lm 必跑） |

### 11.7 下一轮定位与 Gate B1 正式验收口径（2026-09-06 拍板）

**下一轮 = 验收轮**：P1a 是唯一新变量，跑完即做 Gate B1 正式归因。不做其他机制的 A/B（避免混杂）；P1b 开发可并行但不上线（单变量原则）。

**正式验收口径**（`phase0_cache_diag.py analyze` 已支持窗口切分）：
1. **窗口**：轮次起点的日志字节偏移（`--from-offset`，轮启动时记录 `stat -c%s`/`os.path.getsize`）或首个 `--from-marker "LCP snapdown"` 之后
2. **排除**：批跑会话首个请求（cold warmup）、诊断/探针流量
3. **指标**：token 加权增量占比 **< 0.15**；>30K 大请求子集单列（目标走向 <0.10，属 Gate B2/P1b 后）
4. **记录**：轮次 manifest 必须含 `PROXY_CACHE_LCP_SNAPDOWN=1`（环境快照口径；swe-eval 侧 controls 字段，需协调）

**附带决议**：
- 引擎重启协调协议已设立（llama.cpp `docs/05-operations-changelog/engine-restart-coordination-20260906.md` + CLAUDE.md 硬规则块）：批跑/实验运行期间引擎冻结，重启前三查并公告，紧急 OOM 先斩后奏须登记。**404 风暴已归因闭环**（llama.cpp `58d503a`）：重启加载窗口的客户端重试风暴（日志内 205 次重启累积 8,452 个 404，非流氓客户端）；`_wait_for_ready` 已改功能性探针（真实模型名 1-token 请求）修复
- 遗留观察项：Metal 峰值 29.2GB > cap 28.1GB（越限运行）——下轮留意 OOM 余量，P1b 驱逐治理的输入
- epoch 压缩区跨 epoch 渲染的**字节级确定性**仍待实际翻转验证（§11.4 依赖；与 ctx_engine 演进方联合检查）

### 11.8 实施顺序与状态

```text
P1a   fetch 侧消费基础（memory_cache.py）        ✅ 2026-09-06（llama.cpp 9ae90c8，flag 默认关）
P1a.2 生产侧多边界捕获                           ✅ 2026-09-06（llama.cpp d87b3f7）
      request.py 边界字段 → engine_core 透传 →
      batched.py _compute_prefix_boundaries（消息
      截断渲染 + token LCP + select_checkpoint_positions
      纯函数：去重/间距/limit 尾部优先）→ scheduler
      N 段 insert_segments + 每 end_of_segment 现场
      snapshot_linear_states 累积（M=8）→ 全量 store 挂载
P1a.3 Gate A 模型级验证 + 开 flag                ✅ 2026-09-06（llama.cpp b77f643）
      调试中修复三处：模板 assistant 标记导致严格守卫
      全空（放宽——正确性由 B ≤ lcp 不变式保证）/
      LCP 扫描窗口 ±4 + 检查点候选优先（多会话混缓存
      下带检查点条目未必是 bisect 紧邻）/ 边界条目挂载
      Gate A PASS：B=13848 reused 97.1%，snap 9.4s vs
      cold 279.0s（29.7×），输出逐字一致
P1b   段链去重 + 链感知驱逐（Gate B2）            ~1 天
P1c   phase0 复测（Gate B1）+ E2/E3 探针          ✅ 2026-09-06（初步）
      E2 ✅ append-only 增量占比中位 0.0048（引擎缓存能力最终坐实）
      E3 ✅ 缓存语义正常（无部分重算）；交替 MISS =
          ±4 扫描窗在多探针混合条目间的候选选择边缘
          形态（P1b 系统性解法范畴），snap-down 本身
          反复行使（B=4666 99% 复用）
      Gate B1 初步 ✅：flag 生效后窗口（124 请求）增量
          占比 0.1371 < 0.15 达标；>30K 大请求 71 个
          命中率 90%、增量占比 0.1100；正式验收待下个
          批跑周期干净归因
      工具修复：活跃守卫改双采样推理标记检查（mtime
          被健康轮询/4xx 轰炸持续刷新导致守卫永不开启）；
          E3 判定区分 FULL-MISS / PARTIAL / HIT
```

前置协调：已解除（llama.cpp 并行会话改动已各自提交；pre-commit 四层门禁全过：1634 单测 / 签名 / 行为快照 / Promptfoo shadow 5/5）。

---

## 9. agent_go 侧落地清单

> 本设计 proxy-centric，agent_go 端**无核心改动**；以下为口径/协调/闭环三类触点。防过度落地：压缩、epoch、台账实现全在代理进程内（§4.10 ①）；TASK.md/agent_prompt 是子任务启动时一次性注入（首条消息，非每轮变化的 L0），不违反 §4.1 前缀纪律，**不需要改**。

### P0：口径与观测（不做则 A/B 口径污染、指标不可消费）

| # | 事项 | 文档依据 | 落地点 |
|---|------|---------|--------|
| 1 | **bench 口径标注**：代理干预状态进批次记录（压缩模式 / 负反馈注入开关 / S、K 值） | §4.6「开启时必须在转录中标注」、§6.2 四臂 | `batch_governance.py` manifest 运行配置摘要扩展（最小形态，不动 bench schema）；若需记录级区分再加 `bench_schema.py` 字段 |
| 2 | **metering 归因扩展**：解析代理新响应头（epoch 计数、feedback 注入标记），复用 R8 头模式 | §6.1 指标消费 | `api.py:156`（R8 解析模式）扩展 → metering.jsonl 字段 → `eval.py` analyze 可查。代理侧头定义需先向 llama-defender 提需求（integration-requirements 可记 R13） |
| 3 | **并发与 slot 协调**：`--parallel N` 超代理 slot 数时告警/降并发 | §4.10 类② | `profiles.py:319` health_check 扩展（读代理 `/api/status` 的 slot/ctx 配置，R11 先例）；pipeline pre-flight 就绪检查顺带 |

### P1：协调与闭环（增值）

| # | 事项 | 文档依据 | 落地点 |
|---|------|---------|--------|
| 4 | **轮级无进展看门狗**（agent_go 作为代理台账端点的第二消费方，第一是批跑 harness） | Phase 2「agent_go 任务级 diff_stat_hash 模式的轮级移植」 | `subtask.py:131/364`（MAX_GOAL_TURNS watchdog 模式）扩展：轮询 `GET /api/session/<key>/ledger`，dup ≥3 → 记 rabbit_hole 事件（metering/execution.log），开关控制 kill 或仅标注；同时成为「无效轮占比」一等指标的数据源 |
| 5 | **压缩归属分工声明**：「走代理时压缩全权归代理，agent_go 不做客户端压缩」 | 开放问题 4 | 文档动作：`production-model-config.md`/`config-schema.md` 写明分工；未来 client-side compact 功能须先与代理协调 |
| 6 | **模型切换 × 会话状态协调**：代理有状态后，switch/reload 时段的中断代价变为「epoch 重建 + KV slot 失效」 | §4.10 大前提（代理有会话状态） | local-model-management P1 切换原子序列加前置检查：代理活跃会话 >0 时延迟/告警（「活跃任务并发保护」从任务粒度扩到会话粒度） |

### P2：可选

7. runbook/config 文档：缓存命中率排查（`n_tokens/n_past` 读法）、代理上下文工程配置说明
8. web 配置中心：health_check 展示代理会话数/命中率/epoch 统计
9. **A/B 归属决策**：§6.2 四臂实验建议由批跑 harness 承载（公开集口径在那里），agent_go 只消费结果；若改由 agent_go bench 承载，`bench.py` 需支持按臂切代理配置

---

## 10. 诊断数据面完备性：缺口与接口需求

> 视角：agent_go 诊断分析（缓存验证 / 延迟归因 / 形态学复盘 / 会话观测）需要什么 vs 现在有什么。结论：**执行/成本/路由归因已齐；缓存、会话、行为复盘三域有缺口**——全部可通过「代理透传 + 少量新端点」补齐，需上游 llama.cpp 改动的仅一处（且需先验证）。

### 10.1 现状：能回答什么

| 域 | 数据 | 状态 |
|---|---|---|
| 执行归因 | meta.json / execution.log / verify_state / replay | ✅ 齐 |
| 成本/路由归因 | metering.jsonl + R8 头（`api.py:156` 解析） | ✅ 齐（R8 后） |
| 后端健康 | `/api/status` / `/metrics`（含历史、ttft） | ✅ 基本齐 |
| 客户端行为转录 | claude CLI session 日志（批跑 harness 已在用） | ⚠️ 客户端视角——**压缩后模型实际所见 ≠ 客户端所发**，作不了压缩后行为的依据 |

### 10.2 缺口与需求（按提供方分三层）

**① llama.cpp 原生已有，代理只需透传/采集（零上游改动）**

| 数据 | 原生位置 | 支撑诊断 |
|---|---|---|
| per-request `timings`（prompt_n / prompt_ms / predicted_*）+ `usage` | **响应体**（官方文档确认，版本需实测） | **每轮缓存命中率 = 1 − prompt_n / usage.prompt_tokens，无需解析日志**——Phase 0/1 核心指标的干净来源 |
| `/props`（n_ctx / total_slots / model_path） | GET /props | slot/并发协调（§9 P0-3）、epoch 触发阈值 ctx_max 同步、架构元数据（SWA 判定佐证） |
| `/slots` 实时状态 | GET /slots（需 `--slots` 启动） | 并发会话 vs slot 匹配监控 |
| verbose slot 日志（n_past / n_tokens / kv cache rm） | llama-server stdout | 兜底——文本日志格式不稳定，不作主路径 |

**② 需要代理新建（R13 系列，走 integration-requirements 需求流程）**

| 需求 | 内容 | 支撑 |
|---|---|---|
| **R13 响应头扩展** | `X-Proxy-Prompt-Processed-N`（本轮实算 prefill 数）、`X-Proxy-Epoch-Count`、`X-Proxy-Feedback-Injected`——复用 R8 头模式 | agent_go metering 采集（`api.py:156` R8 解析扩展）→ §6.1 指标消费 |
| **R14 会话台账端点** | `GET /api/session/<key>/ledger`（dup / last_dup_turn / 材料清单）——即 Phase 2 已设计项 | agent_go 轮级看门狗（§9 P1-4）+ 无效轮占比一等指标 |
| **R15 档案查询** | L4 只读访问 `GET /api/session/<key>/archive`——从 Phase 3 降级形态**提前**与 Phase 2 同期 | 压缩后行为复盘必须以代理档案为准（视角正确性）；批跑形态学分析（兔子洞）的权威数据源 |
| **R16 /metrics 会话维度扩展** | 每轮结构化落盘（session_key / turn / sent_tokens / processed_tokens / hit_ratio / epoch 触发 / 注入标记）jsonl + 按 session 聚合时序 | 时间线复盘、`canonical_mismatch` 监控、A/B 出数 |

**③ 可能涉及上游 llama.cpp（仅一处，需验证）**
- 当前部署版本的 `/v1/chat/completions` 响应是否实际包含 timings 对象（官方文档载明，本地版本需实测）。缺失退路：代理解析 llama-server 日志或走 `/completion` 端点取 timings；或向上游提 issue——不阻塞设计（日志兜底可用）

### 10.3 优先级（对齐实施 Phase）

| Phase | 必需项 | 理由 |
|---|---|---|
| Phase 0 | ① timings 透传 | 无它无法坐实 `n_tokens/n_past ≈ 1.0` 诊断——Phase 0 是闸门，数据是闸门的闸门 |
| Phase 1 验收 | R13 头 + R16 基础落盘 | 命中率/延迟分档曲线出数 |
| Phase 2 | R14 台账端点；R15 档案（若形态学复盘进 A/B） | 看门狗与兔子洞归因 |
| 常态 | ① /props、/slots 透传 | 并发协调与漂移监控 |

### 10.4 一条设计原则

> **诊断数据采集责任全部归代理，agent_go 只消费结构化接口**（响应头 / 端点 / jsonl）——不解析 llama-server 文本日志（格式不稳定），不把 claude CLI 侧转录当作压缩后行为的依据（视角错位：那是客户端发的，不是模型看的）。
