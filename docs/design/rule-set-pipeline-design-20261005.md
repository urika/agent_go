# 规则集管线概念设计（rule-set pipeline）

> 状态：**概念设计 v0.5（2026-10-05；P0 已落地（离线，授权执行）；P1 影子/生效仍待立项——随 O-12/O-14，**ADR-014 已起草（Proposed）**；S-1 P0 已完成、下一步 A/B（§10/§10.1））**
> 上位依据：jev 试点需求文档 [§9.4／§18](../design/jev-review-triage-pilot-requirements-20261005.md)（数据闭环与规则收敛）；范式来源＝[ADR-012 Spec-to-Test 验收测试管线](adr/ADR-012-spec-to-test-pipeline.md)（已 Accepted、四条护栏）；目标回路＝`roadmap` §H3 自进化（规则迭代回路，2026-10-05 登记）
> 来源分级：`[实测]` 仓库/实验证据 ｜ `[分析]` 推演 ｜ `[设计]` 本件规定

## 0. 一句话

把"从 jev 决策收敛到规则"做成一条**可执行管线**：证据驱动生成规则候选 → 人审冻结（sha256）→ 影子重放（只记不动）→ 验证后 opt-in 生效；**规则是可执行工件、能进 runtime 决策，jev 只是指出规则缺口的探针**——这条管线就是"自动化水平真正提升"的落点。

## 1. 实证前提（[实测]，2026-10-05）

正在运行的 swe-eval 实验 `exp-20260930-splash-efficacy-r2`（3 臂 × 12 实例 × 2 重复 = 72 runs，交错；**已判定 30 runs / 11 实例，未跑完**）：

| 臂 | resolved | 配对（同实例同重复） |
|---|---|---|
| **arm_tdd**（验收测试契约） | **7/10** | 对 plain：**5 胜 4 平 0 负**；对 nudge：**5 胜 5 平 0 负** |
| arm_nudge | 2/10 | — |
| arm_plain | 1/10 | — |

机制（才是可迁移的部分）：**AI 生成 → 人审冻结 → worker 只读 → verify 重放**的结构化契约，配四条护栏，显著改变结果。规则集照同一骨架建（逐项同构见 §3），差别只有一处：契约内容从"验收测试"换成"分流/判定规则"。

## 2. 目的与范围

- **做**：规则的生成、人审、冻结、影子重放、验证晋升、退役与溯源；与试点 `--analyze` 的 `rule_candidates` 对接。
- **不做**：不改 jev 的使用半径（CON-1）；不自动改规则/不自动生效；不引入外部规则引擎依赖；不替换现有硬编码规则（影子先行）；不在无标签样本上迭代。
- **边界**：P0 纯离线（只写 `~/.agent_go/rules/`，不接 runtime）；P1 影子接入 runtime 后置点（只测不动、fail-open）；**P2 生效前须有独立 ADR**（规则执行面＋全局规则数据面属边界变更）。

## 3. 总体设计：四步管线（与 spec-to-test 同构）

```
① 生成（离线，零外发）   ② 人审（复用既有确认渠道）
   ├ labels.jsonl（人工真值）      ├ CLI tty / web 卡片 / MCP 宿主代审
   ├ probe.jsonl（程序化真值）      └ confirmation 回执 + 原文保留
   ├ 弃权集（jev 只作"缺口指针"）
   └ 特征/阈值扫描 → rule_candidates.jsonl
                                  ↓
④ 迭代（复验→升版/退役）   ③ 冻结与重放
   ├ 新标签 → 留出复验（不得回退）   ├ rules.jsonl + frozen_sha256（执行前校验）
   ├ 升版 / retire 留档            ├ 影子：shadow 记录 rule_decisions.jsonl（只测不动）
   └ 扩张后重跑同一预注册判据        └ 生效（opt-in）：在指定 stage 执行
```

