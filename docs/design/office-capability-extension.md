# 设计稿：办公能力扩展（MCP 消费 + 产物导出）

> **状态**：能力 A（MCP 消费层）✅ 已实现（2026-08-01，`agent_go/mcp_client.py` + `mcp_servers` config）；能力 B（产物导出）设计稿（2026-08-01）
> **关联**：[prd.md](../prd.md) 「办公能力扩展」章节、[roadmap.md](../roadmap.md) S9 迭代
> **背景调研**：见同目录调研附件《Office AI 助手生态调研》——业界已通过 MCP 协议标准化 Office 文档自动化，社区生态成熟（excel-mcp-server 4084★、office-powerpoint-mcp-server 1847★）

## 一、问题陈述

### 1.1 现状：agent_go 是代码 diff 导向的编排器

agent_go 的整个执行模型围绕 **git worktree + 代码变更** 构建：

```
subtask → worktree 隔离 → claude 执行 → git commit + tag → (可选) push PR
```

这个模型对"修改源码"非常优秀，但存在两个结构性缺口，导致 agent_go **无法把工作成果搬运到代码之外的交付物**（文档、报告、表格、演示文稿）：

| 缺口 | 表现 | 后果 |
|------|------|------|
| **A. 无外部工具消费能力** | agent_go 仅作为 MCP **server** 暴露，不消费任何外部 MCP server；工具硬编码为 Read/Write/Edit/Bash 四个，沙箱禁网络 | 无法接入已成标准的 Office MCP 生态，子任务只能改代码，不能生成 .pptx/.xlsx |
| **B. 无产物导出路径** | 子任务在临时 worktree 中执行，pipeline 完成后清理 worktree（`pipeline.py:378-383`）；交付模型只有"代码 diff → commit" | 即便子任务生成了文档，文件会随 worktree 清理而丢失，无法交付给用户 |

### 1.2 机会：MCP 生态已成熟，无需自建

业界已通过 MCP 协议将 Office 文档操作标准化，且出现了 **"CLI（高吞吐批量）+ MCP（交互探索）双模"** 的工具层设计范式（参考 sbroenne/mcp-server-powerpoint）。

agent_go 的差异化护城河是 **Plan → Decompose → Execute 编排层**，不是文档生成引擎。因此正确策略是：**补齐"搬运"与"交付"两个架构能力，复用生态工具，而非自建 Office 编辑器。**

### 1.3 设计哲学锚点

对照知识工作分层模型（来自调研附件）：

```
专业数据平台 / 信息服务        agent_go 编排层              Office 消费层
(确定性：口径/监测/沉淀)    ─── (跨层搬运与转换)───    (不确定性：探索/判断/表达)
       │                            │                            │
  数据湖仓 / Wind / 天眼查     Plan→Decompose→Execute         Word / PPT / Excel
                                  │
                          本设计补齐：MCP 消费 + 产物导出
```

> **核心原则**：Agent 负责在数据层与消费层之间搬运和转换，但永远不越过平台的口径和权限边界。agent_go 要成为合格的编排层，需要"搬运"（工具消费）和"交付"（产物导出）能力，而非把自己变成办公软件。

---

## 二、能力 A：MCP 消费层（MCP Client）

### 2.1 目标

让 agent_go 子任务在执行时，能够调用**用户配置的外部 MCP server** 暴露的工具（包括但不限于 Office 文档操作），如同调用原生 Read/Write/Bash 一样自然。

### 2.2 配置契约

在 `config.json` 新增 `mcp_servers` 节（沿用 MCP 客户端的 stdio 启动约定）：

```jsonc
{
  "mcp_servers": {
    "excel": {
      "command": "uvx",
      "args": ["excel-mcp-server", "stdio"],
      "env": {},
      "enabled": true,
      "tool_filter": ["read_sheet", "write_sheet"]   // 可选：只暴露部分工具，省 token
    },
    "ppt": {
      "command": "uvx",
      "args": ["--from", "office-powerpoint-mcp-server", "ppt_mcp_server"],
      "enabled": true
    },
    "ms365": {
      "command": "npx",
      "args": ["-y", "@softeria/ms-365-mcp-server", "--org-mode"],
      "enabled": false,                                // 默认关，需 OAuth 时手动开
      "scope": "planner_only"                          // 可选：仅规划阶段可见
    }
  }
}
```

