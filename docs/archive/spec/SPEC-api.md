# api 模块规格说明

## 概述

`agent_go/api.py` 是 agent_go 的 Plan 生成与缓存模块，负责整个 "Plan -> Decompose -> Execute" 工作流中的 **Plan 阶段**。它调用 LLM HTTP API（支持 anthropic / openai 兼容协议）为开发任务生成结构化执行计划（JSON plan），在 LLM 不可用时提供本地模型 + 规则匹配的降级拆分（`decompose_fallback`），并实现了基于文件系统的 Plan 缓存以加速重复任务。模块仅使用 Python stdlib（`urllib`、`json`、`hashlib` 等），无第三方依赖。

## 公共接口

模块通过 `__all__`（`agent_go/api.py:14`）显式导出 7 个函数。

### `call_api(config, messages, logger) -> str` — `agent_go/api.py:20`

- **参数**：
  - `config: dict[str, Any]` — 全局配置，读取 `config["plan_api"]` 下的 `provider`（默认 `"anthropic"`）、`base_url`、`model`、`max_tokens`（默认 4096）、`temperature`（默认 0.2）。
  - `messages: list[dict[str, Any]]` — chat 消息列表（`[{"role": ..., "content": ...}]`）。
  - `logger: logging.Logger` — 用于记录 `api_call` / `api_error` 事件。
- **返回值**：LLM 响应文本（str）。anthropic 协议取 `data["content"][0]["text"]`，其他 provider 取 `data["choices"][0]["message"]["content"]`。
- **副作用**：向 `base_url` 发送一次 HTTP POST（超时 60 秒）；通过 `log_event` 记录调用事件。
- **异常**：API key 缺失、JSON 解析失败、响应结构异常、HTTP 错误、网络错误、超时/IO 错误均统一转为 `RuntimeError`（详见"错误处理"）。

### `generate_plan(task, repo, config, logger, supplement="", reference_docs="", iteration=1, skill_context="", no_cache=False) -> dict` — `agent_go/api.py:100`

- **参数**：
  - `task: str` — 任务描述。
  - `repo: Path` — 项目根目录。
  - `config / logger` — 同 `call_api`。
  - `supplement` — 用户补充说明（附加到 user content）。
  - `reference_docs` — 参考文档文本（附加到 user content，受预算截断）。
  - `iteration: int` — 第几轮生成（仅第 1 轮参与缓存）。
  - `skill_context` — 预加载的 Skill 领域知识文本，注入 system prompt。
  - `no_cache: bool` — 强制跳过缓存读写。
- **返回值**：plan dict，结构含 `overview`、`steps[]`（每个 step 含 `id/title/description/files/verification/risks/agent_prompt/skills/agent_type`）、`dependencies`、`estimated_effort`、`shared_resources`（缺失时以本地分析结果补齐）。
- **副作用**：可能读写 Plan 缓存文件；调用 LLM API；记录 `plan_generate` / `plan_complete` 事件。
- **异常**：API 返回无法解析为 JSON 时抛 `RuntimeError`。

### `decompose_fallback(task, repo, config, logger) -> list[dict]` — `agent_go/api.py:253`

- **参数**：`task` 任务描述；`repo` 未实际使用（仅为签名兼容）；`config` 中读取 `fallback.local_model_url`（默认 `http://localhost:8000/v1/chat/completions`）与 `fallback.local_model_name`（默认 `"qwen"`）。
- **返回值**：子任务列表，每项含 `id`（`sub-N` 格式）及 `title/description/files_hint/agent_prompt` 等字段。
- **行为**：三级降级 — ① 调本地 OpenAI 兼容模型（超时 10 秒）→ ② 按 `DECOMPOSE_RULES` 关键词匹配预置规则 → ③ 返回单个子任务（直接执行主任务）。
- **副作用**：可能向本地模型发 HTTP 请求。

### `get_cache_key(task, repo) -> str` — `agent_go/api.py:296`

- 计算缓存键：`sha256(task | project_files[:2000] | remote | branch)`，返回 hex 字符串。2026-07-23 起不再混入 `commit`（此前每次提交都使缓存失效，见 docs/ISSUES.md ISSUE-2）。
- **注意**：docstring 写的是 `project_files[0:100]`，实际代码截断 2000 字符（`agent_go/api.py:302`），两者不一致。

