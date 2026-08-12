# llama-defender 对外操作与 API 手册

> 面向外部编排器（如 `agent_go`）和运维人员的操作指南与 API 参考。
> 本文档与 `CLAUDE.md`、`AGENTS.md` 互补：CLAUDE.md 面向 Claude Code 使用，AGENTS.md 面向 AI 编程助手；本文档面向程序化集成与日常运维。

---

## 1. 项目简介

llama-defender 是运行在 macOS（Apple Silicon）上的本地 LLM 推理编排层。它把下游的 `llama-server` / `rapid-mlx` / 云端 OpenAI 兼容 API 包装成一个 Anthropic 兼容的代理服务，供 Claude Code 等客户端使用。

运行模式：

```
Local:  Client → anthropic_proxy.py:4000 → llama-server/rapid-mlx :8081 → GGUF/MLX 模型
Cloud:  Client → anthropic_proxy.py:4000 → DeepSeek / OpenAI API → cloud 模型
```

核心约定：外部客户端永远只连接 `http://127.0.0.1:4000`。后端切换、模型切换、本地/云端切换全部由 `manage.sh` 与代理层完成。

---

## 2. 快速启动

```bash
# 启动本地后端 + 代理（按 configs/active.conf）
./manage.sh start

# 仅启动云端代理
./manage.sh start-cloud

# 查看状态
./manage.sh status

# 热重载配置（不重启代理进程）
./manage.sh reload

# 切换配置
./manage.sh switch <profile-name> && ./manage.sh reload

# 停止
./manage.sh stop
```

常用命令一览：

| 命令 | 说明 |
|------|------|
| `start` | 启动本地后端 + 代理 |
| `start-cloud` | 仅启动云端代理 |
| `start-backend` | 单独启动本地后端 |
| `stop` | 停止代理 + 后端 + watchdog |
| `stop-backend` | 单独停止本地后端，释放 GPU 内存 |
| `restart` | 停止后重新启动 |
| `reload` | SIGHUP 热重载代理配置 |
| `switch <name>` | 切换 `configs/active.conf` 软链 |
| `current` | 显示当前配置详情 |
| `list` | 列出所有可用配置 |
| `status` | 文本状态（人类可读） |
| `watchdog [--daemon]` | 启动 watchdog |
| `watchdog-status` | 输出 watchdog 结构化 JSON |
| `logs [N]` | 查看后端日志 |
| `proxy-logs [N]` | 查看代理日志 |

---

## 3. 配置文件

配置放在 `configs/*.conf`，是 Bash 可 source 的 `KEY="value"` 文件。

| 文件 | 说明 |
|------|------|
| `configs/active.conf` | 指向当前激活配置的符号链接 |
| `configs/<name>.conf` | 具体配置，需包含 `CONFIG_NAME`、`CONFIG_DESC`、`CONFIG_MEMORY` |
| `configs/secret.local.conf` | **git-ignored**，存放 API Key 等敏感信息 |

切换配置：

```bash
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload
```

---

## 4. HTTP API 参考

所有端点监听 `127.0.0.1:4000`，无鉴权（仅限本机回环）。

### 4.1 模型与消息

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/v1/models` | 返回模型别名列表 + 路由 metadata |
| `POST` | `/v1/messages` | Anthropic Messages API（流式 + 非流式） |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions（内部转换后走同一管线） |
| `OPTIONS` | `*` | CORS 预检 |

### 4.2 结构化状态 API（agent_go 集成）

#### `GET /api/status`

返回结构化健康/就绪状态。`state` 为 `healthy` 或 `starting` 时返回 `200`；其他状态返回 `503`，body 仍为 JSON。

```bash
curl -s http://127.0.0.1:4000/api/status | python3 -m json.tool
```

响应示例：

```json
{
  "api_version": "1",
  "proxy": {
    "pid": 12345,
    "uptime_sec": 3600,
    "alive": true
  },
  "backend": {
    "pid": 23456,
    "uptime_sec": 3500,
    "alive": true,
    "model_name": "Qwen3.6-35B-A3B-UD-MLX-4bit",
    "backend_type": "rapid-mlx",
    "base_url": "http://127.0.0.1:8081/v1"
  },
  "active_profile": "rapid-mlx-35b-opt",
  "state": "healthy",
  "ready": true
}
```

`state` 枚举：

- `healthy`：proxy 与 backend 均正常，模型已加载
- `starting`：backend 进程存在，但模型尚未加载完成（`ready=false`）
- `backend_down`：backend 未运行
- `proxy_down`：proxy 未运行
- `model_drift`：backend 实际加载的模型与 `active_profile` 期望的模型不符
- `down`：完全不可用

#### `GET /api/watchdog`

返回 watchdog 结构化状态。

```bash
curl -s http://127.0.0.1:4000/api/watchdog | python3 -m json.tool
```

响应示例：

```json
{
  "enabled": true,
  "running": true,
  "pid": 34567,
  "last_restart_at": "2026-08-12T05:20:00Z",
  "restart_count_1h": 2,
  "last_failure_reason": "backend_unresponsive"
}
```

等价命令：

```bash
./manage.sh watchdog-status
```

#### `GET /api/profiles`

返回所有可用配置及当前激活配置。

```bash
curl -s http://127.0.0.1:4000/api/profiles | python3 -m json.tool
```

响应示例：

```json
{
  "profiles": [
    {
      "name": "deepseek-chat",
      "desc": "DeepSeek Chat API via 代理中转",
      "memory_gb": null,
      "active": false
    },
    {
      "name": "rapid-mlx-35b-opt",
      "desc": "35B MoE Unsloth UD-MLX-4bit ...",
      "memory_gb": 18.0,
      "active": true
    }
  ]
}
```

`memory_gb` 对无法解析的云端配置返回 `null`。

### 4.3 可观测性端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/status` | HTML 状态页（人类可读） |
| `GET` | `/metrics[?n=N]` | 最近 N 条请求的结构化指标（JSON） |
| `GET` | `/metrics/history` | 历史指标 |
| `GET` | `/session?sid=<session_id>` | 单会话分析（JSON 或 HTML） |

