# skills 模块规格说明

## 概述

`agent_go/skills.py` 是 agent_go 的 Skill 加载与注入子系统。它解析采用「YAML frontmatter + Markdown body」格式的 `SKILL.md` 文件，按优先级从用户全局目录和项目目录查找，并将解析结果渲染为两种文本格式：注入 Plan prompt 的轻量摘要、注入子任务 `TASK.md` 的完整知识内容。纯 Python stdlib 实现（frontmatter 用正则 + 简单解析，不依赖 PyYAML），与项目整体的无第三方依赖约束一致。上游调用方为 `cli.py`（Plan 阶段加载/自动匹配）和 `executor.py`（执行阶段注入 TASK.md）。

## 公共接口

模块通过 `__all__` 导出 6 个函数（`agent_go/skills.py:23-26`）。

### 数据类

- `@dataclass class Skill`（`agent_go/skills.py:34-49`）
  - 字段：`name: str`、`description: str`、`path: Path`、`frontmatter: dict = {}`、`body: str = ""`
  - 属性 `allowed_tools -> list[str]`：读取 `frontmatter["allowed-tools"]`，支持逗号分隔字符串或列表两种形式，其他类型返回 `[]`。

### 常量

- `AGENT_GO_SKILLS_DIR = Path.home() / ".agent_go" / "skills"`（`agent_go/skills.py:30-31`）
  - **注意：模块 import 时即执行 `mkdir(parents=True, exist_ok=True)`，有文件系统副作用。**

### 公开函数

- `load_skill(name: str, project_root: Optional[Path] = None) -> Optional[Skill]`（`:105`）
  按名称加载单个 Skill；找不到返回 `None`（不抛异常）。`Skill.name` 优先取 frontmatter 中的 `name`，缺省回退到目录名 `name`。
- `load_skills(names: list[str], project_root: Optional[Path] = None) -> list[Skill]`（`:123`）
  批量加载，静默跳过不存在的 Skill。
- `list_skills(project_root: Optional[Path] = None) -> list[dict]`（`:133`）
  扫描全局目录与项目目录，返回 `[{"name", "description", "path"}]`。只收录含 `SKILL.md` 的子目录，按目录名排序遍历。
- `discover_skills(task: str, project_root: Optional[Path] = None, max_skills: int = 3) -> list[Skill]`（`:161`）
  基于关键词重叠的自动匹配，按命中词数降序取前 `max_skills` 个。
- `render_skill_for_plan(skill: Skill) -> str`（`:185`）
  渲染为 Plan prompt 注入格式：`### Skill: <name>` 标题 + 描述 + 推荐工具 + body 前 500 字符摘要（超长追加 `\n... (截断)`）。
- `render_skill_for_execution(skill: Skill) -> str`（`:202`）
  渲染为 TASK.md 执行注入格式：`## Skill 知识注入: <name>` 标题 + 领域/推荐工具（加粗）+ 完整 body。

### 私有函数（承担关键职责）

- `_parse_frontmatter(text: str) -> tuple[dict, str]`（`:57`，内部）
  正则提取 frontmatter 与正文；逐行解析 `key: value`，key 转小写；value 自动类型转换：`true/false` → bool、纯数字 → int、`[...]` → 尝试 `json.loads`（失败保留原字符串并 debug 日志）。
- `_find_skill_file(name, project_root) -> Optional[Path]`（`:88`，内部）
  按优先级返回第一个存在的候选路径。

## 关键逻辑与流程

**Skill 查找优先级**（`_find_skill_file`，`agent_go/skills.py:88-102`）：
1. `~/.agent_go/skills/<name>/SKILL.md`（全局，优先级更高）
2. `<project_root>/.agent_go/skills/<name>/SKILL.md`（项目级，仅在传入 `project_root` 时参与）

注意这与常见约定相反：**全局优先于项目级**，同名时项目级 skill 会被全局覆盖。

**frontmatter 解析**（`_parse_frontmatter`，`:54-85`）：
- 用 `_FRONTMATTER_RE = ^---\s*\n(.*?)\n---\s*\n`（DOTALL）匹配文件开头的 frontmatter 块；不匹配则返回 `({}, 原文)`。
- 只支持单层 `key: value`（`line.partition(":")` 取第一个冒号），不支持嵌套 YAML、多行值。`#` 开头行视为注释跳过。

**自动匹配算法**（`discover_skills`，`:161-180`）：
- 对每个已安装 Skill 的 `description` 与任务文本分别用 `\w+` 提取词集合（均转小写），计算交集大小作为匹配度，降序排序取前 N。
- `\w` 在 Python re 中对 Unicode 生效，中文字符整体会匹配为长词，因此中文 description 的词粒度匹配效果有限（几乎只有整词/英文关键词能命中）。

**渲染两阶段**：
- Plan 阶段（`cli.py:211` 调用 `render_skill_for_plan`）：截断摘要，控制 prompt 体积。
- 执行阶段（`executor.py:173` 调用 `render_skill_for_execution`）：注入完整 body 到 TASK.md；`executor.py:170-178` 对找不到的 skill 记录 warning 并收集到 `unresolved_skills`。

