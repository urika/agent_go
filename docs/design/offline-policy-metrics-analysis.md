# 策略与规则的离线度量分析（基于日志的策略优化）

> 日期: 2026-08-29
> 范围: llama-defender 代理路由/截断规则 + agent_go worker 路由/验证策略
> 结论摘要: **可行**。代理 `archive/` 已持久化每轮完整 payload，除报表类分析外，连反事实实验（含 H_BE 信念熵回放）都能离线完成，无需在线插桩。

## 1. 背景与问题

代理侧有大量靠实测标定的策略参数（`PROXY_ROUTE_THRESHOLD_CHARS=80000`、`threshold_factor`、fifo 400K chars 截断、KEEP_HEAD/TAIL、熔断 cooldown 300s 等），agent_go 侧有难度路由、验证重试、成本三层控制。这些参数当前的标定方式是「事故驱动 + 手工拍定」。本文回答：能否离线根据已有日志计算策略度量，用于离线优化策略与规则。

## 2. 数据资产盘点（2026-08-29 实测确认）

### 2.1 代理侧（`~/APP/llama.cpp/logs/diag/`）

| 文件 | 粒度 | 关键字段 |
|------|------|----------|
| `sessions.jsonl` | 每请求一行 | `route_target`(local/cloud)、`ttft_ms`、`gen_ms`、`hit_ratio`(prefix cache 命中)、`prompt_sent_tokens`/`prompt_processed_tokens`、`payload_truncated`、`compression`、`epoch_triggered`、`actual_model` |
| `archive/<sid>.jsonl` | 每轮 | `view=sent` 的**完整 payload（全部 messages）**、`route_target`、chars、`payload_truncated` |
| `ledger/<sid>.jsonl` | 每轮 | 工具调用动作序列（tool/target/result_chars/mismatch/dup 派生） |

约束：archive/ledger 受 MB 上限 + TTL 约束（`session_ledger.py` 开头注释），历史窗口有限，重要批次需另存。

### 2.2 agent_go 侧（`~/.agent_go/`）

- `task-*/metering.jsonl`：role/tokens/cost/latency/result/actual_model
- `task-*/meta.json`：任务成败状态（8 态状态机）
- `problems.jsonl`、`deviation.jsonl`、`assessment.jsonl`
- `eval_suite/results_*.jsonl`：bench 口径（已有 eval gate / metric-freeze / evidence 层基础设施）

## 3. 分析能力分层

### 3.1 第一层：纯日志可查（无需回放）

| 度量 | 数据来源 | 用途 |
|------|----------|------|
| 路由阈值反事实分布 | sessions.jsonl 的 chars + route_target | 离线重放「阈值 80K→X」的分流变化，结合 ttft/duration 估成本收益 |
| 截断代价量化 | `payload_truncated` × `hit_ratio` × `ttft_ms` | 验证 fifo 400K 标定；量化「截断→前缀击穿→冷 prefill」代价 |
| 难度路由有效性 | metering 按 difficulty × actual_model 聚合 pass_rate/$/latency | 扩展现有 eval gate 到真实任务口径 |
| 熔断参数评估 | cloud 失败序列 + cooldown 窗口 | cooldown 300s 保守/激进判断 |

### 3.2 第二层：需回放、但回放可行（本文增量）

archive 留存完整 payload ⇒ **反事实实验可行**：

- **离线 H_BE（信念熵）曲线**：取 session 每轮 messages 前缀 + 双探针锚定问题（「当前任务进度？还需什么信息？」），直连后端重算每轮截断熵。用途：标定截断阈值、重试 vs replan 判据。**不需要在线探针**。
- **反事实截断策略对比**：同一完整 payload 分别应用 fifo / 语义压缩 / 不同 KEEP 参数，比较 ΔH_BE。
- 回放规范：temp=0 保可复现；9B 上 135K chars 上下文约 100s 冷 prefill，批量回放须控规模。

### 3.3 前置能力验证（2026-08-29 已实测）

- rapid-mlx 后端 `logprobs: true` + `top_logprobs`（上限 20）正常返回逐 token 分布；top-20 概率质量覆盖 92%–100%，截断熵 H(top20) 可作 H_BE 工程代理（口径一致的相对值即够用）。
- **代理 :4000 会剥掉 logprobs**（实测同请求 `logprobs: None`）——熵类测量必须绕过代理直连后端（:8081/:8084 OpenAI 端点），须做成独立组件，配置解决不了。
- llama.cpp server 原生支持 logprobs（`n_probs`），未实测（GGUF 路径已拆除）。

## 4. 限制与风险

