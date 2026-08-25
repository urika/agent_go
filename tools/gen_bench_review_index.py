#!/usr/bin/env python3
"""生成三臂 bench 人工 review 静态索引页（file:// 直开，无需起服务）。

数据源：
- eval_suite/results_arm_{local35b,qwen38,cloud}_20260823.jsonl（最终固化口径）
- eval_suite/tasks/*.yaml（题目原文）
- ~/.agent_go/task-*/meta.json（delivery_branch / base_commit / worktree）

输出：eval_suite/bench_review_index.html
用法：python3 tools/gen_bench_review_index.py  &&  open eval_suite/bench_review_index.html
"""
import html
import json
from pathlib import Path

import yaml  # dev dep（pyproject 已有）

WS = Path(__file__).resolve().parent.parent
ARMS = [
    ("local35b", "本地 35B（Qwen3.6-35B-A3B）"),
    ("qwen38", "本地 27B（Qwen3.8-27B）"),
    ("cloud", "云端（claude-opus-4-7）"),
]
OUT = WS / "eval_suite" / "bench_review_index.html"


def load_yaml_tasks():
    m = {}
    for f in sorted((WS / "eval_suite" / "tasks").glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8"))
        m[d["id"]] = {"path": f, "task": d.get("task", ""), "difficulty": d.get("difficulty", ""),
                      "repo": d.get("repo", ""), "verification": d.get("verification", [])}
    return m


def fl(p, label):
    """file:// 链接（label 为空用路径末段）。"""
    if not p:
        return ""
    p = Path(p)
    if not p.exists():
        return f"<span class='missing'>{html.escape(label or p.name)}（已清理）</span>"
    return f"<a href='{p.as_uri()}'>{html.escape(label or p.name)}</a>"


def badge(v):
    if v is True:
        return "<span class='ok'>✅</span>"
    if v is False:
        return "<span class='bad'>❌</span>"
    return "<span class='na'>—</span>"


def main():
    yamls = load_yaml_tasks()
    arms_data = {}
    for key, _label in ARMS:
        p = WS / f"eval_suite/results_arm_{key}_20260823.jsonl"
        arms_data[key] = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    # 汇总
    summary_rows = []
    for key, label in ARMS:
        recs = arms_data[key]
        bp = sum(1 for r in recs if r.get("binary_pass"))
        cost = sum(r.get("total_cost_usd", 0) or 0 for r in recs)
        summary_rows.append(f"<tr><td>{label}</td><td>{bp}/58</td><td>${cost:.2f}</td></tr>")

    parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>三臂 bench 人工 Review 索引（2026-08-23/24）</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 24px; font-size: 14px; }}
table {{ border-collapse: collapse; margin: 12px 0; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: left; }}
th {{ background: #f0f0f0; }}
.ok {{ color: #0a0; }} .bad {{ color: #c00; font-weight: bold; }} .na {{ color: #999; }}
.missing {{ color: #999; font-size: 12px; }}
details {{ margin: 8px 0; }} summary {{ cursor: pointer; font-weight: 600; }}
pre {{ background: #f7f7f7; padding: 8px; overflow-x: auto; font-size: 12px; }}
.cmd {{ background: #eef; padding: 2px 6px; font-family: monospace; font-size: 12px; }}
h2 {{ border-bottom: 2px solid #333; padding-bottom: 4px; margin-top: 32px; }}
</style></head><body>
<h1>三臂 bench 人工 Review 索引</h1>
<p>批次：decision suite 29 任务 × 2 repeats × <code>--with-delivery</code>（2026-08-23/24，最终固化口径）。
每个 run 可查看：题目原文、TASK.md / context.md / meta.json / execution.log、交付分支 diff（复制命令到终端执行）。</p>
<table><tr><th>臂</th><th>binary_pass</th><th>成本</th></tr>{''.join(summary_rows)}</table>
"""]

    # 按任务组织：29 任务 × 3 臂 × 2 repeats
    task_ids = [tid for tid in yamls if any(r["task_id"] == tid for r in arms_data["local35b"])]
    for tid in task_ids:
        y = yamls.get(tid, {})
        parts.append(f"<h2 id='{tid}'>{tid} <small>({y.get('difficulty','')})</small></h2>")
        parts.append(f"<details><summary>题目原文（{fl(y.get('path'), 'YAML')}）</summary><pre>{html.escape(y.get('task', ''))}</pre>"
                     f"<p>verification:</p><pre>{html.escape(chr(10).join(str(v) for v in y.get('verification', [])))}</pre></details>")
        parts.append("<table><tr><th>臂</th><th>rep</th><th>binary</th><th>semantic</th><th>kill_reason</th>"
                     "<th>耗时</th><th>成本</th><th>delivery</th><th>产物链接</th><th>交付 review</th></tr>")
        for key, label in ARMS:
            for r in sorted((x for x in arms_data[key] if x["task_id"] == tid), key=lambda x: x.get("repeat", 0)):
                td = Path(r["task_dir"]) if r.get("task_dir") else None
                meta = {}
                if td and (td / "meta.json").exists():
                    try:
                        meta = json.loads((td / "meta.json").read_text(encoding="utf-8"))
                    except Exception:
                        pass
                links = []
                if td:
                    links.append(fl(td, "任务目录"))
                    for sub_dir in sorted(td.glob("sub-*"))[:3]:
                        if (sub_dir / "TASK.md").exists():
                            links.append(fl(sub_dir / "TASK.md", f"TASK({sub_dir.name})"))
                        if (sub_dir / "context.md").exists():
                            links.append(fl(sub_dir / "context.md", f"context({sub_dir.name})"))
                    links.append(fl(td / "execution.log", "log"))
                review = ""
                dbranch = meta.get("delivery_branch")
                base = meta.get("base_commit", "")
                repo = meta.get("repo", "")
                if dbranch and repo:
                    review = (f"<span class='cmd'>git -C {html.escape(repo)} diff "
                              f"{html.escape(base[:10])}..{html.escape(dbranch)}</span>")
                else:
                    # 失败 run：看保留 worktree
                    wts = [res.get("worktree") for res in meta.get("results", []) if res.get("worktree")]
                    if wts:
                        review = "worktree: " + fl(wts[0], Path(wts[0]).name)
                parts.append(
                    f"<tr><td>{label.split('（')[0]}</td><td>{r.get('repeat')}</td>"
                    f"<td>{badge(r.get('binary_pass'))}</td><td>{badge(r.get('semantic_pass'))}</td>"
                    f"<td>{html.escape(str(r.get('kill_reason') or ''))}</td>"
                    f"<td>{r.get('elapsed_sec', 0):.0f}s</td><td>${r.get('total_cost_usd', 0) or 0:.3f}</td>"
                    f"<td>{badge(r.get('accepted_delivery'))}</td>"
                    f"<td>{' · '.join(x for x in links if x)}</td><td>{review}</td></tr>")
        parts.append("</table>")

    parts.append("</body></html>")
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"已生成: {OUT}")
    print(f"打开: open {OUT}")


if __name__ == "__main__":
    main()