| spec-to-test（ADR-012，已跑通） | rule-set（本设计） |
|---|---|
| 验收测试＝冻结的可执行 oracle | 规则＝冻结的可执行分流条件（受限 DSL） |
| LLM 起草 per-task | 证据驱动生成（**禁读 jev 输出当标签**；jev 只提供缺口指针） |
| 人审冻结（`require_review`＋sha256 manifest） | 同构（复用确认渠道与回执形态） |
| worker 只读、verify 前恢复冻结版 | 规则只读、执行前校验 `frozen_sha256` |
| 可执行 oracle 优先于 LLM 语义评估 | **规则优先于 jev**（规则可进 verdict，jev 永不进——CON-1） |
| 出题人 ≠ 解题人 | 候选生成者 ≠ 验证者 ≠ 审批者 |
| 默认关 opt-in | 同（`rule_set.enabled=false`；影子 → 生效两态） |

## 4. 功能模块

```
agent_go/rule_set.py            # 新核心模块（stdlib；catalog 需更新）
  ├─ DSL        parse_condition(text)->AST / eval_condition(AST, state)->bool
  ├─ 清单       load_rules()/save_rules()/verify_frozen()/promote()/retire()
  ├─ 候选       import_candidates(rule_candidates.jsonl)
  └─ 执行       shadow_evaluate(stage, state) -> rule_decisions.jsonl（fail-open）
CLI             agent_go rules list|show|validate|promote|retire
对接             tools/jev_triage.py --analyze 增出 rule_candidates.jsonl
```

**受限 DSL**（`[设计]`）：`feature op literal` 的布尔组合（`and/or/not` ＋ `== != >= <= > <`），`feature` 限 state 白名单键；AST 解析（**禁 `eval`**）、纯函数、无副作用、未知字段/类型不匹配 ⇒ 该规则判"不可用"（fail-open，不误伤）。

## 5. 数据契约（`[设计]`）

| 文件 | 内容 | 关键不变式 |
|---|---|---|
| `~/.agent_go/rules/rules.jsonl` | 规则清单：`rule_id／version／status(candidate｜shadow｜active｜retired)／stage(plan｜verify｜review)／condition(DSL)／cover{failure_class…}／evidence{source,refs[]}／metrics{precision,recall,n,holdout_sha}／frozen_sha256／created_by／reviewed_by／created_at` | 执行前校验 `frozen_sha256`；`status` 只经人审流转 |
| `<task_dir>/rule_decisions.jsonl` | 每次执行：`rule_id／version／stage／inputs(白名单字段)/result／ts` | 只记不动；影子期不改变任何既有输出 |
| `rule_candidates.jsonl`（试点产出） | `feature／op／threshold／direction／cover_n／precision／recall／evidence_refs` | 只读人工标签/探针；不含 jev choice 值 |

项目级规则覆盖全局（同 `rule_id` 取项目级；合并语义须在 ADR 固化）。

## 6. 四道闸（与需求文档 §9.4 一致）映射到实现

| 闸 | 实现点 |
|---|---|
| ① 标签源闸 | 候选生成器只读 `labels.jsonl`/`probe.jsonl`；**代码层断言不读 jev 输出** |
| ② 验证闸 | `promote` 前要求留出 ≥100 条带标签、跨批；回归集钉住"旧样本不得回退"。**实现（2026-10-05）**：`replay` 报告自带可复算 `holdout_sha`（覆盖样本 ref 集／标签分布／规则身份／逐规则与联合统计，剔时间戳），`promote --report` 时先复算校验再盖章 `metrics`，`regression_ok` 只由人 `--regression-ok` 背书——声明值由此可独立复算，不再依赖手填 |
| ③ 落地闸 | 规则＝DSL＋测试＋`rules.jsonl`（可解释/审计/回滚）；P2 走信任门＋边界 ADR |
| ④ 反哺计量闸 | 每次规则扩张后按同一预注册判据重度量（jev 增量区应收缩） |

## 7. 阶段与验收

