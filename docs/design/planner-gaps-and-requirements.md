# Planner 架构分解能力：问题记录与产品需求

> **版本**：v1.0
>
> **目的**：记录 Planner 在架构驱动分解、模块边界识别、接口契约传递等方面的能力缺口，明确产品需求，为后续迭代提供决策依据。
>
> **来源**：2026-08-01 PM 工作会话——基于当前代码分析 + Bench v1 数据，识别 Planner 能力边界。
>
> **日期**：2026-08-01

---

## 一、问题总览

当前 Planner 是为「单任务分解」设计的。输入一句话 prompt → 输出 2-5 个技术层步骤（模型 → API → 测试）。

我们需要的场景是「架构驱动的多模块分解」：输入设计文档 + Task Spec → 输出模块边界清晰的步骤 → 每个步骤携带架构约束 → 接口先行、实现随后、集成收尾。

**核心矛盾：Planner 会拆任务，但不会拆架构。**

---

## 二、问题清单

### P0-1：Planner 不沿模块边界分解

**现象**：

```
当前分解（按技术层）：           需要的分解（按模块边界）：
Step 1: 模型（跨 Transfer + User）  Step 1: Transfer 模块接口骨架
Step 2: API                        Step 2: Account 模块余额变更
Step 3: 测试                        Step 3: Audit 模块转账日志
                                   Step 4: API 集成
                                   Step 5: 端到端测试
```

**根因**：system prompt 要求「2-5 steps, independently executable」，但没有模块边界分解指令。Planner 默认按技术层切分（模型层 → API 层 → 测试层）。

**影响**：
- 单 step 跨越多个模块时，Worker 的窄化上下文包含非本模块的代码，增加出错概率
- bench v1 数据中「数据库优化」标签的 $/pass 最高（$1.79），部分原因是跨模块改动被塞进一个 step
- 代码审查时无法按模块分别 review

**需求**：当 Task Spec 或设计文档中划分了模块边界时，Planner 的 steps 应当沿模块边界分解。

---

### P0-2：Planner 不生成接口骨架先行

**现象**：

```
当前：Step 1 完整实现 → Step 2 完整实现 → Step 3 测试
需要：Step 1 定义接口（stub + docstring）
      Step 2 基于接口实现（与 Step 3 并行）
      Step 3 基于接口实现（与 Step 2 并行）
      Step 4 填充实现
      Step 5 集成测试
```

**根因**：`dependencies` 字段表达的是「B 等 A 完成」，不是「B 依赖 A 的接口（不依赖 A 的实现）」。system prompt 没有「接口先行」的指令。

**影响**：
- 可并行执行的 step 被串行化，总耗时增加
- 如果上游实现有 bug，下游基于错误实现开发，修复成本翻倍
- bench v1 中 django-blog 任务耗时 900-1800s，部分原因是串行执行而非接口先行 + 并行

**需求**：当 step B 仅依赖 step A 的接口（函数签名、类型定义）时，step A 应先生成接口骨架并 commit，使 B 可以并行启动。

---

### P0-3：Planner 不将架构约束传递到 agent_prompt

**现象**：

```json
// 当前产生的 agent_prompt（差）：
"agent_prompt": "创建转账数据模型"

// 需要产生的 agent_prompt（好）：
"agent_prompt": "创建 Transfer 模型。约束: 1) amount 用 DecimalField(max_digits=18,decimal_places=2)
                2) from_user/to_user 用 ForeignKey(to='User',on_delete=PROTECT)
                3) 不在此模型中添加业务逻辑方法"
```

**根因**：`agent_prompt` 字段存在，但 system prompt 没有明确要求 Planner 将设计文档和 Task Spec 中的约束逐条传递到 agent_prompt。Planner 自由发挥，方差大。

**影响**：
- Worker 不知道约束 → 写出的代码违反约束 → 验证失败 → 重试 → 成本膨胀
- bench v1 中 fp-sandbox 任务 100% verify_ok 但 semantic evaluator 判定失败——部分原因是 Worker 不知道业务规则

**需求**：当 `--spec` 或 `--docs` 提供了约束时，Planner 必须将相关约束逐条写入对应 step 的 agent_prompt。

