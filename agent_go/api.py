import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import get_api_key, log_event, DECOMPOSE_RULES, AGENT_GO_DIR
from .git_utils import analyze_project, get_git_info, get_resource_map
from .skills import list_skills
from .role_skill_map import load_role_skill_map
import hashlib

def call_api(config, messages, logger):
    api_cfg = config["plan_api"]
    provider = api_cfg.get("provider", "anthropic")
    base_url = api_cfg["base_url"]
    api_key = get_api_key(config)
    model = api_cfg["model"]
    if not api_key:
        raise RuntimeError("API Key 未配置。请设置 AGENT_GO_API_KEY")

    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    if provider == "anthropic":
        payload = {"model": model, "max_tokens": api_cfg.get("max_tokens", 4096), "temperature": api_cfg.get("temperature", 0.2), "messages": messages}
    else:
        payload = {"model": model, "messages": messages, "max_tokens": api_cfg.get("max_tokens", 4096), "temperature": api_cfg.get("temperature", 0.2)}

    import urllib.request, urllib.error
    req = urllib.request.Request(base_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            latency = time.time() - start
            raw_body = resp.read()
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
                    content = data["content"][0]["text"]
                else:
                    content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as e:
                log_event(logger, "api_error", {
                    "provider": provider, "error": "structure",
                    "message": f"响应结构异常: {e}", "keys": list(data.keys())[:10] if isinstance(data, dict) else str(type(data)),
                })
                raise RuntimeError(f"API 响应结构异常 ({provider}): {e}") from e
            usage = data.get("usage", {})
            log_event(logger, "api_call", {
                "provider": provider, "model": model,
                "latency_ms": round(latency * 1000, 2), "response_len": len(content),
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
            })
            return content
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
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

def generate_plan(task, repo, config, logger, supplement="", reference_docs="", iteration=1, skill_context="", no_cache=False) -> dict:
    plan_start = time.time()
    logger.info("[PLAN] ═══ PLAN MODE ═══")
    logger.info(f"[PLAN]  第 {iteration} 次生成")
    log_event(logger, "plan_generate", {"iteration": iteration, "has_supplement": bool(supplement), "has_docs": bool(reference_docs), "has_skills": bool(skill_context)})

    # Plan 缓存检查
    cache_hit = False
    if not no_cache and iteration == 1 and not supplement and not reference_docs:
        cache_key = get_cache_key(task, repo)
        cached = load_cached_plan(cache_key, config, logger)
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
    MAX_SYSTEM_PROMPT_CHARS = 6000   # system prompt 上限字符数
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

    system_prompt = """你是一位资深软件架构师。请为以下开发任务制定详细的执行方案。\n输出必须是合法的 JSON，不要包含任何其他文字。结构：\n{\n"overview": "任务概述，2-3句话",\n"steps": [\n{\n"id": 1,\n"title": "步骤标题",\n"description": "详细描述该步骤做什么",\n"files": ["涉及文件路径1"],\n"verification": "可执行的验证命令，如 go build ./...",\n"risks": ["潜在风险1"],\n"agent_prompt": "给执行Agent的完整Prompt，包含具体指令、上下文、约束条件",\n"skills": ["从已安装 Skill 清单中选择匹配的 Skill 名称（如无匹配则为空数组）"],\n"agent_type": "必须指定。developer=编码实现, architect=只读分析设计, reviewer=代码审查, tester=测试编写"\n}\n],\n"dependencies": {"2": [1]},\n"estimated_effort": "预估工作量",\n"shared_resources": {\n"directories": ["关键目录1"],\n"git_remote": "git远程地址",\n"git_branch": "当前分支",\n"config_files": ["配置文件1"],\n"env_vars": ["环境变量1"]\n}\n}\n要求：\n1. 每个 step 必须包含 agent_prompt 字段，这是给 Claude Code 执行该步骤时的完整指令\n2. shared_resources 描述所有子任务共享的资源和上下文\n3. 步骤 2-5 个，可独立执行\n4. agent_type 必须根据步骤性质指定合适的 Agent 类型\n5. skills 从已安装 Skill 清单中选取，不匹配则使用空数组 []"""

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

    system_prompt += "\n## 可用 Agent 类型\n- developer: 开发者（编写代码）\n- architect: 架构师（设计分析，只读）\n- reviewer: 审查者（代码审查）\n- tester: 测试者（编写测试）\n必须为每个步骤指定合适的 agent_type。\n\n## 示例步骤\n以下是一个正确填写 agent_type 和 skills 的示例：\n{\n  \"id\": 2,\n  \"title\": \"编写单元测试\",\n  \"description\": \"为认证模块补充测试\",\n  \"files\": [\"tests/test_auth.py\"],\n  \"verification\": \"pytest tests/test_auth.py -v\",\n  \"risks\": [],\n  \"agent_prompt\": \"请为 src/auth.py 编写单元测试，覆盖正常和异常路径\",\n  \"agent_type\": \"tester\",\n  \"skills\": [\"tdd-workflow\"]\n}"

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

def decompose_fallback(task, repo, config, logger):
    logger.warning("Plan Mode 失败，降级")
    local_url = config.get("fallback", {}).get("local_model_url", "http://localhost:8000/v1/chat/completions")
    local_name = config.get("fallback", {}).get("local_model_name", "qwen")
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

def _cache_dir():
    d = AGENT_GO_DIR / "cache" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_cache_key(task, repo):
    """SHA256(task + project_files[0:100] + remote + branch)。"""
    project_files = analyze_project(repo)
    git_info = get_git_info(repo)
    key_parts = [
        task,
        project_files[:2000] if project_files else "",
        git_info.get("remote", ""),
        git_info.get("branch", ""),
        git_info.get("commit", ""),
    ]
    return hashlib.sha256("|".join(key_parts).encode()).hexdigest()


def load_cached_plan(cache_key, config, logger):
    cache_dir = _cache_dir()
    cache_file = cache_dir / cache_key[:2] / f"{cache_key}.json"
    if not cache_file.exists():
        return None

    try:
        entry = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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
        except ValueError:
            pass

    plan = entry.get("plan")
    if not plan or not plan.get("steps"):
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


def save_cached_plan(cache_key, plan, task, repo, config):
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


def _format_age(iso_str):
    try:
        age = time.time() - datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S").timestamp()
        if age < 3600:
            return f"{int(age // 60)}m前"
        elif age < 86400:
            return f"{int(age // 3600)}h前"
        return f"{int(age // 86400)}d前"
    except Exception:
        return "?"


def list_cache_entries():
    entries = []
    cache_dir = _cache_dir()
    for subdir in sorted(cache_dir.glob("*")):
        if subdir.is_dir():
            for f in sorted(subdir.glob("*.json")):
                try:
                    e = json.loads(f.read_text(encoding="utf-8"))
                    entries.append(e)
                except (json.JSONDecodeError, OSError):
                    pass
    return sorted(entries, key=lambda e: e.get("meta", {}).get("created_at", ""), reverse=True)


def clean_expired_cache(config):
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
        except ValueError:
            pass
    return removed
