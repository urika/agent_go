# agent_go 输入准则

> **版本**：v1.0
>
> **目的**：精确定义 agent_go 接受什么输入、每种输入如何被消费、输入质量如何影响输出质量。供前序流程（PM 需求开发、技术 Scoping）明确「要做到什么程度才能交给 agent_go」。
>
> **适用对象**：PM（产出 Task Spec）、工程师（Scoping 阶段）、CI/自动化集成（通过 MCP 调用）。

---

## 一、输入总览

agent_go 接受两种输入形态：

| 形态 | 适用场景 | 信息密度 | agent_go 表现 |
|------|---------|---------|-------------|
| **A. 自由文本 prompt** | 简单任务、探索性任务、用户不想写 spec | 低。Planner 靠猜测补齐缺失信息 | 方差大——好 prompt 好结果，差 prompt 差结果 |
| **B. Task Spec（结构化）** | 复杂任务、需要精确控制的场景、团队协作 | 高。动机/范围/约束/验收标准明确 | 稳定——Planner 在约束内分解，重试率低 |

**原则**：agent_go 不强制使用 Task Spec。但不使用 Spec 时，Planner 的表现取决于用户 prompt 中包含的隐式信息量。

---

## 二、形态 A：自由文本 prompt（当前默认）

### 2.1 输入接口

```bash
agent_go run <repo-path> '<task-description>'
```

### 2.2 输入结构

| 字段 | 来源 | 必填 | 注入位置 | 状态 |
|------|------|------|---------|------|
| `task` | CLI 位置参数 | ✅ | Plan prompt user content：「任务：{task}」 | 已有 |
| `repo` | CLI 位置参数 | ✅ | 自动分析：文件列表 + Git 信息 + 目录结构 | 已有 |
| `--docs` | CLI 选项 | 否 | Plan prompt user content：「===== 参考文档 =====」段。支持逗号分隔多个文件路径 | **已有** |
| `--skill` | CLI 选项 | 否 | 加载指定 Skill 全文，注入 system prompt；同时限制 Planner 可见的 Skill 清单 | 已有 |
| `--agent-type` | CLI 选项 | 否 | 覆盖所有子任务的 agent_type | 已有 |
| `--no-cache` | CLI 选项 | 否 | 跳过 Plan 缓存（默认相似任务复用缓存，SHA256 key） | 已有 |
| `supplement` | Plan 确认交互 [S] 选项 | 否 | Plan prompt user content：「===== 用户补充 =====」段。用户编辑后重新生成 | 已有（交互式） |
| `reference_docs` | Plan 确认交互 [D] 选项 | 否 | Plan prompt user content：「===== 参考文档 =====」段。用户指定文档后重新生成 | 已有（交互式） |
| `--spec` | CLI 选项 | **新增** | 读取结构化 Task Spec Markdown 文件，解析章节，注入对应 prompt 位置（见 §3.3） | ✅ **已落地（S11-P0）** |
| `--context` | CLI 选项 | **新增** | 从 PRD/Roadmap 等长文档中按关键词提取相关段落，注入 Plan prompt（见 §对接点 2） | **远期** |

### 2.3 自动注入的上下文

agent_go 自动收集以下信息并注入 Plan prompt，无需用户提供：

| 自动上下文 | 来源 | 注入方式 |
|-----------|------|---------|
| 项目文件列表 | `git ls-files` | user content |
| Git 信息 | `git remote -v` + `git branch` + `git rev-parse` | user content |
| 目录结构 | `get_resource_map()` | user content |
| 运行时环境 | Python 版本 + OS 信息 | system prompt |
| Skill 清单 | `~/.agent_go/skills/` + 项目 `.claude/skills/` | system prompt（表格格式，含名称/描述/适用场景） |
| Role-Skill 规则 | `~/.agent_go/role_skill_map.json` | system prompt（摘要） |
| Agent 类型定义 | 内置（developer/architect/reviewer/tester） | system prompt |
| 验证命令白名单 | 内置 `SAFE_VERIFICATION_PREFIXES` | system prompt |
| Skill 全文 | 匹配的 Skill Markdown 文件 | system prompt（受 10000 字符预算限制，超限截断） |

### 2.4 你能控制的部分