**字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `command` | 是 | 启动 MCP server 的可执行命令（uvx/npx/python） |
| `args` | 是 | 命令参数 |
| `env` | 否 | 注入子进程的环境变量（API key 等） |
| `enabled` | 否 | 默认 `true`；批量开关 |
| `tool_filter` | 否 | 白名单，只把指定工具暴露给 LLM（省 token、收窄能力） |
| `scope` | 否 | `worker`（默认，仅执行子任务可见）/ `planner_only` / `always` |

### 2.3 模块设计：`agent_go/mcp_client.py`（新增）

```
mcp_client.py
├── MCPServerConnection          # 单个 server 连接的生命周期管理
│   ├── __init__(config_entry)
│   ├── start()                  # subprocess.Popen + JSON-RPC initialize 握手
│   ├── list_tools() -> list     # tools/list → 工具 schema
│   ├── call_tool(name, args)    # tools/call → 结果
│   └── stop()                   # 优雅关闭 + 进程回收
│
├── MCPClientPool                # 多 server 连接池（核心入口）
│   ├── __init__(server_configs: dict)
│   ├── start_all()              # 并发启动所有 enabled server，超时 10s
│   ├── all_tools() -> list      # 合并所有 server 的工具 schema（供 LLM tools 字段）
│   ├── dispatch(tool_name, args, worktree)  # 按 namespace 路由到对应 server
│   └── stop_all()               # pipeline 结束时统一回收
│
└── _namespace_tool(server_key, tool_name)  # 命名空间映射：避免跨 server 工具重名
```

**命名空间约定**：外部工具对外暴露为 `mcp__{server_key}__{tool_name}`（如 `mcp__excel__read_sheet`），dispatch 时按 `__` 拆分路由。这避免 `read` 这类通用名冲突，也让 LLM 和用户都能识别工具来源。（落地为 `mcp_client.py` 的 `_TOOL_PREFIX = "mcp__"` + server key + `__` + tool name）

### 2.4 与执行链路集成

改动点（最小侵入）：

**① `pipeline.py`：pipeline 启动时拉起连接池，结束时回收**

```python
# _run_pipeline() 开头
mcp_pool = MCPClientPool(config.get("mcp_servers", {}))
mcp_pool.start_all()
try:
    # ... 现有 wave 调度 ...
finally:
    mcp_pool.stop_all()
```

**② `executor.py` / `subtask.py`：把 MCP 工具列表注入子任务执行环境**

- **AgentLoop 路径**（`agent_loop.py:187`）：`tools` 字段合并 `ToolRegistry.definitions()` + `mcp_pool.all_tools()`；dispatch 时先查 MCP 命名空间，未命中再走原生工具。
- **claude CLI 路径**（`executor.py:284`）：通过 `--mcp-config <tmpfile>` 参数把 server 配置透传给 claude 进程（claude 原生支持 MCP server 消费）。

**③ 工具调用沙箱边界**：MCP 工具调用不受 Bash blocklist 限制（它不是 shell），但仍受 `--timeout`（60s 默认，可配）和 `result` 截断（8000 字符）约束。Office server 自身有文件路径约束。

### 2.5 故障隔离

遵循 agent_go 既有的"增强模块动态导入 + 优雅降级"原则：

- MCP server 启动失败 → `logger.warning` + 跳过该 server，**不阻断 pipeline**（与 notify/skills 同级降级）。
- 单次工具调用超时/异常 → 返回结构化错误给 LLM，LLM 可选择重试或换路径（已有 ToolResult 错误格式复用）。
- 连接池在 finally 中强制回收，避免僵尸进程（`stop_all()` 用 `proc.terminate()` + 3s grace + `proc.kill()`）。

### 2.6 安全考量

- **白名单优于黑名单**：`tool_filter` 让用户显式声明信任哪些工具，默认暴露全部但鼓励收窄。
- **路径逃逸**：Office MCP server（如 python-pptx/openpyxl 类）能读写任意路径，这是 server 侧职责；agent_go 在文档中明确提示用户审计 server 的权限边界。
- **凭据隔离**：`env` 字段注入的 API key 不写入 metering/日志（与现有 `api_key` 脱敏逻辑一致）。

---

## 三、能力 B：产物导出路径（Artifact Export）

### 3.1 目标

区分两种交付物，并提供独立的导出机制：

| 交付物类型 | 例子 | 现有支持 | 本设计新增 |
|-----------|------|---------|-----------|
| **代码变更**（code-diff） | 修复 bug、新增函数 | ✅ worktree → commit → tag → PR | — |
| **产物文件**（artifact） | .pptx 报告、.xlsx 数据表、.md 文档 | ❌ 随 worktree 清理丢失 | ✅ 导出到用户目录 |

