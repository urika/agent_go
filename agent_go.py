#!/usr/bin/env python3
"""
agent_go.py — Plan Mode 增强版

核心增强：
  1. Plan 输出包含给执行 Agent 的完整 Prompt 模板
  2. 共享资源清单（目录结构、git 地址、依赖文件等）
  3. 用户确认界面展示这些资源，确认后注入子任务
  4. 支持"默认同意"模式（通过配置或环境变量）
"""

import sys, os, subprocess, json, shutil, re, logging, time, threading
from pathlib import Path
from datetime import datetime

AGENT_GO_DIR = Path.home() / ".agent_go"
AGENT_GO_DIR.mkdir(exist_ok=True)
CONFIG_PATH = AGENT_GO_DIR / "config.json"

DEFAULT_CONFIG = {
    "plan_api": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com/v1/messages",
        "api_key": "",
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "temperature": 0.2
    },
    "behavior": {
        "auto_confirm_plan": False,      # 默认同意 Plan 方案
        "auto_confirm_subtasks": False,    # 默认同意子任务列表
        "show_agent_prompt": True,         # 展示给 Agent 的 Prompt
        "show_resource_map": True,          # 展示共享资源清单
        "max_plan_iterations": 5            # 最大 Plan 重生成次数
    },
    "fallback": {
        "local_model_url": "http://localhost:8000/v1/chat/completions",
        "local_model_name": "qwen",
        "enable_rules": True
    }
}

DECOMPOSE_RULES = [
    {
        "patterns": ["JWT", "jwt", "auth", "认证", "token"],
        "subtasks": [
            {"id": "sub-1", "title": "后端JWT签名迁移", "description": "将后端JWT签名算法从HS256迁移至RS256，生成RSA密钥对并更新签名/验证逻辑", "files_hint": "src/auth/**"},
            {"id": "sub-2", "title": "前端登录适配", "description": "前端适配新的公钥获取流程，更新登录页JWT解析和验证逻辑", "files_hint": "src/pages/login/**"},
            {"id": "sub-3", "title": "测试补充", "description": "补充RS256相关的单元测试和端到端测试", "files_hint": "tests/**"},
        ]
    },
    {
        "patterns": ["test", "测试", "coverage"],
        "subtasks": [
            {"id": "sub-1", "title": "分析现有测试覆盖", "description": "识别当前测试未覆盖的模块和函数", "files_hint": "tests/**, src/**"},
            {"id": "sub-2", "title": "编写补充测试", "description": "为未覆盖模块添加单元测试和集成测试", "files_hint": "tests/**"},
        ]
    },
]

def safe_input(prompt=""):
    """包装 input()，在非交互模式下返回空字符串（触发默认确认路径）。"""
    try:
        return input(prompt)
    except EOFError:
        print()
        return ""

# ────────────────────────── 配置 & 日志 ──────────────────────────

def load_config():
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        # 合并默认值（新增字段兼容）
        merged = DEFAULT_CONFIG.copy()
        merged.update(saved)
        return merged
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"⚙️  已创建默认配置: {CONFIG_PATH}")
    return DEFAULT_CONFIG

def get_api_key(config):
    return os.environ.get("AGENT_GO_API_KEY", "") or config.get("plan_api", {}).get("api_key", "")

def setup_logger(task_id, task_dir):
    logger = logging.getLogger(f"agent_go.{task_id}")
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fh = logging.FileHandler(task_dir / "execution.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

def log_event(logger, event, data):
    logger.debug(json.dumps({"timestamp": datetime.now().isoformat(), "event": event, **data}, ensure_ascii=False))

# ────────────────────────── 文档读取 ──────────────────────────

def read_reference_docs(doc_paths, repo, logger):
    contents = []
    for path_str in doc_paths:
        path = Path(path_str)
        if not path.is_absolute():
            path = repo / path_str
        if not path.exists():
            logger.warning(f"文档不存在: {path}")
            continue
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                max_len = 15000
                if len(text) > max_len:
                    text = text[:max_len] + f"\n... [截断，原 {len(text)} 字符]"
                contents.append(f"===== {path.name} =====\n{text}\n===== 结束 =====")
                logger.info(f"已读文档: {path} ({len(text)} 字符)")
            except Exception as e:
                logger.warning(f"读取失败 {path}: {e}")
        elif path.is_dir():
            for md_file in sorted(path.glob("*.md")):
                try:
                    text = md_file.read_text(encoding="utf-8", errors="replace")
                    max_len = 8000
                    if len(text) > max_len:
                        text = text[:max_len] + "\n... [截断]"
                    contents.append(f"===== {md_file.name} =====\n{text}\n===== 结束 =====")
                    logger.info(f"已读文档: {md_file} ({len(text)} 字符)")
                except Exception as e:
                    logger.warning(f"读取失败 {md_file}: {e}")
    return "\n".join(contents) if contents else ""

# ────────────────────────── API 调用 ──────────────────────────

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

# ────────────────────────── 项目分析 ──────────────────────────

def analyze_project(repo):
    """分析项目结构，返回文件列表和关键目录。"""
    try:
        if (repo / ".git").exists():
            result = subprocess.run(["git", "ls-files"], cwd=str(repo), capture_output=True, text=True, timeout=5)
            files = result.stdout.strip().split("\n")[:50]
            return "\n".join(files)
        else:
            result = subprocess.run(["find", ".", "-maxdepth", "2", "-type", "f"], cwd=str(repo), capture_output=True, text=True, timeout=5)
            files = result.stdout.strip().split("\n")[:30]
            return "\n".join(f.lstrip("./") for f in files)
    except Exception:
        return ""

def get_git_info(repo):
    """获取 git 远程地址和当前分支。"""
    info = {"remote": "", "branch": "", "commit": ""}
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo), capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            info["remote"] = r.stdout.strip()
        b = subprocess.run(["git", "branch", "--show-current"], cwd=str(repo), capture_output=True, text=True, timeout=3)
        if b.returncode == 0:
            info["branch"] = b.stdout.strip()
        c = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=3)
        if c.returncode == 0:
            info["commit"] = c.stdout.strip()
    except Exception:
        pass
    return info

