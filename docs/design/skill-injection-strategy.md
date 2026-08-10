# Skill 注入策略与多级目录管理

> **状态**：已决策并落地（2026-08-10）
> **关联**：[subagent-design-research.md](subagent-design-research.md) §10、`skills.py`、`executor.py`

## 1. 背景与问题

agent_go 同时承载两类 worker：
- **claude worker**（默认）：`claude -p` / headless / greywall 执行子任务
- **agent_loop**（`--agent-loop`）：直接调 LLM API + 工具执行，无 claude CLI

两条路径对 skill 的处理能力完全不同。早期实现把 skill 完整 body 逐字注入
TASK.md（`render_skill_for_execution` 单一 full 模式），实测暴露三类问题：

1. **与 claude 原生 skill 机制重复**：claude 会自己扫 `~/.claude/skills/` 并按触发
   条件语义判断加载 skill；agent_go 再注入完整 body 是冗余，浪费上下文。
2. **编排层关键词匹配弱于 claude 语义判断**：`discover_skills`（关键词 + IDF）建议
   的 skill 可能与任务无关，注入到 TASK.md 反而可能误导 worker；而 claude 自己能
   正确识别并转向正确的 skill。
3. **多级目录 + 相对路径失效**：skill 分布在 `~/.cc-switch/skills/`、`~/.agents/skills/`、
   `~/.claude/skills/`、`~/.agent_go/skills/`，通过多层 symlink 汇聚。注入 TASK.md 后，
   skill body 内 `../lark-shared/SKILL.md` 等相对引用在 worktree 中解析错误。

## 2. 决策：按后端类型分模式注入

**核心原则**：claude 负责执行时的语义判断，agent_go 负责规划与隔离；注入降级为轻量指引。

| 后端 | skill 注入模式 | 注入内容 | 理由 |
|------|---------------|---------|------|
| claude worker | `guide` | skill 名 + 领域 + 推荐工具 + **绝对路径** | claude 原生可读 `~/.claude/skills/`，语义判断更强；只给指引避免误导与上下文浪费 |
| agent_loop | `full` | 完整 body（相对路径重写为绝对路径） | 无 claude 机制，编排层必须提供全部知识 |
| planner 阶段 | `render_skill_for_plan`（inventory） | 仅 name + description 摘要 | 让 planner 在拆分前知道可用 skill，按语义引用而非关键词硬匹配 |

### 2.1 `render_skill_for_execution(skill, mode)`

- `mode="guide"`（默认给 claude worker）：
  ```
  ## Skill 知识注入: <name>（轻量指引）
  **领域**: <description>
  **推荐工具**: ...
  **Skill 文件**: `<resolve 后的绝对路径>/SKILL.md`
  请先使用 Read 工具读取上述 Skill 文件，按其指令执行本子任务。
  若文件无法读取或与本任务无关，可忽略并继续。
  ```
- `mode="full"`（agent_loop）：完整 body，相对路径引用（`../x.md`）重写为基于
  skill 文件真实位置的绝对路径（`_rewrite_relative_paths`）。

### 2.2 executor 的决策点

`run_subtask` 中按执行路径选择模式：

```python
_ag_loop_enabled = config.agent_loop.enabled
_is_simple = _is_simple_task(subtask)
_skill_inject_mode = "full" if (_ag_loop_enabled and _is_simple and headless) else "guide"
```

- `agent_loop + 简单任务 + headless` → 走 AgentLoop → `full`
- 其余（含 claude worker 的 headless / 交互 / greywall）→ `guide`

## 3. 决策：多级 skill 目录模型

### 3.1 现状拓扑

```
真实位置:    ~/.cc-switch/skills/    ~/.agents/skills/     ~/.claude/skills/(原生)    ~/.agent_go/skills/(原生)
             (bank/证券/rss/buffett)  (lark 系列)          (byted/learned)           (frontend/orm/readonly/security)
                          │               │                       │
~/.claude/skills/ ←───────┴───────────────┴───────────────────────┘   ← claude CLI 读取
   │
~/.agent_go/skills/ ←─────────────────────────────────────────────────  ← agent_go 读取（symlink 池）
```

