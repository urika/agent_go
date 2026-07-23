# PRD: Plan 阶段智能 Agent 角色与 Skill 分配

> 版本: v1.0  
> 日期: 2026-05-26  
> 作者: Product  
> 状态: ✅ 已实现 (v0.4 — role_skill_map.py + Plan prompt 增强)  
> 关联: [../design/requirements.md](../design/requirements.md) | [../design/architecture.md](../design/architecture.md)

---

## 一、背景与问题

### 1.1 现状

agent_go v2.0 已实现 Agent 类型系统（developer/architect/reviewer/tester）和 Skill 加载机制（YAML frontmatter + Markdown），支持 per-subtask 级别的角色分配。数据链路完整：

```
Plan JSON (LLM 输出)
  → plan_to_subtasks() 透传 agent_type + skills 字段
    → executor 按 subtask 维度加载对应 Agent 配置和 Skill 内容
```

### 1.2 核心问题

尽管数据结构已就绪，**实际运行中 per-subtask 的角色和 Skill 分配几乎不生效**，原因如下：

| 问题编号 | 问题描述 | 影响 |
|----------|----------|------|
| P-1 | LLM 不知道项目安装了哪些 Skill，无法在 plan steps 中引用 | Skill 注入形同虚设 |
| P-2 | `agent_type` 和 `skills` 在 plan prompt 中标注为"可选"，LLM 经常省略 | 所有 subtask 降级为 developer |
| P-3 | Skill 名称不匹配时静默跳过，用户无感知 | 知识注入丢失，无告警 |
| P-4 | 缺少规则兜底推断，完全依赖 LLM 自觉输出 | 输出不稳定、不可控 |

### 1.3 用户影响

- **效率损失**: 架构分析 subtask 以 developer 角色执行，可能产生不必要的代码修改
- **质量损失**: 测试 subtask 未注入 tdd-workflow Skill，测试质量依赖 Claude 自身判断
- **信任损失**: 用户安装了 Skill 并期望生效，但实际被静默跳过

---

## 二、目标

### 2.1 业务目标

| 目标 | 衡量指标 | 基线 | 目标值 |
|------|----------|------|--------|
| subtask 角色分配准确率 | 非 developer 角色的 subtask 占比（适用场景下） | ~0% | ≥30% |
| Skill 注入命中率 | Skill 成功注入 TASK.md 次数 / Skill 被引用次数 | 未知（静默失败） | ≥90% |
| 角色分配失败可见性 | 未命中告警数 / 总分配数 | 0（无告警） | 100% 覆盖 |

### 2.2 非目标

- 不改变现有 Agent 类型定义格式和 Skill 文件格式
- 不引入新的外部依赖
- 不强制要求所有 subtask 必须指定 agent_type（developer 仍为默认）

---

## 三、功能需求

### 3.1 F-1: Skill 清单注入 Plan Prompt

**优先级**: P0  
**问题**: P-1  
**改动文件**: `api.py` → `generate_plan()`  

**需求描述**:  
在调用 LLM 生成 Plan 前，自动扫描项目级和用户级已安装的全部 Skill，将清单注入 system prompt，使 LLM 知道可引用哪些 Skill。

**输入**:  
- `list_skills(project_root)` 返回的已安装 Skill 列表

**输出**:  
追加到 system_prompt 的内容，格式如下：

```
## 项目已安装的 Skill（可在 steps[].skills 中引用）
| Skill 名称 | 描述 | 推荐场景 |
|------------|------|----------|
| security-review | 安全审查 — 涉及认证、权限、加密 | 涉及认证/权限/输入校验的步骤 |
| tdd-workflow | TDD 工作流 | 涉及测试编写的步骤 |
```

**约束条件**:  
- Skill 清单最大注入 20 个，超出时按字母排序截断并附加 `"... 还有 N 个 Skill 未展示"`
- 每条 Skill 描述截断至 80 字符
- 如无已安装 Skill，不追加该段落（保持 prompt 精简）

**验收标准**:  
1. `--skill` 未手动指定时，generate_plan 的 prompt 中包含已安装 Skill 清单
2. `--skill` 已手动指定时，仅注入指定的 Skill（保持现有行为）
3. 无 Skill 安装时，prompt 中无 Skill 相关段落

---

### 3.2 F-2: Agent 类型与 Skill 推荐 Prompt 强化

**优先级**: P0  
**问题**: P-2  
**改动文件**: `api.py` → `generate_plan()`  

**需求描述**:  
将 `agent_type` 和 `skills` 从"可选建议"升级为"强烈推荐填写"，调整 prompt 措辞并提供示例。

**变更**:  

