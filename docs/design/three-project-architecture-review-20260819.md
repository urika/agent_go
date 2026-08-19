# 三项目架构 Review：swe-eval / agent_go / llama-defender

> 状态：Review v1（2026-08-19）
> 范围：`swe-eval`（SWE-bench Pro 评测 harness）、`agent_go`（任务编排 harness）、`llama.cpp`（llama-defender 代理）
> 方法：代码级探查（非仅文档），关键结论均有文件:行号锚点
> 关联：[llama-defender-integration-requirements.md](llama-defender-integration-requirements.md)（R1-R16 契约） · [diag-dataplane-consumer-requirements-20260819.md](diag-dataplane-consumer-requirements-20260819.md)（消费侧落地）

## 1. 整体架构与分层定位

```
┌─────────────────────────────────────────────────────────────────┐
│ L3 决策层   agent_go: eval/bench/gate/recommend/insight          │
│             swe-eval: report.py（resolve rate A/B）              │
├─────────────────────────────────────────────────────────────────┤
│ L2 编排层   agent_go（任务编排）          swe-eval（评测）        │
│             Plan→Decompose→Execute        fetch→run→evaluate→report│
│             worktree 隔离/verify 重试      worktree 防泄漏/junit 判定│
│             metering.jsonl                runs.jsonl              │
├─────────────────────────────────────────────────────────────────┤
│ L1 代理层   llama-defender                                       │
│  数据面: 双协议端点 → 24-stage pipeline（SmartRouter → 压缩/注入  │
│          → 循环防线 → 截断 → BackendDispatcher）                  │
│  控制面: manage.sh（含文件锁）+ watchdog（独立进程）+ /api/*       │
│  观测面: R8-R16 响应头 + /api/session/* + logs/*.jsonl            │
├─────────────────────────────────────────────────────────────────┤
│ L0 后端层   llama-server / rapid-mlx（本地）  DeepSeek/GLM（云端） │
└─────────────────────────────────────────────────────────────────┘
```

契约通道现状：

| 通道 | agent_go ↔ defender | swe-eval ↔ defender |
|---|---|---|
| 请求头 | R8 归因头、R13 诊断头、会话头 | `X-Proxy-Route-To` 路由强制 + 会话头 |
| 结构化端点 | `/api/status`、`/api/session/*`、props（diag.py，fail-open） | **零消费** |
| 文件 | metering ↔ sessions.jsonl 经 request_id 互查 | 直接读代理日志文本（正则解析） |
| 契约保障 | 契约文档 + `tools/check_llama_defender_contract.py`（21 用例） | **无** |

**总体判断**：三层职责划分清晰，代理层的契约治理（新增不破坏 + 字段只增不改名 + 消费方 fail-open + 先观测后改造）是系统最成熟的部分。主要债务集中在两个消费方的不对称：agent_go 已走完「契约化 + 结构化消费」全程，swe-eval 仍停留在「日志 scraping」阶段。

## 2. 按项目归类的发现

### 2.1 llama.cpp（llama-defender 代理）

