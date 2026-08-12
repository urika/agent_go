# 本地模型生命周期管理设计

> 状态：设计稿，待评审（2026-08-12）
> 关联：[goal-mechanism-design.md](goal-mechanism-design.md) · [config-schema.md](config-schema.md) · [llama-defender-integration-requirements.md](llama-defender-integration-requirements.md)（服务方接口需求） · llama-defender（`/Users/jinsongwang/APP/llama.cpp`）
> 触发背景：弱模型 worker 实验证明本地模型已可作为生产 worker 后端（goal_ab 弱模型实验），但 agent_go 只能「读」本地代理状态，无法管理其生命周期。启停、切换、监控完全依赖人工操作 manage.sh。

## 1. 现状与缺口

### 1.1 agent_go 已有的本地模型集成（只读面）

| 能力 | 位置 | 说明 |
|---|---|---|
| `worker_backends` | executor.py:2156 | 模型名 → ANTHROPIC_BASE_URL 映射，按难度路由到本地代理 |
| 本地后端探测 | executor.py:47-95, 2163-2196 | 探测代理 `/status` 页解析真实后端模型名（支持 SIGHUP 热切换后识别） |
| 成本清零 | executor.py:2067 + subtask.py:673-683 | `AGENT_GO_IS_LOCAL=1` 注入，本地模型 metering cost=0 |
| `local_model_names` | config.py:168 | routed 名 → 真实本地模型名映射（探测失败时回退） |

### 1.2 缺口

| 场景 | 现状 | 问题 |
|---|---|---|
| 后端未启动 | 任务开始执行后才在 claude 调用处失败 | 无 pre-flight 检查，浪费 planner 成本，失败原因难定位 |
| 需要换模型 | 人工操作 manage.sh 四步序列（switch/stop-backend/reload/start-backend） | 无编排，中间窗口代理 502；无并发保护 |
| 后端死掉 | watchdog 未启用时无人发现 | 无主动健康监控 |
| 状态可视化 | 需手动 curl /status | CLI/web 无统一入口 |

## 2. llama-defender 控制面（已核实）

双进程架构：`anthropic_proxy.py`（127.0.0.1:4000，Python stdlib）+ 本地后端（llama-server/rapid-mlx，127.0.0.1:8081）。`manage.sh` 统一管理，pidfile 在仓库根目录。

**agent_go 可安全调用面**：

| 操作 | 幂等性 | 说明 |
|---|---|---|
| `GET /v1/models` | ✅ 只读 | 官方健康探针（manage.sh 与 watchdog 均用） |
| `GET /status` | ✅ 只读 | HTML 状态页（真实 MODEL_NAME、PID/RSS/uptime） |
| `GET /metrics` | ✅ 只读 | JSON 指标（ttft p50/p95、quality_flags 等） |
| `manage.sh start` | ✅ 幂等 | 已运行则只补启动 |
| `manage.sh reload` | ✅ 幂等 | SIGHUP 热重载，~0.5s，在途请求不受影响 |
| `manage.sh start-backend` | ✅ 幂等 | 已运行直接返回 0 |
| `manage.sh switch <name>` | ✅ 幂等 | 仅改 active.conf 软链；非交互模式自动跳过确认 |
| `manage.sh stop(-backend)` | ⚠️ 副作用 | 杀进程、在途请求失败；需并发保护 |
| `manage.sh restart` | ⚠️ 副作用 | 重新加载模型（35B 4bit 需数十秒） |
| `manage.sh start-cloud` | ⚠️ 副作用 | **会先停本地后端**，不可误用 |
| `manage.sh watchdog --daemon` | ❌ 禁用 | 第三方自动重启者，与 agent_go 生命周期管理冲突 |

**模型切换四步序列**（本地模型换权重必须走完整序列，中间窗口代理 502）：

```
switch <name> → stop-backend → reload → start-backend（含就绪轮询 GET /v1/models）
```

失败必须回滚 active.conf 软链接。

## 3. 设计

### 3.1 架构定位

```
agent_go model <cmd> ──→ LocalModelManager
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
            manage.sh adapter      HTTP probe
            (启停/切换/重载)        (/v1/models /status /metrics)
                    │
              llama-defender（本机，127.0.0.1）
```

