# task_id ↔ session join 断链问题报告（移交调试）

> 日期: 2026-08-30
> 状态: **未解决，待判别性实验定案**（报告含全部证据链与复现路径）
> 影响面: agent_go worker 会话无法 join 到 llama-defender 数据面（台账/档案/H_BE 探针），真实任务成败标签链路断；swe-eval 与 python（planner）通道不受影响。
> 关联文档: `offline-policy-metrics-analysis.md` §5.2 数据面缺口、§5.3 标签通道 B 的前提。

## 1. 应有的链路

```
agent_go executor.run_subtask（executor.py:2736-2745，C1 注入，if _is_local_url 分支内）
  → env[ANTHROPIC_CUSTOM_HEADERS] = "X-Claude-Code-Session-Id: <md5(task:sub)[:8]>-ag-<task>-<sub>"
  → subtask.py:361 subprocess.Popen(claude -p, env=env)
  → claude CLI 发请求时带上该头
  → 代理 anthropic_proxy.py:474/632 读取，取前 8 字符作为台账/档案/探针 join 键
  → agent_go diag.session_key8() 用同一公式反查
```

## 2. 已证实的事实（每条附数据）

- **F1｜注入代码在真实任务中确实执行了。** `~/.agent_go/task-20260828-125916-049-5915/metering.jsonl` 记录 worker `session_key: "1c2b7cc4-ag-task-20260828-125916-049-5915-sub-1"`（AGENT_GO_SESSION_KEY 透传成功，证明 `if _is_local_url:` 分支进入且注入运行）。
- **F2｜key 公式正确。** `md5("task-20260828-125916-049-5915:sub-1")[:8] = 1c2b7cc4`，与 `diag.session_key()` 现行输出逐字节一致。`diag.py` 自 08-19 创建（commit 7cdc1ba）后零修改，无公式漂移。
- **F3｜claude CLI 2.1.251 支持 ANTHROPIC_CUSTOM_HEADERS。** 干净环境（`env -i HOME=/tmp/fakehome` + PATH + ANTHROPIC_BASE_URL=:4000 + ANTHROPIC_AUTH_TOKEN）实测：代理日志完整收到 `'X-Claude-Code-Session-Id': 'jointest03'`，sess 归并 `jointest`。
- **F4｜代理两个协议端点都读该头。** OpenAI 模式冒烟（/v1/chat/completions + 该头）产生 `sess=hbesmoke` 台账与探针记录；jointest03 走 /v1/messages 同样到达。
- **F5｜但 worker 的 key 从未出现在数据面。** 全量扫描：**4254 个 (task, sub) 组合**（全部 `~/.agent_go/task-*/meta.json`）× `logs/diag/ledger/*.jsonl`（687 个文件）与 `sessions.jsonl`（全历史）= **0 命中**；另测 9 种 key 公式变体（含无冒号、无 sub、ag- 前缀、retry 后缀等）0 命中。
- **F6｜python 通道（planner）工作正常。** 同一任务 planner key `fc5bf27a` 在 sessions.jsonl 有记录（2026-08-28 13:00-13:01，route=cloud/deepseek-v4-pro）。断点特异落在 **claude CLI 通道**。
- **F7｜worker 确实到了本地后端。** 同一任务 worker metering：`actual_model=pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp`。请求到了 :4000 并被本地 35B 服务——**请求到了，头没到（或头到了但不是注入值）**。
- **F8｜同时段代理出现「一次性」8-hex key。** 08-28 12:50-13:01 每分钟一个新 key（`95841fd6`/`9edc027a`/`b1ba027d`/`065bf0c9`…），key_source=header、Ornith 本地——形态即各 agent_go worker 会话，但 key 不是注入值。**疑似 claude CLI 自带 session id 与注入头同名冲突且自带值被代理读到**（与 F3 不矛盾：fakehome 下 CLI 可能不发自己的 id）。
- **F9｜环境陷阱（独立发现，干扰一切手工复现）。** `~/.claude/settings.json` 的 `env` 块把 `ANTHROPIC_BASE_URL` 钉死 `open.bigmodel.cn` 并带 token，**settings env 优先级高于 shell 导出**。本报告前两次手工冒烟因此实际打到智谱而非 :4000。手工复现必须 fakehome 或显式覆盖。

