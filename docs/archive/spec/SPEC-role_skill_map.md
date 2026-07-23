# role_skill_map 模块规格说明

## 概述

`agent_go/role_skill_map.py` 负责"角色-Skill 映射规则"的加载与应用：为 Plan 阶段 LLM 分解出的每个 step 兜底补充 agent 角色（`agent_type`）和所需 Skill。当 LLM 未在 step 中指定角色或 Skill 时，本模块依据规则（关键词 / agent_type / 文件模式匹配）进行补齐，作为 LLM 输出的安全网。它被 `api.py`（向 LLM 注入规则摘要）和 `ui.py`（分解 step 为子任务时应用规则）两处调用。

## 公共接口

`__all__ = ["load_role_skill_map", "apply_rules"]`（`role_skill_map.py:12`）。

| 名称 | 签名 | 说明 |
|------|------|------|
| `DEFAULT_MAP` | 模块级常量 dict（`:14-43`） | 内置默认规则表：5 条 rules、`default_agent_type="developer"`、`recommended_agents`（4 个角色）、`recommended_skills=[]` |
| `load_role_skill_map(project_root=None) -> dict` | `role_skill_map.py:65` | 加载项目级规则文件；不存在或解析失败则返回 `DEFAULT_MAP`。无副作用 |
| `apply_rules(step, role_map, installed_skills=None) -> dict` | `role_skill_map.py:101` | 对单个 step 应用规则，返回 `{"skills", "agent_type", "required_skills", "matched_rules"}`。不修改入参 |
| `match_rules(step, role_map) -> list[dict]` | `role_skill_map.py:96` | 返回所有匹配该 step 的规则列表（未导出在 `__all__`，但被测试直接使用） |
| `_match_rule(rule, step) -> bool` | `role_skill_map.py:72` | （内部）判断单条规则是否匹配；三个条件之间为 AND 关系。测试直接调用 |
| `_global_map_path() -> Path` | `role_skill_map.py:46` | （内部）返回 `~/.agent_go/role_skill_map.json`，由 `load_role_skill_map` 使用（2026-07-23 前为死代码，见 ISSUE-11） |
| `_project_map_path(project_root) -> Path` | `role_skill_map.py:50` | （内部）返回 `<project_root>/.agent_go/role_skill_map.json`；`project_root=None` 时返回 `None` |
| `_load_json(path) -> Optional[dict]` | `role_skill_map.py:56` | （内部）读取 JSON 文件，文件不存在返回 `None`，解析失败记 debug 日志并返回 `None` |

`step` 入参使用的字段：`agent_type`（str）、`title`（str）、`description`（str）、`files`（list[str]）、`skills`（list[str]），均可缺省。

`installed_skills` 形如 `[{"name": ..., "description": ..., "path": ...}, ...]`，仅读取 `"name"` 键；实际由 `skills.list_skills(repo)` 提供（见 `ui.py:288`）。

## 关键逻辑与流程

**规则匹配（`_match_rule`，`:72-93`）**：规则 `match` 子句内三个条件为 AND 关系，各自缺省时跳过：

- `agent_type`：与 step 的 `agent_type` 做大小写不敏感相等比较（`:75-77`）；step 无该字段时视为不匹配。
- `keywords`：将 `title + " " + description` 转小写后，任一关键词子串命中即通过（`:79-84`），大小写不敏感。
- `file_patterns`：step 无 `files` 或为空列表时直接不匹配（`:87-89`）；否则任一文件被任一 glob 模式（`fnmatch`）命中即通过（`:90`）。
- 空 `match`（或无 `match` 键）匹配一切 step（`:73`，`rule.get("match", {})`）。

**规则应用（`apply_rules`，`:101-139`）**：

1. 收集已安装 skill 名称集合（`:102`）。
2. 遍历所有匹配规则（`match_rules`），聚合 `required` 与 `recommended` skill，仅保留已安装者并去重（`:109-116`）——未安装的 skill 被静默跳过。
3. 第一条带有 `agent_type` 的匹配规则确定 `matched_agent_type`（`:117-118`）。
4. 合并 skills：以 step 中 LLM 指定的 `skills` 为基底，追加 `required`（`:120-124`）；`recommended` 仅在 **LLM 未指定任何 skills**（`has_llm_specified` 为 False）时注入，且合并后总数上限为 2（`:126-130`）。
5. `agent_type` 优先级：step 自带 > 规则匹配 > `role_map["default_agent_type"]` > 硬编码 `"developer"`（`:132`）。
6. 返回结果含 `matched_rules`（各规则的 `match` 条件快照，`:138`），供 `ui.py:302` 判断 `_agent_type_source` 是 "rule" 还是 "default"。

**加载（`load_role_skill_map`）**：三层合并——项目级（`.agent_go/role_skill_map.json`）> 全局（`~/.agent_go/role_skill_map.json`）> 内置 `DEFAULT_MAP`。规则列表按 项目→全局→默认 顺序拼接（先匹配者优先），标量键由更具体层级覆盖（2026-07-23 起，此前只读项目级且整体替换）。

