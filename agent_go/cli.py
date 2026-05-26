import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

from .config import load_config, safe_input, setup_logger, AGENT_GO_DIR
from .api import generate_plan, decompose_fallback
from .ui import confirm_plan, plan_to_md, plan_to_subtasks, confirm_subtasks
from .utils import read_reference_docs, _detect_tool_versions
from .pipeline import _run_pipeline
from .skills import load_skill, load_skills, discover_skills, render_skill_for_plan, render_skill_for_execution, list_skills
from .agents import load_agent_type, list_agent_types
from .eval import cmd_eval
from .tui import cmd_status_tui

def cmd_run():
    # 解析参数
    repo_idx = 2
    task_idx = 3
    doc_paths = []
    issue_ref = ""
    skill_names = []
    agent_type_name = ""
    remote_url = ""
    no_cache = "--no-cache" in sys.argv
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

    # 并发模式要求 headless（避免同时打开多个交互式 Claude Code 终端）
    if parallel > 1 and not headless:
        print("⚠️  并发模式 (--parallel) 需要无头模式 (--headless / --yes)，已自动切换到串行执行。")
        parallel = 1

    if auto_yes:
        sys.argv = [a for a in sys.argv if a not in ("--yes", "-y")]
    if "--headless" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--headless"]

    if "--remote" in sys.argv:
        try:
            ri = sys.argv.index("--remote")
            remote_url = sys.argv[ri + 1]
            sys.argv.pop(ri + 1)
            sys.argv.pop(ri)
        except (IndexError, ValueError):
            pass

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

    if "--skill" in sys.argv:
        try:
            sk_idx = sys.argv.index("--skill")
            skill_names = [s.strip() for s in sys.argv[sk_idx + 1].split(",") if s.strip()]
            sys.argv.pop(sk_idx + 1)
            sys.argv.pop(sk_idx)
        except (IndexError, ValueError):
            pass

    if "--agent-type" in sys.argv:
        try:
            ag_idx = sys.argv.index("--agent-type")
            agent_type_name = sys.argv[ag_idx + 1].strip()
            sys.argv.pop(ag_idx + 1)
            sys.argv.pop(ag_idx)
        except (IndexError, ValueError):
            pass

    if len(sys.argv) < 3:
        print("Usage: agent_go run <repo-path> '<task>' [--docs <doc1,doc2>] [--skill <name>] [--agent-type <type>] [--yes] [--headless] [--issue <N>] [--parallel N] [--remote <url>]")
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
        config["behavior"]["auto_verify_subtask"] = True

    # 生成唯一任务 ID：时间戳(毫秒精度) + 随机后缀，防止碰撞
    for _ in range(5):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{datetime.now().microsecond // 1000:03d}"
        suffix = os.urandom(2).hex()
        task_id = f"task-{ts}-{suffix}"
        task_dir = AGENT_GO_DIR / task_id
        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            time.sleep(0.01)
    else:
        task_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(task_id, task_dir)
    logger.info("=" * 60)
    logger.info("任务启动")
    logger.info(f"ID: {task_id}, 任务: {task}, 项目: {repo}")
    if doc_paths:
        logger.info(f"参考文档: {doc_paths}")

    tool_versions = _detect_tool_versions(logger)
    if tool_versions:
        logger.info(f"工具版本: {tool_versions}")

    # ── Skill 加载 ──
    skills = []
    if skill_names:
        skills = load_skills(skill_names, repo)
        if skills:
            logger.info(f"已加载 Skill: {[s.name for s in skills]}")
        else:
            print(f"⚠️  未找到 Skill: {skill_names}")
    elif config.get("skills", {}).get("auto_discover", False):
        max_auto = config.get("skills", {}).get("max_auto_skills", 3)
        skills = discover_skills(task, repo, max_auto)
        if skills:
            logger.info(f"自动匹配 Skill: {[s.name for s in skills]}")

    # ── Agent 类型加载 ──
    agent_type = None
    agent_type_name = agent_type_name or config.get("agents", {}).get("default", "developer")
    agent_type = load_agent_type(agent_type_name, repo)
    if agent_type:
        logger.info(f"Agent 类型: {agent_type.type_name}")

    # 将 Skill 注入 Plan prompt（如果有）
    skill_plan_context = ""
    if skills:
        skill_plan_context = "\n\n".join(render_skill_for_plan(s) for s in skills)

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
            plan = generate_plan(task, repo, config, logger, "", initial_docs, iteration, skill_plan_context, no_cache=no_cache)
            plan["_original_task"] = task
            break
        except Exception as e:
            last_error = e
            logger.error(f"Plan 失败 (尝试 {attempt+1}): {e}")

    if plan is not None:
        # API 成功 → Plan 确认流程
        confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration=1, task=task)
        # 检查降级信号
        if confirmed_plan == "__FALLBACK__":
            print(f"\n⚠️ 降级到本地规则拆解...")
            subtasks = decompose_fallback(task, repo, config, logger)
            doc_paths = []
            confirmed_plan = None  # 跳过下方 subtasks 赋值
        else:
            while confirmed_plan is None and iteration < max_iter:
                iteration += 1
                try:
                    plan = generate_plan(task, repo, config, logger, "", "", iteration, skill_plan_context, no_cache=no_cache)
                except Exception as e:
                    logger.error(f"重试生成 Plan 失败: {e}")
                    print(f"\n⚠️ 重试失败: {e}")
                    print("\n⚠️ 降级到本地规则拆解...")
                    subtasks = decompose_fallback(task, repo, config, logger)
                    doc_paths = []
                    confirmed_plan = None
                    break
                plan["_original_task"] = task
                confirmed_plan, final_doc_paths = confirm_plan(plan, config, repo, logger, iteration, task=task)
                if confirmed_plan == "__FALLBACK__":
                    print(f"\n⚠️ 降级到本地规则拆解...")
                    subtasks = decompose_fallback(task, repo, config, logger)
                    doc_paths = []
                    confirmed_plan = None
                    break

        if confirmed_plan is None and 'subtasks' not in locals():
            print(f"⚠️ 达到最大迭代次数 {max_iter}，使用最后版本")
            confirmed_plan = plan

        subtasks = plan_to_subtasks(confirmed_plan, logger, repo=repo)
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
        "subtasks": confirmed, "results": [],
        "tool_versions": tool_versions,
        "skills": [s.name for s in skills],
        "agent_type": agent_type.type_name if agent_type else "developer",
        "remote_url": remote_url,
    }
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta, remote_url=remote_url)