### `load_cached_plan(cache_key, task, config, logger) -> Optional[dict]` — `agent_go/api.py:310`

- 从缓存读取 plan。校验：文件存在且可解析、未过期（`cache.plan_ttl`，默认 86400 秒，过期即删除文件）、`plan["steps"]` 非空、缓存的 `meta.task` 与当前 `task[:200]` 匹配（防键碰撞）。
- 命中时更新 `meta.last_hit_at` / `meta.hit_count` 并写回文件，返回 plan；否则返回 `None`。

### `save_cached_plan(cache_key, plan, task, repo, config) -> None` — `agent_go/api.py:356`

- 写入缓存文件 `~/.agent_go/cache/plans/<key[:2]>/<key>.json`。`cache.enabled` 为 `False`（默认 `True`）时静默跳过。

### `list_cache_entries() -> list[dict]` — `agent_go/api.py:393`

- 扫描缓存目录下所有 `.json` 条目，跳过无法解析的文件，按 `meta.created_at` 倒序返回完整 entry 列表。

### `clean_expired_cache(config) -> int` — `agent_go/api.py:407`

- 删除所有超过 TTL 的缓存文件，返回删除数量。

### 内部函数

- `_cache_dir() -> Path` — `agent_go/api.py:290`：返回 `~/.agent_go/cache/plans/` 并确保存在（**内部**）。
- `_format_age(iso_str) -> str` — `agent_go/api.py:380`：将 ISO 时间格式化为 "Xm前 / Xh前 / Xd前"，解析失败返回 `"?"`（**内部**）。

## 关键逻辑与流程

### Plan 生成流程（`generate_plan`）

1. **缓存检查**（`agent_go/api.py:107-118`）：仅当 `not no_cache and iteration == 1 and not supplement and not reference_docs` 时查询缓存；命中则直接返回，并记录 `plan_complete`（`cache_hit: True`）。
2. **本地项目分析**（`agent_go/api.py:120-122`）：调用 `analyze_project` / `get_git_info` / `get_resource_map` 收集文件列表、git 信息、共享资源清单。
3. **Prompt 预算控制**：
   - 常量 `MAX_SYSTEM_PROMPT_CHARS = 6000`、`MAX_USER_CONTENT_CHARS = 12000`（`agent_go/api.py:125-126`）。
   - 项目文件列表截断至前 100 行（`agent_go/api.py:128-133`）。
   - Skill 表最多展示 10 条（`SKILL_TABLE_MAX`，`agent_go/api.py:136-149`），角色-Skill 映射规则最多 15 条（`agent_go/api.py:157`）。
   - `skill_context` 仅在 system prompt 剩余空间 > 500 字符时注入，超出则截断（`agent_go/api.py:180-189`）。
   - `reference_docs` 截断至 `MAX_USER_CONTENT_CHARS // 3`（`agent_go/api.py:194-197`）。
   - 最终兜底截断 system / user content 并告警（`agent_go/api.py:207-214`）。
4. **构造 messages 并调用 `call_api`**（`agent_go/api.py:217-222`）。
5. **JSON 解析**（`agent_go/api.py:224-231`）：先 `json.loads`，失败则用正则 `\{.*\}`（DOTALL）提取首个 JSON 对象再解析，仍失败抛 `RuntimeError`。
6. **shared_resources 补齐**（`agent_go/api.py:234-242`）：plan 中缺失则以本地 `resource_map` 填充；已存在但缺 `git_remote` / `git_branch` 时用本地 git 信息补齐。
7. **写缓存并返回**（`agent_go/api.py:248-251`）：仅第 1 轮、非 no_cache、非缓存命中时写入。

### 缓存目录布局

```
~/.agent_go/cache/plans/<cache_key前2字符>/<cache_key>.json
```

按 key 前两字符分桶，避免单目录文件过多。

### 降级拆分流程（`decompose_fallback`）