| 阶段 | 内容 | 工作量 | 验收 | 边界 |
|---|---|---|---|---|
| **P0 离线** ✅ **已落地（2026-10-05；2026-10-05 补强）** | `agent_go/rule_set.py`（受限 DSL／清单／候选导入＋生成／离线复算／影子纯函数）＋`tests/test_rule_set.py` **41 例**；CLI＝`python3 -m agent_go.rule_set …`（`agent_go rules` 子命令随 P1 接入）；补强＝`replay` 产 `holdout_sha`＋`promote --report` 盖章校验、数值阈值候选每字段上限 20（超限取等距分位点）；文档同步＝module-catalog／spec.md | ~1–1.5 人日 | 同一规则在同一 state 上逐位可复算；零 runtime 接入；CI 全量回归绿（含仓库自带 lint 门） | 不触边界（纯本地文件；实现即验证：CLI 生成→导入→复算全链冒烟通过） |
| **P1 影子** | 在 review triage（或 plan 预检）后置影子评估，只写 `rule_decisions.jsonl`，输出"规则 vs 人标/jev"对照 | ~1–2 人日 | fail-open；不改变任何既有行为 | 接 runtime 后置点＝边界，**ADR 草案已出：[ADR-014](adr/ADR-014-rule-set-execution-plane.md)（Proposed，P1 前须 Accepted）** |
| **P2 生效** | 验证过的规则 opt-in 生效；若参与 verdict（如 plan gate）单独评审 | 另评 | 信任门达成 | 独立 ADR |

## 8. 风险与反指标

| 风险 | 反指标（出现即停/降级） |
|---|---|
| 规则过拟合标签集 | 留出集 precision 崩、跨批不稳 |
| 影子数据被误用为 verdict | `rule_decisions` 被任何判定读取 |
| 规则膨胀/维护成本 | 活跃规则数持续增长而无退役 |
| 与人审冲突 | 规则结论与人工标签系统性背离 |
| 知识库污染 | jev 输出被写入 rules/知识库（标签源闸被绕过） |

## 9. 开放事项

| ID | 事项 | 归属 |
|---|---|---|
| R-1 | ~~新 ADR 号与内容~~ **已起草（[ADR-014](adr/ADR-014-rule-set-execution-plane.md)，Proposed）**：执行面＋全局数据面＋两态语义＋回滚，待批 | 架构 |
| R-2 | 数据面位置（全局 `~/.agent_go/rules/` vs 项目级）与合并语义 | 架构/PM |
| R-3 | DSL 边界（是否允许滑动窗口/计数聚合；建议首版只做瞬时字段比较） | 执行 owner |
| R-4 | 与 `failure_class`／`problems.jsonl`／Skill 库的 id 关联（知识库 L0 清单） | 执行 owner |
| R-5 | 立项时机（Go/Conditional 后，与 O-12 同批） | PM |
| R-6 | S-1（spec 覆盖扫描→TDD 输入，§10）的 ADR 与留出表前置；A/B 的"同测试预算"口径 | PM／架构 |

## 10. 延伸落点：spec 覆盖扫描 → TDD 输入（S-1，2026-10-05 登记）

> 把覆盖/风险度量从"失败后复核"**前移到开发前**：对新 spec／修复任务先做"规则+jev"覆盖扫描，用**缺口驱动** ADR-012 的验收测试起草。

**可测性两分法**（这是本落点成立的前提）：

| 量 | 新 case 当下 | 说明 |
|---|---|---|
| 规则命中／jev 非弃权／证据完整度 | ✅ **前瞻可测** | 不需要真值 |
| 有效性（判得对否） | ❌ 只能事后 | 等 case 结果回填 `outcomes.jsonl`（F14） |
| 格位风险估计 | ⚠️ 有条件 | 落到历史格 → 取留出集该格的错误率；**留出表 ≥100 标签前显示"待建"** |

**五步流程**：①建 state（spec/plan＋结构化规则结果，按**维度**组织）→ ②规则扫描（逐维度出命中/未命中）→ ③jev 扫描（B 情境、建议性、不进 verdict）→ ④落格＋取证（四格＋证据完整度＋格错误率*）→ ⑤产出机读 `spec_coverage.json` ＋人读 `spec-review.md`（样例见需求文档 §18）。

**缺口 → TDD 靶的映射（本落点的核心）**：

| 格位 | 语义 | 动作 |
|---|---|---|
| **J-only**（规则判不了、jev 能判） | 该维度的标准只存在于隐性判断里 | **必须落成显式可执行测试**（TDD 输入） |
| **双缺**（都判不了） | 信息不足以定位 | 先补证据/人工裁决，**不进入开发** |
| R+J 低风险 | 双重覆盖且历史风险低 | 沿用既有规则/测试，不重复生成 |
| R-only | 规则已判 | 生成**回归测试**钉住该维度（防规则失效） |