---

## 5. 管理命令详细说明

### 5.1 变更类命令互斥锁

`start` / `stop` / `restart` / `start-backend` / `stop-backend` / `switch` 等变更命令会获取仓库级文件锁 `.manage.lock`，防止与 watchdog 自动恢复并发。若锁被占用，命令会快速失败并提示持有者。

### 5.2 Watchdog

watchdog 监控后端健康，在性能衰减或后端无响应时自动重启。

```bash
# 前台运行
./manage.sh watchdog

# 后台守护进程
./manage.sh watchdog --daemon

# 查看状态
./manage.sh watchdog-status

# 停止
./manage.sh stop   # stop 会自动停止 watchdog
```

watchdog 状态持久化到 `logs/watchdog_state.json`。

### 5.3 生命周期事件

以下事件会追加到 `logs/lifecycle_events.jsonl`（每行 `{ts, event, detail}`）：

| 事件 | 触发时机 |
|------|----------|
| `service_start` | `start` / `start-cloud` / `start-backend` 成功 |
| `service_stop` | `stop` / `stop-backend` |
| `service_restart` | `restart` |
| `config_reload` | `reload` 成功发送 SIGHUP |
| `profile_switch` | `switch <name>` |
| `watchdog_auto_restart` | watchdog 自动重启后端 |

示例：

```json
{"ts": "2026-08-12T05:21:52Z", "event": "profile_switch", "detail": "from=rapid-mlx-35b-opt to=deepseek-chat"}
```

---

## 6. 本地 ↔ 云端热切换

```bash
# 本地 → 云端
./manage.sh switch deepseek-chat && ./manage.sh reload
./manage.sh stop-backend   # 释放本地模型内存

# 云端 → 本地
./manage.sh switch rapid-mlx-35b-opt && ./manage.sh reload
./manage.sh start-backend
```

---

## 7. 关键文件路径

| 文件/目录 | 说明 |
|-----------|------|
| `configs/active.conf` | 当前 profile 软链 |
| `logs/anthropic_proxy.log` | 代理日志 |
| `logs/llama-server.log` | 本地后端日志 |
| `logs/proxy_metrics.jsonl` | 结构化指标 |
| `logs/proxy_requests.jsonl` | 请求记录 |
| `logs/watchdog_state.json` | watchdog 持久化状态 |
| `logs/lifecycle_events.jsonl` | 生命周期事件流 |
| `logs/snapshots/` | 失败请求快照（git-ignored） |

---

## 8. 故障排查速查

| 现象 | 排查 |
|------|------|
| 代理无响应 | `curl -s http://127.0.0.1:4000/api/status` |
| 模型加载中 | `/api/status` 返回 `starting` + `ready=false`，等待 |
| backend 未运行 | `./manage.sh start-backend` 或 `./manage.sh restart` |
| 怀疑模型漂移 | 检查 `/api/status` 的 `state` 是否为 `model_drift` |
| watchdog 频繁重启 | 查看 `logs/watchdog_state.json` 与 `logs/llama-server.log` |
| 需要释放 GPU 内存 | `./manage.sh stop-backend` |

---

## 9. 兼容性说明

- 所有结构化 API 字段名为 `snake_case`，稳定不更名；更名属于 breaking change。
- 端点不存在或字段缺失时，调用方应 fail-open（回退到 `/v1/models` 或 HTML 解析），不阻断任务。
- `api_version` 固定为 `"1"`，便于后续演进。