## 3. 已排除的候选

- key 公式漂移（F2）；diag.py 零修改（git log）
- claude CLI 不支持 custom headers（F3）
- 代理不读头 / 端点不覆盖（F4）
- 台账 FIFO 驱逐假阴性（sessions.jsonl 与台账两套存储，全历史均无命中）
- worker 未到代理（F7）

## 4. 当前最可能的断点（按可能性排序）

1. **claude CLI 在真实 HOME 下发送自己的 X-Claude-Code-Session-Id，与 ANTHROPIC_CUSTOM_HEADERS 同名冲突时自带值优先被代理读到**（解释 F5/F7/F8 全部）。
2. agent_go worker env 组合（全量 os.environ + 注入）中某变量抑制 CUSTOM_HEADERS 生效。
3. worker 请求模式（streaming/重试）走了 diag key 提取的另一分支。

## 5. 判别性下一步（一个实验定案，零模型成本）

```bash
# 1. 本地起 HTTP echo server（~30 行 python）：打印收到的 headers 后返回 200
# 2. ANTHROPIC_BASE_URL 指向它；env 按 agent_go worker 同款构造：
#    全量 os.environ + ANTHROPIC_CUSTOM_HEADERS（照 executor.py:2741 拼法）
# 3. claude -p "ok" --model claude-haiku-4-5
# 4. 看 echo 中 X-Claude-Code-Session-Id 出现次数与值：
#    - 一次且为注入值 → 假设 1 不成立，转查代理取头逻辑
#    - 两次，或值为 CLI 自生成 uuid → 假设 1 成立，按 §6 修
```

## 6. 修复方向预判（定案后执行）

若假设 1 成立：agent_go 改注入 `X-Agent-Go-Session-Id`（避开 CLI 同名头）；代理 `raw_sid` 读取顺序改为 私有头 → X-Claude-Code-Session-Id → fallback。两侧各几行，向后兼容（旧头继续作 fallback 源）。

## 7. 数据资产（复现路径）

- 扫描口径：4254 组合 × `diag.session_key8` ∩ `logs/diag/ledger/*.jsonl`、`logs/diag/sessions.jsonl`
- 对照案例：`task-20260828-125916-049-5915`（metering 含 worker key `1c2b7cc4` / planner key `fc5bf27a`）
- 实证日志：`~/APP/llama.cpp/logs/anthropic_proxy.log` 搜 `jointest03`（到达样本）与 `1c2b7cc4`（无命中）
- 环境陷阱：`~/.claude/settings.json` env 块（bigmodel.cn）

## 8. 关联工作（已完成，同日）

- `/api/session/<key>/hbe` 端点（llama-defender，R17 风格）+ agent_go `diag.get_session_hbe()` 消费函数已上线并端到端验证（s38d79db 返回 10 条双轴记录；未知 key → None；长 key 归并正常）。两侧单测全绿（llama-defender 1284、agent_go 2779+25）。**join 修复后该消费面即可对 agent_go 任务产出双轴数据。**

## 9. 外部会话补充验证（2026-08-30 下午，llama-defender 侧；附录于原报告之后）

原 §5 判别实验已由外部会话执行完毕，并追加三项静态考古。**结论：假设 1（CLI 通用覆盖）与假设 3（代理旁路）均被排除；F8 的一次性 8-hex 键来源已定形；假设排序修正如下。**

### 9.1 判别实验结果（echo server 实测，claude CLI 2.1.251）

