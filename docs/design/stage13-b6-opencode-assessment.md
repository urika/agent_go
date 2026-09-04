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

## B6 批量：opencode × glm-5.3-flash 臂（2026-09-04/05 夜间免费窗口）

golden 6 任务 × repeat 2，worker 走 zai-coding-plan（opencode auth.json），
planner/evaluator 走 api.z.ai/api/anthropic（ZAI_API_KEY）。
结果文件：`eval_suite/results_b6_opencode_glm_20260904.jsonl`（26 条原始记录，
含两轮补跑；dedup 规则：剔除基础设施伪记录后每 (task,repeat) 保留最新真实记录）。

**终版 12/12 通过**（dedup 后），平均 elapsed ~447s，cost 全 $0（免费窗口）。
对照 B5 同模型双臂：claude 10/12、pi 10/12。

诚实口径备注：
- 首跑真实通过率 7/8（剔除基础设施伪记录后；fix-missing-default r1 为真实
  model_failure，补跑后通过）。12/12 含补跑偏向，与 B5 的「首跑+基础设施补跑」
  口径略有差异（B5 两臂的补跑只替换 planner 崩溃类记录）。
- conditional-branching / security-hardening 的 failure_class=verification_failure
  是执行中途验证失败重试后通过的痕迹，binary_pass 为准。

**批量暴露并已修复的 3 个基础设施 bug**（本臂最大的产出）：

1. **api.py decompose_fallback**：planner 401 降级后本地模型输出的 files_hint
   为 JSON 数组，executor 按 str `.strip()` → AttributeError → system_error
   （初跑 3 例秒退的根因）。已修：fallback 边界归一化 list→逗号字符串 + 测试。
2. **opencode snapshot 跨目录污染**：影子仓库（~/.local/share/opencode/snapshot/
   <base-commit>/）按 base commit 全局共享、worktree 路径记录首次注册目录——
   agent_go 每个 worktree 同一 base commit，snapshot 写回会污染主仓库甚至
  无关目录（实测 agent_go 根目录被写入 fixture 文件），并引发后续任务
   dirty-abort 连锁 5s 伪记录。已修：OpenCodeBackend 注入
   `OPENCODE_CONFIG={"snapshot": false}`（实测影子仓库不再写入）+ 2 测试。
3. **backend_promo 破坏测试 hermetic**：executor._effective_config 在 config=None
   时回退真实用户配置，promo 窗口内 executor 测试（只 mock claude 路径）真实拉起
   zcode 进程。已修：conftest autouse fixture 默认禁用 promo（TestBackendPromo 豁免）。

**遗留风险**：glm-5.3-flash 作为较弱模型有「漫游」倾向（初跑 security r1 的 bash
曾越界到 agent_go 根目录跑 pytest）——snapshot 已关，但 bash 工具级的目录约束
opencode 侧没有硬保证；backend 层面可考虑后续加 cwd 监控或文件系统沙箱。
