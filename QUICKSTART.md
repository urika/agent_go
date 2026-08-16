# QUICKSTART：5 分钟上手 agent_go

> 版本：2.0.0 ｜ 适用于：Python ≥3.9，macOS/Linux（Windows 未验证）

## 1. 安装

```bash
pip install -e .        # 开发模式（从仓库）
# 或正式安装
pip install .
agent_go --help         # 验证安装（应列出 run/report/models/web 等子命令）
```

## 2. 配置 API 模型

agent_go 默认调用 LLM 生成计划/执行/评估。两种方式：

```bash
# 方式 A：环境变量（推荐）
export AGENT_GO_API_KEY="sk-..."
# 方式 B：config.json（~/.agent_go/config.json 自动创建）
agent_go config          # 查看当前生效配置
```

**模型池**（已内置 4 个模型注册，见 `~/.agent_go/models.json`）：

| 模型 ID | 类型 | 适用角色 |
|---------|------|---------|
| `glm-5.3` | Anthropic 兼容（智谱） | planner/evaluator（推荐） |
| `kimi-k3` | Anthropic 兼容（Kimi） | planner（coding 拆解强） |
| `deepseek-v4-pro` | OpenAI 兼容（DeepSeek） | worker/兜底 |
| `local-mlx` | 本地（localhost:4000 代理） | 离线/低成本 |

```bash
agent_go models list     # 查看模型池
agent_go models add <id> --provider anthropic --base-url https://... --key-ref env:XXX_KEY
```

角色绑定（router.roles，config.json）：

```json
{
  "router": {
    "enabled": true,
    "roles": {
      "planner":   {"model": "kimi-k3"},
      "evaluator": {"model": "glm-5.3"},
      "worker":    {"hard": {"model": "deepseek-v4-pro"}}
    }
  }
}
```

## 3. 跑第一个任务

```bash
# 最简单（交互式确认 Plan）
agent_go run /path/to/repo '实现用户登录接口'

# 全自动 + 并发
agent_go run /path/to/repo '实现用户登录接口' --yes --parallel 3

# hard 任务端到端（不拆分子任务，保留全局上下文，通过率更高）
agent_go run /path/to/repo '重构存储层并发安全' --yes --e2e

# 产出报告
agent_go report <task-id> --format html    # 分享给团队
```

任务隔离在 git worktree，失败自动重试，完成后可 `agent_go merge <task-id>` 交付。

## 4. Web 操作台（观测 + 处置 + 看板）

```bash
agent_go web --host 127.0.0.1 --port 8091
# 多角色：admin 全权 / viewer 只读
agent_go web --admin-token <secret> --viewer-token <secret>
```

打开 http://127.0.0.1:8091：

- 📋 任务：启动/观测/恢复/取消/清理/审批/merge/PR（全部浏览器完成）
- ⚙️ 配置：云端⇄本地一键切换、健康检查、代理路由策略、配置编辑
- 🗂 看板：5 阶段卡片管理（brainstorm→operations）

## 5. 纯本地模式（离线/内网）

```bash
agent_go config local    # 一键生成本地 profile（探测 localhost:4000 代理）
agent_go config status   # 查看模式 + 各端点健康
agent_go run /path/to/repo '任务' --yes
agent_go config cloud    # 恢复云端
```

要求：本机运行 OpenAI 兼容代理（如 llama.cpp + anthropic_proxy.py，见
`docs/in/api-and-operations-guide.md`）。

## 6. 常用命令速查

```bash
agent_go list                          # 任务列表
agent_go show <task-id>                # 任务详情
agent_go resume <task-id>              # 恢复中断任务
agent_go review --task <task-id>       # 审查 + 审批
agent_go merge <task-id> --push        # 合并交付分支
agent_go inspect <task-id>             # 查看保留的失败 worktree
agent_go models list/add               # 模型池管理
agent_go eval bench --suite golden     # 模型评估
```

## 7. 更多文档

| 文档 | 内容 |
|------|------|
| `docs/README.md` | 文档总索引 |
| `docs/design/model-selection-report.md` | 模型选型报告（通过率/成本/延迟） |
| `docs/design/production-model-config.md` | 生产最优配置（方案 B） |
| `docs/design/model-entity-config-design.md` | 模型三层架构设计 |
| `docs/design/web-console-full-ops-design.md` | Web 操作台设计 |
| `docs/design/runbook.md` | 运维与故障排查 |