- **不耦合具体后端实现**：`LocalModelManager` 定义抽象操作（status/start/stop/switch/list），llama-defender 是第一个 adapter（`manage.sh` 路径可配置）。未来可加 llama-server 直管 adapter。
- **机器级共享资源**：本地后端是所有 agent_go 任务共享的单例，模型操作是**机器级 CLI 命令**，不属于任何单个 task。
- **复用现有探测**：`AGENT_GO_IS_LOCAL` 判定逻辑不变，只在「后端不可达」分支前增加「尝试拉起」。

### 3.2 新模块 `agent_go/local_model.py`

```python
class LocalModelManager:
    def __init__(self, manage_script: Path, proxy_url: str, logger): ...

    def status(self) -> dict          # proxy/backend 存活 + 当前模型 + uptime + metrics
    def list_profiles(self) -> list   # configs/*.conf 解析（CONFIG_NAME/DESC/MEMORY）
    def current_profile(self) -> str  # active.conf 软链目标
    def start(self, backend: bool = True) -> bool      # manage.sh start
    def stop(self, backend_only: bool = False) -> bool # 需并发保护
    def switch(self, profile: str) -> bool             # 四步原子序列 + 失败回滚
    def wait_ready(self, timeout: int = 120) -> bool   # 轮询 GET /v1/models

    # —— 服务保活（诊断 → 修复阶梯）——
    def diagnose(self) -> dict        # 分级诊断：proxy 死 / backend 死 / 模型名漂移 / 就绪中
    def repair(self, level: int = 0) -> bool  # 阶梯修复：reload → start-backend → restart
    def ensure_ready(self, profile: str = "") -> bool  # status → diagnose → repair → wait_ready
```

所有脚本调用走 `subprocess.run(..., timeout=...)`，`set -euo pipefail` 的退出码即成功判据。HTTP 探测复用 executor.py 现有 urllib 逻辑。

### 3.3 Plan 阶段模型感知

任务规划（`generate_plan`）前，planner 需要知道**当前真实可用的模型能力**，否则可能把子任务路由到不可达的后端、或按错误的能力档拆分。

```
cmd_run / generate_plan 前
  → LocalModelManager.status()（enabled=true 时）
  → 得到 {reachable, active_model, backend_type}
  → 注入 plan 决策上下文：
      本地可达   → worker_models 可路由本地（成本 0），planner 按本地能力档拆分
      本地不可达 → auto_start=true 则先修复；否则 planner prompt 标注
                  「本地后端不可用，全部走云端模型」，或明确报错让用户选择
```

- 感知结果是**一次性快照**注入 plan prompt，不在 plan 生成中反复探测。
- 探测失败（manage.sh 缺失/代理无响应）→ 快照标记 `local_available=false`，plan 按云端模型路由，**fail-open 不阻断**。
- 能力档语义：用 `pricing.MODEL_TIER` / `local_model_names` 映射，告诉 planner 本地模型大致档位（如 35B-4bit ≈ haiku 档），避免把 hard 任务派给弱档本地模型。

### 3.4 服务保活：诊断与修复阶梯

发现问题时的处理分为**诊断分级**和**阶梯修复**两部分。

**诊断分级（`diagnose()`）**：

| 级别 | 判定 | 依据 |
|---|---|---|
| `healthy` | proxy + backend 均就绪 | `GET /v1/models` 200 |
| `starting` | 模型加载中 | 探测超时但进程存活（pidfile/pgrep） |
| `backend_down` | proxy 活但后端死 | `/v1/models` 502/超时 + `/status` 无 backend PID |
| `proxy_down` | 代理进程不存在 | pidfile 无 + pgrep 无 |
| `model_drift` | active.conf 与实际加载模型不一致 | `/status` 的 MODEL_NAME ≠ active.conf 目标 |
| `down` | 全部不可用 | 以上全部失败 |

**阶梯修复（`repair()`，逐级升级、每级幂等）**：

```
level 0: reload（SIGHUP 热重载，~0.5s）     ← model_drift / 配置漂移
level 1: start-backend                     ← backend_down（proxy 活）
level 2: restart                           ← proxy_down / down（35B 加载数十秒，属重操作）
每级执行后 wait_ready + diagnose 复检；失败升一级；level 2 仍失败 →
返回明确错误 + 建议（切换云端或人工介入），任务归因 infrastructure_failure
```

**触发时机约束（保护在途任务）**：

