# 阶段十三 B6：OpenCode Backend 评估与实现

日期：2026-09-04。状态：**已立项并实现**（opencode_backend.py + 11 个单元测试 +
mimo-v2.5-free 真实冒烟通过）；Go 套餐臂评估待月度额度重置。

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

## 实现记录（2026-09-04）

- `agent_go/backends/opencode_backend.py`：`OpenCodeBackend`（name="opencode"），
  按上述要点 1-4 全部落地；readonly 模式映射 opencode 内置只读 agent（`--agent plan`）。
- 计量：prompt_tokens = input + cache.read，completion = output；cost 聚合 step_finish.part.cost；
  事件流不携带 model 信息，actual_model 取 ctx.routed_model。
- 防御性处理 `type=="error"` 事件（当前实测未见，若未来版本输出则捕获记录）。
- 测试：`tests/test_backends.py::TestOpenCodeBackend` 11 例（命令构造/readonly/模型透传/
  超时 kill/未安装 127/容错解析/计量写入/工具错误计数/零产出失败映射/error 事件不误判）。
- 真实冒烟（mimo-v2.5-free，/tmp 临时仓库）：rc=0，1 次工具调用，
  35750+90 tokens，$0.00，23s，目标文件真实创建，计量事件正确写入。
- 待办：Go 套餐额度重置（约 10 天）后评估 qwen3.8-flash 臂；Zen 免费臂 A/B
  只适合 eval fixture / 公开代码（免费档数据可能用于训练）。
