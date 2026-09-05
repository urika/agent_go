# ADR-011: Pipeline 本地模型自动限流（云端并行、本地串行）

## 状态

Accepted

## 决策

`--parallel N > 1` 的 DAG 波次内，按 worker 实际路由自动分级调度：

- **云端路由**子任务：语义不变，波次内按 `--parallel N` 并行，且**优先提交**占满 worker 槽位。
- **本地路由**子任务：共用任务级 `threading.Semaphore(1)` 互斥执行（等效串行），排队只在本地子任务之间发生。

开关：`pipeline.local_model_serialize`（默认 `true`；本地后端本身支持并发时可显式关闭）。

## 原因

并发调度原则（2026-09-04 拍板，见 runtime-design-decisions）：本地 GPU/内存是单机独占资源，多个 worker 并行打同一本地后端会同时拖垮所有子任务（推理排队、超时误杀）并污染 elapsed 口径；云端套餐端点对并发友好，不应被本地限流误伤。此前只有 bench 层 `--bench-parallel` 手动控制，pipeline 无自动化——本 ADR 落地自动化。

## 判定口径（什么算「本地路由」）

`executor.worker_routes_local(subtask, config)`，与 `run_subtask` 注入 `AGENT_GO_IS_LOCAL=1` 的判定链同源：

1. **routed_model 解析**：cognitive（`worker_models_by_cognitive`）> task_type（`worker_models_by_type`）> degrade（`config["_degraded"]` 降档）> difficulty→`worker_models`；
2. **后端 URL**：`worker_backends[routed_model]`（deprecated 兼容）→ `plan_api.worker_base_url`；
3. **URL 指向本机**（`127.0.0.1` / `localhost` / `0.0.0.0` / `[::1]`）；
4. **`_verify_local_backend` 深度验证为真本地**（R8 路由归因头 → 代理 `/status` 声明 → claude 探测；结果按 base_url 缓存，pipeline 预判定后 run_subtask 命中同一缓存，不重复探测）。

任一步不成立 → 按云端调度。判定异常 → fail-open 按云端调度（宁可并行也不卡死管线）。

## 并发设计

- 信号量在 `_run_pipeline_impl` 内按任务创建（不跨任务共享，无模块级状态泄漏）。
- 波次提交前在主线程预分类（`_local_ids`），`sorted(wave, key=云端优先)` 稳定排序后提交：pool FIFO 语义下云端子任务先占槽位/先补位，本地子任务在信号量上等待时**不会挤占无关云端子任务的并行度**（等信号量的本地线程至多占用本波无云端可跑的槽位）。
- 信号量只包裹 `_invoke_run_subtask` 调用，与既有 `meta_lock`/`active_pids_lock` 无嵌套（锁序：`_local_sem` 内不取其它调度锁），无死锁路径。
- 串行分支（`parallel=1` 或单子任务波次）不经过信号量，行为完全不变。

## 约束

- `worker_routes_local` 是 `run_subtask` 路由块的**无副作用镜像**，两者必须同步演进（函数 docstring 已互相标注）。
- 深度验证探测（HTTP /status + 可选 claude 探测）只在 URL 指向本机时发生，且按 base_url 缓存——默认云端配置（`worker_base_url` 为空）零开销、零行为变化。
- 降级/熔断中途改变 routed_model 时以**调度时刻**的口径为准；URL 层判定为主导项，模型层偏差只影响 deprecated `worker_backends` 映射场景。

## 实现

- `agent_go/executor.py`：`worker_routes_local`（新增，无副作用判定函数）。
- `agent_go/pipeline.py`：`_run_pipeline_impl` 并发分支——预分类 + `_local_sem` 互斥包裹 + 云端优先提交。
- 配置：`pipeline.local_model_serialize`（`config.py` DEFAULT_CONFIG / `config.example.json` / `docs/design/config-schema.md` §12a）。

## 相关决策

- 并发调度原则（runtime-design-decisions，2026-09-04 拍板）——本 ADR 是其 pipeline 侧自动化落地。
- [ADR-006（bench 进程隔离与批次治理）](ADR-006-bench-isolation-and-batches.md)：bench 层 `--bench-parallel` 手动控制保留，与本机制正交。