| # | 严重度 | 发现 | 证据 | 影响 | 建议 |
|---|---|---|---|---|---|
| L-1 | P2 | char 估算 token 的系统性偏差沿链路传导：全链路 token 为 `chars/ratio` 估算，经 R8/R16 进入两个消费方的成本数字 | `message_converter.py:309`、`:370`；路由成本预估 `pipeline.py` BackendDispatcher | bench gate、$/pass、成本基线建立在估算值上 | 后端返回真实 usage/timings 时优先真实值；metering 标注 `token_source: estimated\|actual` |
| L-2 | P2 | 单进程 ThreadingHTTPServer + `_llama_lock` 串行化：admin/metrics 查询与推理请求竞争同一进程线程池与 GIL；两个 harness 的轮询型消费（看门狗 30s、流量自检）叠加后观测流量挤占数据面 | `anthropic_proxy.py` Handler；`pipeline.py` `_llama_lock` | 当前负载可接受；观测消费增加后有尾部延迟风险 | 中期：观测端点只读快照化（预聚合，不实时扫 jsonl） |
| L-3 | P3 | 8 字符 session key 截断不对称：`manage.sh route-force-*` 传参不截断、内部截断，仅文档化未修行为 | `anthropic_proxy.py:423,550`；diagnostics 设计文档 :302 | 契约的坑留给所有消费方 | 服务方统一截断口径或 manage.sh 侧截断 |
| L-4 | P3 | star import 网 + stage 编号漂移：`anthropic_proxy.py` 对 10+ 模块 `from X import *`；"21 层"历史层号 vs 实际 24 个 stage | `anthropic_proxy.py:19-372`；`pipeline.py` 注释 | 命名空间耦合、文档/代码一致性靠约定维持 | 长期重构项，不阻塞功能 |
| L-5 | P3 | 后端怪癖传导无法兜底：rapid-mlx 忽略 `max_tokens`、无 timings（hit_ratio 数据源缺失）、`--gpu-memory-utilization` 软限制 | 仓库 AGENTS.md §8.7；diagnostics 设计 D9 | 消费侧 hit_ratio 指标当前无数据 | 上游 issue 或换 llama-server 后端；离线 cache_analyzer 兜底（已预留） |
| L-6 | known-issue | `/metrics/history?session=` 已承诺未生效（404） | 2026-08-19 实测；契约脚本 F6 用例 | 消费侧 session 时序对账暂不可用 | 服务方补齐 |

**代理侧优点（固化）**：stdlib-only 硬约束（零第三方 import 已核实）；控制面/数据面/观测面分离（watchdog 独立进程、诊断纯旁路）；SIGHUP 热重载 + `_RELOAD_SPEC` 注册制；models.json 目录坏文件拒绝热替换。

### 2.2 agent_go

| # | 严重度 | 发现 | 证据 | 影响 | 建议 |
|---|---|---|---|---|---|
| A-1 | P2 | `worker_backends` 职责漂移：③部署拓扑应归代理（模型实体三层设计），agent_go 侧配置与代理路由重复，已成漂移源 | `executor.py:2352-2370`（已有 deprecated warning） | 双写漂移：代理路由变了、agent_go 配置没跟上 → 计量/路由误判 | 按既定方向收敛为单值 `worker_base_url`，细粒度路由全部留代理。**✅ 已落地（2026-08-19）**：`config local` 模板不再生成 `worker_backends`；`diag.local_proxy_base_url`/`profiles.health_check`/`_profile_mode`/web 启动前探测全部改为 `worker_base_url` 优先、`worker_backends` 仅 deprecated 兼容读取 |
| A-2 | P3 | 仍解析 HTML `/status` 探测真实模型名——R1 已交付 `/api/status` JSON，契约迁移残留尾巴 | `executor.py:76-121` `_probe_local_model` | 脆契约依赖（HTML 结构变更即破） | 切换到 `/api/status` 的 `backend.model_name`，HTML 解析降级保留一个版本周期。**✅ 已落地（2026-08-19）**：`_probe_local_model` 先走 `/api/status` JSON（fail-open），HTML 解析降级为兜底 |
| A-3 | P3 | 会话头契约知识本地实现（8 字符截断口径在 diag.py 注释），与 swe-eval 各自重复实现 | `agent_go/diag.py` | 契约演进时多处同步 | 见 X-2（契约单点化）。**✅ agent_go 侧已落地（2026-08-19）**：`diag.CONTRACT_API_VERSION = "2"` 显式标注，header 构造/截断口径注释指向契约文档为唯一权威；swe-eval 侧仍待 X-2 |

**agent_go 侧优点（固化）**：计量归因闭环（R8 头 → metering → eval → gate）；诊断消费全部 fail-open（diag.py 统一客户端）；契约测试脚本 21 用例覆盖 R1-R16；成本控制三层（判死机制默认关的审慎策略）。

### 2.3 swe-eval