### 3.2 交付目录契约

引入 `--artifact-dir`（CLI）和 `artifact_dir`（config）配置：

```bash
# 用户显式指定导出目录
agent_go run ./repo "生成 Q3 季度汇报 PPT" --artifact-dir ~/Desktop/reports

# config 默认（不指定时）
{
  "artifact_dir": null   # null = 不导出，产物留在 worktree（向后兼容）
}
```

**导出规则**：

1. **声明制**：子任务通过 `Write` 工具写入 `__artifacts__/` 目录（worktree 内的约定目录）的文件，视为产物。
2. **pipeline 收尾时收集**：`pipeline.py` 在清理 worktree 前，扫描每个 worktree 的 `__artifacts__/`，把文件复制到 `artifact_dir`，按 `{task_id}/{sub_id}/{filename}` 组织。
3. **保留 worktree 优先级**：若 worktree 被 `--preserve-worktrees` 保留（失败现场），不自动清理，产物仍在原处；导出逻辑兼容这种情况。

### 3.3 模块设计：`agent_go/artifacts.py`（新增）

```
artifacts.py
├── ARTIFACT_DIR_NAME = "__artifacts__"   # worktree 内约定目录名
│
├── collect_from_worktree(worktree_path, sub_id) -> list[Path]
│   # 扫描 worktree/__artifacts__/**，返回产物文件列表
│
├── export(task_id, results, artifact_dir, task_dir) -> dict
│   # 遍历所有子任务的 worktree（含保留的），收集产物到 artifact_dir
│   # 返回 {"exported": [...], "skipped": [...], "dir": str}
│
└── render_export_summary(export_result) -> str
    # 生成可读的导出清单（供 final report 展示）
```

### 3.4 与执行链路集成

**① `executor.py`：在 TASK.md 中注入产物目录约定**

```markdown
## 产物输出
如需生成文档/表格/演示文稿等非代码交付物，写入 `__artifacts__/` 目录。
该目录下的文件将在任务完成后导出到指定位置（`--artifact-dir`），不会随 worktree 清理丢失。
```

**② `pipeline.py`：清理 worktree 前调用导出**

```python
# _run_pipeline() 收尾，在 worktree 清理之前
if config.get("artifact_dir"):
    from .artifacts import export, render_export_summary
    export_result = export(task_id, results_map, config["artifact_dir"], task_dir)
    final_report += render_export_summary(export_result)

# 然后才是现有的 worktree 清理逻辑（pipeline.py:378-383）
```

**③ final report 增强**：任务结束报告中列出导出的产物文件清单（文件名 + 路径 + 大小），与现有"保留 worktree 路径"清单并列。

### 3.5 与 MCP 消费层的协同

能力 A（MCP 消费）和能力 B（产物导出）协同才能闭环：

```
子任务调用 excel__write_sheet 生成报表
        ↓（MCP server 写文件到 worktree/某路径）
子任务把成品复制到 __artifacts__/Q3_report.xlsx（或 MCP server 直接写这里）
        ↓
pipeline 收尾扫描 __artifacts__/ → 导出到 ~/Desktop/reports/
```

**约定**：Office MCP server 写入的文件若在 worktree 根目录，agent_go 不自动捕获；只有写入 `__artifacts__/` 的才视为产物。这保证"声明制"——LLM 需显式决定哪些是交付物。TASK.md prompt 会引导这一行为。

---

## 四、不做什么（显式排除）

为防止范围蔓延，以下**明确不做**：

| 排除项 | 理由 |
|--------|------|
| ❌ 内建 Office 编辑器（自研 python-pptx/openpyxl 工具） | 生态已成熟，自建是重复造轮子；违背"编排层"定位 |
| ❌ 内建 PDF/图片转换 | 走 MCP server（如 libre-office-mcp）或留给用户后处理 |
| ❌ 云端文档协作（OneDrive/SharePoint 实时同步） | 走 ms365 MCP server，agent_go 不感知云端协议 |
| ❌ 产物版本管理（artifact 的 git 化） | 产物是"表达层"输出，不进代码仓库；如需版本化由用户侧 DMS 处理 |
| ❌ 产物的在线预览/渲染 | agent_go 是 CLI，预览交给用户系统的关联应用 |

---

## 五、验收标准