用户可以控制输入质量的三个维度：

| 维度 | 差 | 好 |
|------|-----|-----|
| **任务描述清晰度** | "修 bug" | "修复 User 模型 email_verified 字段在创建时未默认设为 False 的问题" |
| **范围明确度** | "重构" | "重构 src/auth/ 下三个文件，不动 public API 签名" |
| **验收标准** | 无 | "验证命令：pytest tests/test_auth.py -v -k email_verify" |

---

## 三、形态 B：Task Spec（结构化输入）✅ 推荐

### 3.1 指定方式

```bash
agent_go run ./my-repo --spec docs/tasks/task-email-verification.md
```

`--spec` 参数指向一个 Markdown 文件。agent_go 解析文件中的结构化字段，注入 Plan prompt。

### 3.2 Task Spec 文件规范

Task Spec 是一个 Markdown 文件，包含以下章节。带 `*` 的为必填。

```markdown
# Task Spec: <任务名称>                    * 必填

## 1. 目标（做什么）                        * 必填
一段话描述这个任务要达成的最终效果。

## 2. 动机（为什么）                        * 必填
为什么要做这个任务。如果有关联的 Issue/PRD 章节，在此引用。

## 3. 范围（动哪里，不动哪里）              * 必填
### 需要改动的文件/模块
### 明确不动的区域

## 4. 约束                                  可选，但强烈建议
技术约束、设计约束、兼容性要求。

## 5. 验收标准（怎么算做完）                 * 必填
具体的验收条件。尽量可自动化（能写成验证命令）。

## 6. 参考资料                              可选
设计文档链接、类似实现参考（commit hash）、相关 Issue 编号。

## 7. 已知风险                              可选
用户已知的风险点。Planner 会在分解时考虑这些风险。
```

### 3.3 字段映射：Task Spec → Plan prompt

| Task Spec 章节 | 注入到 Plan prompt 的哪个位置 | 注入格式 |
|---------------|---------------------------|---------|
| **1. 目标** | user content：「任务：{spec.目标}」替代自由文本 task | 直接替换 `task` 字段 |
| **2. 动机** | user content：「背景：{spec.动机}」 | 追加到任务描述后 |
| **3. 范围** | system prompt：「必须涉及的模块：... 禁止修改的模块：...」→ Planner 的 `files` 字段受约束 | 约束指令，注入 system prompt |
| **4. 约束** | system prompt：「设计约束：...」→ Planner 的分解策略受约束 | 约束指令，注入 system prompt |
| **5. 验收标准** | system prompt → Planner 的 `verification` 字段参考；同时影响 Worker 的验证循环 | 约束指令 + 验证命令自动派生 |
| **6. 参考资料** | user content：「参考：{spec.参考资料}」→ 与 `reference_docs` 合并 | 追加到 user content |
| **7. 已知风险** | system prompt：「已知风险：...」→ Planner 在 `risks` 字段中体现 + `difficulty` 标记 | 约束指令 |

### 3.4 完整示例

**输入：Task Spec 文件（docs/tasks/task-email-verification.md）**

