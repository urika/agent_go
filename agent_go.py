#!/usr/bin/env python3
"""
agent_go.py — Plan Mode 增强版

核心增强：
  1. Plan 输出包含给执行 Agent 的完整 Prompt 模板
  2. 共享资源清单（目录结构、git 地址、依赖文件等）
  3. 用户确认界面展示这些资源，确认后注入子任务
  4. 支持"默认同意"模式（通过配置或环境变量）
"""

import sys, os, subprocess, json, shutil, re, logging, time, threading, shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
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
        path = (repo / path_str).resolve()
        # 防止路径穿越：确保路径在 repo 范围内
        if not str(path).startswith(str(repo.resolve())):
            logger.warning(f"路径越界，已拒绝: {path_str}")
            continue
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
    except (FileNotFoundError, subprocess.SubprocessError):
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
    except (FileNotFoundError, subprocess.SubprocessError):
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

def plan_to_md(plan):
    """将 Plan 转为 Markdown 文档。"""
    lines = [
        f"# 执行方案\n",
        f"## 概述\n{plan.get('overview', 'N/A')}\n",
        f"## 预估工作量\n{plan.get('estimated_effort', 'N/A')}\n",
        f"## 共享资源清单\n",
    ]
    sr = plan.get("shared_resources", {})
    if sr.get("git_remote"): lines.append(f"- Git 远程: {sr['git_remote']}")
    if sr.get("git_branch"): lines.append(f"- 当前分支: {sr['git_branch']}")
    if sr.get("directories"): lines.append(f"- 关键目录: {', '.join(sr['directories'])}")
    if sr.get("config_files"): lines.append(f"- 配置文件: {', '.join(sr['config_files'])}")
    if sr.get("env_vars"): lines.append(f"- 环境变量: {', '.join(sr['env_vars'])}")
    lines.append(f"\n## 执行步骤 ({len(plan.get('steps', []))} 步)\n")
    for step in plan.get("steps", []):
        lines.append(f"### [{step['id']}] {step['title']}\n")
        lines.append(f"{step.get('description', '')}\n")
        if step.get("files"):
            lines.append(f"- 文件: {', '.join(step['files'])}")
        if step.get("verification"):
            lines.append(f"- 验证: `{step['verification']}`")
        if step.get("risks"):
            lines.append(f"- 风险: {'; '.join(step['risks'])}")
        lines.append("")
    deps = plan.get("dependencies", {})
    if deps:
        lines.append("## 依赖关系\n")
        for sid, prereqs in deps.items():
            lines.append(f"- 步骤 {sid} 依赖: {prereqs}")
    return "\n".join(lines)

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

    empty_count = 0
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
                original = plan.get("_original_task", task)
                plan = generate_plan(original, repo, config, logger, supplement, existing_docs, iteration)
                plan["_original_task"] = original
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
                original = plan.get("_original_task", task)
                plan = generate_plan(original, repo, config, logger, "", docs_content, iteration)
                plan["_original_task"] = original
                print(f"\n🔄 已重新生成（第 {iteration} 版）")
            except Exception as e:
                logger.error(f"重新生成失败: {e}")
                print(f"⚠️ 失败: {e}")
        else:
            if choice == "":
                empty_count += 1
                if empty_count > 5:
                    print("⚠️ 检测到非交互模式，请输入有效选项或使用 --yes 标志")
                    sys.exit(1)
            else:
                empty_count = 0
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

    empty_count = 0
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
            if choice == "":
                empty_count += 1
                if empty_count > 5:
                    print("⚠️ 检测到非交互模式，请输入有效选项或使用 --yes 标志")
                    sys.exit(1)
            else:
                empty_count = 0
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

def _slugify(text, max_len=30):
    """将任务标题转为分支名适用的短标识。"""
    slug = re.sub(r'[^a-zA-Z0-9一-鿿]+', '-', text).strip('-')
    return slug[:max_len] if len(slug) > max_len else slug

