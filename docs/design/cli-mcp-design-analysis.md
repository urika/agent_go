# CLI 与 MCP 设计分析：范式总结、最佳实践与 agent_go 改进路线

> 作者：agent_go 架构分析  
> 日期：2026-08-01  
> 版本：v1.0  
> 状态：草稿

---

## 目录

1. [概述](#1-概述)
2. [范式总结](#2-范式总结)
   - 2.1 传统 CLI 设计范式
   - 2.2 Agent-Native CLI 设计范式
   - 2.3 MCP Server 设计范式
   - 2.4 三范式对比矩阵
3. [最佳实践](#3-最佳实践)
   - 3.1 CLI 工程最佳实践
   - 3.2 Agent-Native CLI 最佳实践
   - 3.3 MCP Server 最佳实践
4. [agent_go 现状分析](#4-agent_go-现状分析)
   - 4.1 CLI 层：做到位了 vs 有差距
   - 4.2 MCP 层：做到位了 vs 有差距
5. [改进路线图](#5-改进路线图)
   - 5.1 优先级矩阵
   - 5.2 详细改进方案
6. [参考资料](#6-参考资料)

---

## 1. 概述

agent_go 是一个面向 AI Agent 的结构化工程任务编排工具，同时提供 CLI 和 MCP Server 两种交互入口。本文档结合业界最新实践（CLI 设计规范、Agent-Native CLI 浪潮、MCP 协议最佳实践），对 agent_go 的 CLI 和 MCP 两个模块进行系统性分析，提炼改进方向。

**核心结论**：agent_go 在两个范式上已有扎实基础，但在「Agent-Native 体验」（如错误自修复指令、三层命令架构、输出格式）、「MCP 深度」（Resources/Prompts/Sampling 原语）、以及「产品化细节」（--version、--dry-run、Usage 示例）方面存在明确的提升空间。

---

## 2. 范式总结

### 2.1 传统 CLI 设计范式

传统 CLI 的设计目标是为**人类开发者**提供高效、可脚本化的终端交互界面。核心原则来自 UNIX 哲学和现代 CLI 工程实践（参见 [clig.dev](https://clig.dev/)）。

#### 2.1.1 命令结构

| 原则 | 说明 | 示例 |
|------|------|------|
| **命名约定** | 全小写、短横线连接；动词优先的一级子命令 | `git clone`, `docker build`, `kubectl get pods` |
| **层级深度** | 子命令不超过 3 层 | `app service create` ✓；`app service config create` ✗ |
| **参数顺序** | `cmd [options] [arguments]`，必填参数优先 | `deploy --env prod app-name` |

#### 2.1.2 选项（Flags）设计

```
短选项:   -f, -v, -q            (单字母，- 前缀)
长选项:   --file, --verbose     (完整单词，-- 前缀)
布尔开关: --force               (默认 false，存在即 true)
带值选项: --output=file.txt     (= 或空格分隔)
否定形式: --no-color, --no-cache (--no- 前缀取消布尔)
标准选项: -h/--help, --version, --json, --verbose
```

#### 2.1.3 输出与错误处理

```
退出码:  0=成功, 1=通用错误, 2=参数误用, 126=不可执行, 127=未找到, 130=Ctrl+C
输出:    默认面向人类（简洁可读），--json 面向机器（结构化）
错误:    输出到 stderr，格式 ERROR: <message>，包含修复建议
进度:    长时间操作显示 spinner 或进度条
颜色:    --color / --no-color，自动检测 TTY
```

#### 2.1.4 配置与环境

```
优先级 (高→低):
  1. 命令行参数
  2. 环境变量 (APP_KEY 形式)
  3. 配置文件 (~/.config/app/config.yml)
  4. 默认值

环境变量映射: --api-key ↔ APP_API_KEY (工具自动转换)
配置文件格式: YAML/JSON/TOML，遵循 XDG Base Directory
```

#### 2.1.5 交互模式

- **非交互默认**：脚本友好，所有参数可通过 CLI 传入
- **确认机制**：破坏性操作需 `--force` 或交互确认
- **管道友好**：支持 stdin/stdout，避免不必要的提示

---

### 2.2 Agent-Native CLI 设计范式

Agent-Native CLI 是 2026 年 Q1 爆发的新范式（参见 [OSSInsight 分析](https://ossinsight.io/blog/agent-native-cli-wave-2026)），其核心洞察是：**CLI 正在成为 AI Agent 的「事实标准接口」**——就像 2010 年代的 REST API 之于移动应用。

#### 2.2.1 定义：与传统 API 的根本差异

Agent-Native First 设计问的是「**如何让一个会犯错的智能体可靠地达成目标**」，而传统 API 设计问的是「如何暴露能力」。两者差异是系统性的：

| 维度 | 传统 API 设计 | Agent-Native First 设计 |
|------|-------------|----------------------|
| 使用者 | 人类开发者（读文档、写代码） | 自主智能体（LLM/Agent） |
| 交互模型 | 精确调用，一次一请求 | 目标驱动，多步推理后调用 |
| 认知负担 | 开发者承担全部理解成本 | 工具承担理解成本，降低 Agent 推理开销 |
| 错误假设 | 调用者知道自己在做什么 | 调用者可能推理错误，需要引导修复 |
| 组合方式 | 代码编排（SDK + 逻辑） | 命令管道 + 上下文传递 |
| Token 成本 | 不关心（人类阅读不消耗 token） | 核心优化目标 |

#### 2.2.2 三层命令架构（飞书 CLI 核心创新）

```
L1: Shortcuts    +agenda, +send         语义化快捷命令，智能默认值   AI Agent 高频调用
L2: API Commands calendar events list   与平台 API 1:1 映射          精确控制、脚本自动化
L3: Raw API      lark-cli api <endpoint> 直接访问底层端点             边缘场景、未封装 API
```

**设计价值**：Agent 优先用 L1 快捷命令（最少 token、最少决策）；人类/脚本用 L2 精确命令；高级用户保留 L3 的完全灵活性。

#### 2.2.3 五大关键差异

**差异 1：抽象层级 — 原子操作 vs 意图封装**

```
传统 API: POST /messages → POST /threads → POST /permissions  (3 步)

Agent-Native: +send "周报" to @team --as-doc --notify          (1 步)
              └→ 内部自动: 创建文档 → 写入内容 → 设置权限 → 通知
```

**差异 2：交互协议 — 请求-响应 vs 对话-修复**

```
传统: HTTP 403 → {"error": "insufficient_scope"}  → 人类查文档修复

Agent-Native: ERROR: Missing scope 'im:message:send'
              FIX: Run 'lark auth user login --scopes im:message:send'
              → Agent 直接调用修复命令，无需人类介入
```

**差异 3：参数设计 — 精确显式 vs 智能默认**

```
传统: 所有参数必填，缺失即报错

Agent-Native: 80% 参数有智能默认值，CLI 自动推断或交互补全
              → 减少 Agent 决策点 → 减少 token 消耗 → 降低出错概率
```

**差异 4：输出格式 — 单一结构 vs 多模态自适应**

```
--format json     → Agent 调用，结构化解析，直接注入上下文
--format table    → 人类调试，可读性优先
--format ndjson   → 管道串联，流式处理
--format csv      → 脚本集成，导入 Excel/数据库
```

**差异 5：安全模型 — 全量授权 vs 最小权限 + 动态引导**

```
传统: 应用申请一组权限，运行时拥有全部能力

Agent-Native: 按 Skill 动态申请最小权限，缺失时引导补充
              → Agent 权限边界与任务边界精确对齐
```

#### 2.2.4 2026 年 Agent-Native CLI 浪潮信号

Q1 2026 年，6 个关键仓库在 90 天内集中涌现，总计 130,000+ stars：

| 项目 | 定位 | Stars | Fork 比 | 核心洞察 |
|------|------|-------|---------|---------|
| CLI-Anything (HKUDS) | GUI→CLI 转换框架 | 25k+ | 8.96% | 30 个应用 harness，SKILL.md 标准 |
| Google Workspace CLI | Google API 动态 CLI | 23k+ | — | 从 Discovery Service 自动生成 |
| larksuite/cli | 飞书官方 CLI | 4.7k (5天) | — | 200+ 命令，19 个 AI Agent Skills |
| agent-browser (Vercel) | 浏览器自动化 CLI | — | — | 浏览器成为结构化命令面 |
| Agent-Reach | 社交/内容平台统一 CLI | 13.9k | 7.9% | 无需 API Key 的只读访问层 |
| opencli | 通用 CLI Hub | — | 8.35% | Make Any Website a CLI |

**核心信号**：Fork 比（forks/stars）达到 5-9%，远超典型开源项目的 1-3%，说明这些仓库已从「感兴趣」跨越到「实际使用和扩展」。

---

### 2.3 MCP Server 设计范式

MCP（Model Context Protocol）由 Anthropic 于 2024 年底推出，2025 年底捐赠给 Agentic AI Foundation。定位为「AI 的 USB-C」——标准化 AI 模型与外部工具/数据源的连接协议。

#### 2.3.1 三方架构

```
┌─────────┐      ┌─────────┐      ┌─────────┐
│  Host   │──────│ Client  │──────│ Server  │
│(IDE/AI  │      │(连接管理)│      │(工具/数据)│
│ 应用)   │      │         │      │         │
└─────────┘      └─────────┘      └─────────┘
```

- **Host**：承载 LLM 的应用（Claude Desktop、VS Code、Cursor），是安全边界
- **Client**：与 Server 建立 1:1 连接，负责请求转发
- **Server**：连接实际资源，暴露 Tools、Resources、Prompts、Sampling

#### 2.3.2 四大核心原语

| 原语 | 作用 | 类比 |
|------|------|------|
| **Tools** | 暴露可调用功能，LLM 决定何时调用 | 函数/方法 |
| **Resources** | 只读数据源（文件、数据库、日志） | 数据视图 |
| **Prompts** | 可复用的提示模板 | 预设工作流 |
| **Sampling** | Server 向 LLM 反向请求补充信息 | 反向查询（需用户审核） |

#### 2.3.3 协议设计原则

```
传输无关:   Stdio（本地进程隔离）+ SSE/HTTP（远程连接）
能力协商:   连接建立时握手，声明支持的功能
JSON-RPC 2.0: 无状态、轻量、语言无关
动态发现:   运行时 tools/list 获取工具及 Schema，无需预编译知识
```

#### 2.3.4 MCP vs 传统 API：五个根本差异

| 维度 | 传统 API | MCP |
|------|---------|-----|
| 发现机制 | 静态文档，开发者手动查阅 | 运行时动态发现 |
| 交互模型 | 无状态请求-响应 | 会话级上下文保持，多步推理 |
| 集成成本 | M × N（每个模型适配每个数据源） | M + N（标准化协议统一接入） |
| 抽象层级 | 原子操作（CRUD） | 意图驱动（Tools + Resources + Prompts） |
| 安全边界 | 应用直接持有凭证 | Host 中介，进程隔离，Roots 限制 |

#### 2.3.5 MCP Tool 设计核心原则（来自 AWS 实践）

AWS 的 MCP tool 设计演进揭示了两个核心问题：**Bloat（膨胀）** 和 **Confusion（混淆）**：

```
问题 1 — Bloat: Tool definitions 加载到 LLM context，无论是否使用都消耗 token
问题 2 — Confusion: 太多工具、语义相似、命名模糊 → LLM 选错工具/参数 → 重试消耗更多 context

解决方案演化 (V1→V6):
V1 Raw Passthrough   → 暴露 API 原样，LLM 无引导（反模式）
V2 Rich Descriptions → 自然语言映射 + 同义词，准确度↑，context 占用↑
V3 Schema + Defaults → 枚举约束 + 默认值 + 参数重命名，准确度↑↑，context↓
V4 Lazy Loading      → 工具拆分 + on-demand 分类查询，context 最精简
V5 LLM Introspection → 服务端模型解释模糊查询，客户端 context 保持精简
V6 Agent-as-Tool     → 单工具单参数，服务端 Agent 内部编排，最高控制力
```

#### 2.3.6 Production MCP 六大原则（来自 Itential）

| # | 原则 | 核心思想 |
|---|------|---------|
| 1 | **从结果出发，而非端点** | 暴露意图级别的工具，而非 CRUD 原子操作 |
| 2 | **激进地精选工具面** | 越少工具 → 越清晰的决策 → 越低失败率 |
| 3 | **Resources 是上下文契约** | 小而精准的数据视图，而非全量数据倾倒 |
| 4 | **Prompts 是模型的标准操作规程** | 编码解读方式、验证步骤、停止条件 |
| 5 | **护栏：确定性控制环绕概率推理** | 只读易、写入难、验证门禁、编排优先于即兴 |
| 6 | **生产可靠性在于失败行为** | 结构化错误、清晰失败模式、进度更新、可恢复 |

---

### 2.4 三范式对比矩阵

| 维度 | 传统 CLI | Agent-Native CLI | MCP Server |
|------|---------|-----------------|------------|
| **定位** | 人类 → 应用 | 人类 + Agent → 应用 | LLM → 工具/数据 |
| **发现方式** | 静态（man page / --help） | 静态 + Skills 文件 | 运行时动态（tools/list） |
| **调用者** | 人类 / Shell 脚本 | 人类 + AI Agent | LLM / AI Agent |
| **抽象层级** | 原子命令 | 意图封装 (Skills) | Tools + Resources + Prompts |
| **上下文** | 无状态 | 管道级传递 (`cmd1 \| cmd2`) | 会话级保持 |
| **组合方式** | 管道 + 脚本 | 管道 + 脚本 + Skills 编排 | Tool 链式调用 |
| **Token 成本** | 不敏感 | 核心优化目标 | 敏感（Schema 加载消耗大） |
| **错误处理** | 退出码 + stderr | 退出码 + 可执行修复指令 | 类型化错误 + retryable |
| **部署形态** | 本地可执行文件 | 本地可执行文件 | 本地进程 / 远程 SSE |
| **最佳场景** | 日常开发操作 | 高频单工具调用、脚本 | 多工具 Agent 工作流 |

**三者关系（非竞争，是分层协作）**：

```
┌──────────────────────────────────────────────────┐
│  Agent-Native CLI  ← 人类快速操作 + Agent 调用    │
│  (agent_go run/resume/review + Skills)            │
├──────────────────────────────────────────────────┤
│  MCP Server  ← AI Agent 多工具编排、动态发现       │
│  (agent_go mcp: run_task/inspect_task/...)        │
├──────────────────────────────────────────────────┤
│  传统 CLI 基础  ← 参数解析、输出格式化、配置管理     │
│  (argparse + Console + config)                    │
└──────────────────────────────────────────────────┘
```

---

## 3. 最佳实践

### 3.1 CLI 工程最佳实践

#### 3.1.1 命令设计

| 实践 | 细则 |
|------|------|
| **命名一致** | 全小写、短横线；动词优先子命令；同概念同命名（如统一用 `list` 而非混用 `ls`/`list`） |
| **层级不深** | 子命令 ≤ 3 层；用 flags 替代深层子命令（`--format json` 而非 `output json`） |
| **参数顺序** | `cmd [global-options] <command> [command-options] [positional-args]` |
| **Usage 示例** | 帮助文本必须包含 1-2 个真实用例 |

#### 3.1.2 输出设计

| 实践 | 细则 |
|------|------|
| **默认人类可读** | 表格、颜色、emoji（自动检测 TTY） |
| **`--json` 双模** | 全局 `--json` 标志切换机器可读输出 |
| **进度反馈** | 长时间操作显示 spinner / 进度条 / TUI |
| **stderr 分离** | 错误、警告、进度 → stderr；数据 → stdout |
| **退出码规范** | 0/1/2/126/127/130，语义一致 |

#### 3.1.3 健壮性

| 实践 | 细则 |
|------|------|
| **幂等操作** | `resume` 对已完成任务返回已有结果，不重复执行 |
| **信号处理** | SIGTERM/SIGINT → 保存状态 → 优雅退出 |
| **超时处理** | 长时间命令支持 `--timeout` |
| **并发安全** | 文件锁、状态原子写入 |
| **僵尸清理** | 启动时自动清理卡死的 running 状态 |

#### 3.1.4 安全

| 实践 | 细则 |
|------|------|
| **密钥不进命令行** | Token/密码优先环境变量或文件，禁止 `--password=xxx` |
| **`--dry-run`** | 所有写操作支持预览模式 |
| **确认机制** | 破坏性操作需交互确认或 `--force`/`--yes` |
| **输入校验** | 早期校验路径/参数有效性，给出明确错误 |

---

### 3.2 Agent-Native CLI 最佳实践

#### 3.2.1 意图封装

```
原则: 暴露「要做什么」而非「怎么做的步骤」

好:  agent_go run <repo> '<task>'         ← 一条命令封装 Plan→Execute→Verify
坏:  agent_go plan <repo> '<task>'
     agent_go execute <plan-id> --step 1
     agent_go execute <plan-id> --step 2
     agent_go verify <plan-id>            ← 4 步，Agent 需理解编排
```

**度量标准**：一个常见的端到端任务，Agent 调用的命令数量应 ≤ 3。

#### 3.2.2 Skills 机制

```
Skills = 预构建、参数化的能力包

设计要素:
  1. YAML frontmatter: name, description, triggers, agent_types
  2. Markdown body: 使用说明、参数、示例
  3. 自动发现: 从任务描述或文件模式自动匹配
  4. Plan 注入: Skills 上下文注入 Plan prompt，让 LLM 感知可用能力
  5. 规则兜底: role_skill_map.json 作为 LLM 输出缺失时的 fallback
```

#### 3.2.3 错误自修复

```
失败时返回的不是错误码，而是可执行的修复指令:

传统:  ERROR: verification failed
Agent-Native:
  ERROR: verification failed — test_storage.py:45 assertion error
  CONTEXT: added null check but missed edge case for empty string
  FIX: agent_go resume <task-id> --max-retries 3
  or: agent_go inspect <task-id> to review preserved worktree
```

**关键字段**：`error.code`, `error.message`, `error.fix`（可执行命令）, `error.context`（失败上下文）

#### 3.2.4 输出自适应

```
--json      → Agent 调用，结构化解析
(默认)      → 人类阅读，表格 + emoji
--quiet     → CI/CD 集成，仅错误输出
progressive → 流式事件 (JSON Lines) 实时推送进度
```

#### 3.2.5 Token 效率

```
原则: 每个参数、每行输出都经过「这个信息对 Agent 决策是否必要」的审视

实践:
  - 减少必填参数数量（默认值覆盖 80% 场景）
  - 精简帮助文本的 token 占用（描述 ≤ 2 句）
  - 结构化输出用短键名（"id" 而非 "subtask_identifier"）
  - 避免冗余字段（不返回 Agent 不会使用的数据）
```

---

### 3.3 MCP Server 最佳实践

#### 3.3.1 工具设计

| 实践 | 细则 |
|------|------|
| **意图优先** | 工具对应业务结果，而非内部 API 端点 |
| **精选面** | 工具数 ≤ 7（超过时考虑合并或用 Resources 替代部分功能） |
| **清晰命名** | 动词_名词，避免歧义；`run_task` 而非 `execute` |
| **参数约束** | 枚举、min/max、default 值完善；必要参数数 ≤ 5 |
| **Annotations** | `readOnlyHint` / `destructiveHint` / `idempotentHint` 准确标注 |
| **读写分离** | `inspect_task`(读) / `run_task`(写) 不混在同一个工具 |

#### 3.3.2 Context Engineering（上下文工程）

```
问题: Tool definitions 全量加载到 LLM context → Bloat
解决: 按需加载策略

  V3 Schema + Defaults: 枚举约束减少描述文本，默认值减少参数决策
  V4 Lazy Loading:     分离「搜索工具」和「分类查询工具」，按需获取详情
  V5 LLM Introspection:服务端小模型解释模糊参数，返回精确值
  V6 Agent-as-Tool:    服务端 Agent 内部编排，客户端只暴露单工具单参数
```

**agent_go 当前对应**：V3 级别（有 schema + enum + defaults），可向 V4（Resources 原语做 lazy context）和 V5（服务端 LLM 解释模糊查询）演进。

#### 3.3.3 错误设计

```json
// 好: 结构化错误 + 可重试标志 + 修复指引
{
  "error": {
    "code": "AGENT_GO_REPO_INVALID",
    "message": "仓库不在 allowlist: /tmp/evil",
    "retryable": false,
    "fix": "将仓库路径加入 AGENT_GO_MCP_ALLOWED_REPOS 环境变量"
  }
}

// 坏: 仅返回字符串
{
  "error": "repo not allowed"
}
```

#### 3.3.4 四大原语全覆盖

| 原语 | 是否必需 | agent_go 状态 |
|------|---------|---------------|
| **Tools** | ✅ 必需 | ✅ 已实现 (4 tools) |
| **Resources** | 推荐 | ❌ 未实现 — 可将 task log、metering、plan history 暴露 |
| **Prompts** | 推荐 | ❌ 未实现 — 可暴露 review/plan 模板 |
| **Sampling** | 进阶 | ❌ 未实现 — 可在关键决策点反向询问 |

#### 3.3.5 生产就绪

| 实践 | 细则 |
|------|------|
| **进度通知** | 长时间操作通过 `notifications/progress` 推送 |
| **取消支持** | 响应 `notifications/cancelled` 清理资源 |
| **并发限制** | 服务端限制最大并发任务数 |
| **超时保护** | 工具调用支持 `timeout_sec` |
| **日志审计** | 工具调用记录到 `review_history.jsonl` |

---

## 4. agent_go 现状分析

### 4.1 CLI 层：做到位了 vs 有差距

#### ✅ 做到位了

| 设计点 | 实现细节 | 对标 |
|--------|---------|------|
| **命令命名** | 全小写短横线，动词优先：`run/resume/list/show/inspect/review/clean` | CLI 规范 |
| **`--no-` 否定前缀** | 系统性使用：`--no-cache`, `--no-goal`, `--no-preserve`, `--no-verify-block`, `--no-semantic-eval`, `--no-tui`, `--no-skills` | CLI 规范 |
| **`--json` 双模输出** | 全局 `--json` + Console 抽象层的 `json_mode` | Agent-Native CLI |
| **配置优先级** | CLI args > config.json > 默认值 + `${VAR}` 环境变量模板 | CLI 规范 |
| **交互模式切换** | `--yes` / `--headless` 非交互；并发模式自动要求 headless | CLI 规范 |
| **意图封装 (L1 命令)** | `agent_go run <repo> '<task>'` 封装完整 Plan→Execute→Verify 流程 | Agent-Native CLI |
| **Skills 机制** | 加载/发现/注入/规则兜底，配置驱动 role_skill_map | Agent-Native CLI |
| **验证-重试自修复** | 失败上下文注入 fix prompt → 自动重试 → worktree 保留 | Agent-Native CLI |
| **Plan 版本管理** | `plan-history` / `plan-diff` / `plans/v{N}.json` | 可审计性 |
| **信号处理** | SIGTERM/SIGINT → kill children → save meta.json | 鲁棒性 |
| **僵尸任务清理** | 启动时 `_cleanup_stale_tasks()` 标记 stale_aborted | 鲁棒性 |
| **并发安全** | `git gc.auto` 禁用、ThreadPoolExecutor、meta_lock | 鲁棒性 |
| **Quality Dashboard** | 通过率/验证率/合并就绪三色指示器 | 决策辅助 |

#### ❌ 有差距

| 差距 | 严重度 | 对标来源 |
|------|--------|---------|
| **缺少 `--version` 标志** | 低 | CLI 基础规范 |
| **帮助文本无 Usage 示例** | 中 | CLI 规范 — argparse 默认无示例 |
| **错误消息缺少 `FIX:` 风格的可执行修复指令** | 高 | Agent-Native CLI — 飞书 CLI 的核心差异 |
| **缺少三层命令架构的显式设计** | 中 | Agent-Native CLI — 当前隐式存在但未系统化 |
| **缺少 `--dry-run` 全局支持** | 中 | CLI 安全规范 — 飞书 CLI 所有写操作支持 |
| **缺少输出格式切换（`--format json\|table\|csv`）** | 低 | Agent-Native CLI — 当前仅 `--json` 二态 |
| **Skills 命令帮助信息不够 Agent 友好** | 中 | Agent-Native CLI — 可增加 SKILL.md 自描述 |
| **缺少多账户/多 profile 支持** | 低 | Agent-Native CLI — 飞书 `--account` / `--profile` |
| **凭证存储未使用 OS keychain** | 低 | 安全规范 — 飞书 CLI 使用系统密钥链 |

---

### 4.2 MCP 层：做到位了 vs 有差距

#### ✅ 做到位了

| 设计点 | 实现细节 | 对标 |
|--------|---------|------|
| **JSON-RPC 2.0 标准** | `initialize` 握手、`tools/list`、`tools/call`、`notifications` | MCP 规范 |
| **Tool Annotations** | `readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` | MCP 规范 |
| **参数 Schema 约束** | `type`, `enum`, `minimum`, `maximum`, `default`, `required` | MCP 规范 |
| **异步 + 同步双模式** | `wait=false` 返回 task_id 供轮询；`wait=true` 阻塞流式推送 | MCP 最佳实践 |
| **进度通知** | `notifications/progress` 含 progressToken、progress/total、current_activity | MCP 规范 |
| **活动追踪** | `subtask_activity` 事件 → `activity_store` → `inspect_task` 可返回 | MCP 最佳实践 |
| **结构化错误** | `MCPError(code, message, retryable)` + error code 枚举 | MCP 最佳实践 |
| **Repo Allowlist** | `AGENT_GO_MCP_ALLOWED_REPOS` 环境变量，glob 匹配 | MCP 安全 |
| **并发限制** | `AGENT_GO_MCP_MAX_CONCURRENT` 默认 3 | MCP 生产 |
| **超时保护** | `timeout_sec` 默认 3600s | MCP 生产 |
| **进程隔离** | 每个任务独立 subprocess，stderr drain thread | MCP 安全 |
| **审查审计** | `review_decision.json` + `review_history.jsonl` | MCP 生产 |
| **取消支持** | 响应 `notifications/cancelled`，stdin EOF 时 terminate 所有子进程 | MCP 规范 |

#### ❌ 有差距

| 差距 | 严重度 | 对标来源 |
|------|--------|---------|
| **Resources 原语未使用** | 高 | MCP 规范 — 可将 task log、metering、plan history 暴露为只读 Resource |
| **Prompts 原语未使用** | 中 | MCP 规范 — 可暴露 review template / plan template |
| **Sampling 原语未使用** | 低 | MCP 规范 — Server→LLM 反向查询 |
| **仅 stdio transport** | 中 | MCP 规范 — 缺少 SSE/HTTP，限制远程部署场景 |
| **工具数偏少（4 个）** | 低 | 可考虑增加 `list_tasks`、`cancel_task` 等 |
| **缺少 lazy context 策略** | 中 | AWS MCP 实践 — Resource 可按需加载，避免全量注入 context |
| **错误消息缺少 `fix` 字段** | 中 | Agent-Native 理念 — 当前 error 有 code/message/retryable 但无可执行修复指令 |
| **Tool descriptions 可更精简** | 低 | Token 效率 — 当前描述偏长 |

---

## 5. 改进路线图

### 5.1 优先级矩阵

按「影响面 × 实现成本」排列：

```
                    低成本              中成本              高成本
              ┌─────────────────┬─────────────────┬─────────────────┐
  高影响       │ --version       │ FIX: 错误指令    │ Resources 原语   │
              │ Usage 示例      │ --dry-run       │ Prompts 原语     │
              │ 错误 fix 字段   │ 三层架构显式化   │ Sampling 原语    │
              │                 │                 │ SSE/HTTP transp. │
              ├─────────────────┼─────────────────┼─────────────────┤
  中影响       │ 工具描述精简    │ --format 切换   │ 多 profile 支持  │
              │ SKILL.md 命令   │ cancel_task 工具│ OS keychain      │
              │                 │ list_tasks 工具 │                  │
              ├─────────────────┼─────────────────┼─────────────────┤
  低影响       │ 帮助文本优化    │ 命令别名        │ GUI installer    │
              │                 │                 │                  │
              └─────────────────┴─────────────────┴─────────────────┘
```

### 5.2 详细改进方案

#### P0：立即执行（低成本 + 高影响）

##### P0-1：增加 `--version` 标志

```python
# cli.py _build_parser()
parser.add_argument("--version", "-V", action="version",
                    version="agent_go v1.0.0")
```

##### P0-2：帮助文本增加 Usage 示例

在每个子命令的 `help` 参数中追加示例：

```python
run_parser = subparsers.add_parser("run",
    help="Plan, decompose and execute a task",
    epilog="Examples:\n"
           "  agent_go run ./myproject 'Add unit tests for auth module'\n"
           "  agent_go run ./myproject 'Refactor DB layer' --parallel 3 --remote origin\n"
           "  agent_go run ./myproject 'Fix security issues' --skill security-review --yes\n")
```

##### P0-3：MCP 错误响应增加 `fix` 字段

```python
# mcp_server.py
class MCPError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, fix: str = ""):
        self.code = code
        self.message = message
        self.retryable = retryable
        self.fix = fix

# 使用示例
raise MCPError(
    "AGENT_GO_REPO_INVALID",
    f"仓库不在 allowlist: {repo}",
    fix=f"将仓库路径加入 AGENT_GO_MCP_ALLOWED_REPOS 环境变量，当前值: {self._allowed_repos}"
)
```

##### P0-4：CLI 错误消息增加可执行修复指令

```python
# 改造关键错误路径
console.error(
    "验证失败: 3 次重试后仍未通过\n"
    "FIX: agent_go inspect <task-id>      # 查看保留的 worktree 现场\n"
    "FIX: agent_go resume <task-id>       # 修复后重新执行\n"
    "FIX: agent_go review --task <task-id> # 人工审查变更")
```

#### P1：短期（低成本 + 中影响 / 中成本 + 高影响）

##### P1-1：MCP 增加 Resources 原语

```python
# mcp_server.py — 新增 resources/list 支持
RESOURCES = [
    {
        "uri": "agent_go://tasks/{task_id}/log",
        "name": "Task Execution Log",
        "description": "Execution log for a specific task",
        "mimeType": "text/plain",
    },
    {
        "uri": "agent_go://tasks/{task_id}/metering",
        "name": "Task Metering Data",
        "description": "Token usage and cost breakdown per subtask",
        "mimeType": "application/json",
    },
    {
        "uri": "agent_go://tasks/{task_id}/plan",
        "name": "Latest Plan",
        "description": "The execution plan (latest version)",
        "mimeType": "application/json",
    },
]

# resources/read handler
def _handle_resources_read(self, uri: str) -> dict:
    # 解析 URI 获取 task_id 和资源类型
    # 返回对应内容
```

**价值**：Agent 可以在不调用 tool 的情况下获取上下文，减少 tool call 次数和 token 消耗。

##### P1-2：MCP 增加 Prompts 原语

```python
PROMPTS = [
    {
        "name": "review_code_changes",
        "description": "Template for reviewing code changes from a subtask",
        "arguments": [
            {"name": "task_id", "description": "Task ID", "required": True},
            {"name": "subtask_id", "description": "Subtask ID", "required": True},
        ],
    },
    {
        "name": "create_plan",
        "description": "Template for generating an execution plan",
        "arguments": [
            {"name": "task", "description": "Task description", "required": True},
            {"name": "repo_structure", "description": "Repository structure overview", "required": False},
        ],
    },
]
```

##### P1-3：增加 `--dry-run` 支持

```python
# cli.py run_parser
run_parser.add_argument("--dry-run", action="store_true",
    help="预览 Plan 和子任务拆解，不实际执行")

# cmd_run 中处理
if args.dry_run:
    console.print("[DRY RUN] Plan 已生成，跳过执行")
    console.print(plan_to_md(confirmed_plan))
    return
```

##### P1-4：显式化三层命令架构

```
L1 Shortcuts (Agent 高频):
  agent_go run <repo> '<task>'               ← 主命令，智能默认值
  agent_go review --task <id> --approve       ← 快捷审查批准

L2 Structured (精确控制):
  agent_go resume <id> --parallel 3 --remote origin
  agent_go inspect <id> --json
  agent_go eval bench --tasks custom_suite --repeat 5

L3 Raw (高级/边缘):
  agent_go mcp                                 ← MCP Server 模式
  agent_go --config /path/to/config.json run ... ← 自定义配置路径
```

在帮助文本和文档中体现这三层结构。

##### P1-5：增加 `cancel_task` MCP 工具

```python
{
    "name": "cancel_task",
    "description": "取消正在运行的任务，终止子进程并保留已完成的部分结果",
    "annotations": {"title": "Cancel running task", "destructiveHint": True, "idempotentHint": True},
    "inputSchema": {
        "type": "object",
        "required": ["task_id"],
        "properties": {
            "task_id": {"type": "string"},
        }
    }
}
```

#### P2：中期（中高成本）

##### P2-1：MCP 增加 Sampling 原语

在关键决策点（如 Plan 确认、高风险操作）通过 Sampling 反向询问 LLM：

```python
# Server → Client: sampling/createMessage
# 场景: 生成的 Plan 置信度低，请求 LLM 确认是否继续
if plan_confidence < 0.6:
    sampling_request = {
        "method": "sampling/createMessage",
        "params": {
            "messages": [{
                "role": "user",
                "content": f"Plan confidence is low ({plan_confidence:.0%}). "
                           f"Proceed with execution or regenerate plan?"
            }],
            "maxTokens": 100,
        }
    }
```

##### P2-2：增加 SSE/HTTP Transport

```python
# 支持作为 HTTP Server 运行，接受远程 MCP 连接
# 适用于将 agent_go MCP 部署为独立服务
class MCPHTTPServer:
    def __init__(self, host="0.0.0.0", port=8090):
        ...
    # /sse 端点用于 SSE transport
    # /message 端点用于消息收发
```

##### P2-3：MCP 增加 `list_tasks` 工具

```python
{
    "name": "list_tasks",
    "description": "列出所有任务的概要信息（ID / 状态 / 进度 / 描述）",
    "annotations": {"title": "List all tasks", "readOnlyHint": True, "idempotentHint": True},
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["running", "completed", "failed", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        }
    }
}
```

##### P2-4：增加 `--format` 输出切换

```python
run_parser.add_argument("--format", choices=["text", "json", "ndjson"],
                        default="text", help="输出格式")
```

#### P3：长期（低优先级）

- **OS keychain 集成**：`AGENT_GO_API_KEY` 支持从 macOS Keychain / Linux Secret Service 读取
- **多 profile 支持**：`--profile` 切换不同配置（不同 API endpoint、不同默认 Skills）
- **SKILL.md 自描述命令**：`agent_go skills show <name>` 输出 SKILL.md 内容供 Agent 读取
- **命令别名**：`agent_go r` → `agent_go run`，`agent_go ls` → `agent_go list`

---

## 6. 参考资料

### 业界标准与指南

1. **Command Line Interface Guidelines** — https://clig.dev/
   - 开源 CLI 设计指南，UNIX 原则现代化

2. **CLI UX Best Practices** — Evil Martians (2024)
   - 进度显示、spinner、颜色使用的 UX 模式

3. **PatternFly CLI Handbook** — https://www.patternfly.org/content-design/writing-guides/cli-handbook
   - 一致性、可用性、开发者友好的 CLI 设计

### Agent-Native CLI

4. **The Agent Interface Layer: Software's New Platform Primitive** — OSSInsight (2026-03)
   - https://ossinsight.io/blog/agent-native-cli-wave-2026
   - Q1 2026 六仓库集中涌现分析，Fork 比信号

5. **Lark CLI: Put your AI to work in Lark** — 飞书开放平台 (2026)
   - https://open.feishu.cn/document/mcp_open_tools/feishu-cli-let-ai-actually-do-your-work-in-feishu
   - 三层架构（Shortcuts/API/Raw）、24 个 Skills 的 Agent-Native 设计

6. **How Lark CLI Fits Into an AI Coding Agent Workflow** — Verdent Guides (2026)
   - https://www.verdent.ai/guides/lark-cli-ai-coding-agent-workflow
   - Agent 工作流中的 CLI 定位

7. **CLI-Anything: Bridging the Gap Between AI Agents and Software** — HKUDS (2026)
   - https://github.com/HKUDS/CLI-Anything
   - GUI→CLI 转换框架，SKILL.md 能力声明标准

### MCP 协议与最佳实践

8. **MCP Tool Design: Practical Approaches and Tradeoffs** — AWS (2026-07)
   - https://aws.amazon.com/blogs/machine-learning/mcp-tool-design-practical-approaches-and-tradeoffs/
   - V1→V6 工具设计演化：bloat/confusion 框架，context engineering 策略

9. **Designing MCP Servers That Don't Become a New Mess** — Itential (2026-04)
   - https://www.itential.com/resource/blog/designing-mcp-servers-for-infrastructure/
   - 六大生产原则：结果导向、精选面、Resource 契约、Prompt SOP、护栏、失败行为

10. **MCP Security Best Practices** — Model Context Protocol Spec (2026)
    - https://modelcontextprotocol.io/specification/draft/basic/security_best_practices
    - OWASP 对齐的安全指南

11. **Model Context Protocol Server Development Guide** — cyanheads (GitHub)
    - https://github.com/cyanheads/model-context-protocol-resources
    - 最小权限、工具设计、Resource 管理的最佳实践

### agent_go 相关文档

12. `AGENTS.md` — agent_go 项目级 Agent 指引
13. `docs/spec.md` — agent_go 规格说明
14. `docs/prd.md` — agent_go 产品需求文档
15. `docs/roadmap.md` — agent_go 路线图

---

> **文档维护者**：agent_go 架构组  
> **下次审查**：2026-09-01  
> **变更日志**：
> - v1.0 (2026-08-01)：初始版本，覆盖三范式总结、最佳实践、agent_go 现状分析和改进路线