def get_resource_map(repo, git_info):
    """生成共享资源清单。"""
    resources = {
        "project_root": str(repo),
        "git_remote": git_info.get("remote", ""),
        "git_branch": git_info.get("branch", ""),
        "git_commit": git_info.get("commit", ""),
        "directories": [],
        "key_files": []
    }

    # 扫描关键目录
    for subdir in ["src", "lib", "app", "components", "pages", "tests", "docs"]:
        p = repo / subdir
        if p.exists() and p.is_dir():
            resources["directories"].append(subdir)

    # 扫描关键文件
    for pattern in ["package.json", "requirements.txt", "Cargo.toml", "go.mod", "README.md", ".env.example", "docker-compose.yml"]:
        p = repo / pattern
        if p.exists():
            resources["key_files"].append(pattern)

    return resources

# ────────────────────────── Plan Mode（含 Agent Prompt & 资源清单） ──────────────────────────

def generate_plan(task, repo, config, logger, supplement="", reference_docs="", iteration=1) -> dict:
    logger.info(f"【Plan Mode】第 {iteration} 次生成")
    log_event(logger, "plan_generate", {"iteration": iteration, "has_supplement": bool(supplement), "has_docs": bool(reference_docs)})

    project_files = analyze_project(repo)
    git_info = get_git_info(repo)
    resource_map = get_resource_map(repo, git_info)

    system_prompt = """你是一位资深软件架构师。请为以下开发任务制定详细的执行方案。\n输出必须是合法的 JSON，不要包含任何其他文字。结构：\n{\n"overview": "任务概述，2-3句话",\n"steps": [\n{\n"id": 1,\n"title": "步骤标题",\n"description": "详细描述该步骤做什么",\n"files": ["涉及文件路径1"],\n"verification": "完成后如何验证",\n"risks": ["潜在风险1"],\n"agent_prompt": "给执行Agent的完整Prompt，包含具体指令、上下文、约束条件"\n}\n],\n"dependencies": {"2": [1]},\n"estimated_effort": "预估工作量",\n"shared_resources": {\n"directories": ["关键目录1"],\n"git_remote": "git远程地址",\n"git_branch": "当前分支",\n"config_files": ["配置文件1"],\n"env_vars": ["环境变量1"]\n}\n}\n要求：\n1. 每个 step 必须包含 agent_prompt 字段，这是给 Claude Code 执行该步骤时的完整指令\n2. shared_resources 描述所有子任务共享的资源和上下文\n3. 步骤 2-5 个，可独立执行"""

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

def print_plan(plan, config):
    """展示 Plan，包含 Agent Prompt 和资源清单。"""
    behavior = config.get("behavior", {})

    print("\n" + "=" * 70)
    print("📋 执行方案（Plan Mode）")
    print("=" * 70)
    print(f"\n📝 概述: {plan.get('overview', 'N/A')}")
    print(f"⏱️  预估: {plan.get('estimated_effort', 'N/A')}")

    # 共享资源清单
    sr = plan.get("shared_resources", {})
    if sr and behavior.get("show_resource_map", True):
        print(f"\n📦 共享资源清单:")
        if sr.get("git_remote"):
            print(f"   🔗 Git 远程: {sr['git_remote']}")
        if sr.get("git_branch"):
            print(f"   🌿 当前分支: {sr['git_branch']}")
        if sr.get("directories"):
            print(f"   📁 关键目录: {', '.join(sr['directories'])}")
        if sr.get("config_files"):
            print(f"   ⚙️  配置文件: {', '.join(sr['config_files'])}")
        if sr.get("env_vars"):
            print(f"   🔐 环境变量: {', '.join(sr['env_vars'])}")

    print(f"\n📌 执行步骤:")
    for step in plan.get("steps", []):
        print(f"\n[{step['id']}] {step['title']}")
        print(f"      {step['description']}")
        print(f"      📁 文件: {', '.join(step.get('files', []))}")
        print(f"      ✅ 验证: {step.get('verification', 'N/A')}")
        if step.get("risks"):
            print(f"      ⚠️  风险: {', '.join(step['risks'])}")

        # Agent Prompt
        if behavior.get("show_agent_prompt", True) and step.get("agent_prompt"):
            prompt_preview = step["agent_prompt"][:200] + "..." if len(step["agent_prompt"]) > 200 else step["agent_prompt"]
            print(f"      🤖 Agent Prompt: {prompt_preview}")

    deps = plan.get("dependencies", {})
    if deps:
        print(f"\n🔗 依赖关系:")
        for sid, prereqs in deps.items():
            print(f"      步骤 {sid} 依赖: {prereqs}")
    print("=" * 70)

