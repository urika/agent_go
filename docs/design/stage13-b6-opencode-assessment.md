# 阶段十三 B6 候选：OpenCode Backend 评估

日期：2026-09-04。状态：调研完成，可行性确认，未实现（等 B5 结论后评估是否立项）。

## 结论速览

opencode（本机 1.18.27）**支持类 `claude -p` 的无头模式**，可作为第四种 worker backend
用 B1 标准接口接入，工作量与 PiBackend 同构（命令构造 + JSON 事件流解析 + 超时/计量）。

## 无头模式契约（本机实测）

命令：`opencode run --format json --auto [-m provider/model] [--dir <worktree>] <message>`

- `--format json`：stdout 输出 JSON 事件流（实测事件类型）：
  - `step_start` / `step_finish`：step 边界；`step_finish.part` 携带
    `tokens{input,output,reasoning,cache{read,write}}` 与 `cost`（美元），
    `reason` 为 `stop`（完成）或 `tool-calls`（继续）；
  - `tool_use`：`part.tool` 工具名 + `part.state.status`（completed/error）+ input；
  - `text`：`part.text` 最终回复文本。
- `--auto`：自动批准权限——**headless 必须**，否则 `run` 会无限挂起（实测：
  不带 --auto 跑 trivial prompt 5 分钟零输出零退出）。
- `--dir` 指定工作目录；`--pure` 禁外部插件；另有 `serve`/`attach` 池化形态（未评估）。
- 退出码 0 = 流程结束（语义同 pi/claude，验证仍归 executor）。

## 模型渠道

### Zen（`opencode/<model>`，按量网关）

- 免费模型（官方定价表 Free，限时收集反馈期）：big-pickle、mimo-v2.5-free、
  ling-3.0-flash-fin-free、nemotron-3-ultra-free、nemotron-3.5-lightning-free、
  muse-spark-1.2/1.3-contributor-free。
- **实测**：`opencode/mimo-v2.5-free` 完成真实任务（建文件+回复），事件流齐全，
  cost=0。可用作零成本 worker 通道。
- **隐私注意**：免费档数据可能被用于模型改进（NVIDIA 两个为 trial 条款，
  Muse 两个以数据换折扣）——只适合 eval fixture / 公开代码，不要跑私有代码。
- 付费档定价普遍低于官网刊例（如 DeepSeek V4 Flash Off-Peak $0.22/$0.66）。

### Go 套餐（`opencode-go/<model>`，$10/月）

- 本机已配置（qwen3.8-flash/max、kimi-k2.6/k2.7-code、mimo-v2.5(/-pro)、minimax 等）。
- **2026-09-04 实测不可用**：月度额度已尽（"Monthly usage limit reached. Resets in
  10 days"）。且 opencode 对额度耗尽的处理是「重试 3 次后静默挂起」——不退出不报错，
  接入时必须依赖 BackendContext.hard_timeout 兜底（B1 接口已有）。

## 接入要点（若立项）

1. `OpenCodeBackend`：复用 PiBackend 骨架——Popen + NDJSON 解析 + active_pids +
   hard_timeout kill + 聚合计量（step_finish 的 cost 直写，免费模型为 0）。
2. 必须带 `--auto`；建议带 `--pure` 避免用户插件干扰 bench 口径。
3. 零产出判定同 pi：无 tokens + 无工具调用 + 无最终文本 → returncode=1。
4. 路由：B4 声明式配置直接可用（`worker_backend: "opencode"`）。
5. 价值排序：先跑通 mimo-v2.5-free 零成本臂做 A/B；Go 套餐重置后再评 qwen3.8。
