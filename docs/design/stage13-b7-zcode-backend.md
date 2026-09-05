# 阶段十三 B7：ZCode Backend 设计与实现

日期：2026-09-04。状态：已实现（zcode_backend.py + 11 个单元测试 + glm-5.3-flash
真实冒烟通过）；夜间免费窗口批量臂待排期。

## 动机

GLM Coding Plan「Flash×ZCode」活动（2026-09-04 至 09-20，每晚 23:00–09:00 北京时间）：
glm-5.3-flash 仅在 **ZCode 本体**完全免费，其他支持的 agent 只是额度翻倍。
ZCodeBackend 是该窗口的零成本 worker 通道。

## 无头契约（ZCode 0.16.5 本机实测）

```
ELECTRON_RUN_AS_NODE=1 /Applications/ZCode.app/Contents/MacOS/ZCode \
    .../Resources/glm/zcode.cjs --json --mode <plan|yolo> --cwd <worktree> --prompt <task>
```

- stdout：**单个 JSON 对象**（非事件流）——sessionId / response（最终回复）/
  usage{inputTokens,outputTokens,cacheReadTokens,...} / projection{turnCount,contextUsed}；
  无 cost 字段、无 per-tool-call 统计。
- 退出码 0 = 成功，1 = turn 失败（`Turn execution failed (traceId: ...)`）。
- 权限档：plan（只读）/ build / edit / yolo（--prompt 默认 yolo）。
- 其他：--allowed-tools/--disallowed-tools、--resume <sessionId>、app-server（stdio 协议）。

## 关键差异与决策

- **模型不可 per-run 选择**：内置 runtime 无 --model/--settings（这两个标志只存在于
  非官方 npm 客户端 zcode-app-cli 的 fork；0.16.5 实测报 Unknown option）。
  模型由 `~/.zcode/cli/config.json` 的 model.main 决定；routed_model 不一致时
  log warning，计量按配置实际值（`_configured_model`）记录。
- **认证**：custom provider 模板（provider key 必须为 `zai`，kind=anthropic，
  baseURL=https://api.z.ai/api/anthropic，apiKey 明文，0600）。
  注意 z.ai key 形如 `id.secret`，ZCode 用它做 V4 请求签名——key 截断会报
  `Client signing credential must contain one separator`。
- **零产出判定**：无工具统计，只能靠 tokens + response（无 tokens 且无文本的
  退出 0 → returncode=1）。
- **计量**：cost 记 0（套餐计费；免费窗口内本就为 0）；tokens 口径同 pi/opencode
  （prompt = input + cacheRead）。
- **Electron 启动开销**：实测整体 11s 完成 trivial 写任务，子任务粒度可接受。

## 实现

- `agent_go/backends/zcode_backend.py`：`ZCodeBackend`（name="zcode"），
  骨架同 pi/opencode（Popen + communicate(timeout) + active_pids + 聚合计量）。
  app 路径默认 `/Applications/ZCode.app`，`ZCODE_APP_PATH` 环境变量可覆盖。
- 测试：`tests/test_backends.py::TestZCodeBackend` 11 例（命令构造/readonly plan/
  模型不一致 warning/配置读取/超时 kill/app 缺失 127/前置垃圾行容错/计量/零产出/
  turn 失败透传）。
- 真实冒烟（2026-09-04，glm-5.3-flash）：rc=0，35445+78 tokens，$0，11s，
  目标文件真实创建，计量事件正确。

## 风险

- 依赖闭源桌面 app，升级可能改契约（社区桥接项目报告过 app-server 协议不稳定；
  --prompt/--json 是官方 help 头等功能，相对稳定）。
- 免费窗口限 9-04 至 9-20 每晚 23:00-09:00；窗口外按 coding plan 正常计额度。

## 大促规则落地（2026-09-04）

「glm-5.3-flash 大促期间优先 zcode+glm-5.3-flash」落地为配置驱动的促销窗口路由：

- 新增配置 `backend_promo`（registry.py `_promo_time_active`/`_promo_backend`）：
  时间窗（日期闭区间 + 每日时段，支持跨午夜，固定 tz_offset=8）内且**无任何显式
  backend 声明**时，优先路由到 promo backend；要求 headless 且 backend 本机可用
  （新增 `BaseBackend.available()` 探测，zcode 覆盖为 app+config 存在性检查）。
- 优先级：subtask.backend > worker_backend > by_type > by_difficulty > **promo** >
  agent_loop > claude——显式声明永远优先，窗口外/不可用自动回落既有行为。
- 用户配置已写入 `~/.agent_go/config.json`：`{"backend": "zcode", "start": "2026-09-04",
  "end": "2026-09-20", "daily_start": "23:00", "daily_end": "09:00"}`。
- 实测（23:02 +08:00）：headless 无显式声明 → zcode；交互模式 → claude；
  显式 pi → pi。窗口到期（09-20 后）自动失效，无需手工摘除。

## B7 批量：zcode × glm-5.3-flash 臂（2026-09-05 07:20-07:38 免费窗口）

golden 6 任务 × repeat 2，并行 4，worker 走 zcode（模型由 ~/.zcode/cli/config.json
model.main=zai/glm-5.3-flash 决定，无 per-run 标志），planner/evaluator 走
api.z.ai/api/anthropic。结果文件：`eval_suite/results_b7_zcode_glm_20260905.jsonl`
（12 条，全部真实记录——无 system_error、无 <30s 早退、无人工 kill，是四臂中
唯一无需 dedup/补跑的一批）。

**首跑 10/12 通过**（binary_pass 口径，与 B6 一致），平均 elapsed ~611s
（慢于 opencode 臂 ~447s），cost 全 $0（免费窗口）。

- 失败 2 条均为 add-simple-caching（r1/r2），终态 VERIFICATION_FAILED
  「能力失败优先」。**复查（同日 08:45，单任务 ×3 并行 3）：3/3 通过**
  （165s/220s/335s，远快于失败时的 665s 超时）——判定为执行超时型方差
  而非 zcode harness 系统性短板；结果文件
  `eval_suite/results_b7_caching_recheck_20260905.jsonl`。
- failure_class=verification_failure 的 5 条（conditional-branching r1/r2、
  security-hardening r1/r2、fix-missing-default r2）是执行中途验证失败重试后
  通过的痕迹，binary_pass 为准（同 B6 口径）。
- add-format-helper r1 kill_reason=cleanup_race + timed_out=True 但 binary_pass
  =True（commit 已完成，清理阶段超时），计通过。

## 待办

- 若 ZCode 官方发布稳定独立 CLI（zai-org/feedback#444 在跟踪），迁移过去。
