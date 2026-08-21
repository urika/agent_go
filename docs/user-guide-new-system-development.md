# 使用 agent_go 开发新系统的全过程（产品经理视角）

> 定位：用 agent_go 从 0 到 1 开发一个新系统（如一个业务工具/API 服务）的完整过程说明书
> 目标读者：想用 agent_go 启动新项目的产品/工程负责人
> 可行性基础：本过程涉及的每个功能都已在 agent_go 中实现并经真实任务验证（见文末「概念可行性验证」）

---

## 1. 角色分工

| 角色 | 职责 | 参与阶段 |
|------|------|---------|
| **人（产品/架构负责人）** | 定义目标与需求、做架构决策、审批计划与交付、处理人工介入点 | 需求（全）/设计（审）/审查（全）/交付（批） |
| **planner（规划模型，K3/强模型）** | 把需求拆成执行计划（步骤+验证+依赖） | 设计 |
| **worker（执行模型，本地/云端）** | 按计划实现代码（worktree 隔离环境） | 实现 |
| **evaluator（评估模型，GLM）** | 语义评估实现是否完成需求 | 验证 |
| **看板（kanban）** | 任务分类编排、状态流转、通知 | 全程 |
| **决策辅助（insight/recommend）** | 失败归因分析、模型选型建议 | 出问题/优化时 |
| **决策记录（decision log）** | 每个关键决策的可审计轨迹 | 全程 |

## 2. 全流程（6 阶段）

```
需求 → 设计 → 实现 → 验证 → 审查 → 交付
 ↓      ↓      ↓      ↓      ↓      ↓
人定    planner  worker  evaluator  人审    人批
看板编排  计划确认  worktree 验证循环  审批台   merge/PR
```

### 阶段 1：需求（人主导）

| 活动 | 用什么功能 | 交付物 |
|------|-----------|--------|
| 写任务需求（自然语言/结构化） | 看板建卡（brainstorm 列，implementation 类型）或 `agent_go spec template` | 需求卡片 / Task Spec（7 章节结构化需求） |
| 明确验收标准 | Spec §验收标准 / 看板卡片 description | 可执行的验证命令草案 |

**关键点**：新建系统的需求用 **Task Spec**（结构化契约）承载——spec template 生成 7 章节模板（目标/范围/接口/验收/风险等），L1 准入审查保证完整性。

### 阶段 2：设计（planner + 人审）

| 活动 | 用什么功能 | 交付物 |
|------|-----------|--------|
| 需求 → 执行计划 | `agent_go run <repo> '<任务>'`（planner 生成 Plan：步骤+验证命令+难度+依赖） | PLAN.md（执行方案） |
| 计划确认 | `--confirm-mode web`（看板/web 确认卡片）或 CLI 交互 | 确认的 Plan |
| 难度分级 | planner 标 difficulty；e2e 端到端判定（hard 任务不拆分） | 拆分计划（多子任务）或端到端计划 |

**关键点**：hard/架构级任务走 **e2e 端到端**（保留全局上下文）；可拆分的走 **plan 拆分**（并行子任务）。看板卡片 automation 字段自动判定（架构→manual、明确 spec→auto）。

### 阶段 3：实现（worker 主导）

| 活动 | 用什么功能 | 交付物 |
|------|-----------|--------|
| 隔离环境实现 | worktree 隔离（每子任务独立分支） | 代码变更（worktree 分支） |
| 模型执行 | worker_models 难度路由（easy/medium/hard → 不同模型）+ 降级链 | 实现代码 |
| 上下文注入 | TASK.md（文件清单/验证/风险/共享资源/do-not-touch） | 实现符合边界 |
| goal 循环 | 验证命令死守（失败自动修复重试） | 验证通过的实现 |

**关键点**：worker 在隔离 worktree 里实现，goal 循环保证"验证通过才退出"。本地模型适合模块级实现（medium），hard 走云端强模型（降级链兜底）。

### 阶段 4：验证（evaluator 主导）

| 活动 | 用什么功能 | 交付物 |
|------|-----------|--------|
| 语义评估 | evaluator 评估 diff 是否完成需求 | 评估结果（passed/confidence/reason） |
| 失败处理 | 验证失败 → 撤销越界 → 重试（注入失败上下文） | 修复后的验证通过 |
| 评估仲裁 | 低置信度自动换 evaluator（GLM→备选） | 可信评估结论 |

**关键点**：evaluator 是"验收裁判"——不是模型自己说自己完成，而是独立模型检查 diff 是否真完成任务。假阳性/误判有置信度兜底。

### 阶段 5：审查（人主导）

| 活动 | 用什么功能 | 交付物 |
|------|-----------|--------|
| 聚合审查 | `agent_go review --task <id>`（聚合报告 + diff 摘要） | 审查报告 |
| 深层审查 | `--deep`（独立模型逐子任务分析） | 深层审查意见 |
| 审批决策 | approve/reject/changes-requested（写 review.json + 审计） | 审批结论 |