- **任务启动前**（pipeline pre-flight）：完整 diagnose→repair 阶梯可执行。
- **wave 边界**：仅 `reload`（无连接打断）；`start-backend/restart` 必须确认无活跃 subtask。
- **执行中**：不主动修复（重启会打断在途请求）；worker 请求失败按现有 infra 分类记录，任务结束后由 recover/next run 修复。

### 3.5 CLI 命令

```
agent_go model status                  # 代理+后端+当前模型+metrics 摘要
agent_go model list                    # 可用 profile 列表（含当前激活标记）
agent_go model start [--backend-only]  # 启动（幂等）
agent_go model stop [--backend-only]   # 停止（并发检查）
agent_go model switch <profile>        # 原子切换序列
agent_go model ensure <profile>        # status → 不符则 switch → wait_ready（供 pipeline pre-flight）
agent_go model diagnose                # 分级诊断输出（healthy/backend_down/...）
agent_go model repair [--level N]      # 阶梯修复（默认自动逐级升级）
```

### 3.6 config 扩展

```json
{
  "local_model_manager": {
    "enabled": false,
    "manage_script": "/Users/jinsongwang/APP/llama.cpp/manage.sh",
    "proxy_url": "http://127.0.0.1:4000",
    "auto_start": false,
    "auto_repair": false,
    "default_profile": "",
    "wait_ready_timeout": 120,
    "plan_time_probe": true
  }
}
```

- `enabled=false` 默认关闭，不破坏现有行为。
- `auto_start=true` 时，run_subtask 检测到本地后端不可达 → 尝试 `start` → `wait_ready` → 失败则明确报 `infrastructure_failure`（而非现在的「claude 调用处模糊失败」）。
- `auto_repair=true` 时，pre-flight 发现问题自动走修复阶梯；false 则只诊断、给出建议命令，不自动执行。
- `default_profile` + `--yes` 模式下，任务开始前检查当前 profile 是否匹配 worker_models 期望的后端能力档。
- `plan_time_probe=true` 时，generate_plan 前注入模型可用性快照（§3.3）；false 则跳过探测。

### 3.7 Pipeline 集成点

1. **Plan 前感知（P2）**：`cmd_run` 在 generate_plan 前 `status()` 快照注入 planner 上下文（§3.3）。
2. **Pre-flight readiness + 保活（P2）**：`_run_pipeline` 启动前，若任一 subtask 路由到本地后端（`AGENT_GO_IS_LOCAL` 判定路径），先 `diagnose()`；非 healthy 且 `auto_repair=true` → `repair()` 阶梯 → `wait_ready()`；最终失败则任务标记 `infrastructure_failure` 并给出恢复指引，不进入 wave。
3. **切换/重启并发保护**：`model switch/stop/restart` 执行前扫描 `~/.agent_go/task-*/meta.json`，有 `EXECUTING` 状态的活跃任务 → 拒绝并列出任务（或 `--force` 覆盖）。保护在途 subtask 的 claude 调用不被 502 打断。
4. **计量一致性**：切换后 `/status` 探测到新模型名，`AGENT_GO_IS_LOCAL` 与 `local_model_names` 映射继续工作（现有逻辑已支持 SIGHUP 热切换场景）。

### 3.8 保活职责分工（服务方 vs 消费方）

**核心原则：谁拥有进程生命周期，谁负责保活。** llama-defender 拥有 proxy/backend 进程树、GPU 资源和防风暴重启策略；agent_go 是间歇运行的任务编排器，任务结束后即退出，无法承担 7×24 持续保活。

| 职责 | 归属 | 理由 |
|---|---|---|
| 持续健康监控（60s 轮询） | llama-defender watchdog | 服务独立于任何任务存在，需 7×24 值守 |
| 故障自动重启（含限频防风暴） | llama-defender watchdog | 拥有进程树、GPU 安全检查、模型加载时长知识 |
| 任务启动前就绪检查 | agent_go pre-flight | 任务归属明确，失败需归因到具体任务 |
| 一次性修复触发（pre-flight repair） | agent_go（调 manage.sh） | 幂等、单一入口；只在无在途请求时执行 |
| 降级路由（本地→云端）与失败分类 | agent_go | 只有 agent_go 知道任务语义与成本预算 |

**避免双重重启的协调规则**：