---

### P0-4：Planner 不主动从设计文档中提取结构化信息

**现象**：`--docs` 将设计文档全文注入 user_content，但 Planner 没有被明确指令如何使用它。3000 字设计文档进入 prompt，Planner 可能读了，也可能被 token 截断忽略了。

**根因**：system prompt 没有「当你看到设计文档时，请提取以下结构化信息」的指令。

**影响**：
- 写好的设计文档可能没有被 Planner 有效使用
- 设计文档的价值依赖于 Planner 是否能「主动」从中提取关键信息
- bench 无法区分「设计文档无效」和「Planner 没读设计文档」

**需求**：Planner 在检测到 `--docs` 或 `--spec` 时，应主动提取模块边界、接口契约、约束条件，并在 Plan 中体现。

---

### P1-1：缺少 Pre-Plan 架构分析阶段

**现象**：设计文档 + Task Spec → 直接进入 Plan 生成。Planner 在同一个 LLM 调用中既要理解全局架构，又要分解具体步骤。信息过载。

**根因**：架构理解（系统级、需要全局视角）和步骤分解（任务级、需要具体指令）是两种不同的认知任务，放在同一个 LLM 调用中降低了质量。

**影响**：
- Planner 可能在架构理解和步骤分解之间顾此失彼
- benchmark 无法分别评估「架构理解质量」和「步骤分解质量」
- 架构分析结果无法跨 Task 复用（每次 run 重新分析）

**需求**：在 Plan 生成之前，增加一个独立的「架构分析」步骤，专门负责从设计文档和 Task Spec 中提取结构化信息（模块边界、接口契约、约束列表、推荐分解顺序），然后将分析结果作为 Plan prompt 的输入。

---

### P1-2：Planner 不感知多个 Task Spec 之间的依赖

**现象**：当一个 Feature 被拆成多个 Task Spec（如 task-transfer-model / task-transfer-service / task-transfer-api），每个 Spec 独立执行 agent_go run。Planner 不知道其他 Spec 的存在。

**根因**：agent_go 每次 run 是独立的。没有跨 Spec 的编排层。

**影响**：
- Spec 间依赖靠人手动管理（先跑 A，等 A commit，再跑 B）
- 如果 A 的实现偏离了接口契约，B 会基于错误的实现工作，无人察觉直到集成测试失败
- 架构文档中的「接口契约」没有被 agent_go 强制验证

**需求**：当多个 Task Spec 之间存在接口依赖时，agent_go 应感知依赖关系并验证上游产出符合接口契约。

---

### P2-1：缺少架构一致性验证

**现象**：上游 step A 完成了。下游 step B 基于 A 的产出继续工作。但没有人验证 A 的产出是否符合设计文档中定义的接口契约。

**根因**：当前验证只检查「代码能跑」（shell exit code），不检查「代码符合架构设计」（接口契约一致性）。

**影响**：
- 架构设计文档中的接口契约可能被绕过
- 「代码能跑但架构不对」的偏差逐 step 累积，到集成测试才暴露

**需求**：semantic evaluator 应能检查「代码变更是否符合设计文档中定义的接口契约」。

---

### P2-2：架构设计文档与 Task Spec 的双向追溯缺失

**现象**：设计文档说「TransferService 接口为 X」。Task Spec A 实现了。3 个月后，有人改了 TransferService 的接口。设计文档没有更新。下一个 Task Spec 基于新接口实现——但与设计文档不一致。

**根因**：设计文档和代码之间没有追溯链。改了代码不会触发「设计文档可能需要更新」的提示。

**影响**：设计文档逐步过时，失去参考价值。

**需求**：Task Spec 中引用的设计文档段落，在代码变更影响该段落时，应提示更新。

---

## 三、需求汇总