草稿仍走 **ADR-012 四护栏**（AI 起草 → 人审冻结 sha256 → worker 只读 → verify 重放）——**只换起草输入**（从任务描述换成缺口维度清单）。

**自证（A/B）**：覆盖驱动生成 vs 均匀生成；在**同一测试预算**下比 resolved 率／首过率／返工次数／缺陷逃逸；配对＋McNemar＋实例聚类 bootstrap（仓库既有 `compare_models_paired.py` 口径）。

**边界（四条）**：①不混入已冻结的失败复核试点——spec/plan **全文**外发面远大于现试点（B 情境＋逐次自评、闭网批禁用），判据须独立预注册；②jev 仍建议性（CON-1），覆盖单不得成准入 verdict（"双缺阻断"须经人或既有规则门确认）；③留出表未就绪只报覆盖/证据完整度；④case 结果必须回填 `outcomes.jsonl`，否则闭环断在有效性一侧。

**前置与阶段**：前置＝留出表 ≥100 标签＋新 ADR（spec 全文外发面）；**P0 离线自证已完成**（见下：靶改用 `warning` 群体；下一步＝A/B）。

**P0 首跑已完成（2026-10-05）**：[s1-coverage-audit-findings-20261005.md](s1-coverage-audit-findings-20261005.md)＋仪器 `tools/s1_coverage_audit.py`（9 例测试）。结论（**v0.3 修正**）：缺口效应经**同批同模型对照**后**不成立**（`results.jsonl`×sonnet：29% vs 29%，OR=0.99，p=0.87；缺口样本 98% 来自旧批缺 telemetry ⇒ 读作**仪表缺口**，不作风险信号）；**成立的是 `warning vs passed` 的区分度**（同批同模型内 sonnet +12pp、deepseek +37pp）——**S-1 的靶改用"warning（规则不确定）群体"**。两伪迹定案；`plan_acceptance_coverage` 等验收覆盖字段**从未产出**。**注意**：这验证的是**前提**；S-1 **方法本身**（缺口→TDD 输入→结果改善）仍待 A/B。

---

### 10.1 S-1 A/B 预注册（草案，2026-10-05；执行前置见下）

**问题**：覆盖驱动生成的 TDD 输入（缺口清单 → ADR-012 起草），在**同一测试预算**下是否优于均匀生成？

| 项 | 内容 |
|---|---|
| 臂 A（覆盖驱动） | `tools/s1_spec_scan.py` 的 `gap_dimensions` 作为起草输入 |
| 臂 B（均匀生成） | 同管线、同预算（测试条数上限＝A 臂实际产出条数），输入仅任务描述 |
| 设计 | 配对（同任务两臂）＋交错执行（沿用 `exp-20260930` 口径）；n≥12 对（方向性；不足如实报"不可判"） |
| 指标 | 主＝resolved 率；辅＝首过率、返工次数、缺陷逃逸、测试条数与成本（**预算守恒校验**） |
| 统计 | McNemar（resolved 二值，配对）＋实例聚类 bootstrap（仓库既有 `compare_models_paired.py` 口径） |
| 预算 | 24 次任务执行（≈$0.3–1，delivery-20260820 口径）＋人审冻结 1–2 人时 |
| **Kill（预注册）** | A 不优于 B（p>0.05 且点估不优）⇒ **方法死**，S-1 只保留"覆盖扫描作诊断报表"；A 优但成本超 10% ⇒ Conditional |

**执行前置**：① 覆盖扫描器 ✅（`tools/s1_spec_scan.py`，7 例，本日落地）；② 起草端接入"缺口清单"输入 ✅（**2026-10-05 落地**：`--spec-gaps <缺口清单>`／`spec_test.gaps_file` → `load_coverage_gaps` 注入起草 prompt 的"规则覆盖缺口（注意力分配）"段；`gaps_max_items=12` 控体量；缺失/无缺口/不可解析 → None（fail-open，不阻塞主链）；草稿与冻结 manifest 记 `coverage_gaps{count,sha256}` 作 A/B 臂标记）；③ ADR-012 管线 ✅（opt-in 已存在）；④ **环境空闲**（代理/LLM 当前被 swe-eval 占用）——**唯一硬阻塞**。

