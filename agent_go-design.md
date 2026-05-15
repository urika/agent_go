# agent_go 增强版 — 设计文档

> 日期: 2026-05-15  
> 定位: MacBook 单文件快速原型，支持外接 API Plan Mode、共享资源清单、Agent Prompt、默认同意模式

---

## 一、设计目标

在保持"单文件、零额外依赖（除 Claude Code 和可选 Greywall）、1 小时可跑通"的前提下，实现：

1. **外接 API Plan Mode**：调用 Claude / OpenAI / DeepSeek 生成详细执行方案
2. **共享资源清单**：Plan 输出包含项目 Git 信息、关键目录、配置文件、环境变量
3. **Agent Prompt**：每个步骤包含给 Claude Code 的完整执行指令
4. **补充输入与文档挂载**：Plan 阶段支持用户修正和参考文档注入
5. **默认同意模式**：支持非交互批量执行，同时保留强制交互覆盖能力
6. **完整审计**：全链路结构化日志，支持事后追溯

---

## 二、核心架构

```
[用户输入]
    agent_go run <repo> "<task>" [--docs <doc1,doc2>]
    │
    ▼
[配置加载]
    ├── 读取 ~/.agent_go/config.json
    ├── 合并默认值（兼容新增字段）
    └── 从环境变量 AGENT_GO_API_KEY 读取密钥
    │
    ▼
[1. 项目分析]
    ├── git ls-files（或 find）获取文件列表
    ├── git remote/branch/commit 获取版本信息
    └── 扫描关键目录和配置文件生成资源清单
    │
    ▼
[2. Plan Mode — 外接 API]
    ├── 构造 Prompt（任务 + 项目文件 + 资源清单 + 参考文档）
    ├── 调用外接 API（Anthropic / OpenAI / DeepSeek / Custom）
    └── 解析返回 JSON：
        {
          "overview": "任务概述",
          "steps": [{"id", "title", "description", "files", "verification", "risks", "agent_prompt"}],
          "dependencies": {"2": [1]},
          "estimated_effort": "2-3小时",
          "shared_resources": {"directories", "git_remote", "config_files", "env_vars"}
        }
    │
    ▼
[3. Plan 确认]
    ├── 展示方案（概述 + 资源清单 + 步骤 + Agent Prompt）
    └── 用户选择：
        Y → 确认
        S → 补充输入（重新生成，携带补充内容）
        D → 挂载参考文档（重新生成，携带文档内容）
        E → 编辑步骤（本地修改，不调用 API）
        R → 重新生成（再次调用 API）
        N → 取消
    │
    ▼
[4. 子任务拆解]
    ├── Plan.steps → subtasks
    ├── 注入 Agent Prompt 到子任务描述
    ├── 注入共享资源清单到子任务描述
    └── 生成 TASK.md（Claude Code 可读）
    │
    ▼
[5. 子任务确认]
    ├── 展示子任务列表（含 Agent Prompt 预览）
    └── 用户选择：Y/N/E/A/D
    │
    ▼
[6. 逐个执行]
    对每个子任务 sub-i：
        ├── 创建隔离 worktree：~/.agent_go/task-xxx/sub-i/work
        ├── 复制项目（git clone 或 shutil.copytree）
        ├── 写入 TASK.md（含 Agent Prompt + 资源清单）
        ├── 启动 Greywall + Claude Code（或原生降级）
        ├── 用户与 Claude Code 终端交互完成编码
        └── 用户退出 Claude Code
    │
    ▼
[7. 进展验证]
    ├── git diff --stat 生成变更摘要
    └── 用户选择：C/R/M/A
    │
    ▼
[8. 最终归档]
    ├── 汇总结果
    ├── 写入 meta.json（含参考文档列表）
    ├── 写入 execution.log（结构化审计日志）
    └── 终端展示最终报告
```

---

## 三、关键设计决策

### 3.1 外接 API 统一接口

**问题**：不同供应商 API 格式不同（Anthropic Messages vs OpenAI Chat Completions）。

**方案**：统一封装 `call_api()` 函数，内部根据 `provider` 字段适配请求体和响应解析。

| 供应商 | 认证头 | 请求体 | 响应解析 |
|--------|--------|--------|----------|
| Anthropic | `x-api-key` + `anthropic-version` | `messages` + `max_tokens` | `content[0].text` |
| OpenAI | `Authorization: Bearer` | `messages` + `max_tokens` | `choices[0].message.content` |
| DeepSeek | `Authorization: Bearer` | `messages` + `max_tokens` | `choices[0].message.content` |
| Custom | `Authorization: Bearer` | `messages` + `max_tokens` | `choices[0].message.content` |

**密钥优先级**：环境变量 `AGENT_GO_API_KEY` > 配置文件 `api_key`。

### 3.2 Plan Prompt 工程

**系统 Prompt 设计原则**：