1. 本地模型（10 秒超时）：system prompt 要求输出 JSON 数组（含 `title/description/files_hint/agent_prompt`），用正则 `\[.*\]` 提取数组解析，成功后为每项加 `id: "sub-N"`。
2. 任何异常（网络、解析、结构）都捕获后落入规则匹配：遍历 `DECOMPOSE_RULES`，任务文本（小写）包含任一 `patterns` 关键词即返回该规则的预置子任务。
3. 无匹配则返回 `[{"id": "sub-1", "title": "执行主任务", ...}]` 单任务兜底。

## 依赖关系

### 内部模块依赖

| 来源 | 符号 | 用途 |
|------|------|------|
| `.config` | `get_api_key` | 取 `AGENT_GO_API_KEY` 环境变量，否则 `config["plan_api"]["api_key"]` |
| `.config` | `log_event` | 记录结构化事件（JSON debug 日志） |
| `.config` | `DECOMPOSE_RULES` | 降级拆分的预置规则表（如 JWT/认证类任务） |
| `.config` | `AGENT_GO_DIR` | `~/.agent_go`，缓存根目录 |
| `.git_utils` | `analyze_project(repo) -> str` | 项目文件列表文本 |
| `.git_utils` | `get_git_info(repo) -> {"remote","branch","commit"}` | git 元信息 |
| `.git_utils` | `get_resource_map(repo, git_info) -> dict` | 共享资源清单（含 `project_root/git_remote/git_branch/git_commit/directories/key_files`） |
| `.skills` | `list_skills(repo) -> list[dict]` | 已安装 Skill（`name` / `description`），注入 system prompt |
| `.role_skill_map` | `load_role_skill_map(repo) -> dict` | 角色-Skill 映射（`rules` / `recommended_agents` / `recommended_skills`） |

### 外部依赖

- **HTTP 端点**：`config["plan_api"]["base_url"]`（LLM API）；`config["fallback"]["local_model_url"]`（本地模型，默认 `http://localhost:8000/v1/chat/completions`）。
- **环境变量**：`AGENT_GO_API_KEY`（经 `get_api_key` 间接读取）。
- **文件系统**：`~/.agent_go/cache/plans/`（Plan 缓存读写，import `.config` 时即创建 `~/.agent_go`）。
- **间接依赖**：`git_utils` / `skills` 内部会调用 `git` CLI 并扫描项目目录，本模块不直接执行外部命令。

## 数据结构与持久化

### Plan dict（`generate_plan` 返回值，由 LLM 生成）

```json
{
  "overview": "任务概述",
  "steps": [{"id": 1, "title": "...", "description": "...", "files": [...],
             "verification": "...", "risks": [...], "agent_prompt": "...",
             "skills": [...], "agent_type": "developer|architect|reviewer|tester"}],
  "dependencies": {"2": [1]},
  "estimated_effort": "...",
  "shared_resources": {"directories": [...], "git_remote": "...", "git_branch": "...",
                       "config_files": [...], "env_vars": [...]}
}
```

缺失 `shared_resources` 时以本地 `resource_map`（字段为 `project_root/git_remote/git_branch/git_commit/directories/key_files`）补齐——注意两者字段集合并不完全一致（见"维护注意事项"）。

### 缓存条目（`~/.agent_go/cache/plans/xx/<key>.json`，UTF-8 JSON，indent=2）

```json
{
  "cache_key": "<sha256 hex>",
  "plan": { ... },
  "meta": {
    "created_at": "%Y-%m-%dT%H:%M:%S",
    "last_hit_at": "%Y-%m-%dT%H:%M:%S",
    "hit_count": 0,
    "task": "<task[:200]>",
    "repo": "<repo 路径字符串>",
    "ttl": 86400
  }
}
```

## 错误处理与边界情况

