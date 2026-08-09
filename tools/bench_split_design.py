#!/usr/bin/env python3
"""Split Design Benchmark runner.

对 eval_suite/split_design 测试集中的任务，以统一 prompt 分别调用
Claude Code (`claude -p`) 与 OpenCode (`opencode run`)，要求返回结构化
子任务拆分设计 JSON，供 agent_go 拆分算法参考。

用法:
  python3 tools/bench_split_design.py --agent claude|opencode [--task <id>] [--dry-run]
  python3 tools/bench_split_design.py --agent all --task add-format-helper

输出:
  eval_suite/split_design/results/{task_id}/{agent}.json   # 原始 prompt 输出
  eval_suite/split_design/results/{task_id}/{agent}.parsed.json  # 解析后的 subtasks

说明:
  - 只调用「拆分设计」prompt，不执行任何代码修改（低成本）。
  - 需本地安装 claude（Claude Code CLI）与 opencode。
  - 两侧默认对齐使用 deepseek-v4-flash 模型（agent_go 同款），便于同模型对比；
    可分别用 CLAUDE_MODEL / OPENCODE_MODEL 环境变量覆盖。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPLIT_DIR = REPO_ROOT / "eval_suite" / "split_design"
RESULTS_DIR = SPLIT_DIR / "results"

TASKS = [
    {
        "id": "add-format-helper",
        "difficulty": "easy",
        "repo": "eval_suite/fixtures/task-mgr",
        "yaml": "golden_tasks/tasks/01-add-format-helper.yaml",
        "expect": "no_split",
    },
    {
        "id": "fix-missing-default",
        "difficulty": "easy",
        "repo": "eval_suite/fixtures/task-mgr",
        "yaml": "golden_tasks/tasks/02-fix-missing-default.yaml",
        "expect": "no_split",
    },
    {
        "id": "add-simple-caching",
        "difficulty": "medium",
        "repo": "eval_suite/fixtures/task-mgr",
        "yaml": "golden_tasks/tasks/08-add-simple-caching.yaml",
        "expect": "1-2",
    },
    {
        "id": "implement-done-command",
        "difficulty": "medium",
        "repo": "eval_suite/fixtures/task-mgr",
        "yaml": "golden_tasks/tasks/06-implement-done-command.yaml",
        "expect": "1-2",
    },
    {
        "id": "security-hardening-taskmgr",
        "difficulty": "hard",
        "repo": "eval_suite/fixtures/task-mgr",
        "yaml": "golden_tasks/tasks/13-security-hardening-taskmgr.yaml",
        "expect": "must_split",
    },
    {
        "id": "conditional-branching-datapipeline",
        "difficulty": "hard",
        "repo": "eval_suite/fixtures/data-pipeline",
        "yaml": "golden_tasks/tasks/17-conditional-branching-datapipeline.yaml",
        "expect": "must_split",
    },
]

PROMPT_TEMPLATE = """你是资深软件架构师。请针对下面的开发任务，设计一个子任务拆分方案。

背景：这些子任务将分别派发给独立的 Agent 实例执行，每个 Agent 有独立的
工作目录（git worktree），子任务之间通过 git merge 传递产物。
因此拆分设计必须遵守以下原则：

【拆分原则】
1. 子任务数量尽量少：改动面 <=2 个文件时优先 1 个任务；只有改动真正跨模块
   （不同文件集、可独立验证）时才拆分。
2. 文件互斥：不同子任务不要修改同一文件（避免合并冲突和交叉污染）。
   如果确实需要修改同一文件，必须通过依赖关系（depends_on）串行执行。
3. 每个子任务给出：files（它负责的文件，路径相对于仓库根）、
   depends_on（依赖的子任务 id）、difficulty（easy/medium/hard）、
   verification（验证命令）、rationale（为什么这样拆/为什么这里合并）。
4. 拆分数量范围：小型改动 1 个；中型改动 1-2 个；大型跨模块改动 2-4 个。
   禁止为小改动制造不必要的拆分。

【任务信息】
- 仓库: {repo}
- 难度: {difficulty}
- 任务描述:
{task}

