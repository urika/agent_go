#!/usr/bin/env python3
"""Ornith-1.5-9B 文档处理能力验收（一次性通过口径，确定性校验）。

用法: python3 run_acceptance.py
产出: outputs/<task-id>.md（原始输出） + report.md（逐任务校验明细）
"""
import json
import re
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8088/v1/chat/completions"
MODEL = "ornith-ai/Ornith-1.5-9B-MLX-4bit"
ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent  # agent_go 仓库根
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def cjk_ratio(text):
    cjk = len(re.findall(r"[一-鿿]", text))
    total = len(re.findall(r"[一-鿿A-Za-z]", text))
    return cjk / total if total else 0.0


def code_blocks(text):
    return re.findall(r"```[^\n]*\n(.*?)```", text, re.S)


def table_rows(text):
    return [l for l in text.splitlines() if l.strip().startswith("|")]


def numbers(text):
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def links(text):
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)


def check_translation(src, out):
    fails = []
    for i, blk in enumerate(code_blocks(src)):
        if blk.strip() and blk.strip() not in out:
            fails.append(f"代码块{i+1}被改动")
    if len(table_rows(out)) < len(table_rows(src)) - 1:
        fails.append(f"表格行数 {len(table_rows(out))} < 源 {len(table_rows(src))}")
    missing = {n for n in numbers(src) if n not in out}
    if missing:
        fails.append(f"数字丢失: {sorted(missing)[:5]}")
    r = cjk_ratio(out)
    if r > 0.05:
        fails.append(f"中文残留 {r:.1%} > 5%")
    if len(out) < len(src) * 0.4:
        fails.append("输出过短，疑似漏译")
    return fails


def check_summary(out, max_chars, keywords=()):
    fails = []
    n = len(re.sub(r"\s", "", out))
    if n > max_chars:
        fails.append(f"字数 {n} > 上限 {max_chars}")
    for kw in keywords:
        if kw not in out:
            fails.append(f"缺关键词: {kw}")
    if n < 30:
        fails.append("摘要过短")
    return fails


def check_glossary(src, out, n_terms):
    fails = []
    rows = [l for l in table_rows(out) if "---" not in l][1:]  # 去表头
    if len(rows) != n_terms:
        fails.append(f"术语数 {len(rows)} != {n_terms}")
    for l in rows:
        cells = [c.strip() for c in l.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0]:
            fails.append(f"行格式错误: {l[:40]}")
            continue
        if not re.search(r"[一-鿿]", cells[0]):
            fails.append(f"术语列无中文: {cells[0][:20]}")
        if cells[0] not in src:
            fails.append(f"术语不在原文: {cells[0][:20]}")
    return fails


def check_index(out):
    fails = []
    ls = links(out)
    if not ls:
        fails.append("未产出任何 Markdown 链接")
    for lnk in ls:
        p = lnk.split("#")[0]
        if p and not (REPO / p).exists() and not (REPO / "docs" / p).exists():
            fails.append(f"链接目标不存在: {lnk}")
    return fails


def check_rewrite(src, out, max_chars=None, min_items=0):
    fails = []
    missing = {n for n in numbers(src) if n not in out}
    if missing:
        fails.append(f"数字丢失: {sorted(missing)[:5]}")
    if max_chars:
        n = len(re.sub(r"\s", "", out))
        if n > max_chars:
            fails.append(f"字数 {n} > 上限 {max_chars}")
    if min_items:
        items = len(re.findall(r"^\s*(?:[-*]|\d+[.、])", out, re.M))
        if items < min_items:
            fails.append(f"条目数 {items} < {min_items}")
    return fails