## 依赖关系

- 内部依赖：仅从 `.config` 导入 `AGENT_GO_DIR`（`role_skill_map.py:8`），即 `~/.agent_go/`（`config.py:15`）。注意 import `config` 的副作用是 `AGENT_GO_DIR.mkdir(exist_ok=True)`（`config.py:16`）。
- 标准库：`json`、`logging`、`pathlib.Path`、`fnmatch.fnmatch`、`typing`。
- 文件系统路径：读取 `<project_root>/.agent_go/role_skill_map.json`；`~/.agent_go/role_skill_map.json` 路径虽有 helper 但未实际读取。
- 调用方：`api.py:152`（`load_role_skill_map` 用于拼 system prompt 规则摘要表）、`ui.py:285-289`（`load_role_skill_map` + `apply_rules` 分解子任务）。
- 无外部 CLI 命令、无环境变量依赖。

## 数据结构与持久化

规则文件（`<project_root>/.agent_go/role_skill_map.json`）为只读、无写入，结构：

```json
{
  "rules": [
    {
      "match": {"agent_type": "...", "keywords": ["..."], "file_patterns": ["..."]},
      "skills": {"required": ["..."], "recommended": ["..."]},
      "agent_type": "..."
    }
  ],
  "default_agent_type": "developer",
  "recommended_agents": ["..."],
  "recommended_skills": ["..."]
}
```

- `rules[].match` 三个键均可选，组合时为 AND。
- `rules[].agent_type` 是规则的"输出"字段（匹配后指派的角色），与 `match.agent_type`（匹配条件）语义不同。
- `recommended_agents` / `recommended_skills` 在 `DEFAULT_MAP` 中存在（`:41-42`），但本模块不消费它们（由 `api.py` 的 prompt 组装逻辑读取，见 `api.py:169` 附近）。

`apply_rules` 返回值结构：`{"skills": list[str], "agent_type": str, "required_skills": list[str], "matched_rules": list[dict]}`。

## 错误处理与边界情况

- 规则文件不存在、JSON 解析失败或 IO 错误：`_load_json` 记 debug 日志后返回 `None`，`load_role_skill_map` 回退到 `DEFAULT_MAP`（`:56-69`）——永不抛异常。
- 规则文件加载成功但顶层是空 dict：`if loaded:` 判假，同样回退 `DEFAULT_MAP`。
- step 缺字段时全部有默认值兜底（`title`/`description`/`agent_type` 默认 `""`，`files`/`skills` 默认 `[]`）。
- 未安装的 required/recommended skill 静默跳过（`:112, :115`），不告警。
- 关键词匹配是**子串匹配**而非词边界匹配（如 `"auth"` 会命中 `"author"`）。
- 无超时/中断相关逻辑（纯内存计算）。

## 测试覆盖

对应测试文件 `tests/test_role_skill_map.py`（148 行），覆盖：

- `TestMatchRule`：agent_type 匹配、关键词匹配及大小写不敏感、文件模式匹配、多条件 AND、空 match 匹配一切。
- `TestMatchRules`：多规则同时命中、无命中返回空列表。
- `TestApplyRules`：required skill 注入、不覆盖 LLM 已指定 skill、recommended 仅在 LLM 未指定时注入、未安装 skill 跳过、规则指派 agent_type、LLM 指定的 agent_type 优先、default_agent_type 回退、文件模式指派 architect。
- `TestLoadRoleSkillMap`：默认规则表结构（5 条 rules、`default_agent_type`、recommended 字段）。

未覆盖：项目级 JSON 文件的加载路径（`load_role_skill_map` 传入真实 `project_root` 的分支）。

## 维护注意事项

- **已修复（2026-07-23，docs/ISSUES.md ISSUE-11）**：`_global_map_path()` 曾为死代码且项目级文件整体替换 `DEFAULT_MAP`；现实现三层合并加载（项目 > 全局 > 默认），自定义文件无需自带全部 rules。
- **recommended 上限硬编码**：`len(merged_skills) < 2`（`:129`）意味着 LLM 已指定 1 个 skill 时 recommended 注入被 `has_llm_specified` 拦截，但该上限在语义上是"总数上限"——若未来放开 recommended 注入条件，需注意此魔数。
- **隐式耦合**：返回 dict 的 `"matched_rules"` 键被 `ui.py:302` 用于推导 `_agent_type_source`，改名需同步修改 ui.py；`recommended_agents`/`recommended_skills` 字段被 `api.py` 读取而本模块不使用，删除 DEFAULT_MAP 中这些字段会破坏 prompt 组装。
- `match_rules` 与 `_match_rule` 虽未列入 `__all__`，但测试直接 import，改名会破坏测试。
- 默认规则中的关键词为中英混合（如 "安全"/"security"），新增语言场景需同时补充。