def confirm_plan(plan, config, repo, logger, iteration=1) -> tuple:
    """
    用户确认 Plan。支持默认同意模式。
    返回: (plan, doc_paths) 或 (None, None)
    """
    behavior = config.get("behavior", {})
    auto_confirm = behavior.get("auto_confirm_plan", False)
    reference_doc_paths = []

    # 检查环境变量强制交互
    if os.environ.get("AGENT_GO_INTERACTIVE", "").lower() == "1":
        auto_confirm = False

    while True:
        print_plan(plan, config)

        # 默认同意模式
        if auto_confirm and iteration == 1:
            print(f"\n⚡ 默认同意模式已开启（来自配置 behavior.auto_confirm_plan）")
            print(f"   按 Enter 直接确认，或输入任意键进入交互模式...")
            quick = safe_input("\n> ").strip()
            if not quick:
                logger.info("默认同意模式：自动确认 Plan")
                log_event(logger, "plan_auto_confirmed", {"iteration": iteration})
                return plan, reference_doc_paths
            # 用户输入了内容，进入交互模式
            auto_confirm = False

        print("\n请选择操作:")
        print("  [Y] 确认方案，拆解为子任务并执行")
        print("  [S] 补充输入/修正需求（重新生成）")
        print("  [D] 挂载参考文档（重新生成）")
        print("  [E] 编辑某个步骤")
        print("  [R] 重新生成方案")
        print("  [N] 取消任务")

        choice = safe_input("\n> ").strip().upper()
        log_event(logger, "user_plan_choice", {"choice": choice, "iteration": iteration, "auto_confirm": auto_confirm})

        if choice == "Y" or (choice == "" and auto_confirm):
            logger.info("用户确认 Plan")
            return plan, reference_doc_paths
        elif choice == "N":
            logger.info("用户取消")
            print("❌ 已取消")
            sys.exit(0)
        elif choice == "R":
            logger.info("用户请求重新生成")
            return None, reference_doc_paths
        elif choice == "E":
            idx_str = safe_input(f"编辑第几个步骤 (1-{len(plan['steps'])}): ").strip()
            if idx_str.isdigit() and 1 <= int(idx_str) <= len(plan["steps"]):
                idx = int(idx_str) - 1
                step = plan["steps"][idx]
                new_title = safe_input(f"  标题 [{step['title']}]: ").strip()
                new_desc = safe_input(f"  描述 [{step['description']}]: ").strip()
                new_files = safe_input(f"  文件 [{', '.join(step.get('files',[]))}]: ").strip()
                new_prompt = safe_input(f"  Agent Prompt [{step.get('agent_prompt','')[:50]}...]: ").strip()
                if new_title: step["title"] = new_title
                if new_desc: step["description"] = new_desc
                if new_files: step["files"] = [f.strip() for f in new_files.split(",")]
                if new_prompt: step["agent_prompt"] = new_prompt
                logger.info(f"用户编辑步骤 {step['id']}")
        elif choice == "S":
            print("\n✏️  请输入补充内容（支持多行，空行结束）：")
            lines = []
            while True:
                line = safe_input()
                if line.strip() == "" and lines and lines[-1].strip() == "":
                    break
                lines.append(line)
            supplement = "\n".join(lines).strip()
            if not supplement:
                print("补充为空，未重新生成")
                continue
            logger.info(f"用户补充: {supplement[:200]}...")
            existing_docs = read_reference_docs(reference_doc_paths, repo, logger) if reference_doc_paths else ""
            iteration += 1
            try:
                plan = generate_plan(plan.get("_original_task", ""), repo, config, logger, supplement, existing_docs, iteration)
                plan["_original_task"] = plan.get("_original_task", "")
                print(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                print(f"⚠️ 失败: {e}")
        elif choice == "D":
            print("\n📎 输入参考文档路径（多个逗号分隔，目录自动读 .md）：")
            doc_input = safe_input("\n> ").strip()
            if not doc_input:
                continue
            new_paths = [p.strip() for p in doc_input.split(",")]
            reference_doc_paths.extend(new_paths)
            reference_doc_paths = list(dict.fromkeys(reference_doc_paths))
            docs_content = read_reference_docs(reference_doc_paths, repo, logger)
            if not docs_content:
                print("⚠️ 未读取到有效文档")
                continue
            logger.info(f"挂载 {len(reference_doc_paths)} 个文档，重新生成")
            iteration += 1
            try:
                plan = generate_plan(plan.get("_original_task", ""), repo, config, logger, "", docs_content, iteration)
                plan["_original_task"] = plan.get("_original_task", "")
                print(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                print(f"⚠️ 失败: {e}")
        else:
            print("无效输入")

def plan_to_subtasks(plan, logger):
    """Plan → 子任务，注入 Agent Prompt、资源清单、依赖关系。"""
    subtasks = []
    shared = plan.get("shared_resources", {})
    deps = plan.get("dependencies", {})

    for step in plan.get("steps", []):
        files = step.get("files", [])
        files_hint = ", ".join(files) if files else "*"

        desc_parts = [step.get("description", "")]
        if step.get("agent_prompt"):
            desc_parts.append(f"\n【Agent 执行指令】\n{step['agent_prompt']}")
        if shared:
            resource_text = "\n".join([
                f"Git 远程: {shared.get('git_remote', 'N/A')}" if shared.get('git_remote') else "",
                f"当前分支: {shared.get('git_branch', 'N/A')}" if shared.get('git_branch') else "",
                f"关键目录: {', '.join(shared.get('directories', []))}" if shared.get('directories') else "",
                f"配置文件: {', '.join(shared.get('config_files', []))}" if shared.get('config_files') else "",
                f"环境变量: {', '.join(shared.get('env_vars', []))}" if shared.get('env_vars') else "",
            ])
            resource_text = "\n".join(line for line in resource_text.split("\n") if line)
            if resource_text:
                desc_parts.append(f"\n【共享资源清单】\n{resource_text}")
        if step.get("verification"):
            desc_parts.append(f"\n【验证命令】\n{step['verification']}")
        if step.get("risks"):
            desc_parts.append(f"\n【风险提示】\n{'; '.join(step['risks'])}")

        desc = "\n".join(desc_parts)

        step_id = str(step["id"])
        upstream_ids = deps.get(step_id, [])
        depends_on = [f"sub-{d}" for d in upstream_ids]

        subtasks.append({
            "id": f"sub-{step['id']}",
            "title": step.get("title", f"步骤 {step['id']}"),
            "description": desc,
            "files_hint": files_hint,
            "agent_prompt": step.get("agent_prompt", ""),
            "verification": step.get("verification", ""),
            "risks": step.get("risks", []),
            "depends_on": depends_on,
        })

    log_event(logger, "plan_decomposed", {"count": len(subtasks)})
    return subtasks

# ────────────────────────── 降级拆解 ──────────────────────────

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

# ────────────────────────── 终端交互 ──────────────────────────

def print_subtasks(subtasks, config):
    behavior = config.get("behavior", {})
    print("\n" + "─" * 60)
    print("📋 子任务列表")
    print("─" * 60)
    for st in subtasks:
        print(f"\n[{st['id']}] {st['title']}")
        # 只展示描述前200字符，避免太长
        desc = st.get("description", "")
        preview = desc[:200] + "..." if len(desc) > 200 else desc
        print(f"      {preview}")
        if st.get("files_hint"):
            print(f"      📁 涉及文件: {st['files_hint']}")
        if behavior.get("show_agent_prompt", True) and st.get("agent_prompt"):
            prompt_preview = st["agent_prompt"][:150] + "..." if len(st["agent_prompt"]) > 150 else st["agent_prompt"]
            print(f"      🤖 Agent Prompt: {prompt_preview}")
    print("\n" + "─" * 60)

def confirm_subtasks(subtasks, config, logger):
    behavior = config.get("behavior", {})
    auto_confirm = behavior.get("auto_confirm_subtasks", False)

    # 环境变量强制交互
    if os.environ.get("AGENT_GO_INTERACTIVE", "").lower() == "1":
        auto_confirm = False

    print_subtasks(subtasks, config)

    if auto_confirm:
        print(f"\n⚡ 默认同意模式已开启（behavior.auto_confirm_subtasks）")
        print(f"   按 Enter 直接执行，或输入任意键进入交互...")
        quick = safe_input("\n> ").strip()
        if not quick:
            logger.info("默认同意模式：自动确认子任务")
            log_event(logger, "subtasks_auto_confirmed", {"count": len(subtasks)})
            return subtasks
        auto_confirm = False

    print("\n请选择操作:")
    print("  [Y] 全部确认并执行")
    print("  [N] 取消任务")
    print("  [E] 编辑某个子任务")
    print("  [A] 添加新子任务")
    print("  [D] 删除某个子任务")

    edit_history = []
    while True:
        choice = safe_input("\n> ").strip().upper()
        log_event(logger, "user_subtask_choice", {"choice": choice})
        if choice == "Y":
            return subtasks
        elif choice == "N":
            sys.exit(0)
        elif choice == "E":
            idx_str = safe_input(f"编辑第几个 (1-{len(subtasks)}): ").strip()
            if idx_str.isdigit() and 1 <= int(idx_str) <= len(subtasks):
                idx = int(idx_str) - 1
                st = subtasks[idx]
                t = safe_input(f"标题 [{st['title']}]: ").strip()
                d = safe_input(f"描述 [{st['description'][:100]}...]: ").strip()
                f = safe_input(f"文件 [{st.get('files_hint','')}]: ").strip()
                p = safe_input(f"Agent Prompt [{st.get('agent_prompt','')[:50]}...]: ").strip()
                if t: st["title"] = t
                if d: st["description"] = d
                if f: st["files_hint"] = f
                if p: st["agent_prompt"] = p
            print_subtasks(subtasks, config)
        elif choice == "A":
            title = safe_input("新标题: ").strip()
            desc = safe_input("描述: ").strip()
            files = safe_input("文件: ").strip()
            prompt = safe_input("Agent Prompt: ").strip()
            subtasks.append({"id": f"sub-{len(subtasks)+1}", "title": title, "description": desc, "files_hint": files, "agent_prompt": prompt})
            print_subtasks(subtasks, config)
        elif choice == "D":
            idx_str = safe_input(f"删除第几个 (1-{len(subtasks)}): ").strip()
            if idx_str.isdigit() and 1 <= int(idx_str) <= len(subtasks):
                del subtasks[int(idx_str)-1]
                for i, st in enumerate(subtasks):
                    st["id"] = f"sub-{i+1}"
            print_subtasks(subtasks, config)
        else:
            print("无效输入")

def verify_subtask(current, total, summary, logger, config=None):
    print(f"\n{'='*60}\n✅ {current}/{total} 完成\n{'='*60}")
    print(f"📊 {summary}\n[C]继续 [R]重试 [M]修改 [A]中止")
    auto_verify = config.get("behavior", {}).get("auto_confirm_plan", False) if config else False
    while True:
        c = safe_input("\n> ").strip().upper()
        log_event(logger, "user_verify", {"current": current, "choice": c})
        if c in ("C", "CONTINUE") or (c == "" and auto_verify): return "next"
        elif c in ("R", "RETRY"): return "retry"
        elif c in ("M", "MODIFY"): return "modify"
        elif c in ("A", "ABORT"): return "abort"
        else: print("无效输入")

def _git_merge_upstream(src_worktree, dst_worktree, tag, logger):
    """通过 git fetch + merge 将上游 worktree 的代码传递到当前 worktree。"""
    remote_name = f"upstream-{tag}"
    # 添加上游 worktree 为临时 remote
    subprocess.run(["git", "remote", "add", remote_name, str(src_worktree)],
                   cwd=str(dst_worktree), capture_output=True)
    subprocess.run(["git", "fetch", remote_name, f"refs/tags/{tag}:refs/tags/{tag}"],
                   cwd=str(dst_worktree), capture_output=True)
    result = subprocess.run(
        ["git", "merge", tag, "--allow-unrelated-histories",
         "-m", f"merge upstream {tag}"],
        cwd=str(dst_worktree), capture_output=True, text=True)
    subprocess.run(["git", "remote", "remove", remote_name],
                   cwd=str(dst_worktree), capture_output=True)
    if result.returncode == 0:
        logger.info(f"git merge {tag} 成功")
    else:
        logger.warning(f"git merge {tag}: {result.stderr[:200]}")

def _run_headless(task_md, worktree, env, logger, sub_id):
    """无头模式：claude -p 带实时流式输出、交互检测和超时重试。"""

    def _run_one(prompt, attempt):
        """启动 claude -p 并实时跟踪输出。返回 (proc, output_lines, interaction_detected)。"""
        proc = subprocess.Popen([
            "claude", "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "text",
        ], env=env, cwd=str(worktree), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        last_ts = [time.time()]
        lines = []
        waiting = [False]

        def read(stream, label):
            for line in iter(stream.readline, ''):
                s = line.rstrip()
                if s:
                    ts = datetime.now().strftime("%H:%M:%S")
                    lines.append(f"[{ts}] {s[:200]}")
                    logger.info(f"[claude {label}] {s[:200]}")
                    last_ts[0] = time.time()
                    for pat in [r"waiting for input", r"approve\s+(Write|Edit|Bash|Read)",
                                r"permission required", r"\[y/n\]", r"press.*to continue"]:
                        if re.search(pat, s, re.IGNORECASE):
                            waiting[0] = True
                            logger.error(f"⚠️ 检测到交互请求 (attempt={attempt}): {s[:200]}")
                            break

        t_out = threading.Thread(target=read, args=(proc.stdout, "out"), daemon=True)
        t_err = threading.Thread(target=read, args=(proc.stderr, "err"), daemon=True)
        t_out.start()
        t_err.start()

        idle_logged_at = 0
        while proc.poll() is None:
            idle = time.time() - last_ts[0]
            if idle > 300:
                logger.error(f"claude {idle:.0f}s 无输出 (attempt={attempt})，强制终止")
                proc.kill()
                break
            if idle > 30 and idle - idle_logged_at > 30:
                logger.info(f"claude 工作中... (无输出 {idle:.0f}s, attempt={attempt})")
                idle_logged_at = idle
            time.sleep(5)

        t_out.join(timeout=5)
        t_err.join(timeout=5)
        proc.wait()
        return proc, lines, waiting[0]

    RETRY_SUFFIX = (
"\n\n【系统指令】你必须立即完成上述所有任务，直接执行文件创建和修改操作。"
"不要询问任何问题，不要等待确认，不要输出中间讨论。"
"完成后输出简洁的状态报告和变更摘要。"
    )
    MAX_ATTEMPTS = 2

    logger.info("无头模式: claude -p")
    log_event(logger, "subtask_headless_start", {"id": sub_id})

    all_lines = []
    final_rc = -1
    interaction = False

    for attempt in range(MAX_ATTEMPTS):
        if attempt == 0:
            prompt = task_md
        else:
            logger.warning(f"超时重试 (attempt={attempt+1})，注入催促指令")
            log_event(logger, "subtask_headless_retry", {"id": sub_id, "attempt": attempt + 1})
            prompt = task_md + RETRY_SUFFIX

        proc, lines, waiting = _run_one(prompt, attempt + 1)
        all_lines.extend(lines)
        all_lines.append(f"--- attempt={attempt+1} exit_code={proc.returncode} ---")
        interaction = interaction or waiting
        final_rc = proc.returncode

        if final_rc == 0:
            break
        if not waiting:
            break  # 非交互超时（如 API 超时），不重试

    log_event(logger, "subtask_headless_complete", {
        "id": sub_id, "exit_code": final_rc,
        "interaction_detected": interaction,
        "attempts": attempt + 1,
        "output_lines": len(all_lines),
    })

    return subprocess.CompletedProcess(
        [], final_rc,
        stdout="\n".join(all_lines),
        stderr=""
    )

# ────────────────────────── 核心执行 ──────────────────────────

def run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False):
    sub_id = subtask["id"]
    sub_dir = task_dir / sub_id
    worktree = sub_dir / "work"
    worktree.mkdir(parents=True)

    logger.info(f"【执行】{sub_id}: {subtask['title']}")
    log_event(logger, "subtask_start", {"id": sub_id, "title": subtask["title"],
                "depends_on": subtask.get("depends_on", []), "headless": headless})

    clone_start = time.time()
    if (repo / ".git").exists():
        subprocess.run(["git", "clone", str(repo), str(worktree)], capture_output=True, check=True)
    else:
        shutil.copytree(repo, worktree, dirs_exist_ok=True)

    # 产物传递：通过 git merge 将上游代码合并到当前 worktree
    if upstream_worktrees:
        for up_id, up_path in upstream_worktrees.items():
            if up_path.exists():
                logger.info(f"产物传递 (git merge): {up_id} → {sub_id}")
                _git_merge_upstream(up_path, worktree, up_id, logger)
    clone_time = time.time() - clone_start

    # 构建 TASK.md：包含完整 Agent Prompt、资源清单、上游上下文
    task_md_parts = [f"# 子任务: {subtask['title']}", ""]

    # 注入上游子任务的共享上下文
    shared_ctx = (task_dir / "SHARED_CONTEXT.md")
    if shared_ctx.exists():
        ctx = shared_ctx.read_text(encoding="utf-8")
        if ctx.strip():
            task_md_parts.extend([
                "## 上游子任务上下文",
                "以下是前面子任务的关键信息，请先理解再开始操作：",
                ctx,
                "",
            ])

    task_md_parts.append(f"## 描述\n{subtask['description']}")
    if subtask.get("agent_prompt"):
        task_md_parts.extend(["", "## 执行指令（Agent Prompt）", subtask["agent_prompt"]])

    # 验证要求
    verification = subtask.get("verification", "")
    exec_requirements = [
        "",
        "## 执行要求",
        "- 在此隔离 worktree 中完成修改",
        "- 变更保留在此目录",
    ]
    if verification:
        exec_requirements.append(f"- **必须执行验证**: `{verification}`，确保通过后再完成")
        exec_requirements.append("- 如验证失败，请修复问题后重新验证，直到通过")
    if not headless:
        exec_requirements.append("- 完成后退出 Claude Code（/exit 或 Ctrl+D）")
    task_md_parts.extend(exec_requirements)

    # 将 Agent Prompt 中的源项目路径替换为 worktree 路径，确保隔离
    task_md = "\n".join(task_md_parts).replace(str(repo), str(worktree))
    (sub_dir / "TASK.md").write_text(task_md, encoding="utf-8")

    print(f"\n🚀 {sub_id}: {subtask['title']}")
    env = os.environ.copy()
    env.update({"AGENT_GO_TASK_ID": task_id, "AGENT_GO_SUBTASK_ID": sub_id, "AGENT_GO_WORKTREE": str(worktree)})

    claude_start = time.time()

    if headless:
        sandbox_type = "headless"
        result = _run_headless(task_md, worktree, env, logger, sub_id)
    else:
        try:
            result = subprocess.run(["greywall", "--", "claude", str(worktree)], env=env, cwd=str(worktree))
            sandbox_type = "greywall"
        except FileNotFoundError:
            print("   ⚠️ Greywall 未安装，降级原生")
            result = subprocess.run(["claude", str(worktree)], env=env, cwd=str(worktree))
            sandbox_type = "native"

    claude_time = time.time() - claude_start

    # 记录变更摘要
    diff = subprocess.run(["git", "diff", "--stat"], cwd=str(worktree), capture_output=True, text=True)
    summary = diff.stdout.strip() or "无文件变更"

    # Git 提交 + tag，供下游子任务 merge
    has_changes = summary != "无文件变更"
    if has_changes:
        subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "commit", "-m",
                        f"{sub_id}: {subtask['title']}"],
                       cwd=str(worktree), capture_output=True)
    subprocess.run(["git", "tag", "-f", sub_id], cwd=str(worktree), capture_output=True)
    if has_changes:
        logger.info(f"已提交并打 tag: {sub_id}")
    else:
        logger.info(f"已打 tag (无新增变更): {sub_id}")

    # 验证执行
    verification = subtask.get("verification", "")
    verify_ok = True
    if verification and has_changes:
        logger.info(f"执行验证: {verification}")
        vr = subprocess.run(verification, shell=True, cwd=str(worktree),
                            capture_output=True, text=True, timeout=120)
        if vr.returncode != 0:
            logger.warning(f"验证失败 (rc={vr.returncode}): {vr.stderr[-300:]}")
            if headless:
                logger.info("自动重试: 注入修复指令")
                fix_prompt = task_md + (
"\n\n【验证失败】以下命令执行失败:\n"
f"  {verification}\n"
f"错误输出:\n{vr.stderr[-500:]}\n"
"请修复上述问题，确保验证命令通过。直接修改文件，不要询问。"
                )
                _run_headless(fix_prompt, worktree, env, logger, f"{sub_id}-fix")
                # 重新提交
                subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
                subprocess.run(["git", "commit", "-m",
                                f"{sub_id} (fix): 验证修复"], cwd=str(worktree),
                               capture_output=True)
                subprocess.run(["git", "tag", "-f", sub_id], cwd=str(worktree),
                               capture_output=True)
                # 重新验证
                vr2 = subprocess.run(verification, shell=True, cwd=str(worktree),
                                     capture_output=True, text=True, timeout=120)
                verify_ok = vr2.returncode == 0
                logger.info(f"重试验证: {'通过' if verify_ok else '仍失败'}")
                # 更新 diff
                diff2 = subprocess.run(["git", "diff", "--stat", "HEAD~1"], cwd=str(worktree),
                                       capture_output=True, text=True)
                summary = diff2.stdout.strip() or summary
            else:
                verify_ok = False
        else:
            logger.info("验证通过")

    # 生成共享上下文（供下游子任务使用）
    ctx_parts = [
        f"### {sub_id}: {subtask['title']}",
        f"- 状态: {'通过' if verify_ok else '需关注'}",
        f"- 变更: {summary}",
    ]
    if verification:
        ctx_parts.append(f"- 验证: `{verification}` — {'✅' if verify_ok else '❌'}")
    if subtask.get("risks"):
        ctx_parts.append(f"- 风险: {'; '.join(subtask['risks'])}")
    # 尝试从 Claude 输出中提取关键决策
    if headless and hasattr(result, 'stdout') and result.stdout:
        decisions = re.findall(r'(?:决策|选择|采用|改[用为]|降级|fallback)\S*[：:]\s*(.+)',
                               result.stdout, re.IGNORECASE)
        if decisions:
            ctx_parts.append(f"- 关键决策: {'; '.join(decisions[:3])}")
    ctx_parts.append("")
    shared_ctx = (task_dir / "SHARED_CONTEXT.md")
    existing = shared_ctx.read_text(encoding="utf-8") if shared_ctx.exists() else ""
    shared_ctx.write_text(existing + "\n".join(ctx_parts) + "\n", encoding="utf-8")
    logger.info(f"共享上下文已更新: {len(shared_ctx.read_text().splitlines())} 行")

    status = "completed" if (result.returncode == 0 and verify_ok) else "failed"
    log_event(logger, "subtask_complete", {
        "id": sub_id, "status": status, "sandbox_type": sandbox_type,
        "clone_sec": round(clone_time, 2), "claude_sec": round(claude_time, 2),
        "summary": summary, "verify_ok": verify_ok,
    })

    return {"subtask_id": sub_id, "status": status, "exit_code": result.returncode,
            "summary": summary, "worktree": str(worktree), "sandbox_type": sandbox_type,
            "verify_ok": verify_ok, "duration_sec": round(claude_time, 2)}