当前 prompt：
```
"skills": ["可用的领域Skill名称（可选）"],
"agent_type": "推荐的Agent类型（可选，如 developer/architect/reviewer/tester）"
```

改为：
```
"skills": ["从已安装 Skill 清单中选择匹配本步骤的 Skill 名称"],
"agent_type": "必须指定。developer=编码实现, architect=只读分析, reviewer=代码审查, tester=测试编写"
```

并追加示例 step：
```json
{
  "id": 2,
  "title": "编写单元测试",
  "agent_type": "tester",
  "skills": ["tdd-workflow"],
  ...
}
```

**验收标准**:  
1. prompt 中 `agent_type` 不再标注为"可选"
2. prompt 包含至少 1 个完整示例 step，展示 agent_type + skills 的搭配
3. 生成 5 次计划，至少 3 次包含非 developer 的 agent_type

---

### 3.3 F-3: 规则兜底推断

**优先级**: P1  
**问题**: P-4  
**改动文件**: 新增 `agent_go/routing.py`，修改 `ui.py` → `plan_to_subtasks()`  

**需求描述**:  
当 LLM 未输出 `agent_type` 或输出无效值时，基于 step 特征自动推断最合适的角色。

**推断规则表**:

| 优先级 | 条件 | 推断 agent_type |
|--------|------|-----------------|
| 1 | step.agent_type 值有效（命中已注册类型） | 使用 LLM 指定值 |
| 2 | `verification` 字段包含 `test`/`pytest`/`jest`/`go test` | `tester` |
| 3 | `title` 或 `description` 包含 "审查"/"review"/"review"（不区分大小写） | `reviewer` |
| 4 | `files` 仅包含 `.md`/设计文档，无 `.py`/`.go`/`.ts` 等代码文件 | `architect` |
| 5 | `title` 包含 "架构"/"设计"/"分析"/"architect"/"design" | `architect` |
| 6 | 以上均不匹配 | `developer`（默认） |

**Skill 推断规则**:

| 条件 | 推荐加载的 Skill |
|------|-----------------|
| `agent_type == "tester"` | 自动查找已安装的 `tdd-workflow` 或 `*-test*` / `*-testing*` |
| `title`/`description` 包含 "安全"/"security"/"auth" | 自动查找 `security-review` 或 `*-security*` |
| 用户已通过 `--skill` 指定 | 以用户指定为准，不额外推断 |

**约束条件**:  
- 推断结果不覆盖 LLM 的有效输出（LLM 优先）
- 推断结果应在 subtask 确认界面明确标注 `[自动推断]` 以区分
- 最多自动推断 2 个 Skill，避免过度注入

**验收标准**:  
1. LLM 未输出 agent_type 时，推断结果与上表一致
2. LLM 输出有效 agent_type 时，推断逻辑不介入
3. 推断结果在 `agent_go show <task-id>` 中可见，标注来源

---

### 3.4 F-4: 未命中告警与可观测性

**优先级**: P1  
**问题**: P-3  
**改动文件**: `executor.py` → `run_subtask()`, `cli.py` → `cmd_show()`  

**需求描述**:  
当 Skill 加载失败或 Agent 类型未命中时，在日志和终端输出中明确告警。

**行为变更**:

| 场景 | 当前行为 | 改为 |
|------|----------|------|
| Skill 名在 plan 中被引用但未找到 | 静默跳过 | `⚠️ Skill 未找到: "xxx"，已跳过。已安装: [...]` |
| Agent 类型名无效（如拼写错误） | 降级为 developer，仅 DEBUG 日志 | `⚠️ Agent 类型 "xxx" 未注册，降级为 developer。可用: [developer, architect, reviewer, tester]` |
| 规则推断的 agent_type 与 LLM 指定不同 | N/A（新场景） | `ℹ️ agent_type 自动推断: tester（LLM 未指定）` |

**task_metadata 扩展**:  
在每个 subtask 的元数据中记录：

```json
{
  "agent_type": "tester",
  "agent_type_source": "inferred",   // "llm" | "inferred" | "default"
  "skills": ["tdd-workflow"],
  "skills_unresolved": ["foo-skill"]  // 未找到的 Skill 名称
}
```

**验收标准**:  
1. Skill 未找到时，终端输出包含可用 Skill 列表提示
2. `agent_go show <task-id>` 展示每个 subtask 的 agent_type 来源
3. 日志中包含 `skill_unresolved` 和 `agent_type_source` 字段

---

### 3.5 F-5: 项目级 Agent 和 Skill 配置声明

**优先级**: P2  
**问题**: 跨项目可移植性  
**改动文件**: 新增配置加载逻辑，修改 `config.py`  