请只输出一个 JSON 对象（不要输出任何其他文字或代码块标记），格式如下：
{{
  "subtasks": [
    {{
      "id": "sub-1",
      "title": "...",
      "files": ["file1.py", "file2.py"],
      "depends_on": [],
      "difficulty": "easy",
      "verification": "验证命令（如 pytest ...）",
      "rationale": "为什么拆/合并这一步"
    }}
  ],
  "overall_rationale": "整体拆分策略说明，说明为什么拆 N 个而不是更多/更少"
}}"""


def _load_yaml_task(task: dict) -> str:
    """从 golden task yaml 提取 task 正文（简易解析，不依赖 yaml 库）。"""
    yaml_path = REPO_ROOT / "eval_suite" / task["yaml"]
    lines = yaml_path.read_text(encoding="utf-8").splitlines()
    in_task = False
    body: list[str] = []
    for line in lines:
        if line.startswith("task: |"):
            in_task = True
            continue
        if in_task:
            if line and not line[0].isspace():
                break
            body.append(line)
    text = "\n".join(body).strip()
    if not text:
        raise RuntimeError(f"无法从 {task['yaml']} 提取 task 正文")
    return text


def build_prompt(task: dict) -> str:
    task_text = _load_yaml_task(task)
    repo_abs = REPO_ROOT / task["repo"]
    return PROMPT_TEMPLATE.format(
        repo=str(repo_abs),
        difficulty=task["difficulty"],
        task=task_text,
    )


def _run_cmd(cmd: list[str], cwd: Path, timeout: int = 300) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"__TIMEOUT__ (>{timeout}s)"
    if proc.returncode != 0:
        return f"__EXIT_{proc.returncode}__\nSTDOUT:\n{proc.stdout[:4000]}\nSTDERR:\n{proc.stderr[:2000]}"
    return proc.stdout


def call_claude(prompt: str, cwd: Path) -> str:
    # 默认与 agent_go 对齐使用 deepseek-v4-flash（经 ANTHROPIC_BASE_URL/API 路由），
    # 便于跨 Agent 同模型对比。可用 CLAUDE_MODEL 环境变量覆盖。
    model = __import__("os").environ.get("CLAUDE_MODEL", "deepseek-v4-flash")
    return _run_cmd(
        ["claude", "-p", "--output-format", "text", "--model", model, prompt],
        cwd, timeout=420)


def call_opencode(prompt: str, cwd: Path) -> str:
    cmd = ["opencode", "run", "--pure"]
    # 默认与 agent_go 对齐使用 deepseek-v4-flash，便于跨 Agent 同模型对比
    model = __import__("os").environ.get("OPENCODE_MODEL", "opencode/deepseek-v4-flash")
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return _run_cmd(cmd, cwd, timeout=420)


def parse_split_json(raw: str) -> dict:
    """从 Agent 输出中提取 JSON（容忍代码块/前后缀噪音）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"输出中未找到 JSON 对象:\n{raw[:500]}")
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        raise ValueError(f"JSON 解析失败:\n{candidate[:800]}")


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    agent = "all"
    task_filter = None
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--agent":
            i += 1
            agent = args[i]
        elif args[i] == "--task":
            i += 1
            task_filter = args[i]
        elif args[i] == "--dry-run":
            dry_run = True
        i += 1

    agents = {"claude": call_claude, "opencode": call_opencode}
    if agent == "all":
        agent_calls = agents
    elif agent in agents:
        agent_calls = {agent: agents[agent]}
    else:
        print(f"未知 --agent {agent!r}，可选: all/claude/opencode")
        return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = [t for t in TASKS if task_filter is None or t["id"] == task_filter]
    if not tasks:
        print(f"未找到任务 {task_filter!r}")
        return 2

    summary: list[dict] = []
    for task in tasks:
        prompt = build_prompt(task)
        repo_abs = REPO_ROOT / task["repo"]
        print(f"\n===== {task['id']} (expect={task['expect']}) =====")
        for name, caller in agent_calls.items():
            out_dir = RESULTS_DIR / task["id"]
            out_dir.mkdir(parents=True, exist_ok=True)
            if dry_run:
                print(f"[dry-run] {name}: 跳过实际调用（prompt {len(prompt)} 字符）")
                continue
            print(f"  ▶ {name}: 调用中...")
            raw = caller(prompt, repo_abs)
            raw_path = out_dir / f"{name}.json"
            raw_path.write_text(raw, encoding="utf-8")
            try:
                parsed = parse_split_json(raw)
                (out_dir / f"{name}.parsed.json").write_text(
                    json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
                n = len(parsed.get("subtasks", []))
                files = sorted({f for st in parsed.get("subtasks", []) for f in st.get("files", [])})
                print(f"    ✓ 拆分 {n} 个子任务 | 文件: {', '.join(files[:6]) or '(未声明)'}")
                summary.append({
                    "task": task["id"], "agent": name, "ok": True,
                    "subtask_count": n, "files": files,
                    "expect": task["expect"],
                })
            except ValueError as e:
                print(f"    ✗ 解析失败: {e}")
                summary.append({
                    "task": task["id"], "agent": name, "ok": False,
                    "subtask_count": None, "files": [], "expect": task["expect"],
                })

    if summary:
        (RESULTS_DIR / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