# ────────────────────────── 主命令 ──────────────────────────

def cmd_run():
    # 解析参数
    repo_idx = 2
    task_idx = 3
    doc_paths = []
    auto_yes = "--yes" in sys.argv or "-y" in sys.argv

    if auto_yes:
        sys.argv = [a for a in sys.argv if a not in ("--yes", "-y")]

    if "--docs" in sys.argv:
        docs_idx = sys.argv.index("--docs")
        if docs_idx + 1 < len(sys.argv):
            doc_paths = [p.strip() for p in sys.argv[docs_idx + 1].split(",")]
        if docs_idx < repo_idx:
            repo_idx = 2 if docs_idx > 2 else 2

    if len(sys.argv) < 3:
        print("Usage: agent_go run <repo-path> '<task>' [--docs <doc1,doc2>] [--yes]")
        sys.exit(1)

    repo = Path(sys.argv[repo_idx]).resolve()
    task = sys.argv[task_idx] if len(sys.argv) > task_idx else "请根据项目情况完成改进"

    if not repo.exists():
        print(f"❌ 路径不存在: {repo}")
        sys.exit(1)

    config = load_config()

    if auto_yes:
        config["behavior"]["auto_confirm_plan"] = True
        config["behavior"]["auto_confirm_subtasks"] = True

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    task_id = f"task-{ts}"
    task_dir = AGENT_GO_DIR / task_id
    task_dir.mkdir(parents=True)

    logger = setup_logger(task_id, task_dir)
    logger.info("=" * 60)
    logger.info("任务启动")
    logger.info(f"ID: {task_id}, 任务: {task}, 项目: {repo}")
    if doc_paths:
        logger.info(f"参考文档: {doc_paths}")

    print(f"\n🔧 主任务: {task}")
    print(f"📁 项目: {repo}")
    print(f"🆔 任务ID: {task_id}")
    if doc_paths:
        print(f"📎 参考文档: {', '.join(doc_paths)}")

    # Plan Mode
    print("\n🤖 进入 Plan Mode...")
    initial_docs = read_reference_docs(doc_paths, repo, logger) if doc_paths else ""

    plan = None
    max_iter = config.get("behavior", {}).get("max_plan_iterations", 5)
    iteration = 1
    last_error = None

    for attempt in range(3):
        try:
            plan = generate_plan(task, repo, config, logger, "", initial_docs, iteration)
            plan["_original_task"] = task
            break
        except Exception as e:
            last_error = e
            logger.error(f"Plan 失败 (尝试 {attempt+1}): {e}")

    if plan is not None:
        # API 成功 → Plan 确认流程
        confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration=1)
        while confirmed_plan is None and iteration < max_iter:
            iteration += 1
            plan = generate_plan(task, repo, config, logger, "", "", iteration)
            plan["_original_task"] = task
            confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration)

        if confirmed_plan is None:
            print(f"⚠️ 达到最大迭代次数 {max_iter}，使用最后版本")
            confirmed_plan = plan

        subtasks = plan_to_subtasks(confirmed_plan, logger)
        doc_paths = final_doc_paths
    else:
        # 降级拆解
        print(f"\n⚠️ Plan Mode 失败: {last_error}")
        subtasks = decompose_fallback(task, repo, config, logger)

    # 子任务确认
    confirmed = confirm_subtasks(subtasks, config, logger)

    meta = {
        "task_id": task_id, "task": task, "repo": str(repo),
        "created": ts, "status": "running",
        "reference_docs": doc_paths,
        "subtasks": confirmed, "results": []
    }
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # 执行
    total = len(confirmed)
    final_status = "completed"
    worktree_map = {}  # sub_id → worktree 路径映射

    for i, st in enumerate(confirmed):
        # 收集上游 worktree
        upstream = {}
        for dep_id in st.get("depends_on", []):
            if dep_id in worktree_map:
                upstream[dep_id] = worktree_map[dep_id]

        result = run_subtask(task_id, st, repo, task_dir, logger, upstream, headless=auto_yes)
        worktree_map[st["id"]] = task_dir / st["id"] / "work"
        meta["results"].append(result)
        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        if i + 1 == total:
            print(f"\n{'='*60}\n🎉 全部完成 ({i+1}/{total})\n{'='*60}")
            break

        decision = verify_subtask(i+1, total, result["summary"], logger, config)
        if decision == "abort":
            final_status = "aborted"
            break
        elif decision == "retry":
            shutil.rmtree(task_dir / st["id"], ignore_errors=True)
            result = run_subtask(task_id, st, repo, task_dir, logger, headless=auto_yes)
            meta["results"][-1] = result
        elif decision == "modify":
            worktree = task_dir / st["id"] / "work"
            subprocess.run(["claude", str(worktree)], cwd=str(worktree))
            diff = subprocess.run(["git", "diff", "--stat"], cwd=str(worktree), capture_output=True, text=True)
            meta["results"][-1]["summary"] = diff.stdout.strip() or "无文件变更"

    meta["status"] = final_status
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n📦 最终报告")
    print("─" * 60)
    for r in meta["results"]:
        icon = "✅" if r["status"] == "completed" else "❌"
        print(f"{icon} {r['subtask_id']}: {r['summary']}")
    print("─" * 60)
    print(f"\n📁 {task_dir}")
    print(f"📝 {task_dir}/execution.log")