| # | 严重度 | 发现 | 证据 | 影响 | 建议 |
|---|---|---|---|---|---|
| S-1 | **P1** | 流量自检建立在隐式脆契约上：正则解析代理日志三种文本行格式 + 日志绝对路径硬编码；日志是内部实现细节而非接口，格式变更将静默打破自检，且自检失败 `sys.exit` 中止整批 | `run_agent.py:427-486`（`:451-465` 正则）；`config/targets.yaml:15` 硬编码路径 | 代理侧任何日志格式调整都可能中断跨夜批跑 | 迁移到结构化接口：`/api/session/<key8>/metrics` + R8 头校验；日志正则降级为兜底 |
| S-2 | P1 | 无契约测试：对比 agent_go 的 21 用例契约脚本，swe-eval 侧空白 | 全仓零 HTTP 客户端调用代理管理面 | 代理升级无验收门禁 | 补契约检查脚本（可复用 agent_go F 组用例思路） |
| S-3 | P2 | 成本/usage 数据面未闭环：`usage`/`cost_cny`/`proxy_events` 字段定义了但恒为空 | `report.py:73-75` TODO；runs.jsonl 实测样例 | A/B 报告缺成本维度，模型选型结论不完整 | 复用 R8 头/sessions.jsonl 回填，report 增 $/resolve |
| S-4 | P3 | 无超时/无重试/串行：harness 健壮性完全外包给代理侧 `PROXY_BACKEND_TIMEOUT`，批跑靠 `caffeinate` 续命 | `run_agent.py:325`（无 timeout）、`:557`（串行 for） | 与 agent_go 的 run_timeout/stuck 检测/kill_reason 体系有明显能力落差 | 至少加 per-run timeout + 失败重试一次 |
| S-5 | P3 | 无依赖声明文件（无 requirements/pyproject），依赖靠 .venv 手装 | 仓库根 | 环境不可复现 | 补 requirements.txt |
| S-6 | 提示 | 防泄漏逻辑散布三处脚本，注释为事故驱动补丁层 | run_agent / gen_synthetic / verify_synthetic | 逻辑正确但难审计 | 整理为单一防泄漏清单文档 |

**swe-eval 侧优点（固化）**：拆库边界决策（依赖/产物/通用性/生命周期四理由）；防泄漏设计（浅克隆切对象库、orphan 合成任务、gold_identity 污染检测）；junitxml 逐条判定「宁假 FAIL 不假 PASS」的保守口径；断点续跑与原子写。

### 2.4 跨项目契约问题

| # | 严重度 | 发现 | 影响 | 建议 |
|---|---|---|---|---|
| X-1 | P1 | 两个消费方与代理的契约保障不对称：agent_go 有契约文档+契约脚本+fail-open 探测；swe-eval 全部裸奔 | 代理演进时 swe-eval 无预警 | S-1/S-2 即修复路径 |
| X-2 | P2 | 契约知识三处分散（llama.cpp docs、agent_go diag.py、swe-eval targets.yaml 注释），header 构造/截断口径两处重复实现 | 契约演进多处同步、易漏 | 契约文档为唯一权威并带 `api_version`；两个消费方 CI 各跑契约测试；header 构造显式标注契约版本 |

## 3. 改进项汇总（按优先级）

| 优先级 | 项目 | 事项 |
|---|---|---|
| P1 | swe-eval | S-1 流量自检迁移结构化接口 + S-2 补契约测试（X-1 同步解决） |
| P2 | llama-defender | L-6 `/metrics/history?session=` 补齐（已承诺） |
| P2 | swe-eval | S-3 成本回填（$/resolve 进报告） |
| ~~P2~~ | agent_go | ~~A-1 收敛 `worker_backends` 为单值 `worker_base_url`~~ **✅ 已落地（2026-08-19）** |
| P2 | 跨项目 | X-2 契约单点化（api_version + 双侧契约测试） |
| P2 | llama-defender | L-1 token 真实值优先 + 口径标注 |
| ~~P3~~ | agent_go | ~~A-2 `/status` HTML 解析切换 `/api/status` JSON~~ **✅ 已落地（2026-08-19）**；A-3 agent_go 侧契约版本标注同步完成 |
| P3 | swe-eval | S-4 per-run timeout/重试；S-5 requirements.txt |
| P3 | llama-defender | L-3 截断不对称修复；L-2 观测端点快照化（中期） |