| # | 条件 | 捕获结果 | 推论 |
|---|---|---|---|
| T1 | fakehome + `ANTHROPIC_CUSTOM_HEADERS` 注入 | 头**恰一次**、值为注入值（`1c2b7cc4-ag-echo-test-sub-1`），无 CLI 自发头 | CLI 核心不覆盖自定义头 → **假设 1（通用形态）不成立** |
| T2 | fakehome、**无**注入变量 | CLI **自发** `X-Claude-Code-Session-Id: 4af0db82-46bc-4e47-a07c-155f991f92a4`（UUID） | 代理截前 8 位 = `4af0db82`——**与 F8 一次性 8-hex 键形态完全匹配。F8 = env 缺失注入变量的 claude 调用**（CLI 自发 UUID 是"变量不在 env"的直接签名） |
| T3 | 真实 HOME（含/不含 `--settings {}` 旁路） | 请求未到本地 echo，两次均**静默打到 bigmodel 真实 API** | settings env 钉死实测压过进程 env，且 `--settings` 空 JSON **不能**旁路——F9 加强版；08-28 运行日钉死尚不存在（mtime 08-29 09:27），但**今天起一切真实 HOME 手工复现都会静默外打，危险** |

### 9.2 静态考古

- **代理侧**：`anthropic_proxy.py:474/632` 在 do_GET/do_POST 入口单分支取头，无流式/重试旁路 → **假设 3 排除**。
- **注入代码**：`agent_go/executor.py` C1 注入 **08-19 已提交**（7cdc1ba），且 else 分支完备（env 无该变量时直接赋值）→ 代码正确，"注入不存在于 08-28"的解释排除。
- **worker 命令行**（`subtask.py:309`）：`-p --permission-mode bypassPermissions --no-session-persistence --output-format stream-json --verbose --include-partial-messages`——**无 --resume/--session-id 类自管会话旗标**，与 T1 裸 `-p` 测试基本等价（未覆盖差异：stream-json/verbose/MCP/cwd=worktree 组合）。

### 9.3 修正后的假设排序（替代 §4）

1. **08-28 worker 的最终 Popen env 中 `ANTHROPIC_CUSTOM_HEADERS` 实际缺失或未生效**。注意 F1 证明的是 `AGENT_GO_SESSION_KEY` 透传与分支执行，**不证明 CUSTOM_HEADERS 进入子进程 env**；而 T2 表明 CLI 自发 UUID 恰是"变量不在 env"的签名，与 F5/F7/F8 全部吻合。
2. worker 完整旗标组合（stream-json/verbose/no-session-persistence/MCP/cwd）下 CLI 对自定义头的处理差异（T1 未覆盖的组合）。
3. 08-28 当日 CLI 自动更新版本的行为差异（不可考，兜底）。

### 9.4 建议的最终判别（替代 §5——零模型成本，直接命中 1'）

不再用手工 echo 复现（被 settings 钉死阻断），改从 agent_go 侧二选一：

```python
# 方案 A(最轻): subtask Popen 前一行日志
logger.info(f"[{sub_id}] custom_headers_env={bool(env.get('ANTHROPIC_CUSTOM_HEADERS'))!r} "
            f"head20={str(env.get('ANTHROPIC_CUSTOM_HEADERS'))[:20]!r}")
# 方案 B(定案): 临时把 worker base_url 指向本地 echo(保留完整旗标组合), 跑一个 subtask
```

方案 A 若打出 `False`/`None` → 假设 1' 定案，断点在 env 构造链（executor 设值 → subtask Popen 之间的丢失点）；若 `True` 仍断链 → 方案 B 定案 CLI 组合行为。

### 9.5 修复建议增补

- §6 私有头方案不变；**增补**：修复前在 subtask metering 增加 `headers_env_set: bool` 字段，使断链状态本身可观测（当前 F1 型证据无法区分"分支执行"与"头实际下发"）。
- settings 钉死（08-29 写入）建议尽快处置：它使所有真实 HOME 的 claude 调用绕过 :4000 直打 bigmodel——对本 join 问题是干扰源，对成本与数据面是更大的静默风险（本日两次实测均静默外打）。