| ID | 需求 | 优先级 | 工作量 | 依赖 |
|----|------|--------|--------|------|
| **REQ-1** | Planner 按模块边界分解（system prompt 增加模块边界分解指令） | P0 | ~0.5d | 无 |
| **REQ-2** | Planner 接口先行（system prompt 增加接口骨架先行指令，dependencies 支持「接口依赖」类型） | P0 | ~0.5d | 无 |
| **REQ-3** | Planner 约束传递到 agent_prompt（system prompt 增加约束逐条传递指令） | P0 | ~0.5d | 无 |
| **REQ-4** | Planner 主动提取设计文档结构化信息（system prompt 增加设计文档解析指令） | P0 | ~0.5d | 无 |
| **REQ-5** | Pre-Plan 架构分析阶段（独立 LLM 调用，提取模块边界/接口契约/约束/分解顺序） | P1 | ~2d | REQ-1~4 验证有效 |
| **REQ-6** | 多 Spec 依赖编排（agent_go 感知 Spec 间接口依赖，验证上游产出符合契约） | P1 | ~3d | REQ-5 |
| **REQ-7** | 架构一致性验证（semantic evaluator 检查代码是否符合设计文档中的接口契约） | P2 | ~3d | S10 bench v2 |
| **REQ-8** | 设计文档与代码双向追溯（Task Spec 引用的设计段落，代码变更时提示更新） | P2 | ~2d | KnowledgeStore |

---

## 四、优先级判断

### P0（4 项，共 ~2d）：Prompt Engineering — 不改架构，只改指令

**ROI**：最高。不改代码结构，只改 system prompt 文案。Planner 在被明确指令的情况下，LLM 本身有能力做模块分解、接口先行、约束传递——只是当前没有被要求做。

**验证方式**：S10 bench v2 全因子设计对比「新 prompt」vs「旧 prompt」的 pass_rate 和 $/pass。

**风险**：低。改 prompt 不影响现有行为（在不提供 `--spec`/`--docs` 时，prompt 行为不变）。

### P1（2 项，共 ~5d）：Pre-Plan + 多 Spec 编排 — 加一个轻量架构分析层

**触发条件**：P0 验证有效（bench 数据显示 prompt 改进后 pass_rate 提升但仍有架构级偏差）。

**风险**：中。Pre-Plan 增加一次 LLM 调用（~$0.01-0.03），需验证成本收益比。

### P2（2 项，共 ~5d）：架构一致性验证 + 双向追溯 — 远期质量保障

**触发条件**：P1 落地后，架构分解流程跑通，需要自动化质量保障。

**风险**：中高。架构一致性验证需要 semantic evaluator 理解接口契约，对 judge 模型能力要求高。

---

## 五、与现有迭代的关系

```
S11-P0（--spec + L1 gate）          ← 正在进行
  ↓
S10-P1（cross_judge）               ← 并行
  ↓
REQ-1~4（P0 prompt engineering）    ← S11-P0 完成后立即做
  ↓
S10-P2（全因子 bench）               ← 用新 prompt 跑
  ↓
S10-P3（分析结果）                   ← 对比新旧 prompt 效果
  ↓
REQ-5~6（P1 Pre-Plan + 多 Spec）    ← bench 数据验证 P0 有效后启动
```

**P0 四项（REQ-1~4）建议并入 S11-P0**，总工作量从 ~2d 变为 ~4d。都是 prompt 层面的改动，不影响架构。

---

## 六、不做的事

| 不做 | 理由 |
|------|------|
| 自建代码分析引擎提取模块依赖 | `analyze_project` + `get_resource_map` 已提供文件列表和目录结构。模块间 import 关系交给 LLM 分析（Plan 阶段做一次即可），不需要静态分析工具 |
| 架构设计文档的自动生成 | 架构设计是人的决策。AI 辅助设计（在 Claude Code 交互中）已经足够，不需要 agent_go 生成设计文档 |
| 可视化模块依赖图 | 属于项目管理工具的职责（Phase 0-2），不是 agent_go 的职责（Phase 3） |
| 自动重构模块边界 | 模块边界变更是架构决策，需要人判断。agent_go 只执行 Spec 中明确要求的边界变更 |

---

*关联文档：*
- [architecture-design-to-agent.md](architecture-design-to-agent.md) — 架构设计如何传递给 agent_go
- [agent-go-input-spec.md](agent-go-input-spec.md) — Task Spec 规范和 agent_go 输入准则
- [knowledge-transfer-and-management.md](knowledge-transfer-and-management.md) — 知识传递机制