**关键点**：交付前必须人工审批（审批台）——approve 才能进交付，reject/changes 回退重做。

### 阶段 6：交付（merge/PR）

| 活动 | 用什么功能 | 交付物 |
|------|-----------|--------|
| 合并交付 | `agent_go merge <id>`（mergeability 预检 + 显式 merge + 工作区同步） | merge commit（交付分支合并到目标分支） |
| 或创建 PR | `agent_go pr <id> --push` | PR 链接 |
| 交付确认 | ACCEPTED_DELIVERY 状态 + explicit_merge_commit | 最终交付产物 |
| 报告分发 | `agent_go report <id>`（md/html 报告） | 可分享的项目交付报告 |

**关键点**：merge 是显式交付命令（mergeability 预检防冲突），交付状态机推进到 ACCEPTED_DELIVERY。

## 3. 贯穿性能力（支撑全流程）

| 能力 | 功能 | 用途 |
|------|------|------|
| **看板编排** | kanban（5 阶段卡片）| 多任务并行管理、分类自动化、状态可视化 |
| **观测** | web 操作台（任务/成本/模型/健康/审计） | 实时观测执行状态与成本 |
| **度量归因** | metering（tokens/cost/latency/route_target） | 成本核算与模型归因 |
| **决策辅助** | insight（失败分析）+ recommend（模型选型） | 出问题时的诊断与优化建议 |
| **决策审计** | decision log + web_audit | 每个关键决策可复盘 |
| **可靠性** | 降级链/锁互斥/evaluator 仲裁 | 故障兜底与质量保证 |

## 4. 交付物总览（新建系统项目的完整产出）

| 阶段 | 交付物 | 位置 |
|------|--------|------|
| 需求 | Task Spec / 看板需求卡片 | docs/tasks/ 或 ~/.agent_go/kanban.json |
| 设计 | PLAN.md（执行方案） | ~/.agent_go/<task>/PLAN.md |
| 实现 | 代码变更（隔离分支） | worktree 分支 → delivery 分支 |
| 验证 | 评估结果 + 验证记录 | meta.json/results + verify_state.json |
| 审查 | review.json（审批决策） | ~/.agent_go/<task>/review.json |
| 交付 | merge commit / PR 链接 + ACCEPTED_DELIVERY 状态 | 目标仓库 + meta.json |
| 报告 | md/html 交付报告（可分享） | agent_go report 输出 |
| 审计 | metering 成本 + decision log + web_audit | ~/.agent_go/ |
| 看板 | 项目任务全景（分类/状态/成本/建议） | web 看板视图 |

## 5. 概念可行性验证（本方案的可行性依据）

本过程不是理论设计——**每个环节都已在 agent_go 中实现并经真实任务验证**：

| 环节 | 实证（本会话验证记录） |
|------|----------------------|
| 需求→计划 | Task Spec 准入审查 + planner 拆解（K3 planner 拆出可执行步骤）✓ |
| 计划确认 | web 确认卡片（pending/decision 文件协议）✓ |
| 实现 | worktree 隔离 + worker 端到端实现（email_validator/safe_json_load 等真实功能，测试全过）✓ |
| 验证 | evaluator 语义评估（GLM 评估准确，假阳性有置信度兜底）✓ |
| 审查 | 审批台（approve/review.json 写入）✓ |
| 交付 | merge 到目标分支（ACCEPTED_DELIVERY，789dbb2 等真实 merge）✓ |
| 看板编排 | PoC 端到端（建卡→分类→派发→执行→交付→流转）✓ |
| 成本归因 | metering R8 归因正确（force_fallback 场景标 cloud）✓ |
| 决策辅助 | insight 分析产出有效建议（证据校验防编造）✓ |

**结论**：本过程描述的"用 agent_go 开发新系统"全流程，其每个环节都有实现 + 真实任务验证——**概念上完全可行，且已被证明可行**（agent_go 自身的多个功能（Web 操作台、看板、模型三层）就是这样开发出来的——dogfooding 证据）。

## 6. 快速启动（新建系统第 1 天）

```bash
# 1. 环境：配置模型（方案 B：K3 planner + GLM evaluator + worker 云端/本地）
agent_go config local   # 或保持云端

# 2. 需求：写 Task Spec
agent_go spec template /path/to/new-repo --output docs/tasks/my-system.md
# 编辑填充 7 章节（目标/范围/接口/验收/风险/边界/参考）

# 3. 看板：建系统卡片（implementation 类型）
# web → 看板 → 新建卡片（标题=系统名，description=核心需求）

# 4. 执行：启动（web 确认模式）
agent_go run /path/to/new-repo --spec docs/tasks/my-system.md --confirm-mode web

# 5. 观测：web 看板/任务页实时看执行、成本、验证状态

# 6. 审查交付：审批台 approve → merge → ACCEPTED_DELIVERY
agent_go review --task <id> --approve
agent_go merge <id> --push

# 7. 报告分享
agent_go report <id> --format html --output delivery-report.html
```
