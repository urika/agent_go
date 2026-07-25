# M1 通知通道配置化（Webhook）设计 Spec

> 版本: v1.0
> 状态: Draft
> 关联: PRD P0 缺失 M1（任务完成通知）、roadmap.md「下一批」
> 设计目标: 将现有 `_notify_complete`（macOS 桌面通知 + 自定义命令）升级为可配置的多通道通知体系，首发支持 Webhook，让「关电脑走人」后能在手机/IM 上收到任务结果。

---

## 1. 现状与问题

### 1.1 现状

`pipeline.py:_notify_complete()` 在管线结束时触发，现有两个通道：

| 通道 | 实现 | 局限 |
|------|------|------|
| macOS 桌面通知 | `osascript -e 'display notification ...'` | 只在本地 macOS 有效；关了终端/锁屏后价值有限；Linux 无通道 |
| 自定义命令 | `behavior.notify_command` 字符串 `.format()` 后 `shlex.split` 执行 | 单通道、无结构化 payload、无重试、模板变量只有 4 个标量 |

开关：`behavior.notify_on_complete`（默认 true）。

### 1.2 问题

1. **无 Webhook 通道** — 无法接入 Slack / 钉钉 / 企业微信 / ntfy 等 IM，而这些才是「人不在电脑前」的触达方式
2. **payload 过于单薄** — 只有 task_id/status/completed/total 四个标量，没有失败原因、耗时、成本、保留 worktree 等决策所需信息（与 M2 失败摘要、S1 计量数据未打通）
3. **无事件细分** — 只有「管线结束」一个时机，成功/失败/部分阻断不区分，用户无法配置「仅失败时通知」
4. **可靠性无保障** — 无超时统一控制、无重试、失败静默（仅 debug 日志）

## 2. 设计目标

1. **通道可配置**：config 声明式配置多通道，通道间互相独立、失败互不影响
2. **首发 Webhook 通道**：通用 JSON POST + 主流 IM 适配模板（Slack / 钉钉 / 企业微信 / ntfy）
3. **结构化 payload**：聚合 M2 失败摘要与 S1 计量数据，通知即可决策「要不要回来看」
4. **事件可订阅**：`on_complete` / `on_failed` / `on_blocked` 按通道订阅
5. **故障隔离**：通知任何失败不得影响管线与退出码，全部留痕到 execution.log

非目标（本期不做）：邮件通道、通知回执交互（如 Slack 按钮触发 merge）、通知队列持久化。

## 3. 配置设计

`~/.agent_go/config.json` 新增 `notify` 块（与现有 `behavior.notify_*` 并存，旧键保留兼容）：

```json
{
  "notify": {
    "enabled": true,
    "timeout_sec": 5,
    "retry": 1,
    "channels": [
      {
        "type": "desktop",
        "events": ["on_complete", "on_failed"]
      },
      {
        "type": "webhook",
        "name": "team-slack",
        "url": "${AGENT_GO_WEBHOOK_URL}",
        "format": "slack",
        "events": ["on_failed", "on_blocked"],
        "headers": {
          "Authorization": "Bearer ${AGENT_GO_WEBHOOK_TOKEN}"
        }
      },
      {
        "type": "command",
        "command": "curl -X POST https://ntfy.sh/my-topic -d '{message}'",
        "events": ["on_complete", "on_failed", "on_blocked"]
      }
    ]
  }
}
```

### 字段约定

| 字段 | 说明 |
|------|------|
| `enabled` | 总开关，默认 true；false 时全部通道静默 |
| `timeout_sec` | 单通道单次请求超时，默认 5s |
| `retry` | 失败重试次数（仅网络/5xx 重试，4xx 不重试），默认 1 |
| `channels[].type` | `desktop` / `webhook` / `command` |
| `channels[].events` | 订阅事件子集，缺省 = 全部三个事件 |
| `channels[].format` | webhook 适配器：`generic`（默认）/ `slack` / `dingtalk` / `wecom` / `ntfy` |
| `${VAR}` | url/headers/command 中的环境变量插值，**secret 只允许经环境变量注入，不写进 config 明文** |

### 兼容策略

- `behavior.notify_on_complete=false` 视同 `notify.enabled=false`（旧配置继续生效）
- `behavior.notify_command` 非空且未配置 `notify.channels` 时，自动等价为一个 `command` 通道
- 两者并存时以 `notify` 块为准，`behavior.notify_*` 忽略（打 warning 提示迁移）

## 4. 事件与 Payload

### 事件定义

| 事件 | 触发时机 |
|------|---------|
| `on_complete` | 管线结束且全部子任务 completed/no_changes |
| `on_failed` | 管线结束且存在 failed 子任务 |
| `on_blocked` | 管线结束且存在 blocked 子任务（验证循环阻断） |

一次管线结束只派发一个事件（优先级：on_blocked > on_failed > on_complete），避免轰炸；多状态并存时 payload 内携带完整统计。

### 通用 payload（format=generic 的 POST body，也是各适配器的数据源）