def cmd_resume():
    """恢复被中断的任务。"""
    if len(sys.argv) < 3:
        print("Usage: agent_go resume <task-id> [--yes] [--headless] [--parallel N] [--remote <url>]")
        sys.exit(1)
    task_id = sys.argv[2]
    task_dir = AGENT_GO_DIR / task_id
    if not task_dir.exists():
        print(f"任务不存在: {task_id}")
        sys.exit(1)
    meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("status") not in ("running", "paused"):
        print(f"任务状态为 {meta['status']}，无法恢复。仅 running/paused 状态可恢复")
        sys.exit(1)

    confirmed = meta.get("subtasks", [])
    results = meta.get("results", [])
    # 如果 meta.json 中 results 为空，尝试从独立 result.json 文件恢复
    if not results:
        for st in confirmed:
            result_file = task_dir / st["id"] / "result.json"
            if result_file.exists():
                try:
                    r = json.loads(result_file.read_text(encoding="utf-8"))
                    results.append(r)
                except (json.JSONDecodeError, OSError):
                    pass
    worktree_map = {}
    results_map = {}
    completed_ids = set()
    for r in results:
        wid = r["subtask_id"]
        wt = task_dir / wid / "work"
        if wt.exists() and (wt / ".git").exists():
            worktree_map[wid] = wt
        results_map[wid] = r
        if r.get("status") in ("completed", "no_changes", "degraded"):
            completed_ids.add(wid)

    repo = Path(meta["repo"])
    logger = setup_logger(task_id, task_dir)
    config = load_config()

    auto_yes = "--yes" in sys.argv or "-y" in sys.argv
    headless = auto_yes or "--headless" in sys.argv
    parallel = 1
    remote_url = ""
    if "--parallel" in sys.argv:
        try:
            pi = sys.argv.index("--parallel")
            parallel = max(1, int(sys.argv[pi + 1]))
        except (IndexError, ValueError):
            parallel = 3
    if "--remote" in sys.argv:
        try:
            ri = sys.argv.index("--remote")
            remote_url = sys.argv[ri + 1]
        except (IndexError, ValueError):
            pass
    issue_ref = meta.get("issue", "")

    if auto_yes:
        config["behavior"]["auto_confirm_plan"] = True
        config["behavior"]["auto_confirm_subtasks"] = True
        config["behavior"]["auto_verify_subtask"] = True

    # 恢复时优先使用命令行 --remote，其次 meta.json 中记录的
    remote_url = remote_url or meta.get("remote_url", "")
    meta["remote_url"] = remote_url

    logger.info(f"═══ 恢复任务 {task_id} ═══")
    logger.info(f"已完成: {len(completed_ids)}/{len(confirmed)}, 剩余: {len(confirmed) - len(completed_ids)}")
    if remote_url:
        logger.info(f"远程推送: {remote_url}")
    meta["status"] = "running"
    (task_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    _run_pipeline(confirmed, repo, task_dir, logger, config, headless, parallel, issue_ref, meta,
                  worktree_map, results_map, completed_ids, remote_url=remote_url)

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
        # Agent 角色和 Skill 可观测性
        agent_type = st.get("agent_type", "developer")
        source = r.get("agent_type_source", "default") if r else st.get("_agent_type_source", "default")
        source_label = {"llm": "LLM", "rule": "规则", "default": "默认", "inferred": "推断"}.get(source, source)
        print(f"       👤 Agent: {agent_type} (来源: {source_label})")
        skills = st.get("skills", [])
        if skills:
            print(f"       🧠 Skill: {', '.join(skills)}")
        unresolved = r.get("skills_unresolved", []) if r else []
        if unresolved:
            print(f"       ⚠️  Skill 未找到: {', '.join(unresolved)}")
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
    """实时监控所有任务状态。默认 TUI 模式。--no-tui 回退文本模式。"""
    if "--no-tui" in sys.argv:
        _cmd_status_text()
    else:
        cmd_status_tui()


def _cmd_status_text():
    """文本模式（原有实现）。--watch 持续刷新，--verbose 显示 Claude 事件。"""
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
        completed = sum(1 for r in results if r.get("status") in ("completed", "no_changes", "degraded"))
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
        repos_to_prune = set()
        for t in tasks:
            meta_path = t / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    repo_str = meta.get("repo", "")
                    if repo_str and Path(repo_str).exists():
                        repos_to_prune.add(repo_str)
                except (json.JSONDecodeError, OSError):
                    pass
        for t in tasks:
            # 读取 task_id 用于清理 tag
            meta_path = t / "meta.json"
            task_id = t.name
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    task_id = meta.get("task_id", t.name)
                except (json.JSONDecodeError, OSError):
                    pass
            _shutil.rmtree(t, ignore_errors=True)
        for repo_path in repos_to_prune:
            subprocess.run(["git", "worktree", "prune"], cwd=repo_path, capture_output=True)
            # 清理对应 task 的 tags
            tag_list = subprocess.run(["git", "tag", "-l", f"{task_id}/*"], cwd=repo_path, capture_output=True, text=True)
            for tag in tag_list.stdout.strip().split("\n"):
                if tag:
                    subprocess.run(["git", "tag", "-d", tag], cwd=repo_path, capture_output=True)
        print(f"已清理 {len(tasks)} 个任务")
    else:
        print("已取消")

def cmd_skills():
    """列出所有可用的 Skill。"""
    skills = list_skills()
    if not skills:
        print("\n暂无可用 Skill。在 ~/.agent_go/skills/<name>/SKILL.md 创建。")
        print("示例 Skill 格式: YAML frontmatter + Markdown body")
        return
    print(f"\n📚 可用 Skill ({len(skills)} 个)")
    print("─" * 55)
    for s in skills:
        desc = s["description"][:45] + "..." if len(s["description"]) > 45 else s["description"]
        print(f"  {s['name']:<30} {desc}")
    print("─" * 55)

def cmd_cache():
    """Plan 缓存管理。"""
    from .api import list_cache_entries, clean_expired_cache

    if len(sys.argv) < 3:
        print("Usage: agent_go cache <list|clean|clear|stats>")
        return

    sub = sys.argv[2]
    config = load_config()

    if sub == "list":
        entries = list_cache_entries()
        if not entries:
            print("暂无缓存")
            return
        print(f"{'缓存键':<14} {'任务':<30} {'创建':<18} {'命中':<6}")
        print("─" * 70)
        for e in entries:
            m = e.get("meta", {})
            key = e.get("cache_key", "")[:12]
            task = m.get("task", "?")[:30]
            created = m.get("created_at", "?")[:16]
            hits = m.get("hit_count", 0)
            print(f"{key:<14} {task:<30} {created:<18} {hits:<6}")
    elif sub == "clean":
        removed = clean_expired_cache(config)
        print(f"清理 {removed} 条过期缓存")
    elif sub == "clear":
        import shutil
        from .api import _cache_dir
        d = _cache_dir()
        if d.exists():
            shutil.rmtree(d)
            d.mkdir(parents=True, exist_ok=True)
        print("已清除所有缓存")
    elif sub == "stats":
        entries = list_cache_entries()
        print(f"缓存条目: {len(entries)}")
        if entries:
            total_hits = sum(e.get("meta", {}).get("hit_count", 0) for e in entries)
            print(f"总命中: {total_hits}")
            print(f"磁盘: {_cache_size()}")
    else:
        print(f"未知子命令: {sub}。可用: list, clean, clear, stats")


def _cache_size():
    from .api import _cache_dir
    d = _cache_dir()
    total = 0
    for f in d.rglob("*.json"):
        total += f.stat().st_size
    if total < 1024:
        return f"{total}B"
    elif total < 1024 * 1024:
        return f"{total / 1024:.1f}KB"
    return f"{total / 1024 / 1024:.1f}MB"


def cmd_agents():
    """列出所有可用的 Agent 类型。"""
    agents = list_agent_types()
    print(f"\n🤖 Agent 类型 ({len(agents)} 种)")
    print("─" * 55)
    for a in agents:
        src = "内置" if a.get("source") == "builtin" else "用户"
        desc = a["description"][:40] + "..." if len(a["description"]) > 40 else a["description"]
        print(f"  {a['type']:<25} [{src}] {desc}")
    print("─" * 55)

def main():
    try:
        cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
        if cmd == "run": cmd_run()
        elif cmd == "resume": cmd_resume()
        elif cmd == "list": cmd_list()
        elif cmd == "show": cmd_show()
        elif cmd == "status": cmd_status()
        elif cmd == "config": cmd_config()
        elif cmd == "clean": cmd_clean()
        elif cmd == "pr": cmd_pr()
        elif cmd == "skills": cmd_skills()
        elif cmd == "agents": cmd_agents()
        elif cmd == "cache": cmd_cache()
        elif cmd == "eval": cmd_eval()
        else:
            print("""\nagent_go — Plan Mode 增强版（支持 Agent Prompt + 资源清单 + 默认同意）\nUsage:\nagent_go run <repo> '<task>' [--docs <paths>] [--skill <name>] [--agent-type <type>] [--yes] [--headless] [--issue <N>] [--parallel N]\nagent_go pr <task-id> [--offline]\n选项:\n--yes, -y        跳过所有确认，直接执行（等同 --headless + 自动确认）
--headless       子任务使用 claude -p 无头执行（Plan 仍可交互编辑）\n--issue <N>      关联 GitHub Issue 编号（注入 commit + TASK.md）\n--parallel N     最大并发子任务数（默认 1=串行，3=推荐）\n--docs <paths>   挂载参考文档（逗号分隔，支持文件和目录）\n命令:\nagent_go list                  查看所有任务摘要\nagent_go show <task-id>        查看任务详情\nagent_go pr <task-id>          生成 PR 描述并创建 PR（需 gh CLI）\nagent_go pr <task-id> --offline 仅生成 PR.md 文件\nagent_go config                查看当前配置\nagent_go clean                 清理所有任务\nagent_go skills                列出所有 Skill\nagent_go agents                列出所有 Agent 类型\nagent_go eval quality [task-id|--all]    执行质量报告\nagent_go eval perf [task-id|--all]       性能分析报告\n配置:\n~/.agent_go/config.json\nbehavior.auto_confirm_plan: false\nbehavior.auto_confirm_subtasks: false\n环境变量:\nAGENT_GO_API_KEY=<key>       API 密钥\nAGENT_GO_INTERACTIVE=1       强制交互模式（覆盖 --yes）\nExamples:\nexport AGENT_GO_API_KEY="sk-ant-..."\nagent_go run ~/my-app "重构认证" --issue 42 --yes\nagent_go run ~/my-app "升级依赖" --docs "CHANGELOG.md" -y\nagent_go pr task-20260515-130936 --offline\n""")
    except KeyboardInterrupt:
        print("\n\n⏹️  用户中断（Ctrl+C）")
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)

