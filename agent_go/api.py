import hashlib
import sys
import json, re, time, logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from .config import get_api_key, log_event, DECOMPOSE_RULES, AGENT_GO_DIR, meter_event
from .git_utils import analyze_project, get_git_info, get_resource_map
from .skills import list_skills
from .role_skill_map import load_role_skill_map
from .metrics import estimate_cost
from .router import resolve_provider, call_with_role
from .utils import SAFE_VERIFICATION_PREFIXES

logger = logging.getLogger(__name__)

__all__ = [
    "call_api", "generate_plan", "decompose_fallback",
    "get_cache_key", "load_cached_plan", "save_cached_plan",
    "list_cache_entries", "clean_expired_cache",
]

def _resolve_thinking_payload(api_cfg: dict, model: str, provider: str) -> dict:
    """解析 thinking 配置（声明式，三层设计 P1.4）：② binding(api_cfg) 覆盖 > ① registry 声明。

    优先级：
      1. api_cfg.thinking=True（② 场景显式开启）→ 按 provider 格式构造
      2. ① registry[model].reasoning.thinking.required=True → 声明式开启（接新模型零代码）
      3. 否则不开启
    返回 thinking payload dict（空 dict = 不注入）。
    """
    # ② 场景显式开启（api_cfg.thinking）
    if api_cfg.get("thinking"):
        if provider == "anthropic":
            return {"type": "enabled", "budget_tokens": int(api_cfg.get("thinking_budget", 1024))}
        return {"type": "enabled"}
    # ① registry 声明式（模型固有推理特性）
    try:
        from .models_registry import get_model
        ent = get_model(model)
    except Exception:
        ent = None
    if ent and ent.thinking.required:
        budget = int(api_cfg.get("thinking_budget", ent.thinking.budget_tokens or 1024))
        if ent.thinking.format == "anthropic" or provider == "anthropic":
            param = ent.thinking.budget_param or "budget_tokens"
            return {"type": "enabled", param: budget}
        return {"type": "enabled"}
    return {}


def _resolve_planner_api_cfg(config: dict[str, Any]) -> dict[str, Any]:
    """planner 配置解析（三层设计 P3.1）：router.roles.planner > planner_api > plan_api。

    router.enabled 且 roles.planner 配置时优先（② 角色绑定）；否则 fallback 到
    planner_api / plan_api 配置块（现有逻辑，router 未启用时完全兼容）。
    """
    try:
        from .router import resolve_role
        route = resolve_role("planner", config)
        if route is not None:
            p = route.primary
            cfg: dict[str, Any] = {
                "provider": p.provider,
                "base_url": p.base_url,
                "model": p.model,
                "api_key": p.api_key,
            }
            # ② 场景绑定字段透传（thinking/budget 覆盖①默认）
            if getattr(p, "thinking", None) is not None:
                cfg["thinking"] = p.thinking
            if getattr(p, "thinking_budget", None) is not None:
                cfg["thinking_budget"] = p.thinking_budget
            return cfg
    except Exception:
        pass
    return config.get("planner_api") or config["plan_api"]