### 5.1 MCP 消费层（能力 A）

| # | 验收项 | 门禁 |
|---|--------|------|
| A1 | 配置 `mcp_servers` 后，子任务 LLM 能看到并调用外部工具 | `tools` 列表含 `mcp__excel__*` 命名空间工具 |
| A2 | 外部 server 启动失败时不阻断 pipeline | 降级 warning，任务正常完成 |
| A3 | 工具调用超时/异常返回结构化错误，LLM 可重试 | 错误格式与原生 ToolResult 一致 |
| A4 | 连接池在 pipeline 结束后无僵尸进程 | `ps` 无残留 uvx/npx 进程 |
| A5 | `tool_filter` 白名单生效 | 被过滤工具不出现在 LLM tools 列表 |

### 5.2 产物导出（能力 B）

| # | 验收项 | 门禁 |
|---|--------|------|
| B1 | 子任务写 `__artifacts__/report.md`，`--artifact-dir` 指定后文件出现在目标目录 | 文件存在且内容一致 |
| B2 | 不指定 `--artifact-dir` 时，产物留在 worktree（向后兼容） | 无导出行为，无报错 |
| B3 | 失败保留的 worktree 中的产物也能被收集 | `--preserve-worktrees` 场景下导出正常 |
| B4 | final report 列出导出清单 | 文件名、路径、大小可读 |

### 5.3 端到端场景

```bash
# 配置 excel + ppt MCP server，生成季度汇报
agent_go run ./repo "读取 sales.xlsx Q2 数据，生成季度汇报 PPT" \
  --yes --artifact-dir ~/reports --config office.json

# 预期：~/reports/{task_id}/{sub_id}/Q2_report.pptx 存在且内容正确
```

---

## 六、依赖与风险

### 6.1 新增依赖

| 依赖 | 类型 | 说明 |
|------|------|------|
| 无运行时依赖 | — | MCP client 用 stdlib 实现 JSON-RPC over stdio（与 mcp_server.py 一致，零外部依赖原则） |
| 用户侧可选 | 可选 | uvx（uv 工具）/ npx（Node）—— 用户按所选 MCP server 自行安装 |

### 6.2 风险

| 风险 | 对策 |
|------|------|
| 外部 MCP server 稳定性参差（如 GongRzhe 有已知 #39 issue） | 故障隔离 + 降级；文档推荐成熟 server（excel-mcp-server 4084★） |
| MCP server 进程泄漏 | finally 强制回收 + 超时 kill；CI 加进程计数断言 |
| 工具 schema 数量爆炸吃 token | `tool_filter` 白名单 + `scope` 限制可见性；prompt 压缩 |
| 产物大文件撑爆磁盘 | 导出时记录大小，超阈值（默认 100MB）警告 |
| openpyxl 公式不计算的陷阱 | 文档说明 + 引导 LLM 写值而非公式（见调研附件） |

---

## 七、后续演进（H2/H3）

| 阶段 | 能力 | 说明 |
|------|------|------|
| H2 | MCP server 健康度面板 | `agent_go status --mcp` 显示各 server 连接状态、工具数、调用统计 |
| H2 | 产物模板系统 | `__templates__/` 目录放企业 PPT/Excel 模板，子任务自动套用 |
| H3 | Skill-as-script | SKILL.md frontmatter 增加 `script:` 字段，技能可直接执行生成脚本（与 MCP 互补） |
| H3 | 产物血缘追踪 | artifact 关联到生成它的子任务/数据源，便于审计（呼应调研附件"消灭 PPT 当信息源反模式"） |

---

## 附：调研结论核查（2026-08-01）

本设计基于调研附件，关键事实已核查：

| 论点 | 核查 |
|------|------|
| M365 Copilot 声明式智能体 MCP 支持 GA（2026.4） | ✅ 属实 |
| 社区 Office MCP 生态成熟（excel 4084★, PPT 1847★） | ✅ 属实 |
| 国内 AI PPT"一超多强"，AiPPT/讯飞有开放 API | ✅ 属实 |
| "CLI + MCP 双模工具层"是业界共识范式 | ✅ 属实（sbroenne 项目验证） |
| 代码 Agent 通过 MCP 生态向办公扩展是趋势 | ✅ 属实（通过 MCP，非原生内建） |

**结论**：agent_go 不需要自建 Office 能力，但必须补齐 MCP 消费 + 产物导出两个架构能力，才能成为合格的跨层编排者。
