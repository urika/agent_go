# Greywall 沙箱集成与运维参考

> 2026-08-25 实测：greywall 0.3.7 + greyproxy 0.4.5 @ macOS（Homebrew cask 安装，`greywall check` 全过）。

## 是什么

[Greywall](https://github.com/GreyhavenHQ/greywall)：面向 AI 编程 agent 的**无容器沙箱**（Go，Apache-2.0，Fence/Tusk AI 的 fork，灵感来自 Anthropic sandbox-runtime）。三件套：

| 组件 | 作用 |
|------|------|
| `greywall` | deny-by-default 沙箱：文件系统只放行工作目录，网络默认全断，危险命令（`rm -rf /`、`git push --force` 等）拦截 |
| `greyproxy` | 透明代理（SOCKS5 `:43052` / DNS `:43053`），所有网络流量经它转发；**域名 allow/deny 全部由它管理**，仪表盘 http://localhost:43080 |
| `greywatch` | 观测模式（= `greywall --watch`）：全放行但全记录，用于「先看清 agent 干什么，再决定锁什么」 |

macOS 用 sandbox-exec (Seatbelt) 实现；无 Linux 的 TUN 透明捕获/DNS 捕获（网络管控靠代理环境变量）。内置 claude/codex/cursor 等 agent profile（自动 deny `~/.ssh/id_*`、`~/.gnupg/**`、`.env`、`.bashrc` 等）。

成熟度备注：0.x 阶段、社区较小（2026-08 约 287 stars），方向正确但属可选增强，不作为硬依赖。

## 落地策略（2026-08-25 拍板）

**先观察记录、后限制**——把对应用的影响降到最小，分三步走：

1. **观察期（已实现，当前状态）**：交互式子任务路径包装为 `greywall --watch -- claude`（全放行全记录，`sandbox_type=greywatch`），在仪表盘积累「agent 实际访问了哪些文件/域名」的真实数据，不做任何拦截。实现点：`agents.py get_claude_command` 交互分支 + `executor.py` 兜底包装；eval 可靠性报告的沙箱覆盖率（`greywall_pct`）统计 greywall+greywatch 两态。
2. **再收紧**：基于观察期数据配好 profile + greyproxy 域名放行规则后，去掉两处 `--watch` 切回 deny-by-default。避免一上来默认全断把正常执行卡死。
3. **headless 维持裸跑（现状即终态，非权宜）**：`claude -p` 无头模式的设计意图就是**零交互成本、静默无人值守执行**——这与 greywall 的交互式 profile 确认提示、首次网络拦截需人工放行天然冲突。因此 headless 路径保持原生裸跑，不纳入沙箱。
   - 若未来确有 headless 沙箱需求，前提条件：profile 预学习（`--learning` 提前生成）+ 网络规则预放行 + 无任何交互确认——即必须先解决「零交互」再谈接入，且同样先 greywatch 观察期后收紧。

## agent_go 集成现状（关键事实）

- **只包装非 headless 路径**：`agents.py get_claude_command` 的 headless 分支（`claude -p`，bench / `--yes` 无人值守的主路径）直接返回裸 `claude`，**不经 greywall**；只有交互式子任务路径才 `greywall -- claude <worktree>`（`executor.py:874` 单点包装，注释明确禁止重复包装）。
- **探测与降级**：`shutil.which("greywall")` 存在则包装，缺失降级原生 claude（`sandbox_type=native`），`FileNotFoundError` 再兜底一次。装/卸 greywall 对 agent_go 零配置。
- **观测**：`sandbox_type` 记录进结果，`eval` 报告统计 `greywall_pct`；任务启动时 `utils.py _detect_tool_versions` 探测版本写 DEBUG 日志。
- **影响面**：bench 与 headless 任务口径不受 greywall 装否影响；收益仅在交互式路径（文件系统默认拒绝 + 危险命令拦截）。

## 安装（macOS）

```bash
brew tap greyhavenhq/tap
brew install --cask greyhavenhq/tap/greywall   # 自动装依赖 greyproxy + terminal-notifier
```

**Homebrew 6.0 信任机制坑**：第三方 tap 的 cask 默认被拒，需先最小授权信任（不要信任整个 tap）：

```bash
brew trust --cask greyhavenhq/tap/greywall
brew trust --cask greyhavenhq/tap/greyproxy   # 依赖，单独信任
```

验证：

```bash
greywall check    # 应输出：✓ sandbox-exec (Seatbelt) ✓ greyproxy running，All checks passed
greywall -- echo sandbox-ok
```

greyproxy 以 launchd user agent 自启，仪表盘 http://localhost:43080。

## 网络与 MCP 放行 playbook

greywall 默认网络全断；域名规则在 **greyproxy 层**管理（greywall.json 的 `network` 段只管代理地址/本地回环，不管域名）。

1. **仪表盘放行（最常用）**：MCP/API 请求首次被拦 → 仪表盘出现该请求 → allow 生成持久规则，之后所有沙箱会话自动放行。
2. **先观察再收紧（新 MCP 推荐）**：`greywatch -- claude` 跑一遍，仪表盘列出实际访问的全部域名 → 照单配 allow → 切回 `greywall -- claude`。
3. **API key 凭证注入**：远程 MCP 的 key 不放进沙箱——greyproxy 在 HTTP 层替换真实值，沙箱内只有占位符。配置 `~/Library/Application Support/greywall/greywall.json`：
   ```json
   { "credentials": { "inject": ["MCP_API_KEY"] } }
   ```
4. **本地 MCP 不受影响**：stdio 型 MCP 无网络；localhost 服务型 MCP 由 `network.allowLocalOutbound`（macOS）控制。

**agent_go 注意点**：交互式子任务里 claude 需访问模型代理（如 `localhost:4000`）和云端 API，首次被拦时到仪表盘放行对应域名（规则持久）。若未来把 headless 路径也纳入沙箱，必须**预先**放行模型代理与 MCP 域名，否则 worker 无声断网。

## 常用命令速查

```bash
greywall -- claude                 # 沙箱运行（首次弹 profile 确认，选 Y 后不再问）
greywatch -- claude                # 观测模式：全放行全记录
greywall --learning -- <cmd>       # 学习模式：追踪文件访问自动生成最小权限 profile
greywall profiles list/show <name> # 查看内置/已学习 profile
greywall check                     # 依赖与安全特性自检
```