TASKS = [
    # ── A. 中→英翻译 ──
    {"id": "A1-translate-verification", "cat": "翻译", "src": "docs/design/verification-design.md",
     "prompt": "把以下 Markdown 技术文档完整翻译成英文。要求：代码块和命令逐字保留；表格结构不变；专有名词（agent_go、worktree 等）不译；不要添加原文没有的内容。\n\n{src}",
     "max_tokens": 6000, "check": lambda s, o: check_translation(s, o)},
    {"id": "A2-translate-failure-class", "cat": "翻译", "src": "docs/design/m0-failure-class.md",
     "prompt": "把以下 Markdown 技术文档完整翻译成英文。要求：代码块和命令逐字保留；表格结构不变；专有名词不译；不要添加原文没有的内容。\n\n{src}",
     "max_tokens": 6000, "check": lambda s, o: check_translation(s, o)},
    {"id": "A3-translate-func-arch", "cat": "翻译", "src": "docs/design/functional-architecture.md",
     "prompt": "把以下 Markdown 技术文档完整翻译成英文。要求：代码块逐字保留；表格结构不变；专有名词不译；不要添加原文没有的内容。\n\n{src}",
     "max_tokens": 6000, "check": lambda s, o: check_translation(s, o)},
    {"id": "A4-translate-greywall", "cat": "翻译", "src": "docs/design/sandbox-greywall.md",
     "prompt": "把以下 Markdown 技术文档完整翻译成英文。要求：代码块和命令逐字保留；表格结构不变；专有名词不译；不要添加原文没有的内容。\n\n{src}",
     "max_tokens": 8192, "check": lambda s, o: check_translation(s, o)},
    # ── B. 摘要 ──
    {"id": "B1-summary-trust", "cat": "摘要", "src": "docs/design/trust-metrics-eval-d1-2026-08-28.md",
     "prompt": "用中文为以下文档写一段不超过 200 字的摘要，必须覆盖「信任指标」和「放行门」两个概念。只输出摘要正文，不要标题。\n\n{src}",
     "max_tokens": 1500, "check": lambda s, o: check_summary(o, 200, ["信任指标", "放行门"])},
    {"id": "B2-summary-kanban", "cat": "摘要", "src": "docs/design/kanban-board.md",
     "prompt": "用中文为以下文档写一段不超过 200 字的摘要，必须提到「看板」。只输出摘要正文，不要标题。\n\n{src}",
     "max_tokens": 1500, "check": lambda s, o: check_summary(o, 200, ["看板"])},
    {"id": "B3-summary-blindspot", "cat": "摘要", "src": "docs/design/blind-spot-attribution-workflow.md",
     "prompt": "用中文为以下文档写一段不超过 300 字的摘要，必须覆盖「盲区」和「归因」。只输出摘要正文，不要标题。\n\n{src}",
     "max_tokens": 1500, "check": lambda s, o: check_summary(o, 300, ["盲区", "归因"])},
    # ── C. 术语表 ──
    {"id": "C1-glossary-state", "cat": "术语表", "src": "docs/design/m0-state-machine.md",
     "prompt": "从以下文档中提取 5 个最关键的术语，输出 Markdown 表格，三列：中文术语 | English | 一句话解释。术语必须原文出现过。不要输出表格外的任何内容。\n\n{src}",
     "max_tokens": 2000, "check": lambda s, o: check_glossary(s, o, 5)},
    {"id": "C2-glossary-role-matrix", "cat": "术语表", "src": "docs/design/model-role-config-matrix.md",
     "prompt": "从以下文档中提取 6 个最关键的术语，输出 Markdown 表格，三列：中文术语 | English | 一句话解释。术语必须原文出现过。不要输出表格外的任何内容。\n\n{src}",
     "max_tokens": 2000, "check": lambda s, o: check_glossary(s, o, 6)},
    # ── D. 索引维护 ──
    {"id": "D1-index-entry", "cat": "索引维护",
     "src": "docs/design/result-schema.md",
     "prompt": "以下是 docs/design/result-schema.md 的内容。请为 wiki 索引页产出一个索引条目，格式为 Markdown 列表项：- [标题](docs/design/result-schema.md) — 一句话中文说明。只输出这一个列表项。\n\n{src}",
     "max_tokens": 500, "check": lambda s, o: check_index(o)},
    {"id": "D2-cross-ref", "cat": "索引维护", "src": "docs/design/kanban-task-orchestration.md",
     "prompt": "以下文档与 docs/design/kanban-board.md 相关。请产出两行「相关文档」引用，格式：- [文档标题](相对链接) — 一句话说明。一行指向 docs/design/kanban-board.md，另一行指向 docs/design/kanban-task-orchestration.md 自身章节锚点可省略。只输出这两行。\n\n{src}",
     "max_tokens": 500, "check": lambda s, o: check_index(o)},
    # ── E. 结构化改写 ──
    {"id": "E1-checklist-gates", "cat": "改写", "src": "docs/design/m0-e2e-gates.md",
     "prompt": "把以下文档改写成面向新人的验收 checklist。要求：保留全部命令和数值阈值；用 `- [ ]` 条目；不超过 400 字。\n\n{src}",
     "max_tokens": 2000, "check": lambda s, o: check_rewrite(s, o, max_chars=400)},
    {"id": "E2-rewrite-issues", "cat": "改写", "src": "docs/design/m0-batch-governance.md",
     "prompt": "把以下文档的核心规则改写成 3 条 FAQ（问答对）。要求：保留全部数字；每条答案不超过 100 字。\n\n{src}",
     "max_tokens": 2000, "check": lambda s, o: check_rewrite(s, o, min_items=3)},
    {"id": "E3-rewrite-metric-freeze", "cat": "改写", "src": "docs/design/m0-metric-freeze.md",
     "prompt": "把以下文档改写成发布说明（release note）风格：一条「新增」、一条「变更」、一条「修复」。保留全部数字。总长度不超过 250 字。\n\n{src}",
     "max_tokens": 2000, "check": lambda s, o: check_rewrite(s, o, max_chars=250, min_items=3)},
]