1. **logprobs 不随请求落盘**——熵类指标只能回放重算，属「离线作业」而非「报表」。
2. **outcome 标签需 join**：代理 session_key 与 agent_go task_id 是两个命名空间，跨库关联靠时间戳或显式埋点。规模化前建议在 worker 启动时把 task_id 注入请求头。
3. **H_BE 区分度须在自家数据重验**：论文 Pearson r=−0.684 是别人的分布，必须用 3–5 个真实 session 的回放数据验证「熵曲线 vs 任务成败」的区分度后才可用于规则优化。
4. 全部原料在本机，数据安全口径下无合规障碍。

## 5. 落地路径（优先级排序）

1. 第一层聚合脚本（`tools/`）：读 sessions.jsonl + metering.jsonl 出路由/截断现状基线报表。
2. 第二层 H_BE 回放验证：挑 3–5 个已完成真实 session，验证熵曲线区分度。
3. 两项都成立后，再把指标接入规则离线优化闭环（阈值搜索、策略 A/B）。

### 5.1 实施状态（2026-08-29）

**H_BE shadow 探针已上线**（llama-defender 侧，`hbe_probe.py`，只测不动）：成功的本地响应后搭 prefix cache 便车追加双探针锚定提问，top_logprobs=20 截断熵落盘 `logs/diag/hbe.jsonl`（schema v1：h_mean_bits/h_max_bits/coverage_mean/n_tokens/probe_latency_ms + session_key/turn/request_id 关联键）。默认关，`PROXY_HBE_*` 7 个变量均注册进 CONFIG_REGISTRY 且 SIGHUP 可热开；当前 active.conf 以 `SAMPLE_EVERY=1` 标定档运行（验证后应调回 4）。冒烟实测：16K tokens 上下文探针 1.2s（cache 便车成立），覆盖率 0.97。单测 14 例 + 全量 1220 例通过。实施中确认的部署陷阱：active.conf 裸赋值（无 export）不进启动环境、仅 SIGHUP reload 解析。下一步即第 2 步——积累真实 session 样本后做熵曲线 vs 成败区分度验证。

### 5.2 首日数据分析结论（2026-08-30，319 条 / 30 session）

- **语义效度成立**：回答语义分组与 H 强相关（顺利 0.60 / 中性 0.74 / 故障 0.97 bits）；高熵个案复盘确认探针能精确定位工具失败、约束校验失败等真实受阻决策点（案例：s38d79db t5 熵峰 1.545 精确落在 Write 失败后，t6 恢复骤降 0.43）。
- **与规模解耦**：H 与 turn/payload_chars/prompt_tokens Spearman |ρ|<0.05——H_BE 不是上下文长度的代理，这正是它的信号价值。
- **79% 方差在会话内**——主要是逐轮动态指标；H_max/H_mean 中位 4.0，不确定性集中在少数「分叉 token」。
- **信噪比约 50%**（top-6 高熵：3 真受阻 / 1 冷启动 / 1 路径发散假阳性 / 1 待查）——够格当分诊器，不够格当自动触发器。
- **已知伪迹与对策**：工具调用型回答（2.2%，H 虚低）按 answer_preview 前缀过滤；冷启动伪高熵（turn≤2）待加 turn≥3 门槛。
- **数据面缺口**：hit_ratio 全空（rapid-mlx 不上报 timings）；session_key ↔ agent_go task_id 无 join 键；当日无截断事件（缺阳性对照）。

> ⚠️ **§5.2 解读纪律修订（2026-08-30，双轴前提）**：本节所有「低熵 = 健康」的解读降级为「低熵 = 锐度高，校准未知」。熵曲线平稳只证明模型没慌，不能证明没有失忆——「一致地错」是熵探针的固有盲区（见 §5.4）。

### 5.3 H_BE 区分度验证方案（2026-08-30 定稿）

**定位**：从「分诊器」（人工复核的注意力分配）到「决策器」（自动触发 replan/重试判据）之间的唯一放行门。三个决策共同依赖它：(a) 是否挂进验证循环；(b) 阈值定多少；(c) 探针持续成本的存废。

**必要性**：不验证的默认路径是自动化偏误——50% 信噪比的信号被当成 90% 用，误报直接转化为 replan 成本和执行路径偏移；证据不足的决策器比没有决策器更危险（虚假安全感）。

**假设（可证伪）**：
- H1（结局区分）：失败 session 后期熵（后 1/3 轮次中位数）显著高于成功 session
- H2（模式区分）：「H≥阈值 且持续 ≥3 轮」对失败有预测力；「单轮脉冲」无预测力
- H3（干预有效，下一阶段）：按熵判据触发 replan 的期望成本 < 不触发的期望浪费