def cmd_list():
    tasks = sorted(AGENT_GO_DIR.glob("task-*"))
    if not tasks:
        print("暂无任务")
        return
    print(f"{'任务ID':<<26} {'状态':<<12} {'子任务':<<8} {'参考文档':<<12} {'描述'}")
    print("─" * 90)
    for t in tasks:
        meta_path = t / "meta.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = meta.get("status", "unknown")
        icon = {"completed": "🟢", "aborted": "🟡", "failed": "🔴"}.get(status, "⚪")
        docs = ",".join(meta.get("reference_docs", []))[:15]
        print(f"{t.name:<25} {icon} {status:<10} {len(meta.get('subtasks',[])):<8} {docs:<12} {meta.get('task','')[:30]}")

def cmd_show():
    if len(sys.argv) < 3:
        print("Usage: agent_go show <task-id>")
        sys.exit(1)
    task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print("任务不存在")
        sys.exit(1)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    print(f"\n🆔 {task_id}")
    print(f"📝 {meta['task']}")
    print(f"📁 {meta['repo']}")
    print(f"📊 {meta.get('status','unknown')}")
    if meta.get("reference_docs"):
        print(f"📎 参考文档: {', '.join(meta['reference_docs'])}")
    for i, st in enumerate(meta.get("subtasks", [])):
        r = meta["results"][i] if i < len(meta.get("results", [])) else None
        icon = "✅" if r and r["status"] == "completed" else "❌" if r else "⏳"
        print(f"\n{icon} [{st['id']}] {st['title']}")
        if st.get("agent_prompt"):
            print(f"       🤖 Agent Prompt: {st['agent_prompt'][:100]}...")
        if r:
            print(f"       📊 {r['summary']}")

