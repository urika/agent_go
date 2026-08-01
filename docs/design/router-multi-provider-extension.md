# Router 多 Provider 扩展设计

## 背景

当前 Router 模块 (`agent_go/router.py`) 仅影响 Plan 生成阶段，对子任务执行（subtask execution）无影响。主要存在三个已知问题：

| 问题 | 描述 |
|------|------|
| P1. 不影响子任务执行 | `executor.py` 直接 `subprocess.run(["claude", ...])`，使用本地 CLI 配置 |
| P2. agent_type 硬编码 | `api.py:246` 写死 `resolve_provider("architect", config)` |
| P3. API Key 共享 | `call_with_role()` 使用 `get_api_key(config)` 单一 key 调用所有 provider |

本文档提出渐进式解决方案，将 Router 的能力扩展到整个 Agent 执行链路。

---

## 目标

1. **Router 覆盖全链路**：Plan 生成、子任务执行、验证评估均支持角色感知模型路由
2. **多 Provider 无缝切换**：不同角色（planner/worker/reviewer）可用不同 provider 和模型
3. **per-provider API Key**：每个 provider 可独立配置 API Key，支持 `${ENV_VAR}` 语法
4. **为开源 Agent 集成预留扩展点**：接口抽象化，后续可替换 `claude -p` 实现

---

## 架构总览

```
                    ┌──────────────────────────────────┐
                    │         Router Config              │
                    │  roles: { planner, worker,         │
                    │           reviewer }                │
                    │  agent_type_mapping                 │
                    │  circuit_breaker                    │
                    └──────────┬───────────────────────┘
                               │ resolve_provider()
                               ▼
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌─────────────────┐ ┌──────────────┐ ┌──────────────┐
    │   Plan 生成      │ │  子任务执行   │ │  语义评估     │
    │  call_with_role()│ │ Agent 策略   │ │ call_with_role│
    │  (已实现)        │ │ (待实现)     │ │ (待实现)      │
    └─────────────────┘ └──────────────┘ └──────────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼                       ▼
          ┌─────────────────┐    ┌──────────────────┐
          │ ClaudeHeadless  │    │ OpenSourceAgent   │
          │ claude -p (env) │    │ (未来扩展)        │
          └─────────────────┘    └──────────────────┘
```

---

## 设计方案

### Phase 0：per-provider API Key（低风险，独立可用）

#### 改动文件：`agent_go/router.py`

**ProviderConfig 新增 `api_key` 字段：**

```python
@dataclass
class ProviderConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = ""              # ← 新增：per-provider API key
    max_tokens: int = 4096
    temperature: float = 0.2
    max_concurrency: int = 4
    timeout_ms: int = 120000

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        raw_key = data.get("api_key", "")
        # 支持 ${ENV_VAR} 语法
        if raw_key.startswith("${") and raw_key.endswith("}"):
            raw_key = os.environ.get(raw_key[2:-1], "")
        return cls(
            ...
            api_key=raw_key or "",
        )
```

**`call_with_role()` 调整：**

```python
def call_with_role(route, messages, api_key, ...):
    def _try_provider(pc, is_fallback=False):
        effective_key = pc.api_key or api_key   # 优先 per-provider key
        content, pt, ct = _call_api_internal(pc, messages, effective_key)
        ...
```

#### 配置示例：

```json
{
  "router": {
    "enabled": true,
    "roles": {
      "planner": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "api_key": "${ANTHROPIC_API_KEY}",
        "base_url": "https://api.anthropic.com/v1/messages"
      },
      "worker": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "api_key": "${DEEPSEEK_API_KEY}",
        "base_url": "https://api.deepseek.com/v1/chat/completions"
      }
    }
  }
}
```

---

### Phase 1：子任务执行环境变量注入（低风险，快速见效）

#### 改动文件：`agent_go/executor.py`

在 `run_subtask()` 中，构建 `env` 字典之前查询 Router 配置：