```markdown
# Task Spec: User Email Verification

## 1. 目标
为 User 模型添加 email_verified 字段和邮箱验证逻辑。
包含：数据库迁移、验证 token 生成/校验、验证 API 端点、测试。

## 2. 动机
- 当前注册无需邮箱验证，垃圾注册增加
- 相关 Issue：#142
- PRD 引用：PRD §2.3 "用户认证增强"

## 3. 范围
### 需要改动
- `src/models/user.py` — User 模型新增 email_verified 字段
- `src/auth/tokens.py` — 复用并扩展 TokenManager 支持 email_verification 类型
- `src/auth/verify.py` — 新增验证 token 生成与校验逻辑
- `src/api/user.py` — 新增 `/verify-email?token=xxx` 端点
- `tests/test_auth.py` — 新增邮箱验证流程测试
- 数据库迁移文件（Alembic）

### 明确不动
- 不改变现有登录流程（未验证邮箱不影响登录）
- 不引入邮件发送服务（仅生成验证链接，发送由外部服务处理）
- 不动 `src/api/admin.py`（管理后台）

## 4. 约束
- 数据库迁移必须可回滚（Alembic upgrade/downgrade 配对）
- 验证 token 有效期 24 小时
- API 响应格式遵循 `src/api/_conventions.md` 约定
- Python 3.9+，使用现有依赖（不新增 PyPI 包）
- 保持现有 TokenManager 的公开 API 签名不变，仅扩展 token_type 参数

## 5. 验收标准
- [ ] `pytest tests/test_auth.py::test_email_verification_flow -v` 全部通过
- [ ] 新创建的 User 实例 email_verified 默认为 False
- [ ] 验证 token 过期后（>24h）拒绝验证请求，返回 410 Gone
- [ ] 已有用户数据迁移不丢失、不回退已验证状态
- [ ] TokenManager 原有 token 类型（password_reset）功能不受影响

## 6. 参考资料
- 设计文档：[产品需求文档](../prd.md)
- 类似实现参考：commit `a1b2c3d`（密码重置 token，TokenManager 扩展模式）
- 相关 Issue：#142, #167

## 7. 已知风险
- User 表 ~50w 行，迁移需注意锁表时间（建议用 batch update + 非锁表 DDL）
- TokenManager 当前仅支持 password_reset 类型，扩展 email_verification 需要验证不破坏现有功能
```

**对应效果**：agent_go Plan 阶段拿到这份 Spec 后：

1. **目标 + 动机** 替代用户的一句话 prompt，Planner 知道完整上下文
2. **范围** 约束 Planner 的 `files` 字段——不会动 `src/api/admin.py`，不会引入邮件库
3. **约束** 影响分解策略——迁移必须独立成一步（需要 batch update 策略），TokenManager 扩展必须独立成一步（需要兼容性验证）
4. **验收标准** 直接转化为验证命令（Planner 可能拆分为 3-5 个验证步骤，对应不同的验收条目）
5. **已知风险** 被标记在相关子任务的 `risks` 字段中，Worker 执行时会看到

---

## 四、输入质量对输出质量的影响

### 4.1 信息完整度 vs Plan 质量

| 信息完整度 | 用户输入示例 | Planner 的猜测量 | 典型结果 |
|-----------|------------|----------------|---------|
| **高（Task Spec）** | 完整的 7 章节 Spec | 不需要猜测 | Plan 精准、重试率低、$/pass 稳定 |
| **中（详尽的 prompt）** | "在 src/models/user.py 加 email_verified 字段，用 TokenManager（src/auth/tokens.py）生成验证 token，参照 commit a1b2c3d 的模式" | 少量猜测（文件位置、依赖关系） | Plan 基本准确，偶有调整 |
| **低（一句话）** | "加邮箱验证" | 大量猜测（哪个模块、用什么方式、边界在哪） | Plan 质量方差大，重试率高，成本膨胀 |

### 4.2 成本杠杆

Bench v1 数据显示：Planner 分解质量直接影响 Worker 执行成本。一次差分解导致：
- Worker 在错误的方向上工作 → 验证失败 → 重试 → token 消耗 ×2-3
- 或 Worker 动到了不该动的模块 → 级联失败 → 下游全部 blocked

Task Spec 的「范围」和「约束」章是**成本控制的最前端杠杆**——在 Plan 阶段之前就限定了探索空间。

### 4.3 何时必须用 Task Spec

| 条件 | 建议 |
|------|------|
| 任务涉及 ≥3 个模块 | **必须用** Spec 标注范围和约束 |
| 任务有「不能动」的区域 | **必须用** Spec 标注「明确不动」 |
| 任务有数据库迁移 | **必须用** Spec 标注迁移约束（可回滚、锁表策略） |
| 任务是对已有功能的扩展（非全新功能） | **强烈建议** Spec 标注参考资料（类似实现） |
| 简单单文件改动 | 自由文本 prompt 即可 |
| 探索性任务（不确定怎么做） | 先用 Claude Code 交互式探索，确定方案后再写 Spec |

---

## 五、前序流程的工作目标

### 5.1 前序流程的职责

Scoping 阶段（PM/工程师在 Claude Code 中完成，或通过 `agent_go scope` 辅助）的产出物是一份 Task Spec 文件。

### 5.2 工作目标检查清单

在把 Task Spec 交给 agent_go 之前，逐项确认：