1. **强制 JSON 输出**：要求模型只返回合法 JSON，不含 markdown 代码块
2. **字段完整性**：每个 step 必须包含 `agent_prompt` 和 `shared_resources`
3. **上下文丰富**：注入项目文件列表、Git 信息、资源清单，让模型了解项目结构
4. **可迭代**：支持 `supplement`（用户补充）和 `reference_docs`（参考文档）注入

**Prompt 结构**：

```
System: 你是资深软件架构师。输出必须是合法 JSON，结构为...
User:
  任务: <task>
  项目路径: <repo>
  Git 信息: 远程=<remote>, 分支=<branch>, 提交=<commit>
  项目文件列表: <files>
  项目资源: 目录=<dirs>, 关键文件=<files>
  ===== 用户补充 =====
  <supplement>
  ===== 参考文档 =====
  <reference_docs>
```

### 3.3 共享资源清单设计

**资源类型**：

| 类型 | 采集方式 | 用途 |
|------|----------|------|
| `git_remote` | `git remote get-url origin` | Agent 知道代码来源 |
| `git_branch` | `git branch --show-current` | Agent 知道当前分支 |
| `git_commit` | `git rev-parse --short HEAD` | Agent 知道基准版本 |
| `directories` | 扫描 `src/`, `tests/`, `docs/` 等 | Agent 了解项目结构 |
| `config_files` | 扫描 `package.json`, `.env.example` 等 | Agent 了解配置依赖 |
| `env_vars` | 从 `.env.example` 或配置推断 | Agent 了解环境变量 |

**注入方式**：
- Plan API 返回的 `shared_resources` 优先
- 本地分析结果作为兜底补充
- 最终注入每个子任务的 `TASK.md` 和 `description`

### 3.4 Agent Prompt 设计

**目标**：给 Claude Code 的指令必须**自包含**，即 Agent 仅凭 `TASK.md` 就能理解要做什么，无需额外上下文。

**Agent Prompt 包含要素**：

1. **角色定义**："你是一个后端安全专家"
2. **具体任务**："修改 src/auth/jwt.ts，将 jwt.sign 的算法从 HS256 改为 RS256"
3. **操作步骤**："1. 生成 RSA 密钥对 2. 修改签名逻辑 3. 新增公钥端点"
4. **共享资源**："Git 远程: xxx，当前分支: main，关键目录: src/auth/**"
5. **验证命令**："完成后运行 npm run test:auth"
6. **约束条件**："不要改动接口格式，保留现有 JWT 结构"

### 3.5 默认同意模式

**使用场景**：批量任务、信任度高的重复任务、CI/CD 集成。

**实现方式**：

```python
# 配置层级
config.behavior.auto_confirm_plan      # Plan 自动确认
config.behavior.auto_confirm_subtasks  # 子任务自动确认

# 环境变量强制覆盖
os.environ.get("AGENT_GO_INTERACTIVE") == "1"  # 强制进入交互模式
```

**交互流程**：

```
if auto_confirm and 首次迭代:
    展示方案
    提示: "按 Enter 直接确认，或输入任意键进入交互"
    if 用户按 Enter:
        自动确认，记录日志
    else:
        进入交互模式
```

### 3.6 补充输入与文档挂载

**补充输入（S 键）**：

- 用户输入多行文本（空行结束）
- 文本作为新上下文注入 Prompt：`===== 用户补充 =====`
- 重新调用 API 生成方案
- 保留原有参考文档（如有）

**文档挂载（D 键 / --docs）**：

- 支持文件路径和目录（目录自动读取所有 `.md`）
- 文件内容截断（单文件最大 15000 字符，避免 Prompt 过长）
- 注入 Prompt：`===== 参考文档 =====`
- 重新调用 API 生成方案

**迭代控制**：

- `max_plan_iterations` 配置（默认 5）
- 超过上限时使用最后版本

### 3.7 降级策略

**三层降级**：

| 优先级 | 方式 | 触发条件 | 质量 |
|--------|------|----------|------|
| 1 | 外接 API Plan Mode | API Key 有效、网络正常 | 最高 |
| 2 | 本地模型拆解 | localhost:8000 可访问 | 中等 |
| 3 | 规则模板 | 关键词匹配 | 最低 |

**降级日志**：每次降级记录原因，便于后续优化。

---

## 四、数据结构

### 4.1 Plan（API 返回）

```json
{
  "overview": "任务概述",
  "steps": [
    {
      "id": 1,
      "title": "后端JWT签名迁移",
      "description": "生成RSA密钥对...",
      "files": ["src/auth/jwt.ts", "src/config/auth.ts"],
      "verification": "npm run test:auth",
      "risks": ["密钥文件权限需600"],
      "agent_prompt": "你是一个后端安全专家。请修改 src/auth/jwt.ts..."
    }
  ],
  "dependencies": {"2": [1], "3": [1, 2]},
  "estimated_effort": "2-3小时",
  "shared_resources": {
    "directories": ["src", "tests"],
    "git_remote": "git@github.com:acme/platform.git",
    "git_branch": "main",
    "config_files": ["package.json", ".env.example"],
    "env_vars": ["JWT_SECRET", "JWT_EXPIRES_IN"]
  }
}
```