```python
from .router import resolve_provider

# 在构建 env 之前（~780行附近）
route = resolve_provider(subtask.get("agent_type", "developer"), config)
if route:
    pc = route.primary
    effective_key = pc.api_key or get_api_key(config)

    if pc.provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = effective_key
        env["ANTHROPIC_BASE_URL"] = pc.base_url
    else:
        # 非 Anthropic provider：设置为 OpenAI 兼容端点
        env["ANTHROPIC_BASE_URL"] = pc.base_url
        env["ANTHROPIC_API_KEY"] = effective_key
        env["OPENAI_API_KEY"] = effective_key

    logger.info(
        f"[Router] subtask {sub_id} ({subtask.get('agent_type', '?')}) "
        f"→ {pc.provider}:{pc.model}"
    )
```

#### 说明：

- `claude` CLI 通过 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_API_KEY` 环境变量控制 API 端点
- 指向 OpenAI 兼容端点（如 DeepSeek）时，`claude` 可通过兼容模式工作
- 这是最轻量的改动（约 30 行），立刻让 Router 影响 subtask 执行

---

### Phase 2：Agent 执行接口抽象（中等风险，为后续扩展奠基）

#### 改动文件：`agent_go/agent_interface.py`（新文件）

```python
"""Agent 执行接口抽象 — 支持多种 Agent 实现。

为 subtask 执行定义统一接口，支持：
- ClaudeHeadlessAgent（当前 claude -p 实现）
- OpenSourceAgent（未来开源 Agent 适配）

所有 Agent 实现需提供以下工具能力：
- Read: 读取文件
- Write: 写入文件
- Edit: 精确编辑文件（行替换）
- Bash: 在 worktree 中执行命令
- Grep: 搜索文件内容
- Glob: 搜索文件路径
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AgentResult:
    """Agent 执行结果。"""
    exit_code: int
    sandbox_type: str
    output: str = ""
    duration_sec: float = 0.0
    commit_sha: Optional[str] = None


class AgentInterface(ABC):
    """Agent 执行器接口。"""

    @abstractmethod
    def run(
        self,
        prompt: str,
        worktree: Path,
        env: dict[str, str],
        agent_type_config: Optional[dict] = None,
    ) -> AgentResult:
        """在 worktree 中执行 prompt 指定的任务。

        Args:
            prompt: 任务 prompt
            worktree: git worktree 路径
            env: 环境变量
            agent_type_config: Agent 类型配置（provider, model, tools 等）

        Returns:
            AgentResult: 执行结果
        """
        ...
```

#### 改动文件：`agent_go/agents.py`

新增工具定义数据结构：

```python
@dataclass
class AgentTool:
    """Agent 可用的工具定义。"""
    name: str          # Read / Write / Edit / Bash / Grep / Glob
    description: str   # 工具描述
```

`AgentType` 新增 `allowed_tools` 的标准化描述：

```python
@dataclass
class AgentType:
    type_name: str
    description: str = ""
    claude_config: dict = field(default_factory=dict)
    preload_skills: list[str] = field(default_factory=list)
    # tools: list[AgentTool] = field(default_factory=list)  # 预留
```

---

### Phase 3：开源 Agent 适配器（中高风险，按需实现）

#### 改动文件：`agent_go/agents/open_source_adapter.py`（新文件）

```python
"""开源 Agent 适配器 — 替代 claude -p 实现。

集成点：
1. executor.py 的 _run_claude() 处策略分发
2. 根据 router 配置的 provider 选择实现
3. 提供 Read/Write/Edit/Bash 工具