1. **单一执行入口**：双方修复动作都经 manage.sh（pidfile 幂等 + pgrep 自愈），agent_go 不直接 kill/start 进程。
2. **时机互斥**：agent_go 的 repair 只在任务启动前（无在途请求时）执行；执行中发现故障只记录 `infrastructure_failure`，由 llama-defender watchdog 在后台恢复，agent_go 任务走正常失败/重试路径。
3. **状态读取而非控制**：agent_go 可读取 watchdog 状态（`manage.sh watchdog-status`）辅助诊断，但不启停对方 watchdog。
4. **并发任务冲突**：多个 agent_go 任务并发时，repair 前同样执行活跃任务检查；llama-defender watchdog 的重启限频（每小时 ≤6 次）天然防止恢复风暴。

**配置项调整**：`auto_repair` 语义收窄为「任务启动前的一次性修复触发」，不包含持续保活；持续保活由 llama-defender 侧 `manage.sh watchdog --daemon` 按需启用（服务侧运维决策，非 agent_go 配置）。

### 3.9 安全与降级

- **fail-open**：`local_model_manager.enabled=false` 或 manage.sh 不存在 → 所有 model 命令明确报错，run 流程完全不受影响（现有探测路径不变）。
- **不接管对方 watchdog**：agent_go 不启停、不依赖 llama-defender watchdog；保活是服务方职责（§3.8），agent_go 只读其状态。
- **无副作用原则**：`status/list/current/diagnose` 纯只读；`start/reload` 幂等；只有 `stop/switch/restart/repair` 需并发保护。

## 4. 分阶段落地

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0 只读管理面** | `local_model.py` + `model status/list/current/diagnose`；解析 /status 真实模型名；配置项落地 | 对运行中 llama-defender 输出正确分级诊断；单元测试覆盖解析 |
| **P1 启停 + 切换** | `model start/stop/switch`；切换四步原子序列 + 失败回滚；并发活跃任务检查 | 真实切换 27B→35B 成功且失败可回滚；活跃任务时拒绝切换 |
| **P2 保活 + Pipeline 集成** | 修复阶梯 `repair()`（reload→start-backend→restart）；pre-flight diagnose+repair；`auto_start`/`auto_repair`；**Plan 前模型感知快照注入** | 后端停止状态下跑任务：repair 拉起成功 or 明确失败指引；本地不可达时 plan 按云端路由 |
| **P3 监控** | `agent_go status` 面板 + web 页面展示后端健康/模型/ttft 指标 | web 只读展示 /metrics 数据 |

## 5. 非目标

- 不做模型权重下载/安装管理（HF 下载由 manage.sh 的 `_wait_for_ready` 处理）。
- 不做多机/多后端池化调度。
- 不做持续保活（7×24 watchdog）——保活归 llama-defender 服务方（§3.8）；agent_go 只做任务启动前的一次性就绪保障。
- 不改变 difficulty→worker_models→worker_backends 的现有路由语义。
- 不做云端模型生命周期管理（云端无进程概念）。

## 6. 风险

| 风险 | 缓解 |
|---|---|
| switch 中间窗口在途请求 502 | 活跃任务检查 + 拒绝；原子序列 + 回滚 |
| auto_start 拉起大模型耗时长（数十秒） | `wait_ready_timeout` 上限；失败明确归因 infra |
| manage.sh 路径硬编码耦合 | adapter 抽象 + 配置注入；llama-defender 不可用时 fail-open |
| 与 llama-defender watchdog 双重重启冲突 | 分工明确（§3.8）：watchdog 持续保活，agent_go 仅 pre-flight 一次性修复且走 manage.sh 单入口 |
| 修复阶梯在执行中打断在途请求 | 触发时机约束（§3.4）：执行中不修复，仅任务启动前 / wave 边界 |
| plan 前探测失败阻断任务 | fail-open：快照标记 local_available=false，按云端路由继续 |
| plan 快照过时（plan 后模型被切换） | 快照仅用于规划期路由建议；执行前 pre-flight 以 diagnose 实时状态为准 |

## 7. 与 Goal 机制的关系

无功能耦合。弱模型 worker 实验（goal_ab）已证明本地模型可作为 worker 后端；本地模型管理是把「实验态手工操作」转为「产品态可管理」的基础设施。Goal 实验期间人工执行的 `manage.sh switch` 序列正是本设计要编排的能力。