```
□ 1. 目标：读完「目标」章，能一句话说出这个任务要达成什么效果？
□ 2. 动机：如果 3 个月后有人问「为什么当时要这么做」，能从「动机」章得到答案？
□ 3. 范围-动什么：受影响的所有文件/模块都已列出？有没有遗漏的？
□ 4. 范围-不动什么：「明确不动」的区域是否足够清晰，能阻止 Planner 的猜测？
□ 5. 约束：有没有隐含的约束（如「不能新增依赖」、「必须兼容 Python 3.9」）没写出来？
□ 6. 验收标准：每个验收标准是否可以客观判断通过/失败？能否自动化为验证命令？
□ 7. 参考资料：设计文档、类似实现是否已链接？（能显著降低 Planner 的猜测量）
□ 8. 风险：有没有已知的坑？（如大表迁移、兼容性问题）
```

### 5.3 PM 的产出与 agent_go 的关系

```
PM 在 Claude Code 中的工作：
  │
  ├── 需求分析（数据分析、竞品研究、用户反馈）→ 写入 PRD
  ├── 方案设计（系统设计、架构决策）→ 写入 docs/design/
  ├── 排期决策（优先级、依赖）→ 写入 Roadmap
  └── 任务定义（具体功能的 Task Spec）→ 写入 docs/tasks/
                                          │
                                          ▼
                                    agent_go run --spec
```

**PM 不写代码，PM 写 Task Spec。** Task Spec 是 PM 和 agent_go 之间的接口契约——
PM 把「要做什么」说清楚，agent_go 负责「怎么执行」。

---

## 六、实施路径

| 优先级 | 能力 | 描述 | 状态 |
|--------|------|------|------|
| **P0** | `--spec` 参数 | 读取 Task Spec Markdown 文件，解析 7 章节，注入 Plan prompt | ✅ **已落地（S11-P0）** |
| **P0** | `agent_go spec template` | 生成空白 Task Spec 模板（预填 repo 结构的模块列表） | ✅ **已落地（S11-P0）** |
| **P0** | `agent_go spec validate` | 对 Spec 文件运行 L1 准入审查（必填章节/路径/白名单/长度） | ✅ **已落地（S11-P0）** |
| **P0** | L1 硬门禁（4 项确定性检查） | cmd_run 在 Plan 前强制跑；`--force` 跳过，`--yes` 仍跑 | ✅ **已落地（S11-P0）** |
| **P0** | Plan prompt 约束注入 | Spec §3/§4/§5/§7 → system prompt 硬约束；§1→task；§2/§6→user content | ✅ **已落地（S11-P0）** |
| **已有** | `--docs` | 将参考文档全文注入 Plan prompt。当前等价于 `--spec` 的「参考资料」注入，但无结构化解析 | ✅ 已落地（`cmd_run` → `read_reference_docs` → `generate_plan(reference_docs=...)`） |
| **已有** | Plan 确认 [S] 补充 / [D] 文档 | 交互式手动补充上下文后重新生成 Plan | ✅ 已落地（`confirm_plan` 交互菜单） |
| **P1** | `agent_go scope` | 轻量 Scoping：读代码库 + 追问澄清 → 输出 Task Spec 草稿 | 待设计（依赖 S10 bench） |
| **P1** | L1.5 AST 冲突检测 | 多子任务同符号冲突的静态检测（零 LLM 成本，[arXiv:2603.24284](sdd-references-and-frameworks.md) 97% 精度）。`detect_step_conflicts()` 用 ast 提取顶层符号，符号级冲突在 Plan 确认后拦截 | ✅ **已落地（S11 L1.5）** |
| **P2** | `--context` 参数 | 从 PRD/Roadmap 等长文档中按需提取相关段落注入 | 远期 |
| **P2** | Task Spec 库 | 历史 Spec 搜索复用 | 依赖 KnowledgeStore |

---

*关联文档：*
- [bench-analysis-2026-08-01.md](../archive/reference/bench-analysis-2026-08-01.md) — Bench v1 数据（输入质量的成本影响，历史参考）
- [bench-v2-data-requirements.md](bench-v2-data-requirements.md) — Bench v2 数据需求
- [prd.md](../prd.md) — 产品 KPI 与设计原则