**当前状态**：A/B 暂不可跑（前置②已完成，剩环境）。

**姊妹 P0（并行会话已落地）**：`agent_go/rule_set.py`（规则清单／受限 DSL／候选生成／离线 replay＋`holdout_sha` 盖章，41 例）——S-1 的"规则侧"与"草稿侧"由此各有一件离线仪器。

---

## 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.7 | 2026-10-05 | **S-1 前置②落地（起草端接入缺口清单）**：`spec_test.load_coverage_gaps`（解析扫描报告/裸列表；缺失/无缺口 → None，fail-open）＋`draft_acceptance(coverage_gaps=…)`（或配置 `spec_test.gaps_file` 自动加载）注入"规则覆盖缺口（注意力分配）"提示段；`--spec-gaps PATH`（run/resume）与 `gaps_max_items`（默认 12）接入；草稿与冻结 manifest 增 `coverage_gaps{count,sha256}` 臂标记（A/B 归因用）；**边界不变**：仅建议性输入，安全门（`_is_safe_verification_command`/路径白名单）与人审门（ADR-012 四护栏）均不改；§10.1 前置②标 ✅。 |
| v0.6 | 2026-10-05 | **P0 补强（离线；不接 runtime）**：①验证闸可复算化——`replay_report` 增 `sample_refs`／`holdout_sha`（纯函数 `holdout_sha_of` 可独立复算，剔除时间戳），`promote_rule(..., report=…)` 与 CLI `promote --report [--regression-ok]` 先校验报告自洽（sha 复算、rule_id/version/frozen_ok 对齐）再盖章 `metrics{holdout_n,holdout_sha,precision,recall,regression_ok}`；②候选生成规模化——数值阈值候选改为分位点采样（`NUMERIC_THRESHOLD_CAP=20`，含两端），消除 O(字段×取值数×样本数)；③§6 闸②、§7 P0 行同步（41 例）。 |
| v0.5 | 2026-10-05 | P1 门就绪：**新增 [ADR-014](adr/ADR-014-rule-set-execution-plane.md)（Proposed）**——规则执行面＋全局规则数据面（`~/.agent_go/rules/`、`<task_dir>/rule_decisions.jsonl`）＋shadow→active 两态＋fail-open/只读无副作用＋回滚；§7 P1 行与 §9 R-1 更新为"ADR 草案已出"；S-1 前置行更新为"P0 已完成"；状态行同步。 |
| v0.4 | 2026-10-05 | S-1 P0 仪器落地：`tools/s1_spec_scan.py`（计划/验收面覆盖扫描，缺口清单输出，7 例测试）；`planning.validate_plan_quality` 增**结构性验收覆盖回退口径**＋`plan_coverage_basis` 字段（修复 `plan_acceptance_coverage` 恒空——S-1 缺口映射的依赖项；`cli.py`/`bench.py` 同步透传）；§10.1 增 **S-1 A/B 预注册**（臂/设计/指标/预算/kill/前置；唯一硬阻塞＝环境占用）。 |
| v0.3 | 2026-10-05 | **P0 落地（离线，授权执行）**：`agent_go/rule_set.py`（受限 DSL／AST 三值求值／清单 frozen_sha256／promote 验证闸 holdout≥100／候选导入＋单特征生成／离线复算报告／影子纯函数）＋`tests/test_rule_set.py` 37 例；CLI `python3 -m agent_go.rule_set`；§7 P0 行标注已落地；同步 module-catalog／spec.md。仍零 runtime 接入。 |
| v0.2 | 2026-10-05 | 增 **§10 延伸落点 S-1：spec 覆盖扫描 → TDD 输入**（前瞻/回溯两分法、五步流程、缺口→TDD 靶映射、A/B 自证、四条边界、前置与 P0 离线自证）；§9 增 R-6。 |
| v0.1 | 2026-10-05 | 初稿：以运行中的 tdd 臂（`exp-20260930-splash-efficacy-r2`，interim 7/10 vs 2/10 vs 1/10、配对零负）为范式，给出规则集管线四步同构设计、受限 DSL、数据契约、四闸映射（§9.4）、P0–P2 阶段与风险。 |