### 4.2 子任务（内部使用）

```json
{
  "id": "sub-1",
  "title": "后端JWT签名迁移",
  "description": "完整描述 + Agent Prompt + 资源清单 + 验证命令 + 风险提示",
  "files_hint": "src/auth/jwt.ts, src/config/auth.ts",
  "agent_prompt": "你是一个后端安全专家...",
  "verification": "npm run test:auth",
  "risks": ["密钥文件权限需600"]
}
```

### 4.3 TASK.md（写入 worktree）

```markdown
# 子任务: 后端JWT签名迁移

## 描述
生成RSA密钥对，修改 src/auth/jwt.ts...

## 执行指令（Agent Prompt）
你是一个后端安全专家。请修改 src/auth/jwt.ts...

## 共享资源清单
Git 远程: git@github.com:acme/platform.git
当前分支: main
关键目录: src, tests
配置文件: package.json, .env.example

## 执行要求
- 在此隔离 worktree 中完成修改
- 完成后退出 Claude Code（/exit 或 Ctrl+D）
- 变更保留在此目录
```

### 4.4 任务元数据（meta.json）

```json
{
  "task_id": "task-20260515-093045",
  "task": "将JWT签名从HS256改为RS256",
  "repo": "/Users/you/projects/platform",
  "created": "20260515-093045",
  "status": "completed",
  "reference_docs": ["README.md", "docs/auth-spec.md"],
  "subtasks": [...],
  "results": [...]
}
```

---

## 五、日志系统

### 5.1 日志事件类型

| 事件 | 触发时机 | 关键字段 |
|------|----------|----------|
| `task_init` | 任务创建 | task_id, task, repo |
| `plan_generate` | Plan 生成开始 | iteration, has_supplement, has_docs |
| `api_call` | API 调用完成 | provider, latency_ms, response_len |
| `plan_complete` | Plan 生成完成 | iteration, step_count |
| `plan_auto_confirmed` | 默认同意自动确认 | iteration |
| `user_plan_choice` | 用户 Plan 阶段选择 | choice, iteration |
| `subtasks_confirmed` | 子任务列表确认 | final_count, edit_count |
| `subtasks_auto_confirmed` | 子任务自动确认 | count |
| `user_subtask_choice` | 用户子任务阶段选择 | choice |
| `subtask_start` | 子任务开始 | id, title, worktree |
| `subtask_complete` | 子任务完成 | id, status, sandbox_type, duration_sec |
| `subtask_modified` | 子任务修改后 | id, modify_duration_sec |
| `user_verify` | 验证阶段用户选择 | current, choice |
| `task_aborted` | 任务中止 | completed_subtasks, abort_after |

### 5.2 日志格式

同一文件双格式：

```
# 文本行（INFO 级别，人类可读）
2026-05-15 09:30:45 | INFO     | agent_go.task-xxx | 任务拆解完成，生成 3 个子任务

# JSON 行（DEBUG 级别，机器解析）
2026-05-15 09:30:45 | DEBUG    | agent_go.task-xxx | {"event": "decompose_complete", "method": "api", "subtask_count": 3}
```

---

## 六、安全边界

| 层级 | 机制 | 说明 |
|------|------|------|
| **项目隔离** | Git worktree / shutil.copytree | 原始项目只读，变更仅在副本 |
| **进程隔离** | Greywall（Landlock + Seccomp） | 可选，阻断危险命令 |
| **网络隔离** | Greywall TUN 代理 | 可选，限制外联域名 |
| **凭证隔离** | 无（POC 阶段） | 使用主机默认 Git/SSH |
| **API 密钥** | 环境变量优先 | 不硬编码在脚本中 |

---

## 七、配置系统

### 7.1 配置层级

```
默认值（代码内嵌）
    ↓ 覆盖
~/.agent_go/config.json（用户配置）
    ↓ 覆盖
环境变量（AGENT_GO_API_KEY, AGENT_GO_INTERACTIVE）
```

### 7.2 配置字段

```json
{
  "plan_api": {
    "provider": "anthropic|openai|deepseek|custom",
    "base_url": "...",
    "api_key": "",
    "model": "...",
    "max_tokens": 4096,
    "temperature": 0.2
  },
  "behavior": {
    "auto_confirm_plan": false,
    "auto_confirm_subtasks": false,
    "show_agent_prompt": true,
    "show_resource_map": true,
    "max_plan_iterations": 5
  },
  "fallback": {
    "local_model_url": "http://localhost:8000/v1/chat/completions",
    "local_model_name": "qwen",
    "enable_rules": true
  }
}
```

---

## 八、使用约束

1. **Python >= 3.11**
2. **Claude Code 已安装**
3. **外接 API Key**（Plan Mode 需要，降级模式不需要）
4. **Greywall 可选**
5. **项目需为 Git 仓库或普通目录**

---

*文档结束*