def cmd_config():
    config = load_config()
    print(json.dumps(config, indent=2, ensure_ascii=False))

def cmd_clean():
    import shutil as _shutil
    tasks = sorted(AGENT_GO_DIR.glob("task-*"))
    if not tasks:
        print("暂无任务")
        return
    print(f"将清理 {len(tasks)} 个任务目录:")
    for t in tasks:
        print(f"  {t.name}")
    confirm = safe_input("\n确认删除? [y/N]: ").strip().lower()
    if confirm == "y":
        for t in tasks:
            _shutil.rmtree(t, ignore_errors=True)
        print(f"已清理 {len(tasks)} 个任务")
    else:
        print("已取消")

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "run": cmd_run()
    elif cmd == "list": cmd_list()
    elif cmd == "show": cmd_show()
    elif cmd == "config": cmd_config()
    elif cmd == "clean": cmd_clean()
    else:
        print("""\nagent_go — Plan Mode 增强版（支持 Agent Prompt + 资源清单 + 默认同意）\nUsage:\nagent_go run <repo> '<task>' [--docs <doc1,doc2>] [--yes]\n选项:\n--yes, -y        跳过所有确认，直接执行（Plan → SubTask → Verify 全自动）\n--docs <paths>    挂载参考文档（逗号分隔，支持文件和目录）\n配置:\n~/.agent_go/config.json\nbehavior.auto_confirm_plan: false      # true = Plan 自动确认\nbehavior.auto_confirm_subtasks: false   # true = 子任务自动确认\nbehavior.show_agent_prompt: true        # 展示 Agent Prompt\nbehavior.show_resource_map: true         # 展示共享资源清单\n环境变量:\nAGENT_GO_API_KEY=<key>       # API 密钥\nAGENT_GO_INTERACTIVE=1       # 强制交互模式（覆盖 --yes）\nExamples:\n# 基础使用\nexport AGENT_GO_API_KEY="sk-ant-..."\nagent_go run ~/projects/my-app "将JWT从HS256改为RS256"\n# 带参考文档 + 自动确认\nagent_go run ~/projects/my-app "重构认证模块" \\n--docs "README.md,docs/auth-spec.md" --yes\n# 带参考文档\nagent_go run ~/projects/my-app "重构认证模块" \\n--docs "README.md,docs/auth-spec.md"\n""")

if __name__ == "__main__":
    main()