def call_api(config: dict[str, Any], messages: list[dict[str, Any]], logger: logging.Logger) -> str:
    # planner 配置解析优先级（模型实体三层 P3.1：plan_api/planner_api 合并 → roles.planner）：
    #   ② router.roles.planner（角色绑定，router.enabled 时）> planner_api > plan_api
    api_cfg = _resolve_planner_api_cfg(config)
    provider = api_cfg.get("provider", "anthropic")
    base_url = api_cfg["base_url"]
    api_key = get_api_key(config)
    model = api_cfg["model"]
    # 本地后端（127.0.0.1/localhost）跳过 api_key 强制检查：本地代理通常无需
    # key（已实测 localhost:4000 无 key 可用）。纯本地模式（无网络、无云 key）
    # 下 generate_plan/evaluator 才能正常走本地，否则 api_key 空直接 raise 卡死。
    _is_local_url = re.search(r"(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])", base_url)
    if not api_key and not _is_local_url:
        raise RuntimeError("API Key 未配置。请设置 AGENT_GO_API_KEY")

    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    # thinking 注入（声明式，P1.4）：② binding 覆盖 > ① registry 声明（接新模型零代码）
    _thinking = _resolve_thinking_payload(api_cfg, model, provider)
    if provider == "anthropic":
        payload = {"model": model, "max_tokens": api_cfg.get("max_tokens", 4096), "temperature": api_cfg.get("temperature", 0.2), "messages": messages}
        if _thinking:
            payload["thinking"] = _thinking
    else:
        payload = {"model": model, "messages": messages, "max_tokens": api_cfg.get("max_tokens", 4096), "temperature": api_cfg.get("temperature", 0.2)}
        if _thinking:
            payload["thinking"] = _thinking
        if api_cfg.get("reasoning_effort"):
            payload["reasoning_effort"] = api_cfg["reasoning_effort"]
        # JSON 输出（planner/evaluator 需合法 JSON）：response_format json_object。
        if api_cfg.get("json_output") or api_cfg.get("response_format") == "json_object":
            payload["response_format"] = {"type": "json_object"}

    import urllib.request, urllib.error
    req = urllib.request.Request(base_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    start = time.time()
    _timeout_sec = api_cfg.get("timeout_ms", 120000) / 1000.0
    try:
        with urllib.request.urlopen(req, timeout=_timeout_sec) as resp:
            latency = time.time() - start
            raw_body = resp.read()
            # R8 路由归因（llama.cpp）：X-Proxy-Route-Target/Actual-Model/Reason/Cost 响应头，
            # 纠正 metering is_local 误判（force_fallback 云端失败回退本地时，按 URL 会全标 local）。
            # getattr 安全读取（mock/自定义响应对象可能无 headers 属性，兼容测试与旧代理）。
            _resp_headers = getattr(resp, "headers", {}) or {}
            route_target = _resp_headers.get("X-Proxy-Route-Target", "")
            route_actual_model = _resp_headers.get("X-Proxy-Route-Actual-Model", "")
            route_reason = _resp_headers.get("X-Proxy-Route-Reason", "")
            try:
                route_cost = float(_resp_headers.get("X-Proxy-Route-Cost", "") or 0.0)
            except (TypeError, ValueError):
                route_cost = 0.0
            try:
                data = json.loads(raw_body)
            except json.JSONDecodeError as e:
                log_event(logger, "api_error", {
                    "provider": provider, "error": "json_parse",
                    "message": str(e)[:200], "response_preview": raw_body[:200].decode("utf-8", errors="replace"),
                })
                raise RuntimeError(f"API 返回无法解析为 JSON: {e}") from e
            # 解析响应内容
            try:

                if provider == "anthropic":
                    _blocks = data.get("content", [])
                    _text_block = next((b for b in _blocks if isinstance(b, dict) and b.get("type") == "text"), None)
                    content = _text_block["text"] if _text_block else _blocks[0].get("text", str(_blocks[0]))
                else:
                    content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                log_event(logger, "api_error", {
                    "provider": provider, "error": "structure",
                    "message": f"响应结构异常: {e}", "keys": list(data.keys())[:10] if isinstance(data, dict) else str(type(data)),
                })
                raise RuntimeError(f"API 响应结构异常 ({provider}): {e}") from e
            usage = data.get("usage", {})
            # 兼容 Anthropic (input_tokens/output_tokens) 和 OpenAI (prompt_tokens/completion_tokens) 格式
            prompt_tokens = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("output_tokens") or usage.get("completion_tokens", 0)
            # R8 归因纠正：actual_model 用代理真实后端模型（route_actual_model），cost 按它重算；
            # is_local 按 route_target（cloud→False 云端计费，local→True 本地/TCO）。
            # 无 R8（非代理/旧代理）时保持现有按 URL/路由名的行为（兼容）。
            _meter_model = route_actual_model or model
            if route_target:
                _is_local = route_target == "local"
                _cost = route_cost if route_cost > 0 else round(
                    estimate_cost(provider, _meter_model, prompt_tokens, completion_tokens), 6)
            else:
                _is_local = None
                _cost = round(estimate_cost(provider, model, prompt_tokens, completion_tokens), 6)
            log_event(logger, "api_call", {
                "provider": provider, "model": model,
                "latency_ms": round(latency * 1000, 2), "response_len": len(content),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": _cost,
            })
            # Phase 1 配套：结构化计量日志
            _event = {
                "role": "planner",
                "virtual_model": "agentgo-planner",
                "actual_provider": provider,
                "actual_model": _meter_model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": _cost,
                "latency_ms": round(latency * 1000, 2),
                "result": "success",
                "fallback_reason": "",
                "task_id": config.get("_task_id", ""),
                "subtask_id": "",
            }
            if route_target:
                _event["route_target"] = route_target
                _event["route_actual_model"] = route_actual_model
                _event["route_reason"] = route_reason
                _event["is_local"] = _is_local
            meter_event(config.get("_metering_path"), _event)
            return content
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception as exc:
            logger.debug("Failed to read HTTP error body: %s", exc)
            err_body = str(e)
        log_event(logger, "api_error", {
            "provider": provider, "status_code": e.code,
            "error_message": str(e)[:200], "response_body": err_body,
        })
        raise RuntimeError(f"API 请求失败 ({provider}, HTTP {e.code}): {err_body}") from e
    except urllib.error.URLError as e:
        log_event(logger, "api_error", {
            "provider": provider, "error": "network",
            "reason": str(e.reason)[:200],
        })
        raise RuntimeError(f"网络错误 ({provider}): {e.reason}") from e
    except (OSError, TimeoutError) as e:
        log_event(logger, "api_error", {
            "provider": provider, "error": "timeout_or_io",
            "message": str(e)[:200],
        })
        raise RuntimeError(f"连接超时或 IO 错误 ({provider}): {e}") from e


def _detect_runtime_info(repo: Path) -> str:
    """Detect project runtime version info (Python/Django/Node etc.) to help planner avoid incompatible code."""
    parts = []
    parts.append(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    req_file = repo / "requirements.txt"
    if req_file.exists():
        try:
            lines = req_file.read_text().splitlines()
            for line in lines:
                line = line.strip()
                if line and not line.startswith(("#", "-", "git+")):
                    parts.append(line)
        except Exception:
            pass
    pyproj = repo / "pyproject.toml"
    if pyproj.exists():
        try:
            text = pyproj.read_text()
            for line in text.splitlines():
                line = line.strip()
                if "requires-python" in line or "dependencies" in line or line.startswith('"'):
                    parts.append(line.strip('", '))
        except Exception:
            pass
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            import json
            data = json.loads(pkg.read_text())
            if "dependencies" in data:
                deps = list(data["dependencies"].keys())[:10]
                parts.append(f"Node deps: {', '.join(deps)}")
        except Exception:
            pass
    if len(parts) > 15:
        parts = parts[:15]
        parts.append("... (truncated)")
    return "\n".join(parts)


def generate_plan(task: str, repo: Path, config: dict[str, Any], logger: logging.Logger, supplement: str = "", reference_docs: str = "", iteration: int = 1, skill_context: str = "", no_cache: bool = False, spec_context: str = "") -> dict[str, Any]:
    plan_start = time.time()
    logger.info("[PLAN] ═══ PLAN MODE ═══")
    logger.info(f"[PLAN]  第 {iteration} 次生成")
    log_event(logger, "plan_generate", {"iteration": iteration, "has_supplement": bool(supplement), "has_docs": bool(reference_docs), "has_skills": bool(skill_context), "has_spec": bool(spec_context)})

    # Plan 缓存检查
    cache_hit = False
    if not no_cache and iteration == 1 and not supplement and not reference_docs:
        cache_key = get_cache_key(task, repo)
        cached = load_cached_plan(cache_key, task, config, logger)
        if cached:
            plan = cached
            cache_hit = True
            plan_duration_ms = round((time.time() - plan_start) * 1000)
            log_event(logger, "plan_complete", {"iteration": iteration, "step_count": len(plan.get("steps", [])),
                                                 "plan_duration_ms": plan_duration_ms, "cache_hit": True})
            logger.info(f"[缓存] 使用缓存 Plan，耗时 {plan_duration_ms}ms")
            return plan

    project_files = analyze_project(repo)
    git_info = get_git_info(repo)
    resource_map = get_resource_map(repo, git_info)

    # ── Prompt 预算控制 ──
    MAX_SYSTEM_PROMPT_CHARS = 10000  # system prompt 上限字符数（含 Skill 表 + OUTPUT BUDGET + scope isolation 规则）
    MAX_USER_CONTENT_CHARS = 12000   # user content 上限字符数
    # 截断项目文件列表（保留前 100 个）
    file_lines = project_files.split("\n") if project_files else []
    if len(file_lines) > 100:
        file_lines = file_lines[:100]
        file_lines.append(f"... 共 {len(project_files.split(chr(10)))} 个文件，仅展示前 100 个")
        project_files = "\n".join(file_lines)
        logger.info(f"[PLAN] 项目文件列表截断: {len(project_files.split(chr(10)))} → 100")

    # Skill 表限制条目
    SKILL_TABLE_MAX = 10

    system_prompt = """You are a senior software architect. Output ONLY valid JSON. No markdown, no explanation.

SCHEMA:
{
  "overview": "task overview (2-3 sentences)",
  "steps": [
    {
      "id": 1,
      "title": "step title",
      "description": "what to do",
      "files": ["relative/file/path"],
      "verification": "verification command",
      "risks": ["risk description"],
      "agent_prompt": "detailed instructions for the agent executing this step",
      "rationale": "why this step is separated and what boundary it owns",
      "scope_boundary": "what this step may change",
      "do_not_touch": ["files or modules explicitly out of scope"],
      "requirement_ids": ["REQ-001"],
      "acceptance_criteria_ids": ["AC-001"],
      "skills": ["skill-name"],
      "agent_type": "developer|architect|reviewer|tester",
      "difficulty": "easy|medium|hard",
      "cognitive_mode": "explore|implement|review",
      "allowed_tools": ["Read", "Write", "Edit", "Bash"],
      "permission_mode": "bypassPermissions|acceptEdits|default"
    }
  ],
  "dependencies": {"2": [1]},
  "estimated_effort": "estimated effort",
  "shared_resources": {
    "directories": ["dirs"],
    "git_remote": "remote url",
    "git_branch": "branch",
    "config_files": ["configs"],
    "env_vars": ["env vars"]
  }
}

REQUIREMENTS:
- **CRITICAL — Scope isolation**: Each step's `files` MUST include every file that step reads or modifies. If verification needs a file that belongs to another step, add a dependency.
- **CRITICAL — Risk isolation**: Each step's `risks` must ONLY describe problems that occur DURING that step's own execution. NEVER list another step's task as a risk.
- **Common mistake to avoid**: If step-1 adds caching and needs a fixture, the fixture belongs in step-1's `files` OR step-2 must be a dependency of step-1. Do NOT put "step-2 needs to add a fixture" in step-1's `risks`.
- 2-5 steps, independently executable
- Each step MUST have: agent_type, difficulty, agent_prompt
- Include rationale, scope_boundary, requirement_ids, and acceptance_criteria_ids when the Spec provides stable IDs.
- agent_type: developer=coding, architect=read-only design, reviewer=code review, tester=testing
- difficulty: easy=single file small change, medium=single feature, hard=cross-file architecture
- cognitive_mode (optional): explore=research/cheap model, implement=coding/strong model, review=independent inspection. Architect/reviewer steps default to explore/review. When in doubt, omit — system infers from agent_type.
- allowed_tools / permission_mode (optional): omit for full default tools; use only when a step MUST be restricted (e.g. read-only review step: ["Read","Grep","Glob"]).
- Use empty array [] when no skills match
- Dependencies map step IDs to prerequisite step IDs

EXAMPLE (3-step plan):
{
  "overview": "Add user authentication module with JWT tokens",
  "steps": [
    {"id": 1, "title": "Create auth middleware", "description": "Implement JWT verification middleware", "files": ["src/middleware/auth.py"], "verification": "python -m pytest tests/test_auth_middleware.py -v", "risks": ["Token expiration handling"], "agent_prompt": "Create src/middleware/auth.py with JWT decode/verify functions. Use PyJWT library.", "skills": ["security-review"], "agent_type": "developer", "difficulty": "medium"},
    {"id": 2, "title": "Add login endpoint", "description": "Create login API that returns JWT", "files": ["src/api/login.py"], "verification": "python -m pytest tests/test_login_api.py -v", "risks": ["Password hashing"], "agent_prompt": "Create login endpoint that validates credentials and returns JWT token.", "skills": [], "agent_type": "developer", "difficulty": "medium"},
    {"id": 3, "title": "Write auth tests", "description": "Test auth middleware and login", "files": ["tests/test_auth.py"], "verification": "python -m pytest tests/test_auth.py -v", "risks": [], "agent_prompt": "Write tests covering token validation, expiration, and invalid tokens.", "skills": [], "agent_type": "tester", "difficulty": "easy"}
  ],
  "dependencies": {"2": [1], "3": [1, 2]},
  "estimated_effort": "2-3 hours",
  "shared_resources": {"directories": ["src", "tests"], "git_remote": "", "git_branch": "main", "config_files": [], "env_vars": ["JWT_SECRET"]}
}"""

    # F-0: 注入项目运行时版本信息（帮助 LLM 生成兼容的代码）
    _runtime_info = _detect_runtime_info(repo)
    if _runtime_info:
        system_prompt += f"\n## 项目运行时环境（避免生成不兼容的 API 调用）\n```\n{_runtime_info}\n```\n"

    # F-1: 注入已安装 Skill 清单（限制条目数）
    installed = list_skills(repo)
    if installed:
        skill_table = "| Skill 名称 | 描述 |\n|------------|------|\n"
        for s in installed[:SKILL_TABLE_MAX]:
            desc = (s.get("description") or "-")[:80]
            skill_table += f"| {s['name']} | {desc} |\n"
        if len(installed) > SKILL_TABLE_MAX:
            skill_table += f"\n... 还有 {len(installed) - SKILL_TABLE_MAX} 个 Skill 未展示\n"
        system_prompt += f"\n## 项目已安装的 Skill（可在 steps[].skills 中引用）\n{skill_table}\n"

    # 注入 OUTPUT BUDGET 段：告诉 plan LLM 当前 worker 的输出预算，让它自动拆任务
    _plan_api_cfg = config.get("planner_api") or config.get("plan_api", {})
    _worker_max_tokens = int(_plan_api_cfg.get("worker_max_tokens", 0) or 0)
    _plan_max_tokens = int(_plan_api_cfg.get("max_tokens", 4096) or 4096)
    # 估算 worker 单次可用输出：扣除 prompt + tool 调度 overhead，预留 30% 安全边界
    _safe_output_chars = max(1000, int(_worker_max_tokens * 0.7 * 1.5))  # 中文 1.5 char/token
    _safe_output_chars = min(_safe_output_chars, 60000)  # 单个 step 上限 6 万中文字符（防失控）
    system_prompt += (
        f"\n## OUTPUT BUDGET（输出预算）\n"
        f"- Plan 阶段 max_tokens: {_plan_max_tokens}\n"
        f"- Worker 阶段 max_tokens: {_worker_max_tokens or '未设置（默认 ~8K）'}\n"
        f"- **每个 step 估算最大输出 ≈ {_safe_output_chars} 中文字符**（约 {_safe_output_chars // 1000}K 字）\n"
        f"- **拆任务规则**：如果任务整体产出 > {_safe_output_chars} 字，必须拆成多个 step，"
        f"每个 step 的输出分别落在不同文件（例如 chapter-01.md, chapter-02.md ...）。\n"
        f"- 在每个 step 的 agent_prompt 里显式写：「**本次输出上限 {_safe_output_chars} 中文字符，分多次 Write 完成**」。\n"
    )
    system_prompt += (
        "\n## 分解粒度规则（Decomposition Scope Rules）\n"
        "- **先估算改动规模**，再决定步骤数量：\n"
        "  - 单文件、<20 行改动 → 1 个步骤（不分解，实现+测试合并为一个步骤）\n"
        "  - 单文件、20-50 行或 ≤2 个文件 → 1-2 个步骤\n"
        "  - 多文件、50+ 行 → 2-4 个步骤\n"
        "  - 跨模块架构改动（重构/迁移/跨服务）→ 3-5 个步骤\n"
        "- **不要为同一文件的简单改动拆出独立的「编写测试」步骤**——测试应与实现合并到同一步骤\n"
        "- **不要为单行/极少量改动（如改默认值、修 typo、加 null guard）创建多个步骤**\n"
        "- 每个步骤应产出有意义、可独立验证的工作单元\n"
        "\n"
        "## 拆分三原则（Split Design Principles，必须同时满足）\n"
        "1. **文件互斥（File Mutual Exclusion）**：不同步骤不要修改同一文件。"
        "如果两个改动必须落在同一文件，合并为一个步骤。\n"
        "   - **核心源文件（实现代码，如 src/**.py、*.ts、*.go）只允许一个步骤负责实现**——"
        "即使加 dependencies 串行，先后重写同一实现文件仍会互相覆盖、语义验收无法对应最终 diff，"
        "必须合并为一个步骤。\n"
        "   - 测试/配置/文档等辅助文件（tests/**.py、*.json、*.yaml、*.md）允许多个步骤先后触碰，"
        "确需分离时用 dependencies 串行执行。\n"
        "2. **独立可验证（Independent Verifiability）**：每个步骤的 verification 必须能独立运行"
        "（不依赖其他步骤的产物或未合并的文件）。若某改动脱离其他改动就无法验证，说明它们强耦合，应合并为一个步骤。\n"
        "3. **小改动不拆（No Over-split）**：改动面 ≤2 个文件时优先 1 个步骤。"
        "只有当改动真正跨模块（不同文件集、各自可独立验证）时才拆分，禁止为小改动制造不必要的拆分。\n"
        "4. **微小改动必合（Tiny Changes Merge）**：每个文件改动 <15 行的微小改动，"
        "即使涉及 2-3 个不同文件，也应合并为一个步骤（如数据层多处各 5 行的加固、"
        "多个函数签名的小修正）——分开只会增加 worktree 与上下文重复注入成本，"
        "无任何并行收益。只有改动总规模达到「每个文件 ≥15 行且跨模块」才考虑拆分。\n"
        "\n"
        "每个步骤的 `rationale` 字段必须填写，说明：为什么拆/合并这一步；"
        "并简要说明「为什么不是 N 个而是 N+1 或 N-1 个」（即如何权衡了合并开销与并行收益）。\n"
    )

    # F-1: 注入角色-Skill 映射规则摘要
    role_map = load_role_skill_map(repo)
    if role_map.get("rules"):
        rule_lines = ["## Agent 角色与 Skill 分配规则（优先匹配，无匹配时自主判断）",
                      "| 匹配条件 | Agent 类型 | 必须 Skill | 推荐 Skill |",
                      "|----------|-----------|-----------|-----------|"]
        for rule in role_map["rules"][:15]:
            cond = rule.get("match", {})
            skills = rule.get("skills", {})
            cond_str = " + ".join(
                f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in cond.items()
            )
            agent = rule.get("agent_type", "-")
            required = ", ".join(skills.get("required", [])) or "-"
            recommended = ", ".join(skills.get("recommended", [])) or "-"
            rule_lines.append(f"| {cond_str[:50]} | {agent} | {required} | {recommended} |")
        system_prompt += "\n" + "\n".join(rule_lines) + "\n"

    # F-5: 项目级推荐 Agent 和 Skill
    recommended_agents = role_map.get("recommended_agents", [])
    recommended_skills = role_map.get("recommended_skills", [])
    if recommended_agents:
        agents_str = ", ".join(recommended_agents)
        system_prompt += f"\n## 项目推荐的 Agent 类型\n本项目推荐使用以下 Agent 类型（优先选择）：{agents_str}\n"
    if recommended_skills:
        skills_str = ", ".join(recommended_skills)
        system_prompt += f"\n## 项目推荐的 Skill\n以下 Skill 应优先在合适的步骤中引用：{skills_str}\n"

    # 如果有 Skill 上下文，注入到 system prompt（受预算限制）
    if skill_context:
        ctx_chars = len(skill_context)
        remaining = MAX_SYSTEM_PROMPT_CHARS - len(system_prompt)
        if remaining > 500:
            if ctx_chars > remaining:
                skill_context = skill_context[:remaining-100] + "\n... [Skill 上下文已截断]"
                logger.info(f"[PLAN] Skill 上下文截断: {ctx_chars} → {remaining} 字符")
            system_prompt += f"\n## 可用领域知识（Skill）\n以下是项目/用户提供的领域知识，可在制定方案时参考：\n{skill_context}\n请在 plan 的 steps 中使用 skills 字段引用相关的 Skill 名称。"
        else:
            logger.warning(f"[PLAN] 跳过 Skill 上下文注入（system prompt 已达上限 {len(system_prompt)} 字符）")

    # 如果有 Task Spec 结构化约束（S11-P0），注入为 Planner 硬约束
    if spec_context:
        remaining = MAX_SYSTEM_PROMPT_CHARS - len(system_prompt)
        if remaining > 500:
            if len(spec_context) > remaining:
                spec_context = spec_context[:remaining-100] + "\n... [Spec 约束已截断]"
            system_prompt += (
                f"\n## Task Spec 硬约束（来自 --spec，必须严格遵守）\n"
                f"用户提供了结构化 Task Spec。以下约束在分解 steps 时必须逐条遵守，"
                f"并把相关约束写入对应 step 的 agent_prompt：\n{spec_context}\n"
                f"关键要求：\n"
                f"- steps[].files 必须在 Spec §3「需要改动」范围内，不得触及「明确不动」的区域\n"
                f"- steps[].verification 应覆盖 Spec §5 的验收标准\n"
                f"- Spec §7 的已知风险写入对应 step 的 risks 字段，高风险步骤 difficulty 标记为 hard\n"
            )
            logger.info(f"[PLAN] 注入 Task Spec 约束: {len(spec_context)} 字符")
        else:
            logger.warning(f"[PLAN] 跳过 Spec 约束注入（system prompt 已达上限 {len(system_prompt)} 字符）")

    system_prompt += "\n## 可用 Agent 类型\n- developer: 开发者（编写代码）\n- architect: 架构师（设计分析，只读）\n- reviewer: 审查者（代码审查）\n- tester: 测试者（编写测试）\n必须为每个步骤指定合适的 agent_type。\n\n## 允许的验证命令\nsteps[].verification 仅限以下命令前缀（安全白名单）：\n" + "\n".join(f"- `{p}`" for p in SAFE_VERIFICATION_PREFIXES) + "\n\n## 验证命令生成规范\n生成的 steps[].verification 必须遵守以下规则（违反会被安全门禁拒绝并判失败）：\n- 命令必须从白名单前缀开始，**不要**用 `bash -c '...'` 或 `sh -c '...'` 包裹任何命令（bash/sh 不在白名单）。\n- `python -c \"...\"` 只能写**单行**代码：不能含换行、不能含 `def`/装饰器/`with` 块等复合语句；需要多行逻辑时改用 `python -m <模块>`（如 `python -m src.cli stats`）或测试框架命令。\n- 命令前**不要**添加自然语言描述或注释前缀（如 `Check fixtures exist...: pytest`），直接写命令本身。\n- 运行项目内 CLI/模块用 `python -m <模块> <参数>`（模块名用点分路径，如 `python -m src.cli stats`），不要用 `bash` 包裹。\n- pytest 的 `-k` 参数只支持**测试函数名关键字**表达式（如 `-k 'not (spa_fallback or error_says_which)'`），**不支持** `Class::method` nodeid 语法（如 `-k 'not TestFoo::test_bar'` 会被 pytest 忽略导致排除失效）。排除特定测试时用测试名关键字，不要用 `::` nodeid。\n- 前端项目验证范式：TypeScript 项目用 `tsc --noEmit`（类型检查）；ESLint 用 `eslint src/ --ext .ts,.tsx`；构建用 `npm run build` / `yarn build` / `pnpm build`；测试用 `jest` / `vitest` / `mocha` 或 `npm test`。\n- E2E/视觉验证范式：交互流程用 `npx playwright test`（含 `toHaveScreenshot` 视觉回归断言）或 `cypress run --headless`；截图对比用 `npx playwright test --update-snapshots`（首次生成基线）。**视觉回归优先用 Playwright 快照**（确定性像素 diff，零 LLM 成本），不要依赖截图 LLM 判断。\n\n## 示例步骤\n以下是一个正确填写 agent_type 和 skills 的示例：\n{\n  \"id\": 2,\n  \"title\": \"编写单元测试\",\n  \"description\": \"为认证模块补充测试\",\n  \"files\": [\"tests/test_auth.py\"],\n  \"verification\": \"pytest tests/test_auth.py -v\",\n  \"risks\": [],\n  \"agent_prompt\": \"请为 src/auth.py 编写单元测试，覆盖正常和异常路径\",\n  \"agent_type\": \"tester\",\n  \"difficulty\": \"medium\",\n  \"skills\": [\"tdd-workflow\"]\n}"

    # ── Prompt 预算控制：截断 user content ──
    if reference_docs and len(reference_docs) > MAX_USER_CONTENT_CHARS // 3:
        ref_doc_chars = len(reference_docs)
        reference_docs = reference_docs[:MAX_USER_CONTENT_CHARS // 3 - 100] + "\n... [参考文档已截断]"
        logger.info(f"[PLAN] 参考文档截断: {ref_doc_chars} → {MAX_USER_CONTENT_CHARS // 3} 字符")

    user_content = f"""任务：{task}\n项目路径：{repo}\nGit 信息：远程={git_info['remote']}, 分支={git_info['branch']}, 提交={git_info['commit']}\n项目文件列表：\n{project_files}\n项目资源：\n- 目录：{', '.join(resource_map['directories'])}\n- 关键文件：{', '.join(resource_map['key_files'])}"""

    if supplement:
        user_content += f"\n===== 用户补充 =====\n{supplement}\n===== 结束 ====="
    if reference_docs:
        user_content += f"\n===== 参考文档 =====\n{reference_docs}\n===== 结束 ====="
    # 任务级难度提示（软引导）：让 planner 感知任务整体难度，子任务标注与之一致
    _task_diff = str(config.get("min_difficulty", "") or "")
    if _task_diff in ("easy", "medium", "hard"):
        user_content += (
            f"\n===== 任务难度 =====\n本任务整体难度为 {_task_diff}。"
            "请据此标注每个 step 的 difficulty——整体 {_task_diff} 时，"
            "核心实现步骤应标 " + _task_diff + "（不要全部降为更低的难度）。\n===== 结束 ====="
        )

    # ── 最终预算检查 ──
    if len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
        original_len = len(system_prompt)
        system_prompt = system_prompt[:MAX_SYSTEM_PROMPT_CHARS - 100] + "\n... [system prompt 已达到字符上限，已截断]"
        logger.warning(f"[PLAN] system prompt 截断: {original_len} → {MAX_SYSTEM_PROMPT_CHARS} 字符")
    if len(user_content) > MAX_USER_CONTENT_CHARS:
        original_len = len(user_content)
        user_content = user_content[:MAX_USER_CONTENT_CHARS - 100] + "\n... [user content 已达到字符上限，已截断]"
        logger.warning(f"[PLAN] user content 截断: {original_len} → {MAX_USER_CONTENT_CHARS} 字符")
    logger.info(f"[PLAN] Prompt 大小 — system: {len(system_prompt)} 字符, user: {len(user_content)} 字符")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    # ── 角色感知模型路由：Plan 阶段使用 architect → planner 路由 ──
    route = resolve_provider("architect", config)
    if route:
        api_key = get_api_key(config)
        logger.info(f"[PLAN] 路由: {route.role} → {route.primary.provider}:{route.primary.model}")
        content, metering = call_with_role(route, messages, api_key, logger, task_id=config.get("_task_id", ""), metering_path=config.get("_metering_path"))
        log_event(logger, "api_call", metering)
    else:
        content = call_api(config, messages, logger)

    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            plan = json.loads(match.group())
        else:
            raise RuntimeError("API 返回无法解析为 JSON")

    # 注入资源清单（如 API 未返回则使用本地分析）
    if "shared_resources" not in plan:
        plan["shared_resources"] = resource_map
    else:
        # 合并本地分析结果
        sr = plan["shared_resources"]
        if not sr.get("git_remote"):
            sr["git_remote"] = git_info["remote"]
        if not sr.get("git_branch"):
            sr["git_branch"] = git_info["branch"]

    plan_duration_ms = round((time.time() - plan_start) * 1000)
    log_event(logger, "plan_complete", {"iteration": iteration, "step_count": len(plan.get("steps", [])),
                                         "plan_duration_ms": plan_duration_ms, "cache_hit": cache_hit})
    # 写入缓存
    if not no_cache and iteration == 1 and not cache_hit:
        cache_key = get_cache_key(task, repo)
        save_cached_plan(cache_key, plan, task, repo, config)
    return plan

def decompose_fallback(task: str, repo: Path, config: dict[str, Any], logger: logging.Logger) -> list[dict[str, Any]]:
    logger.warning("Plan Mode 失败，降级")
    # 本地模型端点：优先复用 plan_api.base_url 若指向本地（纯本地模式配置一致性），
    # 否则用 fallback.local_model_url（默认 localhost:4000，此前 8000 为废弃端口）。
    _plan_api = config.get("plan_api", {}) or {}
    _plan_base = str(_plan_api.get("base_url", ""))
    _is_local_plan = bool(re.search(r"(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])", _plan_base))
    _fallback_cfg = config.get("fallback", {}) or {}
    if _is_local_plan:
        local_url = _plan_base
        local_name = str(_plan_api.get("model", "claude-sonnet-4-6"))
    else:
        local_url = _fallback_cfg.get("local_model_url", "http://localhost:4000/v1/chat/completions")
        local_name = _fallback_cfg.get("local_model_name", "claude-sonnet-4-6")
    try:
        import urllib.request
        payload = json.dumps({
            "model": local_name,
            "messages": [
                {"role": "system", "content": "拆分为2-4个子任务。输出JSON数组，每个元素包含title、description、files_hint、agent_prompt。"},
                {"role": "user", "content": f"任务: {task}"}
            ],
            "temperature": 0.3,
            "max_tokens": 800
        }).encode()
        req = urllib.request.Request(local_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                parsed = json.loads(match.group())
                return [{"id": f"sub-{i+1}", **st} for i, st in enumerate(parsed)]
    except Exception as e:
        logger.warning(f"本地模型失败: {e}")

    task_lower = task.lower()
    for rule in DECOMPOSE_RULES:
        if any(p.lower() in task_lower for p in rule["patterns"]):
            return [{"id": f"sub-{i+1}", **st} for i, st in enumerate(rule["subtasks"])]
    return [{"id": "sub-1", "title": "执行主任务", "description": task, "files_hint": "*", "agent_prompt": task}]


# ═══════════════════════════════════════════════════════════════
# Plan Cache
# ═══════════════════════════════════════════════════════════════

def _cache_dir() -> Path:
    d = AGENT_GO_DIR / "cache" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cache_key(task: str, repo: Path) -> str:
    """SHA256(task + project_files[:2000] + remote + branch)。

    注意：不包含 commit hash——否则仓库每次提交都会使缓存 key 变化，
    导致活跃仓库中 Plan 缓存命中率趋近于零。
    """
    project_files = analyze_project(repo)
    git_info = get_git_info(repo)
    key_parts = [
        task,
        project_files[:2000] if project_files else "",
        git_info.get("remote", ""),
        git_info.get("branch", ""),
    ]
    return hashlib.sha256("|".join(key_parts).encode()).hexdigest()


def load_cached_plan(cache_key: str, task: str, config: dict[str, Any], logger: logging.Logger) -> Optional[dict[str, Any]]:
    # cache.enabled=false 时读缓存同样禁用（此前只禁写、不禁读）
    if not config.get("cache", {}).get("enabled", True):
        return None
    cache_dir = _cache_dir()
    cache_file = cache_dir / cache_key[:2] / f"{cache_key}.json"
    if not cache_file.exists():
        return None

    try:
        entry = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Corrupt or missing cache file %s: %s", cache_file, e)
        return None

    ttl = config.get("cache", {}).get("plan_ttl", 86400)
    created = entry.get("meta", {}).get("created_at", "")
    if created:
        try:
            created_ts = datetime.strptime(created, "%Y-%m-%dT%H:%M:%S").timestamp()
            if time.time() - created_ts > ttl:
                cache_file.unlink(missing_ok=True)
                logger.info(f"[缓存] 已过期，删除: {cache_key[:12]}...")
                return None
        except ValueError as e:
            logger.debug("Invalid cache timestamp in %s: %s", cache_key[:12], e)

    plan = entry.get("plan")
    if not plan or not plan.get("steps"):
        return None

    # 校验 task 描述是否匹配，避免缓存键碰撞导致的不匹配
    cached_task = entry.get("meta", {}).get("task", "")
    if cached_task and cached_task != task[:200]:
        logger.warning(f"[缓存] task 不匹配 (缓存: {cached_task[:50]}..., 当前: {task[:50]}...)，跳过缓存")
        return None

    meta = entry["meta"]
    meta["last_hit_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    meta["hit_count"] = meta.get("hit_count", 0) + 1
    entry["meta"] = meta
    cache_file.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")

    cache_cfg = config.get("cache", {})
    if cache_cfg.get("enabled", True):
        logger.info(f"[缓存] 命中 {cache_key[:12]}... ({meta['hit_count']} 次, {_format_age(created)})")
    return plan


def save_cached_plan(cache_key: str, plan: dict[str, Any], task: str, repo: Path, config: dict[str, Any]) -> None:
    cache_cfg = config.get("cache", {})
    if not cache_cfg.get("enabled", True):
        return
    cache_dir = _cache_dir()
    subdir = cache_dir / cache_key[:2]
    subdir.mkdir(parents=True, exist_ok=True)

    entry = {
        "cache_key": cache_key,
        "plan": plan,
        "meta": {
            "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "last_hit_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "hit_count": 0,
            "task": task[:200],
            "repo": str(repo),
            "ttl": cache_cfg.get("plan_ttl", 86400),
        },
    }
    (subdir / f"{cache_key}.json").write_text(
        json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")


def _format_age(iso_str: str) -> str:
    try:
        age = time.time() - datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S").timestamp()
        if age < 3600:
            return f"{int(age // 60)}m前"
        elif age < 86400:
            return f"{int(age // 3600)}h前"
        return f"{int(age // 86400)}d前"
    except Exception as e:
        logger.debug("Failed to format age for '%s': %s", iso_str, e)
        return "?"


def list_cache_entries() -> list[dict[str, Any]]:
    entries = []
    cache_dir = _cache_dir()
    for subdir in sorted(cache_dir.glob("*")):
        if subdir.is_dir():
            for f in sorted(subdir.glob("*.json")):
                try:
                    e = json.loads(f.read_text(encoding="utf-8"))
                    entries.append(e)
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read cache entry %s: %s", f, e)
    return sorted(entries, key=lambda e: e.get("meta", {}).get("created_at", ""), reverse=True)


def clean_expired_cache(config: dict[str, Any]) -> int:
    ttl = config.get("cache", {}).get("plan_ttl", 86400)
    now = time.time()
    removed = 0
    for entry in list_cache_entries():
        created = entry.get("meta", {}).get("created_at", "")
        try:
            if now - datetime.strptime(created, "%Y-%m-%dT%H:%M:%S").timestamp() > ttl:
                cache_dir = _cache_dir()
                key = entry.get("cache_key", "")
                f = cache_dir / key[:2] / f"{key}.json"
                if f.exists():
                    f.unlink()
                    removed += 1
        except ValueError as e:
            logger.debug("Invalid timestamp in cache entry: %s", e)
    return removed