def _format_commit(title, issue_ref="", sub_id=""):
    """生成 Conventional Commits 格式的提交消息。"""
    prefix = "feat" if "实现" in title or "新增" in title else "chore"
    msg = f"{prefix}: {title}"
    if issue_ref:
        msg += f"\n\nRefs: #{issue_ref}"
    msg += f"\n\nagent_go: {sub_id}"
    return msg

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
    """无头模式：claude -p 带 stream-json 实时监控、交互检测和超时重试。"""
    PFX = f"[{sub_id}]"

    INTERACTION_PATTERNS = [
        r"waiting for input", r"approve\s+(Write|Edit|Bash|Read)",
        r"permission required", r"\[y/n\]", r"press.*to continue",
    ]
    IDLE_TIMEOUT = 600   # 10 分钟纯静默才 kill（思考阶段无任何事件）
    HEARTBEAT = 60       # 60s 无事件发心跳

    def _run_one(prompt, attempt):
        """启动 claude -p (stream-json) 并实时解析事件。"""
        proc = subprocess.Popen([
            "claude", "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ], env=env, cwd=str(worktree), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        last_ts = [time.time()]
        lines = []
        waiting = [False]
        current_tool = [None]
        tool_input = [""]

        def parse_and_log(raw_line, label):
            s = raw_line.rstrip()
            if not s:
                return
            ts = datetime.now().strftime("%H:%M:%S")
            last_ts[0] = time.time()

            # 交互检测（stderr 文本行）
            if label == "err":
                lines.append(f"[{ts}] {s[:200]}")
                logger.info(f"{PFX} [claude err] {s[:200]}")
                for pat in INTERACTION_PATTERNS:
                    if re.search(pat, s, re.IGNORECASE):
                        waiting[0] = True
                        logger.error(f"⚠️ 交互: (attempt={attempt}): {s[:200]}")
                return

            # 尝试解析 stream-json 事件
            try:
                event = json.loads(s)
            except json.JSONDecodeError:
                # 非 JSON 输出（如纯文本），直接记录
                lines.append(f"[{ts}] {s[:200]}")
                logger.info(f"{PFX} [claude] {s[:200]}")
                return

            ev_type = event.get("type", "")

            # stream_event: 流式内容增量
            if ev_type == "stream_event":
                inner = event.get("event", {})
                it = inner.get("type", "")

                if it == "content_block_start":
                    cb = inner.get("content_block", {})
                    tool_name = cb.get("name", "")
                    if tool_name:
                        current_tool[0] = tool_name
                        tool_input[0] = ""
                        logger.info(f"{PFX} [{tool_name}] 开始...")

                elif it == "content_block_delta":
                    delta = inner.get("delta", {})
                    dt = delta.get("type", "")
                    if dt == "text_delta":
                        text = delta.get("text", "")
                        # 只记录非纯空白的文本
                        if text.strip():
                            lines.append(f"[{ts}] {text[:200]}")
                            logger.info(f"{PFX} [text] {text[:200]}")
                    elif dt == "input_json_delta":
                        tool_input[0] += delta.get("partial_json", "")

                elif it == "content_block_stop":
                    if current_tool[0]:
                        ti = tool_input[0]
                        preview = ti[:200] if len(ti) > 200 else ti
                        logger.info(f"{PFX} [{current_tool[0]}] 完成 {preview}")
                        current_tool[0] = None

            # assistant: 消息批次
            elif ev_type == "assistant":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            t = block.get("text", "")
                            if t.strip():
                                lines.append(f"[{ts}] {t[:200]}")
                                logger.info(f"{PFX} [assistant] {t[:200]}")
                        elif block.get("type") == "tool_use":
                            logger.info(f"{PFX} [tool_use] {block.get('name', '?')}")

            # result: 最终结果
            elif ev_type == "result":
                subtype = event.get("subtype", "")
                logger.info(f"{PFX} [result] {subtype}")

            # user: 工具结果
            elif ev_type == "user":
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out = block.get("content", "")
                        if isinstance(out, str) and out.strip():
                            preview = out[:200] if len(out) > 200 else out
                            logger.info(f"{PFX} [tool_result] {preview}")

            else:
                # 其他事件类型，轻量记录
                pass

        def read_stdout():
            for line in iter(proc.stdout.readline, ''):
                parse_and_log(line, "out")

        def read_stderr():
            for line in iter(proc.stderr.readline, ''):
                parse_and_log(line, "err")

        t_out = threading.Thread(target=read_stdout, daemon=True)
        t_err = threading.Thread(target=read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        idle_logged_at = 0
        while proc.poll() is None:
            idle = time.time() - last_ts[0]
            if idle > IDLE_TIMEOUT:
                logger.error(f"claude {idle:.0f}s 无事件 (attempt={attempt})，强制终止")
                proc.kill()
                break
            if idle > HEARTBEAT and idle - idle_logged_at > HEARTBEAT:
                logger.info(f"{PFX} 等待中... (无事件 {idle:.0f}s, attempt={attempt})")
                idle_logged_at = idle
            time.sleep(2)

        t_out.join()
        t_err.join()
        proc.wait()
        return proc, lines, waiting[0]

    RETRY_SUFFIX = (
"\n\n【系统指令】你必须立即完成上述所有任务，直接执行文件创建和修改操作。"
"不要询问任何问题，不要等待确认，不要输出中间讨论。"
"完成后输出简洁的状态报告和变更摘要。"
    )
    MAX_ATTEMPTS = 2

    logger.info(f"{PFX} 无头模式: claude -p")
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

def run_subtask(task_id, subtask, repo, task_dir, logger, upstream_worktrees=None, headless=False, issue_ref=""):
    sub_id = subtask["id"]
    sub_dir = task_dir / sub_id
    worktree = sub_dir / "work"
    worktree.mkdir(parents=True)

    logger.info(f"─── {sub_id} START: {subtask['title']} ───")
    log_event(logger, "subtask_start", {"id": sub_id, "title": subtask["title"],
                "depends_on": subtask.get("depends_on", []), "headless": headless, "issue": issue_ref})

    clone_start = time.time()
    if (repo / ".git").exists():
        # 分支命名: feature/{issue}-{slug} 或 agent_go/{task_id}
        branch = f"feature/{issue_ref}-{_slugify(subtask['title'])}" if issue_ref else f"agent_go/{task_id}/{sub_id}"
        subprocess.run(["git", "clone", str(repo), str(worktree)], capture_output=True, check=True)
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(worktree), capture_output=True)
        logger.info(f"分支: {branch}")
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

    # Git 提交 + tag（Conventional Commits 格式），供下游子任务 merge
    has_changes = summary != "无文件变更"
    if has_changes:
        commit_msg = _format_commit(subtask['title'], issue_ref, sub_id)
        subprocess.run(["git", "add", "-A"], cwd=str(worktree), capture_output=True)
        subprocess.run(["git", "commit", "-m", commit_msg],
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
        try:
            vr = subprocess.run(shlex.split(verification), cwd=str(worktree),
                                capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, OSError):
            # shlex 解析失败时（如中文描述），尝试 shell=True 或跳过
            logger.info(f"验证命令非可执行文件，尝试 shell 模式")
            vr = subprocess.run(verification, shell=True, cwd=str(worktree),
                                capture_output=True, text=True, timeout=120)
        if vr.returncode != 0 and vr.returncode != 127:  # 127 = command not found
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
                try:
                    vr2 = subprocess.run(shlex.split(verification), cwd=str(worktree),
                                         capture_output=True, text=True, timeout=120)
                except (FileNotFoundError, OSError):
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

    # 状态判定: completed(达预期) / degraded(完成但产出不足) / failed(异常)
    if result.returncode == 0 and verify_ok:
        status = "degraded" if summary == "无文件变更" else "completed"
    else:
        status = "failed"
    logger.info(f"─── {sub_id} DONE: {subtask['title']} [{status}] ───")
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
    issue_ref = ""
    auto_yes = "--yes" in sys.argv or "-y" in sys.argv
    headless = auto_yes or "--headless" in sys.argv  # --yes 隐含 headless
    parallel = 1  # 默认串行
    if "--parallel" in sys.argv:
        try:
            pi = sys.argv.index("--parallel")
            parallel = max(1, int(sys.argv[pi + 1]))
            sys.argv.pop(pi + 1)
            sys.argv.pop(pi)
        except (IndexError, ValueError):
            parallel = 3

    if auto_yes:
        sys.argv = [a for a in sys.argv if a not in ("--yes", "-y")]
    if "--headless" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--headless"]

    if "--issue" in sys.argv:
        try:
            iss_idx = sys.argv.index("--issue")
            issue_ref = sys.argv[iss_idx + 1]
            sys.argv.pop(iss_idx + 1)
            sys.argv.pop(iss_idx)
        except (IndexError, ValueError):
            pass

    if "--docs" in sys.argv:
        docs_idx = sys.argv.index("--docs")
        if docs_idx + 1 < len(sys.argv):
            doc_paths = [p.strip() for p in sys.argv[docs_idx + 1].split(",")]
        if docs_idx < repo_idx:
            repo_idx = 2 if docs_idx > 2 else 2

    if len(sys.argv) < 3:
        print("Usage: agent_go run <repo-path> '<task>' [--docs <doc1,doc2>] [--yes] [--headless] [--issue <N>] [--parallel N]")
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
        # 保存 Plan 文档
        (task_dir / "PLAN.md").write_text(plan_to_md(confirmed_plan), encoding="utf-8")
        logger.info("[PLAN] PLAN.md 已保存")
    else:
        # 降级拆解
        print(f"\n⚠️ Plan Mode 失败: {last_error}")
        subtasks = decompose_fallback(task, repo, config, logger)

    # 子任务确认
    confirmed = confirm_subtasks(subtasks, config, logger)

    meta = {
        "task_id": task_id, "task": task, "repo": str(repo),
        "created": ts, "status": "running",
        "reference_docs": doc_paths, "issue": issue_ref,
        "subtasks": confirmed, "results": []
    }
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # 执行（支持并发）
    sub_map = {st["id"]: st for st in confirmed}    # id -> subtask
    worktree_map = {}  # sub_id -> worktree 路径
    results_map = {}   # sub_id -> result
    meta_lock = threading.Lock()
    final_status = "completed"
    degraded_count = 0
    total = len(confirmed)

    if parallel > 1 and total > 1:
        logger.info(f"[并发] max_workers={parallel}, 拓扑调度")

    # 拓扑排序分波次: wave 0 = 无依赖, wave N = 依赖已满足
    remaining = list(confirmed)
    wave_num = 0
    completed_ids = set()

    while remaining:
        wave = [st for st in remaining
                if all(dep in completed_ids for dep in st.get("depends_on", []))]
        if not wave:
            logger.error("依赖循环或无法满足的依赖！")
            break

        logger.info(f"[Wave {wave_num}] {', '.join(st['id'] for st in wave)}")
        actual_workers = min(parallel, len(wave)) if parallel > 1 else 1

        if actual_workers == 1:
            # 串行执行（单任务或 parallel=1）
            for st in wave:
                upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                result = run_subtask(task_id, st, repo, task_dir, logger, upstream, headless=headless, issue_ref=issue_ref)
                with meta_lock:
                    worktree_map[st["id"]] = task_dir / st["id"] / "work"
                    results_map[st["id"]] = result
                    if result.get("status") == "degraded":
                        degraded_count += 1
                    meta["results"] = [results_map.get(s["id"]) for s in confirmed if s["id"] in results_map]
                    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                completed_ids.add(st["id"])
        else:
            # 并发执行当前 wave
            with ThreadPoolExecutor(max_workers=actual_workers) as executor:
                futures = {}
                for st in wave:
                    upstream = {dep: worktree_map[dep] for dep in st.get("depends_on", []) if dep in worktree_map}
                    fut = executor.submit(run_subtask, task_id, st, repo, task_dir, logger, upstream, headless, issue_ref)
                    futures[fut] = st

                for fut in as_completed(futures):
                    st = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:
                        result = {"subtask_id": st["id"], "status": "failed",
                                  "exit_code": -1, "summary": str(e), "worktree": "",
                                  "sandbox_type": "headless", "verify_ok": False, "duration_sec": 0}
                        logger.error(f"并发异常 {st['id']}: {e}")
                    with meta_lock:
                        worktree_map[st["id"]] = task_dir / st["id"] / "work"
                        results_map[st["id"]] = result
                        if result.get("status") == "degraded":
                            degraded_count += 1
                        meta["results"] = [results_map.get(s["id"]) for s in confirmed if s["id"] in results_map]
                        (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                    completed_ids.add(st["id"])

        # 移除本 wave 已完成的子任务
        remaining = [st for st in remaining if st["id"] not in completed_ids]
        wave_num += 1

    print(f"\n{'='*60}\n🎉 全部完成 ({total}/{total})\n{'='*60}")

    if final_status == "completed" and degraded_count > 0:
        final_status = "degraded"
    meta["status"] = final_status
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n📦 最终报告")
    print("─" * 60)
    for r in meta["results"]:
        icon = {"completed": "✅", "degraded": "⚠️", "failed": "❌"}.get(r["status"], "❓")
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

def cmd_pr():
    """根据已完成任务的 meta.json + git log 生成 PR 描述。"""
    if len(sys.argv) < 3:
        print("Usage: agent_go pr <task-id> [--offline]")
        sys.exit(1)

    task_id = sys.argv[2]
    offline = "--offline" in sys.argv
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print(f"任务不存在: {task_id}")
        sys.exit(1)

    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))

    # 收集变更信息
    subtask_lines = []
    for r in meta.get("results", []):
        icon = "✅" if r.get("status") == "completed" else "❌"
        subtask_lines.append(f"- {icon} **{r['subtask_id']}**: {r.get('summary', 'N/A')} ({r.get('sandbox_type', '?')}, {r.get('duration_sec', 0):.0f}s)")

    # 读取共享上下文
    ctx_file = task_dir / "SHARED_CONTEXT.md"
    context = ctx_file.read_text(encoding="utf-8") if ctx_file.exists() else ""

    pr_body = f"""## Summary

{meta.get('task', 'N/A')}

## Subtasks

{chr(10).join(subtask_lines)}

## Verification

{context if context else '_No verification details_'}
"""

    if meta.get("issue"):
        pr_body = f"Fixes #{meta['issue']}\n\n{pr_body}"

    if offline:
        out = task_dir / "PR.md"
        out.write_text(pr_body, encoding="utf-8")
        print(f"PR 描述已写入 {out}")
        print(f"请手动创建 PR 或稍后执行: agent_go pr {task_id}")
    else:
        # 在线模式：通过 gh CLI 创建 PR
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as tf:
            tf.write(pr_body)
            pr_file = tf.name
        title = meta.get("task", "agent_go task")[:72]
        base = meta.get("base_branch", "main")
        result = subprocess.run([
            "gh", "pr", "create", "--title", f"{title}",
            "--body-file", pr_file, "--base", base,
        ], capture_output=True, text=True)
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print(f"❌ gh pr create 失败: {result.stderr.strip()}")
            (task_dir / "PR.md").write_text(pr_body, encoding="utf-8")
            print(f"PR 描述已备份到 {task_dir}/PR.md")
        os.unlink(pr_file)

def cmd_status():
    """实时监控所有任务状态。--watch 持续刷新，--verbose 显示 Claude 事件。"""
    watch = "--watch" in sys.argv or "-w" in sys.argv
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    def _get_task_tail_lines(log_path, count=2):
        """从执行日志尾部提取最后 count 条 Claude 事件。"""
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        # 从最后 50 行中筛选 claude 相关行
        tail = lines[-50:]
        claude_lines = [l for l in tail if "[claude" in l or "[text]" in l
                        or "[Read]" in l or "[Write]" in l or "[Bash]" in l
                        or "[tool_result]" in l or "[result]" in l]
        return claude_lines[-count:]

    def _get_task_status(task_dir):
        meta_path = task_dir / "meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = meta.get("status", "unknown")
        zombie = False
        log_path = task_dir / "execution.log"
        ZOMBIE_TIMEOUT = 600  # 10 分钟无日志输出视为僵尸任务

        # 僵尸检测：status=running 但日志已超过 ZOMBIE_TIMEOUT 未更新
        if status == "running" and log_path.exists():
            log_mtime = log_path.stat().st_mtime
            if time.time() - log_mtime > ZOMBIE_TIMEOUT:
                zombie = True
                meta["status"] = "failed"
                meta["_zombie_note"] = f"进程异常退出，日志于 {datetime.fromtimestamp(log_mtime).strftime('%H:%M:%S')} 停止更新"
                meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
                status = "failed"

        results = meta.get("results", [])
        completed = sum(1 for r in results if r.get("status") == "completed")
        total = len(meta.get("subtasks", []))
        current = ""
        if results and status == "running":
            last = results[-1]
            current = f"{last.get('subtask_id', '?')}: {last.get('summary', '?')[:40]}"
        if log_path.exists():
            for line in reversed(log_path.read_text(encoding="utf-8").strip().split("\n")[-10:]):
                if "subtask_start" in line:
                    try:
                        evt = json.loads(line.split(" | ")[-1].strip())
                        current = evt.get("title", current)
                    except Exception:
                        pass
                    break
        progress = f"{completed}/{total}" if total > 0 else "-"
        icon = {"completed": "✅", "degraded": "⚠️", "running": "🔄", "failed": "❌", "aborted": "⏹️"}.get(status, "❓")
        elapsed = ""
        created = meta.get("created", "")
        if created:
            try:
                start = datetime.strptime(created, "%Y%m%d-%H%M%S")
                # 运行中=实时，已完成=冻结在最后日志时间
                if status == "running":
                    end = datetime.now()
                elif log_path.exists():
                    end = datetime.fromtimestamp(log_path.stat().st_mtime)
                else:
                    end = datetime.now()
                delta = end - start
                elapsed = f"{int(delta.total_seconds() // 60)}m{int(delta.total_seconds() % 60)}s"
            except Exception:
                pass
        tail_lines = _get_task_tail_lines(log_path) if verbose and status == "running" else []
        return {
            "id": task_dir.name, "icon": icon, "status": status,
            "progress": progress, "current": current, "elapsed": elapsed,
            "task": meta.get("task", "?")[:50], "issue": meta.get("issue", ""),
            "tail": tail_lines,
        }

    while True:
        tasks_dirs = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
        if not tasks_dirs:
            print("暂无任务")
            return

        rows = [_get_task_status(td) for td in tasks_dirs]
        rows = [r for r in rows if r is not None]

        if watch:
            os.system("clear" if os.name == "posix" else "cls")

        print(f"{'任务ID':<24} {'状态':<6} {'进度':<8} {'耗时':<8} {'Issue':<6} {'当前子任务'}")
        print("─" * 110)
        for r in rows:
            issue_str = f"#{r['issue']}" if r['issue'] else "-"
            print(f"{r['id']:<24} {r['icon']} {r['status']:<4} {r['progress']:<8} "
                  f"{r['elapsed']:<8} {issue_str:<6} {r['current'][:50]}")
            if r["tail"]:
                for tl in r["tail"]:
                    line_text = tl.split(" | ")[-1] if " | " in tl else tl
                    print(f"  └ {line_text.strip()[:90]}")
        print("─" * 110)
        flags = " --watch" if watch else ""
        flags += " --verbose" if verbose else ""
        print(f"共 {len(rows)} 个任务 | agent_go status{flags} | Ctrl+C 退出\n")

        if not watch:
            break
        time.sleep(5)

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
    elif cmd == "status": cmd_status()
    elif cmd == "config": cmd_config()
    elif cmd == "clean": cmd_clean()
    elif cmd == "pr": cmd_pr()
    else:
        print("""\nagent_go — Plan Mode 增强版（支持 Agent Prompt + 资源清单 + 默认同意）\nUsage:\nagent_go run <repo> '<task>' [--docs <paths>] [--yes] [--headless] [--issue <N>] [--parallel N]\nagent_go pr <task-id> [--offline]\n选项:\n--yes, -y        跳过所有确认，直接执行（等同 --headless + 自动确认）
--headless       子任务使用 claude -p 无头执行（Plan 仍可交互编辑）\n--issue <N>      关联 GitHub Issue 编号（注入 commit + TASK.md）\n--parallel N     最大并发子任务数（默认 1=串行，3=推荐）\n--docs <paths>   挂载参考文档（逗号分隔，支持文件和目录）\n命令:\nagent_go list                  查看所有任务摘要\nagent_go show <task-id>        查看任务详情\nagent_go pr <task-id>          生成 PR 描述并创建 PR（需 gh CLI）\nagent_go pr <task-id> --offline 仅生成 PR.md 文件\nagent_go config                查看当前配置\nagent_go clean                 清理所有任务\n配置:\n~/.agent_go/config.json\nbehavior.auto_confirm_plan: false\nbehavior.auto_confirm_subtasks: false\n环境变量:\nAGENT_GO_API_KEY=<key>       API 密钥\nAGENT_GO_INTERACTIVE=1       强制交互模式（覆盖 --yes）\nExamples:\nexport AGENT_GO_API_KEY="sk-ant-..."\nagent_go run ~/my-app "重构认证" --issue 42 --yes\nagent_go run ~/my-app "升级依赖" --docs "CHANGELOG.md" -y\nagent_go pr task-20260515-130936 --offline\n""")

if __name__ == "__main__":
    main()