```json
{
  "event": "on_failed",
  "task_id": "task-20260725-0301",
  "task": "重构认证模块",
  "repo": "/Users/x/proj",
  "status": "failed",
  "subtasks": {"total": 4, "completed": 2, "failed": 1, "blocked": 1},
  "duration_sec": 612,
  "failures": [
    {"subtask_id": "sub-3", "title": "迁移 OAuth2", "failure_reason": "pytest tests/test_auth.py exit=1: ..."}
  ],
  "preserved_worktrees": [
    {"subtask_id": "sub-3", "path": "~/.agent_go/task-xxx/sub-3/work", "branch": "agent_go/task-xxx/sub-3"}
  ],
  "cost": {"total_usd": 0.083, "by_role": {"planner": 0.011, "worker": 0.072}},
  "task_dir": "~/.agent_go/task-20260725-0301",
  "ts": "2026-07-25T03:15:42"
}
```

字段来源：`failures` 复用 M2 `failure_reason`；`cost` 复用 S1 `metrics.aggregate_metering(task_dir/metering.jsonl)`；`preserved_worktrees` 复用 `.preserved` 标记数据。**全部为已有数据源，零新增采集。**

### IM 适配器映射

| format | body 模板 |
|--------|----------|
| `slack` | `{"text": "...", "blocks": [...]}`，失败原因入 `blocks`，字段取通用 payload |
| `dingtalk` | `{"msgtype": "markdown", "markdown": {"title": ..., "text": ...}}` |
| `wecom` | 同钉钉 markdown 结构（企业微信 webhook 兼容） |
| `ntfy` | 纯文本 body + `X-Title` header；url 即 topic |
| `generic` | 通用 payload 原样 JSON POST |

适配器只做「字段映射 + 文本渲染」，各不超过 30 行；渲染文案中英跟随现有 console 风格（中文）。

## 5. 实现方案

新增 `agent_go/notify.py`（约 150 行，纯 stdlib），`pipeline.py` 只保留一行调用：

```
notify_event(event, context, config)        → 唯一入口，遍历订阅该事件的通道
  ├── _render_payload(event, context)       → 组装通用 payload（聚合 metering/.preserved/failure_reason）
  ├── _send_desktop(payload, cfg)           → 迁移现有 osascript 逻辑
  ├── _send_webhook(payload, channel, cfg)  → urllib POST，format 适配器渲染 body
  │     ├── _interpolate(str)               → ${VAR} 环境变量插值（未设置的变量 → 跳过该通道 + warning）
  │     └── 超时/重试/4xx 不重试
  └── _send_command(payload, channel)       → 迁移现有 notify_command；模板变量只暴露系统生成的
                                              安全标量（task_id/status/counts/cost/message），
                                              不含 failure_reason（LLM 输出属不可信输入，防注入）
```

`pipeline.py` 改动：

```
_notify_complete(...)  →  删除，替换为：
event = "on_blocked" if has_blocked else "on_failed" if has_failed else "on_complete"
notify_event(event, {"meta": meta, "results_map": results_map, "task_dir": task_dir, ...}, config)
```

### 调用时序约束

`notify_event` 在 worktree 清理 **之后** 调用（现状即如此），这样 `preserved_worktrees` 列表是最终态；但需在 metering.jsonl 写盘完成之后——两者在现有管线顺序中天然满足。

## 6. 安全约束（铁律）

1. **secret 不出现在 config 明文**：url/headers 中的密钥必须 `${ENV_VAR}` 插值；插值失败（环境变量不存在）跳过该通道并 warning，不以明文兜底
2. **payload 不含任何密钥**：渲染前过滤 env 插值结果不回写入 payload；payload 字段为白名单制（第 4 节列出的字段，不接受 config 扩展字段）
3. **webhook URL 必须 https**（localhost/127.0.0.1 例外，用于自建 ntfy 等内网服务）
4. **command 通道命令经 4 级白名单同款 shlex 解析 + 注入扫描**，模板变量值先 `_slugify` 风格转义防注入（failure_reason 含 LLM 输出，属不可信输入）
5. **通知失败不阻断**：任何通道异常 catch 后 `log_event("notify_error")`，管线退出码不受影响

## 7. 落地路径

```
Step 1（1 天）: notify.py 骨架 + generic webhook + desktop/command 迁移
  → config 解析 + ${VAR} 插值 + 事件订阅过滤
  → 单元测试：payload 组装、事件过滤、插值失败跳过、通知异常不阻断

Step 2（0.5 天）: IM 适配器（slack/dingtalk/wecom/ntfy）
  → 各适配器渲染函数 + 单测（mock urllib）

Step 3（0.5 天）: 兼容层 + 文档
  → behavior.notify_* 旧键映射 + 迁移 warning
  → config.example.json 增加 notify 块、README/docs 同步
```

预估 2 天。验收口径：配置钉钉 webhook，跑一个含失败子任务的任务，`--yes` 无头结束后手机收到含失败原因与成本的通知；通知通道全部配错时任务照常完成、退出码不变。

## 8. 风险与开放问题

| 风险 | 缓解 |
|------|------|
| webhook 端点慢/挂导致管线尾延迟 | timeout 5s × (1+retry) 上限 15s；各通道串行但总耗时可忽略 |
| failure_reason 过长撑爆 IM 消息上限 | 每条 failure_reason 截断 500 字符，payload 标注 `truncated: true` |
| 适配器与 IM 实际 API 漂移 | 适配器函数纯函数化，单测锁定渲染结构；generic 兜底永远可用 |
| 开放：是否需要 on_start 事件 | 本期不做；「周五派发」场景只关心结果。若用户提出，事件机制已预留扩展位 |