**需求描述**:  
支持在项目根目录 `.agent_go/config.json` 中声明该项目推荐使用的 Agent 类型和 Skill，作为 Plan prompt 的补充上下文。

**配置格式**:

```json
{
  "recommended_agents": ["developer", "tester"],
  "recommended_skills": ["security-review", "tdd-workflow"],
  "routing_hints": {
    "test_files": "tester",
    "security_files": "reviewer"
  }
}
```

**行为**:  
- `recommended_agents`: 限制 LLM 可选的 Agent 类型范围（不在列表中的类型不出现在 prompt 中）
- `recommended_skills`: 始终注入 Plan prompt，不论 `--skill` 是否指定
- `routing_hints`: 文件路径模式 → Agent 类型的映射，作为推断规则的补充

**约束条件**:  
- 项目配置为可选项，不配置时保持现有行为
- 与 `~/.agent_go/config.json` 全局配置合并时，项目级优先

**验收标准**:  
1. 项目配置存在时，prompt 中仅包含 `recommended_agents` 中列出的 Agent 类型
2. `recommended_skills` 中的 Skill 始终出现在 prompt 中
3. 无项目配置时，行为与当前完全一致

---

## 四、技术方案概要

### 4.1 改动范围

| 文件 | 改动类型 | 涉及需求 |
|------|----------|----------|
| `agent_go/api.py` | 修改 `generate_plan()` | F-1, F-2 |
| `agent_go/ui.py` | 修改 `plan_to_subtasks()` | F-3 |
| `agent_go/executor.py` | 修改 `run_subtask()` | F-4 |
| `agent_go/cli.py` | 修改 `cmd_show()` | F-4 |
| `agent_go/config.py` | 新增项目配置合并逻辑 | F-5 |
| `agent_go/routing.py` | **新增** | F-3 |
| `tests/test_routing.py` | **新增** | F-3 |

### 4.2 数据流（改进后）

```
用户命令
  │
  ├── --agent-type <name> ──→ 全局覆盖（优先级最高）
  ├── --skill <names> ──────→ 全局覆盖
  │
  ↓
  [F-5] 加载项目级 config → recommended_agents / recommended_skills
  │
  ↓
  [F-1] list_skills() → 注入已安装 Skill 清单到 system_prompt
  [F-2] 强化 prompt: agent_type 标注为必须，附示例
  │
  ↓
  generate_plan() → LLM 返回带 agent_type + skills 的 plan JSON
  │
  ↓
  [F-3] plan_to_subtasks()
    → LLM 指定了 agent_type → 直接使用
    → LLM 未指定 → routing.py 推断
    → LLM 指定了 skills → 直接使用
    → LLM 未指定 → routing.py 按关键词匹配
  │
  ↓
  [F-4] run_subtask()
    → 加载 agent_type → 失败则告警 + 降级
    → 加载 skills → 部分失败则告警 + 记录 unresolved
    → 写入 task_metadata: agent_type_source, skills_unresolved
  │
  ↓
  TASK.md 生成（含 Skill 完整知识注入）
  Claude Code 执行（含 Agent 角色权限配置）
```

---

## 五、实施计划

| 阶段 | 需求 | 预估工作量 | 依赖 |
|------|------|------------|------|
| Phase 1 | F-1 + F-2 | 1 天 | 无 |
| Phase 2 | F-4（告警部分） | 0.5 天 | 无 |
| Phase 3 | F-3（规则推断） | 1 天 | F-1, F-2 |
| Phase 4 | F-4（元数据展示） | 0.5 天 | F-3 |
| Phase 5 | F-5（项目级配置） | 1 天 | F-3 |

**总计**: 约 4 天

---

## 六、风险评估

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| Prompt 注入 Skill 清单后 token 超限 | 低 | Plan 生成失败 | 限制清单大小（最多 20 条），描述截断 |
| 规则推断产生错误角色分配 | 中 | subtask 以错误权限执行 | 推断结果在确认界面标注，用户可手动修改 |
| LLM 输出无效 agent_type 名称 | 中 | 降级为 developer | F-4 告警机制覆盖 |
| 项目级配置与全局配置冲突 | 低 | 行为不一致 | 明确优先级：CLI 参数 > 项目配置 > 全局配置 |

---

## 七、成功标准

1. **功能完整性**: F-1 至 F-4 全部通过验收标准
2. **向后兼容**: 不指定 `--skill` 和 `--agent-type` 时，行为与 v2.0 一致
3. **零新依赖**: 所有改动仅使用 Python stdlib
4. **测试覆盖**: 新增 `test_routing.py`，覆盖推断规则表中的 6 条规则
5. **可观测性**: `agent_go show <task-id>` 可查看每个 subtask 的角色来源和 Skill 命中情况
