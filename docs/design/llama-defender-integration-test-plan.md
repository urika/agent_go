# llama-defender 集成测试设计

> 状态：测试设计稿（2026-08-12）
> 关联：[llama-defender-integration-requirements.md](llama-defender-integration-requirements.md)（需求契约） · [local-model-management-design.md](local-model-management-design.md)（agent_go 侧设计）
> 验收脚本：`tools/check_llama_defender_contract.py`（纯 stdlib，可直接对运行中的 llama-defender 执行）

## 1. 目标与范围

对方（llama-defender）按需求文档完成开发后，用本测试集验收：

1. **接口契约**：R1-R7 的端点存在性、字段、枚举、时延、错误语义。
2. **行为场景**：S1-S7 端到端流程（就绪检查→感知→诊断→修复→切换→并发→监控）。
3. **故障注入**：backend 死、proxy 死、模型漂移、加载中等分级诊断正确性。
4. **并发与幂等**：文件锁互斥、命令幂等。
5. **降级兼容**：新接口缺失时 agent_go 侧 fail-open。

**通过标准**：P0 用例全部 PASS；P1 用例 PASS 或明确 SKIP（对方未实现且有降级路径）；P2 可选。

## 2. 测试环境

- llama-defender 运行中：`http://127.0.0.1:4000`（proxy）+ 本地后端（8081）。
- manage.sh 路径：`/Users/jinsongwang/APP/llama.cpp/manage.sh`。
- 至少 2 个 profile（如 `rapid-mlx-35b-opt` + `qwen3.6-27b-4bit`）用于切换用例；单 profile 时切换类用例 SKIP。
- 脚本默认 **safe 模式**（只读，不改变服务状态）；`--full` 才执行变更操作用例。

## 3. 用例索引

| 分组 | 用例数 | 覆盖需求 | 模式 |
|---|---|---|---|
| A 接口契约 | 12 | R1/R2/R3/R5/R6/R7 | safe |
| B 行为场景 | 6 | S1/S2/S3/S4/S5/S7 | safe+full |
| C 故障注入 | 4 | S3/S4（backend_down/proxy_down/drift/starting） | full |
| D 并发幂等 | 3 | R3/R4/S6 | full |
| E 降级兼容 | 2 | 需求文档 §6 | safe |

## 4. A 组：接口契约测试（safe）

| # | 用例 | 步骤 | 预期 | 需求 |
|---|---|---|---|---|
| A1 | 结构化状态端点存在 | `GET /api/status`（备选 `/status?format=json`） | 200 + `application/json` | R1 |
| A2 | 必含字段 | 解析响应 | `proxy.pid/alive`、`backend.model_name/backend_type/alive`、`active_profile`、`state`、`ready` 全存在 | R1 |
| A3 | state 枚举合法 | 检查 `state` 值 | ∈ `{healthy, starting, backend_down, proxy_down, model_drift, down}` | R1 |
| A4 | 状态端点时延 | 计时 | < 1s | R1/协议 |
| A5 | model_name 为真实后端模型 | 与 `/status` HTML 中 Model 行比对 | 一致（非别名） | R1/S2 |
| A6 | ready 字段语义一致 | `state==healthy` 时 | `ready==true` 且为 bool | R2 |
| A7 | manage.sh 只读命令非交互 | stdin=DEVNULL 执行 `status`/`current`/`list` | < 10s 完成，exit 0，无输入阻塞 | R3 |
| A8 | manage.sh 非法命令退出码 | 执行 `manage.sh __nonexistent__` | exit ≠ 0，stderr 有原因 | R3 |
| A9 | 只读命令幂等 | `status` 连续两次 | 两次 exit 0 | R3 |
| A10 | watchdog 状态可查 | `manage.sh watchdog-status` 或 HTTP 等价 | 输出含 enabled/last_restart 等结构化信息 | R5 |
| A11 | profile 列表端点（可选） | `GET /api/profiles` | 200 + 列表含 active 标记；404 → SKIP | R6 |
| A12 | 生命周期事件（可选） | 读 `logs/lifecycle_events.jsonl` | 存在且逐行 JSON；不存在 → SKIP | R7 |

## 5. B 组：行为场景测试

| # | 用例 | 步骤 | 预期 | 需求 | 模式 |
|---|---|---|---|---|---|
| B1 | 就绪检查（S1） | 服务健康时 `GET /v1/models` + `GET /api/status` | 200 且 `ready=true`，总耗时 < 2s | R1/R2 | safe |
| B2 | 模型感知（S2） | 读 `active_profile` 与 `backend.model_name` | 与 `configs/active.conf` 软链目标一致 | R1 | safe |
| B3 | 诊断分级-健康（S3） | 健康时查 state | `healthy` | R1 | safe |
| B4 | 修复后恢复（S4） | `stop-backend` → 查 state → `start-backend` → 轮询 ready | state 变 `backend_down`→ 最终 `ready=true` | R2/R3 | full |
| B5 | 切换原子序列（S5） | `switch <other>` → `stop-backend` → `reload` → `start-backend` → 查 status | active_profile 与 model_name 更新为新 profile；**测试后切回原 profile** | R3 | full |
| B6 | 监控数据（S7） | `GET /metrics` | JSON 含 total/status 等既有字段（字段名不更名） | 协议 | safe |