def call(prompt, max_tokens):
    body = {"model": MODEL, "stream": False, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    dt = time.time() - t0
    m = d["choices"][0]["message"]
    return m.get("content") or "", d.get("usage", {}), dt


def main():
    results = []
    for i, t in enumerate(TASKS, 1):
        src = read(t["src"])
        prompt = t["prompt"].format(src=src)
        print(f"[{i}/{len(TASKS)}] {t['id']} ...", flush=True)
        try:
            out, usage, dt = call(prompt, t["max_tokens"])
        except Exception as e:
            results.append({"id": t["id"], "cat": t["cat"], "error": str(e)})
            continue
        (OUT / f"{t['id']}.md").write_text(out, encoding="utf-8")
        fails = t["check"](src, out)
        results.append({"id": t["id"], "cat": t["cat"], "pass": not fails,
                        "fails": fails, "secs": round(dt, 1),
                        "pt": usage.get("prompt_tokens"), "ct": usage.get("completion_tokens")})
        print(f"    {'PASS' if not fails else 'FAIL: ' + '; '.join(fails)} ({dt:.0f}s)", flush=True)

    passed = sum(1 for r in results if r.get("pass"))
    lines = ["# Ornith-1.5-9B 文档处理验收报告", "",
             f"- 任务数: {len(results)}，一次性通过: {passed}（{passed/len(results):.0%}）",
             f"- 模型: {MODEL}（MLX 4bit, thinking off, temp 0.6）", ""]
    cur = None
    for r in results:
        if r["cat"] != cur:
            cur = r["cat"]
            lines.append(f"## {cur}")
        if "error" in r:
            lines.append(f"- ❌ **{r['id']}**：调用失败 {r['error']}")
        else:
            mark = "✅" if r["pass"] else "❌"
            detail = "" if r["pass"] else " — " + "；".join(r["fails"])
            lines.append(f"- {mark} **{r['id']}**（{r['secs']}s, {r['ct']} tok）{detail}")
        lines.append("")
    (ROOT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== {passed}/{len(results)} 一次性通过 ===")
    print("报告: report.md，原始输出: outputs/")


if __name__ == "__main__":
    main()