需要开源 Agent 框架提供：
- 多 provider 原生支持
- 可自定义工具集
- 纯 Python 库（可 import）
- 轻量依赖
"""

class OpenSourceAgent(AgentInterface):
    """开源 Agent 适配器。"""

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        tools: list[str],
    ):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.tools = tools

    def run(
        self,
        prompt: str,
        worktree: Path,
        env: dict[str, str],
        agent_type_config: Optional[dict] = None,
    ) -> AgentResult:
        """使用开源 Agent 执行任务。"""
        # 1. 初始化 Agent（使用 provider/model/api_key）
        # 2. 注入工具（Read/Write/Edit/Bash）
        # 3. 运行 Agent 循环
        # 4. 收集结果
        # 5. 返回 AgentResult
        ...
```

#### 选择开源 Agent 的考量标准：

| 标准 | 要求 |
|------|------|
| 工具系统 | 能自定义 Read/Write/Edit/Bash 工具 |
| 多 provider | 原生支持 DeepSeek/OpenAI/Anthropic |
| 集成方式 | 纯 Python 库，可 import |
| 依赖规模 | 轻量，不引入过多间接依赖 |
| 社区活跃 | 近 3 个月有更新 |
| 许可证 | MIT / Apache 2.0 |

#### 候选框架评估（示例，需实际调研）：

| 框架 | 多 Provider | 工具系统 | 纯 Python | 依赖 | 备注 |
|------|-------------|----------|-----------|------|------|
| Pi Agent | ✅ | ✅ | ✅ | 轻量 | 待评估 |
| LangChain | ✅ | ✅ | ✅ | 较重 | 过度抽象 |
| AutoGPT | ✅ | ✅ | ⚠️ | 中 | CLI 为主 |
| smolagents | ✅ | ✅ | ✅ | 轻量 | HuggingFace |

---

### Phase 4：Evaluator 接入 Router（低风险）

#### 改动文件：`agent_go/evaluator.py`

```python
from .router import resolve_provider, call_with_role

def evaluate_semantic(...):
    route = resolve_provider("reviewer", config)
    if route:
        content, metering = call_with_role(
            route, messages, api_key, logger,
            task_id=task_id, subtask_id=sub_id,
        )
    else:
        # 回退到原有逻辑
        content = call_api(config, messages, logger)
    ...
```

---

## 实现路线图

> **落地状态（2026-08-01）**：Phase 0 ✅（per-provider key + `${ENV_VAR}`，`config.py:plan_api/planner_api`）；Phase 1 ✅（`worker_backends` → `ANTHROPIC_BASE_URL` 注入，见 executor.py:1237-1245）；Phase 4 ✅（`evaluator.py` 经 `router.py` reviewer 角色）；Phase 2/3 以不同模块落地——未建 `agent_interface.py`/`open_source_adapter.py`，实际以 `agent_loop.py` + `tool_executor.py`（`--agent-loop` 混合策略）实现等价能力。

```
Phase 0: per-provider API Key
  ├── ProviderConfig.api_key
  ├── ${ENV_VAR} 语法支持
  └── call_with_role() 优先使用 per-provider key
  ⏱ 1天 | 🔴 无风险 | ✅ 已实现

Phase 1: 环境变量注入
  ├── executor.py 查询 router
   ├── 注入 ANTHROPIC_API_KEY / BASE_URL
   └── logger 记录路由信息
   ⏱ 0.5天 | 🔴 无风险 | ✅ 已实现

Phase 2: Agent 接口抽象
  ├── AgentInterface / AgentResult
  ├── ClaudeHeadlessAgent 适配
  └── executor.py 策略分发
  ⏱ 1天 | 🟡 中风险（重构需测试覆盖）| 🔶 以 agent_loop.py + tool_executor.py 落地（--agent-loop）

Phase 3: 开源 Agent 适配器
  ├── 选定具体框架
  ├── OpenSourceAgent 实现
  ├── 工具实现（Read/Write/Edit/Bash）
  └── 多轮对话 + 修复循环
  ⏱ 3-5天 | 🟠 中高风险（依赖稳定性）| ⏳ 待实施

Phase 4: Evaluator 接入
  ├── evaluator.py 调用 resolve_provider
  └── 使用 reviewer 路由
  ⏱ 0.5天 | 🔴 无风险 | ✅ 已实现
```

---

## 配置参考（完整示例）

```json
{
  "router": {
    "enabled": true,
    "agent_type_mapping": {
      "developer": "worker",
      "architect": "planner",
      "reviewer": "reviewer",
      "tester": "worker"
    },
    "roles": {
      "planner": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "api_key": "${ANTHROPIC_API_KEY}",
        "base_url": "https://api.anthropic.com/v1/messages",
        "max_tokens": 4096,
        "temperature": 0.2,
        "fallback": {
          "provider": "openai",
          "model": "gpt-4o",
          "api_key": "${OPENAI_API_KEY}",
          "base_url": "https://api.openai.com/v1/chat/completions"
        }
      },
      "worker": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "api_key": "${DEEPSEEK_API_KEY}",
        "base_url": "https://api.deepseek.com/v1/chat/completions"
      },
      "reviewer": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "api_key": "${ANTHROPIC_API_KEY}",
        "base_url": "https://api.anthropic.com/v1/messages",
        "max_tokens": 2048,
        "temperature": 0.1
      }
    },
    "circuit_breaker": {
      "failure_threshold": 5,
      "cooldown_seconds": 60,
      "half_open_requests": 2
    }
  }
}
```

---

## 风险与注意事项

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| `claude` CLI 对非 Anthropic 端点的兼容性 | 功能异常 | Phase 1 为过渡方案，Phase 3 完全绕开 |
| `${ENV_VAR}` API Key 泄漏 | 安全风险 | 文档强调不写入 config.json，使用环境变量 |
| Fallback 跨 provider | Key 必须分别配置 | 配置示例明确标注 |
| `ANTHROPIC_BASE_URL` 影响范围 | 全局生效 | 仅对当前子进程的 env 生效，不影响其他进程 |
| 开源 Agent 框架 API 变更 | 适配器损坏 | 锁定版本 + 单元测试 |

---

## 相关文件

| 文件 | 作用 |
|------|------|
| `agent_go/router.py` | Router 核心：ProviderConfig, RoleRoute, CircuitBreaker, resolve_provider, call_with_role |
| `agent_go/executor.py` | 子任务执行器：_run_claude, _run_headless, run_subtask |
| `agent_go/agents.py` | Agent 类型定义：AgentType, load_agent_type, get_claude_command |
| `agent_go/subtask.py` | 无头模式执行：_run_headless, _git_merge_upstream |
| `agent_go/evaluator.py` | 语义评估：evaluate_semantic |
| `agent_go/config.py` | 配置加载：DEFAULT_CONFIG, load_config |
| `agent_go/api.py` | Plan 生成：generate_plan, call_api |

---

## Phase 5：SDK 化 Agent 执行引擎（替代方案分析）

### 背景

[OpenChamber](https://openchamber.dev/) 使用 `@opencode-ai/sdk`（TypeScript）实现程序化 Agent 调用，传入 prompt 和工具定义，返回结构化的工具调用结果。这与 agent_go 当前通过 `subprocess.run(["claude", "-p", ...])` 调用 Claude Code 子进程的方案形成对比。

本节分析 SDK 方案对 agent_go 的借鉴意义，并给出三种可选实施路径。

### SDK vs 子进程方案对比

| 维度 | SDK 方案（OpenChamber） | 子进程方案（agent_go 当前） |
|------|------------------------|---------------------------|
| 集成方式 | `import / require` 库 | `subprocess.run()` |
| Agent 循环 | 自己实现（或 SDK 内置） | Claude Code 内置（Read/Write/Edit/Bash 全栈） |
| 工具执行 | SDK 返回 tool_call → 自己执行 → 回传结果 | Claude Code 自动执行并提交 git commit |
| 多 provider | ✅ 原生支持 | ⚠️ 仅 Anthropic（通过环境变量 hack 可指向其他） |
| 成本控制 | ✅ 完全可见（每 token 可追踪） | ⚠️ 黑盒子（需从 stream-json 解析） |
| 启动开销 | 无（内存中调用） | ~2–5s 子进程创建 + claude 初始化 |
| 稳定性 | 依赖 SDK 版本兼容性 | 依赖 claude CLI 安装 |
| 架构复杂度 | 高（需实现工具执行器） | 低（Claude Code 全包） |

### 核心差异

SDK 方案的核心价值不是"用 SDK 替代子进程"，而是**把 Agent 引擎从黑盒变成可编程的**：

**子进程方案：**
```
prompt → [claude -p 黑盒] → git commit（你只能看到结果）
```

**SDK 方案：**
```
prompt → [你的代码] ←→ tool_calls（你控制每一步）
                 ↓
            LLM API（模型/成本完全可见）
```

### 方案 A：直接 API + 工具执行器（推荐起点）

既然 agent_go 已经直接调用 LLM API 做 Plan 生成（`call_api()` / `call_with_role()`），可以把同样的方式扩展到子任务执行：

**当前链路：**
- Plan 生成 → `call_api()` ✅（已有）
- 子任务执行 → `subprocess.run(["claude", ...])`（黑盒）

**目标链路：**
- Plan 生成 → `call_api()` ✅
- 子任务执行 → `call_api()` + 工具执行器（Read/Write/Edit/Bash 在 Python 中实现）

#### 需要实现的组件

| 组件 | 代码量 | 风险 | 说明 |
|------|--------|------|------|
| `FileReadTool` | ~30 行 | 低 | 读取 worktree 中的文件 |
| `FileWriteTool` | ~30 行 | 低 | 写入文件 |
| `FileEditTool` | ~100 行 | 中 | 精确行替换/正则替换（类似 Claude Code Edit） |
| `BashTool` | ~50 行 | 低 | 在 worktree 中执行命令 |
| `AgentLoop`（多轮） | ~150 行 | 中 | 对话历史管理：LLM 返回 tool_call → 执行 → 回传 → 继续 |
| 验证 + 修复循环 | ~100 行 | 中 | 复用现有 `_build_repair_prompt` |
| **合计** | **~460 行** | | |

#### 架构示意

```
agent_go 编排层
    │
    ├── Plan 生成: call_with_role(route, messages)     ← 已有
    │
    └── 子任务执行: AgentEngine.run(prompt, worktree)
            │
            ▼
    ┌─────────────────┐
    │  AgentLoop       │ ← 多轮对话管理
    │  ┌─────────────┐ │
    │  │ call_api()   │ │ ← 复用现有 api.py
    │  └──────┬──────┘ │
    │         ▼        │
    │  tool_calls      │
    │         │        │
    │  ┌──────┴──────┐ │
    │  │ ToolExecutor │ │ ← 新实现
    │  │  ├─ Read    │ │
    │  │  ├─ Write   │ │
    │  │  ├─ Edit    │ │
    │  │  └─ Bash    │ │
    │  └──────┬──────┘ │
    │         ▼        │
    │  tool_results    │
    │         │        │
    │  ┌──────┴──────┐ │
    │  │ 是否完成?    │ │
    │  │ 是→git commit│ │
    │  └─────────────┘ │
    └─────────────────┘
```

### 方案 B：OpenCode CLI 替换

使用开源 [OpenCode](https://github.com/suyash-thakur/opencode) 子进程替代 claude 子进程：

```python
# 当前
subprocess.run(["claude", "-p", prompt, ...])

# 替代
subprocess.run(["opencode", "-p", prompt,
    "--model", "deepseek-v4-flash",
    "--provider", "openai-compatible",
    "--api-key", os.environ["DEEPSEEK_API_KEY"]])
```

**优点：**
- 开源，可定制
- 原生支持多 provider
- 社区活跃

**缺点：**
- 需要额外安装 OpenCode
- OpenCode 的 Agent 能力与 Claude Code 有差距（特别是文件编辑精度、git 集成）
- 需要维护两个 Agent 引擎的兼容性

**工作量：~50 行（主要修改 `executor.py` 的 `_run_claude()`）**

### 方案 C：混合策略（强烈推荐）

```
Plan 生成: call_with_role()  ← 已有，直接 API
子任务执行:
  ├── 简单任务（单文件修改、测试编写等）
  │   └── call_api() + 简单工具执行器   ← 方案 A
  └── 复杂任务（跨文件重构、探索性编程等）
      └── claude -p 子进程              ← 保持现状
```

**简单 vs 复杂任务的判定依据：**
- 涉及文件数量 ≤ 3 → 简单
- 涉及文件数量 > 3 → 复杂
- 包含"探索"、"理解"、"分析"等关键词 → 复杂
- 仅"添加"、"修改"、"删除"特定代码 → 简单

**优点：**
- 简单任务获得多 provider 灵活性和成本可见性
- 复杂任务保留 Claude Code 的强 Agent 能力
- 渐进式迁移，风险可控

**工作量：~200 行（工具执行器 + 任务分类逻辑）**

### 方案选型建议

| 方案 | 工作量 | 收益 | 推荐 |
|------|--------|------|------|
| 直接 API + 工具执行器 | ~460 行 | 完全多 provider 支持、成本可见 | ⭐⭐⭐ |
| OpenCode CLI 替换 | ~50 行 | 开源、多 provider | ⭐⭐ |
| 混合策略 | ~200 行 | 兼顾灵活性和稳定性 | ⭐⭐⭐⭐⭐ |
| `@opencode-ai/sdk` 集成 | 高（跨语言） | agent_go 是 Python，SDK 是 TypeScript | ⭐ |

### 实施路线图

推荐从**方案 C（混合策略）**开始，分三步走：

**Step 1：工具执行器基础实现（~200 行）**
- `FileReadTool`、`FileWriteTool`、`FileEditTool`、`BashTool`
- 简单 `AgentLoop`（单轮 tool_call → 执行 → 返回）
- 新增模块：`agent_go/tool_executor.py`、`agent_go/agent_loop.py`
- ⏱ 1–2 天

**Step 2：简单任务分流逻辑（~50 行）**
- 任务复杂度判定函数（基于文件数量 + 关键词）
- `executor.py` 决策分支：简单→直接 API，复杂→claude -p
- ⏱ 0.5 天

**Step 3：完善 AgentLoop 多轮对话（~150 行）**
- 多轮对话历史管理（token 上限控制）
- 验证 + 修复循环集成（复用现有逻辑）
- git commit 自动提交
- ⏱ 1–2 天

**总工期：~3–4 天 | 风险：中低 | 迁移路径：渐进式**

### 与现有 Router 模块的关系

方案 C 与已有 Router 模块 (`router.py`) 无缝配合：

```python
# executor.py 中简单任务分支
from .router import resolve_provider, call_with_role

if is_simple_task(subtask):
    route = resolve_provider(subtask.get("agent_type", "developer"), config)
    if route:
        pc = route.primary
        # 使用 call_with_role 调用 API，复用现有路由逻辑
        result = agent_loop.run(
            prompt=subtask_prompt,
            worktree=worktree_path,
            route=route,
            api_key=config.get("api_key"),
        )
```

## 验证方案

1. **Phase 0 单元测试**：`tests/test_router.py` 新增测试用例，验证 per-provider API Key 解析
2. **Phase 1 集成测试**：使用 E2E 测试（`tests/`），验证不同 provider 下 subtask 执行
3. **Phase 2 单元测试**：Mock Agent 接口，验证策略分发逻辑
4. **Phase 3 E2E 测试**：实际调用开源 Agent 完成简单任务（如添加函数）
5. **Phase 5 单元测试**：Mock LLM API，验证工具执行器（Read/Write/Edit/Bash）的正确性
6. **Phase 5 集成测试**：验证简单/复杂任务分流逻辑
7. **整体验证**：`python3 agent_go.py run <repo> '<task>' --yes --parallel 2`