## 6. C 组：故障注入测试（full）

| # | 用例 | 注入方式 | 预期诊断 | 恢复 |
|---|---|---|---|---|
| C1 | backend_down | `manage.sh stop-backend` | `state=backend_down`，`backend.alive=false`，`proxy.alive=true` | `start-backend` |
| C2 | proxy_down | `kill <anthropic_proxy.pid>` | 端点不可达 → agent_go 侧判定 proxy_down（脚本验证端点超时/拒绝） | `manage.sh start` |
| C3 | model_drift | `switch` 改软链但**不 reload** | `state=model_drift`（active_profile ≠ 实际模型） | `reload` 后回 healthy；**恢复软链** |
| C4 | starting | `restart` 后轮询 | 加载期间 `state=starting` 且 `ready=false`，完成后 `healthy/ready=true` | 自动 |

**安全约束**：C 组每步记录操作前状态，finally 块恢复原 profile 与进程状态；任意步骤失败立即回滚软链并 `start`。

## 7. D 组：并发与幂等测试（full）

| # | 用例 | 步骤 | 预期 | 需求 |
|---|---|---|---|---|
| D1 | 变更锁互斥 | 后台持锁 `.manage.lock`（flock）→ 执行 `manage.sh reload` | 快速非 0 退出（< 5s），stderr 说明锁占用；**不得等待或执行** | R4 |
| D2 | start 幂等 | 服务已运行时 `manage.sh start` 两次 | 两次 exit 0，进程数不增加 | R3 |
| D3 | reload 幂等 | `manage.sh reload` 连续两次 | 两次 exit 0，在途探测不受影响 | R3 |

## 8. E 组：降级兼容测试（safe，agent_go 侧）

| # | 用例 | 步骤 | 预期 |
|---|---|---|---|
| E1 | 新端点缺失降级 | 对未实现 `/api/status` 的旧版实例执行 agent_go 探测 | agent_go 回退 HTML 解析/pidfile，`model status` 仍可用，任务不阻断 |
| E2 | 字段缺失降级 | mock 一个缺 `state` 字段的响应 | agent_go 标记 `state=unknown` 并 warning，不崩溃 |

E 组在 agent_go `local_model.py` 实现后以单元测试固化（mock HTTP），不依赖对方环境。

## 9. 执行方式

```bash
# safe 模式（只读契约检查，可随时运行）
python3 tools/check_llama_defender_contract.py

# full 模式（含变更操作/故障注入，需无活跃 agent_go 任务）
python3 tools/check_llama_defender_contract.py --full

# 指定环境
python3 tools/check_llama_defender_contract.py --proxy-url http://127.0.0.1:4000 \
    --manage-script /Users/jinsongwang/APP/llama.cpp/manage.sh

# JSON 输出（供 CI 消费）
python3 tools/check_llama_defender_contract.py --json
```

退出码：P0 用例全过 = 0；任一 P0 FAIL = 1；仅 P1/P2 FAIL/SKIP = 0 但报告标注。

## 10. 当前基线（开发前，已实测）

对当前 llama-defender（未实现 R1-R7）执行 safe 模式，实测结果（2026-08-12）：

- **7 PASS**：A7/A8/A9（manage.sh 契约已满足）、A10（watchdog-status）、A12（lifecycle_events.jsonl 已存在）、B1（就绪检查）、B6（metrics 稳定）
- **1 FAIL**：A1（`/api/status` 未实现，404）——预期基线
- **5 SKIP**：A2/A3/A4/A6（依赖 R1 端点）、A11（R6 可选端点未实现）
- 脚本退出码 = 1（P0 FAIL 存在）

该基线用于对方开发完成后的**差异验收**——FAIL/SKIP 全部转 PASS（或 P1/P2 明确 SKIP）即集成完成。

## 11. 验收结果（对方交付后，已实测）

对方交付（2026-08-12，`docs/in/api-and-operations-guide.md`）后实测：

**safe 模式：13 PASS / 0 FAIL / 0 SKIP，P0 全部通过，exit=0。**

| 组 | 结果 |
|---|---|
| A1-A6（R1/R2 状态端点） | ✅ 全过：`/api/status` 200、字段齐全、state=healthy、时延 0.06s、ready=true |
| A7-A9（R3 manage.sh 契约） | ✅ 全过：非交互、退出码、幂等 |
| A10（R5 watchdog 状态） | ✅ `watchdog-status` + `/api/watchdog` |
| A11（R6 profiles） | ✅ `/api/profiles` 200 |
| A12（R7 生命周期事件） | ✅ `logs/lifecycle_events.jsonl` 有样本 |
| B1（就绪）/B6（metrics） | ✅ |

**过程发现**：对方部署重启代理期间（~13:33-13:37）曾出现短暂 404（`/api/*` 未加载），代理重启完成后端点全部生效——验证了新端点需代理进程重启加载（代码级变更不能 SIGHUP 热重载）。

**待执行**：C/D 组故障注入与并发用例（`--full`），因需停止后端（35B 重载数十秒），待服务空闲窗口执行。