### 3.2 目录约定

| 目录 | 性质 | 读取方 | 用途 |
|------|------|--------|------|
| `~/.claude/skills/` | 原生 + symlink 汇聚 | claude CLI | claude 自主 skill |
| `~/.agent_go/skills/` | **symlink 池**（可含原生） | agent_go | agent_go 能见到的 skill 全集 |
| `<repo>/.agent_go/skills/` | 真实目录 | agent_go | **项目级覆盖**（优先级最高） |
| `~/.config/opencode/skills/` | 真实目录 | opencode | 独立，agent_go 不可见 |

**加载优先级**（`_find_skill_file`）：`~/.agent_go/skills/` > `<repo>/.agent_go/skills/`。

### 3.3 诊断命令

新增 `agent_go skills resolve <name>`：追踪 symlink 解析链（入口 → 每跳 → 最终
SKILL.md + 存在性），用于排查断裂 / 多级链 / 循环引用。`--json` 输出结构化。

```bash
$ agent_go skills resolve bank-risk-report
🔗 Skill 解析链: bank-risk-report
  → ~/.agent_go/skills/bank-risk-report
  → ~/.claude/skills/bank-risk-report
  ✔ ~/.cc-switch/skills/bank-risk-report
📄 最终解析: ~/.cc-switch/skills/bank-risk-report/SKILL.md
✅ 文件存在: 是
```

## 4. 决策：`discover_skills` IDF 加权

`discover_skills` 自动匹配从「重叠词数」改为「IDF 加权 + 阈值」：

- 计算每个词在**当前已安装 skill 集合**中的文档频率 `df`
- 匹配分 = `Σ 1/df`：专属词（如「风控」「银行」）得高分，泛词（如「报告」「整理」）趋零
- 硬门槛：共同词 ≥2 且 IDF 分 ≥1.0
- 效果：修复「银行业智能风控报告」任务误配 `lark-workflow-meeting-summary`
  （报告/整理/生成 均为泛词，IDF 分 0.75 <1.0）或 `byted-ark-seedance-skill` 的问题

## 5. 与 claude skill 机制的关系

- **claude 是 skill 语义判断的最终裁决者**：agent_go 的 `guide` 指引即使建议了错误
  skill，claude 读到后会自主判断「与任务无关」并转向正确的 skill（实测验证）。
- **agent_go 的价值在规划层**：把 skill inventory 注入 planner prompt，让 planner
  拆分子任务前就知道可用 skill——这是 claude 原生机制覆盖不到的阶段。
- **自动匹配降权**：`auto_discover` 默认关闭；需要时用 IDF 加权保证精确。

## 6. 落地清单（已完成）

| # | 改动 | 文件 |
|---|------|------|
| 1 | `render_skill_for_execution(skill, mode)` guide/full 双模式 | `skills.py` |
| 2 | `_rewrite_relative_paths`（full 模式相对路径→绝对） | `skills.py` |
| 3 | `discover_skills` IDF 加权 + 阈值 | `skills.py` |
| 4 | `resolve_skill_chain` + `skills resolve` 命令 | `skills.py`, `cli.py` |
| 5 | `run_subtask` 按后端计算 `skill_inject_mode` | `executor.py` |
| 6 | 测试：guide/full 注入、resolve 链、IDF 匹配 | `test_skills.py`, `test_discover_skills.py`, `test_executor.py` |

## 7. 后续可选项

- **项目级 skill 覆盖可视化**：`skills resolve` 显示项目级覆盖关系（当前只显示全局解析）。
- **多 agent 作用域隔离**：按 planner/worker/reviewer 分离 skill 可见范围（当前共用同一池）。
- **skill 缓存/失效**：symlink 池变更时（如 cc-switch 切模型）缓存失效策略。