- **统一异常策略**：`call_api` 内所有失败（缺 key、JSON 解析错、响应结构错、HTTP 错误、网络错误、超时/IO）均记录 `api_error` 事件后转为带上下文的 `RuntimeError`（`agent_go/api.py:26-27, 50-55, 62-67, 76-98`），不向上层暴露 `urllib` 异常类型。
- **HTTP 错误体读取**：`e.read()` 自身失败时兜底用 `str(e)`（`agent_go/api.py:77-81`）。
- **Plan JSON 修复**：先用正则提取 `{...}` 再解析；提取失败抛 `RuntimeError`（`agent_go/api.py:224-231`）。正则贪婪匹配，若响应含多个 JSON 块可能取错。
- **缓存健壮性**：缓存文件损坏（JSON 解析/IO 错误）→ debug 日志并视为未命中（`agent_go/api.py:316-320`）；时间戳非法 → debug 日志并跳过过期判断（不删除，`agent_go/api.py:331-332`）；过期文件命中时直接删除。
- **降级链兜底**：`decompose_fallback` 用裸 `except Exception` 吞掉本地模型的所有错误，保证总能返回至少一个子任务（`agent_go/api.py:276-277, 283`）。
- **超时**：主 API 60 秒（`agent_go/api.py:45`），本地模型 10 秒（`agent_go/api.py:269`）。均无重试机制。
- **无键盘中断处理**：未捕获 `KeyboardInterrupt`，中断时由上层处理。

## 测试覆盖

测试文件：`tests/test_api.py`（524 行），使用 `unittest.mock` 模拟 `urllib.request.urlopen`，以 `tmp_path` + monkeypatch 隔离 `~/.agent_go` 缓存目录。

- `TestCallApi`：anthropic / openai / deepseek 三种 provider 的请求头与响应解析、自定义 `base_url`、缺 API key 抛错。
- `TestPlanCache`：cache key 的确定性、不同任务/不同 repo 产生不同 key、save→load 往返、`cache.enabled=False` 时不写入、过期缓存删除、task 不匹配跳过、`list_cache_entries` 排序、`clean_expired_cache` 删除计数。
- `TestDecomposeFallback`：规则匹配（JWT、test 关键词）、无规则时单任务兜底、本地模型路径、`sub-N` id 格式。
- `TestGeneratePlan`：缺 key 抛错、首轮缓存命中直接返回、项目文件列表截断至 100 行、`skill_context` 截断、`supplement`/`reference_docs` 透传至 user content。

未覆盖：`call_api` 的 HTTP/网络/超时错误分支、`generate_plan` 的 JSON 正则修复路径与 `shared_resources` 补齐逻辑。

## 维护注意事项

- **已修复（2026-07-23，docs/ISSUES.md ISSUE-2）**：`get_cache_key` 曾混入 `commit` 导致每次提交后缓存失效，且 docstring 与实现不符；现已从 key 中移除 `commit` 并修正 docstring。
- **shared_resources 字段集合不一致**：LLM 约定输出 `config_files`/`env_vars`，本地 `resource_map` 提供 `project_root`/`key_files`，`generate_plan` 的补齐逻辑只补 `git_remote`/`git_branch`（`agent_go/api.py:234-242`），下游消费方需容忍两种字段形态。
- **已修复（2026-07-23，docs/ISSUES.md ISSUE-10）**：`load_cached_plan` 曾不检查 `cache.enabled`（只禁写不禁读）；现函数开头即检查，`enabled=false` 时直接返回 None。
- **缓存查询条件重复**：缓存键计算本身调用 `analyze_project` + `get_git_info`（`agent_go/api.py:298-299`），未命中时 `generate_plan` 会再调用一次（`agent_go/api.py:120-122`），大仓库下有重复 I/O 开销，可考虑复用结果。
- **硬编码值**：Prompt 预算 6000/12000 字符、文件列表 100 行、Skill 表 10 条、映射规则 15 条、skill_context 注入阈值 500 字符、API 超时 60s、本地模型超时 10s、TTL 默认 86400s 均硬编码或仅有 config 局部覆盖，未集中到配置层。
- **正则解析脆弱**：`generate_plan` 的 `\{.*\}` 与 `decompose_fallback` 的 `\[.*\]` 均为贪婪匹配，LLM 输出含额外花括号/方括号文本时可能解析到错误片段。
- **隐式耦合**：`decompose_fallback` 返回的子任务使用 `files_hint` 字段，而 `generate_plan` 的 steps 使用 `files`，两者 schema 不同，调用方（如 executor）需分别处理；`repo` 参数在 `decompose_fallback` 中未使用，改动签名时需同步调用点。
- **provider 判断分支**：`call_api` 中 anthropic 与其余 provider 的 payload 构造仅字段顺序不同（`agent_go/api.py:36-39`），属冗余代码，可合并。