**标签来源（三通道互补）**：
- A（主力）：swe-eval 任务客观判分——当日 session 全是 swe-eval 夹具任务，产出可机器验证（stats.json 数值、写作约束校验），判分脚本 ≈ 0.5 人日
- B：agent_go meta.json 状态——需先在 worker 启动时把 task_id 注入请求头打通 join（代理 + agent_go 各一处小改）
- C（阳性对照）：主动构造失忆——超小 PROXY_CTX_CHARS_LIMIT 跑 3–5 个任务，验证信号对截断型失忆的敏感性（其最初设计目的）

**流程（防自欺的关键在顺序）**：① 冻结假设与判据 → ② 盲标结局（不看熵曲线）→ ③ 揭盲算统计量 → ④ 定阈值出决策表（各阈值召回/误报率，粗 ROC）→ ⑤ 前瞻复验（下一周期新数据重算）。

**统计与样本量**：Mann-Whitney U + rank-biserial 效应量（小样本适用）；目标 ≥40 session、失败 ≥12 例，低于此结论仅写「探索性」。现有 30 session 待标 + 通道 C 补 3–5 例，缺口不大。

**成本**：总计约 1.5 人日（判分脚本 0.5 + 盲标复核 0.5 + 统计决策表 0.5）；无新建基础设施。

**风险与对策**：失败样本不足 → 通道 C 主动构造 + 延长收集窗；标签歧义 → 三级标签（成功/部分/失败），部分组单列；单模型单引擎 → 结论限定 Ornith-35B 口径不外推；语义伪迹 → 沿用 §5.2 过滤规则。

**放行/止损判据**：H1+H2 通过且阈值决策表误报率可接受 → 升级为验证循环信号（进入 H3 阶段）；不通过 → 探针降级为纯观测仪表盘或关停，不做任何自动化挂接。

### 5.4 方向性约束：双轴度量（锐度 × 校准，2026-08-30 追加）

**前提（防研究方向性错误）**：「改善熵」不是把熵压低——低熵可能是「清晰地错」（校准陷阱，MMPO 综述 §6 明确盲区，熵探针测不出「一致地错」）。研究目标是**清晰且准确**的信念，因此每个环节都必须双轴度量：

- **锐度轴（已有）**：`H_BE`——模型「自认为清楚」的程度（top-k 截断熵，hbe_probe schema v2 在采）。
- **校准轴（待实现）**：`D_ledger`——信念与事实的可验证差。IFC 设计（`llama.cpp/docs/02-architecture-design/information-fidelity-control-design-20260829.md` R9.2/§4.1）已定义未实现：锚定问题半抽取化（Q2「列出已读取/修改过的关键文件」），答案路径集合与 R14 台账 materials 对账，`D_ledger = 1 − recall(答案路径集, 台账句柄集)`。**coding agent 场景的结构性优势：台账即 ground truth，精度轴不需要外部标注**。

**对本方案的修订**：

1. **度量纪律**：hbe 记录扩为 `{H_BE, D_ledger}` 分量分开上报，**永不合成单一分数**（IFC 原文：避免过早加权固化）。任何未来门控输入 D_ledger 优先于熵；熵仅作无 ground truth 问题（Q3 类）的趋势量。
2. **验证方案修订（对 §5.3）**：H1/H2 必须双轴评估（结局组间差异须同时在熵和 D_ledger 上检验）；放行判据并入 IFC 效度关卡——**≥200 ILE 上 |ρ|≥0.4 或 AUC≥0.7，不达标止步于度量**。D_ledger 顺带提供一条不依赖外部成败标签的效度检验路径，缓解 §5.3 的金标签瓶颈。
3. **解读纪律**：「低熵 = 健康」一律改读为「锐度高，校准未知」（§5.2 已加注）。首日数据中的工具调用型低熵回答（H≈0.05）即「一致地错」的雏形——没有精度轴会被误判为信念清晰。
4. **与 IFC 的口径差异（须知晓）**：IFC 设计探针走云端 only（不占本地并发槽）；我们因数据安全口径走纯本地，探针占用本地引擎槽位（成本面已在 §3.3/§5.1 讨论）。Q2 锚定问题的引入会改变探针 prompt——与既有 337 条记录的可比性按 `completion_budget` 时代字段同款机制处理（schema 版本+问题版本回显）。

## 6. 参考

- 概念框架: MMPO / 信念熵综述（`mem_sys/local-wiki/wiki/syntheses/mmpo-belief-entropy-review.md`）——作为问题诊断词汇表（belief deviation / BTR）可用，训练管线与 1.75M token 成绩不可外推。
- IFC 设计（精度轴来源）: `~/APP/llama.cpp/docs/02-architecture-design/information-fidelity-control-design-20260829.md`（R9 信息保真控制：三层传感器 + D_ledger 对账审计 + 效度关卡）。
- 代理配置标定史: `~/APP/llama.cpp/configs/active.conf` 注释（fifo 失忆事故链、OOM 墙标定）。
