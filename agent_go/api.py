import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import get_api_key, log_event, DECOMPOSE_RULES
from .git_utils import analyze_project, get_git_info, get_resource_map

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

    import urllib.request
    req = urllib.request.Request(base_url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    start = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp:
        latency = time.time() - start
        data = json.loads(resp.read())
        content = data["content"][0]["text"] if provider == "anthropic" else data["choices"][0]["message"]["content"]
        log_event(logger, "api_call", {"provider": provider, "latency_ms": round(latency*1000, 2), "response_len": len(content)})
        return content

def generate_plan(task, repo, config, logger, supplement="", reference_docs="", iteration=1) -> dict:
    logger.info("[PLAN] ═══ PLAN MODE ═══")
    logger.info(f"[PLAN]  第 {iteration} 次生成")
    log_event(logger, "plan_generate", {"iteration": iteration, "has_supplement": bool(supplement), "has_docs": bool(reference_docs)})

    project_files = analyze_project(repo)
    git_info = get_git_info(repo)
    resource_map = get_resource_map(repo, git_info)

    system_prompt = """你是一位资深软件架构师。请为以下开发任务制定详细的执行方案。\n输出必须是合法的 JSON，不要包含任何其他文字。结构：\n{\n"overview": "任务概述，2-3句话",\n"steps": [\n{\n"id": 1,\n"title": "步骤标题",\n"description": "详细描述该步骤做什么",\n"files": ["涉及文件路径1"],\n"verification": "可执行的验证命令，如 go build ./...",\n"risks": ["潜在风险1"],\n"agent_prompt": "给执行Agent的完整Prompt，包含具体指令、上下文、约束条件"\n}\n],\n"dependencies": {"2": [1]},\n"estimated_effort": "预估工作量",\n"shared_resources": {\n"directories": ["关键目录1"],\n"git_remote": "git远程地址",\n"git_branch": "当前分支",\n"config_files": ["配置文件1"],\n"env_vars": ["环境变量1"]\n}\n}\n要求：\n1. 每个 step 必须包含 agent_prompt 字段，这是给 Claude Code 执行该步骤时的完整指令\n2. shared_resources 描述所有子任务共享的资源和上下文\n3. 步骤 2-5 个，可独立执行"""

    user_content = f"""任务：{task}\n项目路径：{repo}\nGit 信息：远程={git_info['remote']}, 分支={git_info['branch']}, 提交={git_info['commit']}\n项目文件列表：\n{project_files}\n项目资源：\n- 目录：{', '.join(resource_map['directories'])}\n- 关键文件：{', '.join(resource_map['key_files'])}"""

    if supplement:
        user_content += f"\n===== 用户补充 =====\n{supplement}\n===== 结束 ====="
    if reference_docs:
        user_content += f"\n===== 参考文档 =====\n{reference_docs}\n===== 结束 ====="

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

    log_event(logger, "plan_complete", {"iteration": iteration, "step_count": len(plan.get("steps", []))})
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