## 依赖关系

**内部依赖**：无 import 其他 agent_go 模块（叶子模块）。被以下模块调用：
- `cli.py:13` — `load_skills` / `discover_skills` / `render_skill_for_plan` / `list_skills`（Plan 阶段与 `cmd_skills` 子命令）
- `executor.py:167` — `load_skill` / `render_skill_for_execution` / `list_skills`（TASK.md 生成，函数内延迟 import）
- `api.py:9`、`ui.py:286` — `list_skills`

**外部依赖**：
- 仅 stdlib：`re`、`json`、`logging`、`pathlib.Path`、`typing.Optional`、`dataclasses`
- 文件系统路径：`~/.agent_go/skills/<name>/SKILL.md`、`<project>/.agent_go/skills/<name>/SKILL.md`
- 无 CLI 命令、无环境变量依赖

## 数据结构与持久化

**无持久化写入**（除模块加载时创建 `~/.agent_go/skills/` 目录）。模块只读 Skill 文件。

**SKILL.md 文件格式**（模块 docstring，`:1-14`）：
```
---
name: security-review
description: 安全审查 — 涉及认证、权限、加密
allowed-tools: Read, Write
---
# Skill 正文内容（Markdown）
```

**Skill dataclass**（`:34-40`）：`name` / `description` / `path` / `frontmatter` / `body`，见「公共接口」。

**`list_skills` 返回 dict**：`{"name": str, "description": str, "path": str}`。

## 错误处理与边界情况

- **Skill 不存在**：`load_skill` 返回 `None`，`load_skills` 静默跳过；由调用方（`executor.py:177`、`cli.py:194`）决定 warning/提示。
- **文件读取**：`read_text(encoding="utf-8", errors="replace")`（`:111`），非法编码字节被替换为 U+FFFD，不抛异常。
- **frontmatter 缺失或为空**：返回 `({}, 原文)`；`Skill.name` 回退目录名，`description` 回退 `""`。
- **JSON 列表解析失败**：保留原始字符串，仅 debug 日志（`:82-83`）。
- **`allowed-tools` 类型异常**（非 str/list）：返回 `[]`。
- 无超时/中断处理（纯本地文件操作）。
- 未捕获的潜在异常：`path.read_text` 的 `OSError`（权限等）、`list_skills` 中 `iterdir` 的权限错误均会向上抛出。

## 测试覆盖

对应测试文件：`tests/test_skills.py`（5 个测试类，约 15 个用例）。覆盖场景：

- `TestFrontmatterParsing`：基本解析、无 frontmatter、bool 值、JSON list 值、空 frontmatter。
- `TestSkillProperties`：`allowed_tools` 的 str / list / 缺省三种形态。
- `TestRenderSkill`：两种渲染格式的内容断言、plan 渲染 500 字符截断。
- `TestLoadSkills`：批量加载、缺失跳过、不存在返回 `None`、`list_skills` 列举与空目录；通过 `patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path)` 隔离全局目录。`test_load_builtin_skill` 依赖本机全局安装，属于环境相关测试（无则跳过断言）。

**未覆盖**：`discover_skills`（自动匹配算法无测试）、`project_root` 项目级查找优先级。

## 维护注意事项

- **import 副作用**：`agent_go/skills.py:31` 在模块导入时创建 `~/.agent_go/skills/`。测试若 patch `AGENT_GO_SKILLS_DIR` 必须在 import 之后进行，且该目录创建无法通过 patch 避免。
- **优先级反直觉**：全局目录优先于项目目录（`:92-97`），若未来希望项目级覆盖全局，需调整 `_find_skill_file` 中 candidates 顺序，并同步检查 `list_skills` 的去重行为（当前不去重，同名 skill 会在列表中出现两次：全局一次、项目一次，且项目条目的 `name/description` 实际来自全局文件，因为 `load_skill` 内部仍按全局优先解析——这是一个隐式不一致点）。
- **frontmatter 解析器能力有限**：仅单层 key-value，不支持带引号字符串、嵌套、多行值；value 含冒号（如 URL `https://...`）时 `partition(":")` 行为正确（取第一个冒号），但 `key` 中含冒号会解析错误。引入更复杂格式时需换用真正的 YAML 解析（注意项目无第三方依赖约束）。
- **硬编码值**：plan 渲染截断长度 500 字符（`:195-197`）；`discover_skills` 默认 `max_skills=3`（cli.py 中可被 `config.skills.max_auto_skills` 覆盖）。
- **中文匹配效果差**：`discover_skills` 基于 `\w+` 词集合交集，中文无分词，description 为纯中文长句时几乎匹配不到；改进方向是按 bigram/子串匹配或引入关键词字段。
- **`__all__` 未导出 `Skill` 与 `AGENT_GO_SKILLS_DIR`**，但测试与调用方均直接 import 它们，属事实上的公共接口，重构时需保持兼容。
