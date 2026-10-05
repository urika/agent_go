#!/usr/bin/env python3
"""spec-to-test 端到端冒烟（ADR-012）：真 CLI + 真 worker + 真 verify 重放。

与 tests/ 的区别：本脚本跑**真实链路**（agent_go CLI 子进程、claude worker、真实 git 提交），
代价是分钟级与真实模型花费；`tests/test_spec_test.py` 里的集成用例是它的离线对应物。

三种口径（--mode）：
  provided       评测口径：provided_dir 提供冻结件（出题人≠解题人），**零 planner/draft LLM 调用**
                 → 不占本地引擎；worker 仍走 claude CLI 的真实端点。
  draft-unreviewed 起草口径：真 LLM 起草 + require_review=false（无人审直接冻结）
                 → 占本地引擎（plan + draft 两次调用）。
  draft-web      全链路：真 LLM 起草 + web 确认门人审（脚本代人工提交回执，可附编辑）
                 → 占本地引擎；额外验证确认门 → 回执 → 冻结 的闭环。

安全闸：draft* 模式默认检测外部批次锁（swe-eval 等 .batch.lock）并**拒绝开跑**，
避免与他人在飞的评测批次争用同一台本地引擎；`--allow-engine-share` 可显式放行。

用法：
  python3 tools/spec_test_smoke.py --mode provided
  python3 tools/spec_test_smoke.py --mode draft-web --keep

结果落档：eval_suite/spec_test_smoke/results.jsonl（UTF-8 JSONL，追加）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_FILE = ROOT / "eval_suite" / "spec_test_smoke" / "results.jsonl"
#: 已知的外部批次锁（决定是否允许 draft 模式与本地引擎争用）
FOREIGN_BATCH_LOCKS = [
    Path.home() / "APP" / "swe-eval" / "results" / ".batch.lock",
    Path.home() / "workspace" / "swe-eval" / "results" / ".batch.lock",
]

TASK = (
    "在 calc 包中实现 add(a, b)：返回两数之和。仓库根有 README 描述契约；"
    "实现必须让冻结的验收测试通过（不要修改测试文件）。"
)

PROVIDED_TEST = '''from calc import add


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0
'''

CALC_INIT = '''"""calc 包（冒烟夹具）：add 待实现。"""


def add(a, b):
    raise NotImplementedError("add 尚未实现")
'''

README = """# calc（spec-to-test 冒烟夹具）

契约：`calc.add(a, b)` 返回 `a + b`。

验收测试位于 `tests/acceptance/`（冻结件），实现必须使其通过；不得修改测试。
"""


def log(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_json(url: str, body: dict | None = None, timeout: float = 10.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}


def check_foreign_batches(allow: bool, mode: str) -> list[str]:
    """draft* 模式要调本地 planner/draft LLM：检测外部在飞批次并默认拒绝。"""
    found = [str(p) for p in FOREIGN_BATCH_LOCKS if p.exists()]
    if found and mode != "provided" and not allow:
        log("检测到外部批次锁（本地引擎另有用途）：")
        for p in found:
            log(f"  - {p}")
        log("draft* 模式默认拒绝开跑（避免争用引擎污染他人批次）；如确认可争用，加 --allow-engine-share")
        raise SystemExit(2)
    return found


def make_fixture_repo(base: Path) -> Path:
    repo = base / "repo"
    (repo / "calc").mkdir(parents=True)
    (repo / "calc" / "__init__.py").write_text(CALC_INIT, encoding="utf-8")
    (repo / "README.md").write_text(README, encoding="utf-8")
    (repo / "tests").mkdir()
    for cmd in (["git", "init"], ["git", "config", "user.email", "smoke@test.local"],
                ["git", "config", "user.name", "smoke"], ["git", "add", "."],
                ["git", "commit", "-m", "fixture: calc.add 待实现"]):
        subprocess.run(cmd, cwd=str(repo), capture_output=True, check=True)
    return repo


def make_provided_dir(base: Path) -> Path:
    prov = base / "provided"
    prov.mkdir()
    (prov / "test_acceptance_add.py").write_text(PROVIDED_TEST, encoding="utf-8")
    (prov / "commands.json").write_text(
        json.dumps(["python3 -m pytest tests/acceptance/test_acceptance_add.py -q"]), encoding="utf-8")
    return prov


def make_home(base: Path, mode: str, provided_dir: Path, frozen_dir: str = "tests/acceptance") -> Path:
    """隔离 HOME：.claude 用符号链接复用真实凭证（不复制密钥），config.json 为冒烟专用。"""
    home = base / "home"
    adir = home / ".agent_go"
    adir.mkdir(parents=True)
    # worker 凭证：符号链接到真实 ~/.claude（不落密文副本）
    real_claude = Path.home() / ".claude"
    if real_claude.exists():
        os.symlink(real_claude, home / ".claude")
    # 真 ~/.claude.json（claude CLI 的登录态在部分版本里放这里）
    real_claude_json = Path.home() / ".claude.json"
    if real_claude_json.exists():
        os.symlink(real_claude_json, home / ".claude.json")
    cfg: dict = {
        "plan_api": {"provider": "openai", "base_url": "http://localhost:4000/v1/chat/completions",
                     "model": "claude-opus-4-7", "api_key": "local-no-key"},
        "evaluator": {"enabled": False},
        "verification": {"max_retries": 1},
        "spec_test": {
            "enabled": True,
            "frozen_dir": frozen_dir,
            "provided_dir": str(provided_dir) if mode == "provided" else "",
            "require_review": mode != "draft-unreviewed",
        },
    }
    (adir / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return home


def start_web(home: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.Popen([sys.executable, "-m", "agent_go", "web", "--port", str(port)],
                            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        try:
            status, _ = _http_json(f"http://127.0.0.1:{port}/api/health", timeout=2)
            if status == 200:
                return proc
        except Exception:  # noqa: BLE001 - 启动期重试
            continue
    proc.terminate()
    raise SystemExit("[smoke] web 控制台启动失败")


def wait_pending(task_dir: Path, timeout: float) -> dict:
    pf = task_dir / "pending_confirmation.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pf.exists():
            try:
                return json.loads(pf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        time.sleep(2)
    raise SystemExit("[smoke] 等待 pending_confirmation.json 超时（Plan 阶段未到达确认门）")


def wait_terminal(task_dir: Path, proc: subprocess.Popen, timeout: float) -> tuple[str, dict]:
    mp = task_dir / "meta.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            st = str(meta.get("status") or "")
            if st and st not in ("EXECUTING", "PLANNING", "PAUSED"):
                return st, meta
        if proc.poll() is not None:
            time.sleep(2)
            if mp.exists():
                meta = json.loads(mp.read_text(encoding="utf-8"))
                return str(meta.get("status") or "?"), meta
        time.sleep(3)
    raise SystemExit("[smoke] 任务未在超时内到达终态")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    return r.stdout.strip()


def _git_blob(repo: Path, spec: str) -> str:
    """取 blob 原文（不 strip：sha256 必须与文件字节一致）。"""
    r = subprocess.run(["git", "show", spec], cwd=str(repo), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def collect_asserts(task_dir: Path, repo: Path, mode: str, meta: dict,
                    edit_marker: bool) -> list[dict]:
    """四护栏 + 追溯断言（每条 {name, ok, detail}）。"""
    out: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    acc = meta.get("acceptance") or {}
    manifest_path = task_dir / "acceptance" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None

    # 冻结事实（护栏①前置 + ④口径）
    add("acceptance.enabled", acc.get("enabled") is True)
    add("acceptance.frozen", acc.get("frozen") is True and bool(manifest))
    add("acceptance.reviewed", acc.get("reviewed") is True)
    add("acceptance.source", acc.get("source") == ("task" if mode == "provided" else "drafted"),
        f"source={acc.get('source')}")
    if mode == "provided":
        add("acceptance.review_channel=provided", acc.get("review_channel") == "provided")
    elif mode == "draft-web":
        add("acceptance.review_channel=web", acc.get("review_channel") == "web")
        add("acceptance.review_actor", str(acc.get("review_actor") or "").startswith("web"),
            f"actor={acc.get('review_actor')}")
        if edit_marker:
            add("审阅编辑生效（review_edits≥1）", int(acc.get("review_edits") or 0) >= 1,
                f"edits={acc.get('review_edits')}")
    add("留痕：draft/frozen 时间与命令", bool(acc.get("commands")), f"commands={acc.get('commands')}")
    if mode.startswith("draft"):
        add("留痕：drafted_at", bool(acc.get("drafted_at")))
        add("留痕：draft_cost 口径明确",
            ("draft_cost_usd" in acc) and (acc.get("draft_cost_source") in ("metering_delta", "unavailable")),
            f"cost={acc.get('draft_cost_usd')} src={acc.get('draft_cost_source')}")

    # 运行侧（护栏①②）：验收命令执行且带标签；命令来自冻结件
    results = meta.get("results") or []
    acc_entries = [vr for r in results for vr in (r.get("verification_results") or [])
                   if isinstance(vr, dict) and vr.get("type") == "acceptance"]
    frozen_cmds = set(manifest.get("commands") or []) if manifest else set()
    add("护栏②：验收命令来自冻结件",
        bool(acc_entries) and all(e.get("command") in frozen_cmds for e in acc_entries),
        f"entries={len(acc_entries)}")
    add("验收命令通过", bool(acc_entries) and all(e.get("exit_code") in (0, 127) for e in acc_entries),
        f"exit_codes={[e.get('exit_code') for e in acc_entries]}")
    add("首次尝试即通过", any(e.get("attempt") == 1 and e.get("exit_code") == 0 for e in acc_entries))

    # 护栏①：冻结件随交付 commit 落地且哈希一致
    commit = ""
    for r in results:
        commit = commit or str(r.get("commit_hash") or "")
    if not commit:
        commit = _git(repo, "rev-parse", "HEAD")
    if manifest and commit:
        ok_all = True
        for item in manifest["files"]:
            rel = f"{manifest.get('frozen_dir')}/{item['path']}"
            blob = _git_blob(repo, f"{commit}:{rel}")
            if not blob:
                ok_all = False
                break
            if hashlib.sha256(blob.encode("utf-8")).hexdigest() != item["sha256"]:
                ok_all = False
                break
        add("护栏①：冻结件在交付 commit 且哈希一致", ok_all, f"commit={commit[:12]}")
    # 实现落盘（worker 真的干了活）
    add("交付包含实现", "return a + b" in _git_blob(repo, f"{commit}:calc/__init__.py")
        if commit else False)
    if edit_marker and mode == "draft-web" and manifest and commit:
        rel = f"{manifest.get('frozen_dir')}/{manifest['files'][0]['path']}"
        add("护栏①：人审编辑随冻结件进入 commit", "smoke-edit-marker" in _git_blob(repo, f"{commit}:{rel}"))
    return out


def run_eval_acceptance(home: Path) -> dict:
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([sys.executable, "-m", "agent_go", "--json", "eval", "acceptance"],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="spec-to-test 端到端冒烟（ADR-012）")
    ap.add_argument("--mode", choices=["provided", "draft-unreviewed", "draft-web"], default="provided")
    ap.add_argument("--timeout-min", type=float, default=30.0, help="单轮总超时（分钟）")
    ap.add_argument("--keep", action="store_true", help="保留临时 HOME/仓库/任务目录")
    ap.add_argument("--allow-engine-share", action="store_true",
                    help="检测到外部批次锁时仍允许 draft* 模式（默认拒绝）")
    ap.add_argument("--no-edit", action="store_true", help="draft-web 不做人审编辑（默认注入一行标记）")
    args = ap.parse_args()

    if not shutil.which("claude"):
        raise SystemExit("[smoke] 未找到 claude CLI：真实链路需要 worker 可执行")

    foreign = check_foreign_batches(args.allow_engine_share, args.mode)
    log(f"mode={args.mode}｜外部批次锁={foreign or '无'}")

    base = Path(tempfile.mkdtemp(prefix="spec_test_smoke_"))
    web_proc = None
    run_proc = None
    started = time.time()
    record: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": args.mode, "ok": False}
    try:
        repo = make_fixture_repo(base)
        provided = make_provided_dir(base)
        home = make_home(base, args.mode, provided)
        port = _free_port()
        if args.mode == "draft-web":
            web_proc = start_web(home, port)
            log(f"web 控制台就绪: http://127.0.0.1:{port}")

        cmd = [sys.executable, "-m", "agent_go", "--json", "run", str(repo), TASK,
               "--yes", "--e2e", "--accept-tests", "--parallel", "1"]
        if args.mode == "draft-web":
            cmd += ["--confirm-mode", "web"]
        env = dict(os.environ, HOME=str(home))
        run_log = base / "run.log"
        with run_log.open("wb") as lf:
            run_proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=lf, stderr=subprocess.STDOUT)
        log(f"已启动任务（pid={run_proc.pid}），等待 task_id...")

        # 解析 task_id：轮询临时 HOME 下的任务目录（cli 会在 plan 前建目录）
        task_id = ""
        deadline = time.time() + 120
        while time.time() < deadline and not task_id:
            dirs = sorted((home / ".agent_go").glob("task-*"))
            if dirs:
                task_id = dirs[-1].name
            time.sleep(1)
        if not task_id:
            raise SystemExit(f"[smoke] 未创建任务目录；run.log 尾部：\n{run_log.read_text()[-2000:]}")
        task_dir = home / ".agent_go" / task_id
        record["task_id"] = task_id
        log(f"task_id={task_id}")

        if args.mode == "draft-web":
            pending = wait_pending(task_dir, timeout=600)
            draft = (pending.get("payload") or {}).get("_acceptance_draft") or {}
            add_ok = bool(draft.get("files")) and bool(draft.get("commands"))
            record["pending_draft_ok"] = add_ok
            log(f"确认门：草稿 {len(draft.get('files') or [])} 文件 / {len(draft.get('commands') or [])} 命令")
            if not add_ok:
                raise SystemExit("[smoke] pending payload 未携带验收草稿")
            body: dict = {"stage": "plan", "decision": "Y"}
            if not args.no_edit:
                files = [dict(f) for f in draft["files"]]
                files[0]["content"] = files[0]["content"] + "\n# smoke-edit-marker\n"
                body["acceptance"] = {"decision": "approved", "edits": 1, "files": files}
                log("回执携带 1 处人审编辑（smoke-edit-marker）")
            else:
                body["acceptance"] = {"decision": "approved", "edits": 0, "files": []}
            status, resp = _http_json(f"http://127.0.0.1:{port}/api/tasks/{task_id}/confirm", body)
            log(f"确认回执：HTTP {status} {json.dumps(resp, ensure_ascii=False)[:160]}")
            if status != 200:
                raise SystemExit("[smoke] 确认回执被拒")

        status, meta = wait_terminal(task_dir, run_proc, timeout=args.timeout_min * 60)
        log(f"终态: {status}")

        asserts = collect_asserts(task_dir, repo, args.mode, meta, edit_marker=(not args.no_edit))
        agg = run_eval_acceptance(home)
        asserts.append({"name": "聚合命令能数到本任务",
                        "ok": (agg.get("cohort", {}).get("tasks") or 0) >= 1,
                        "detail": f"cohort={agg.get('cohort')}"})
        failed = [a for a in asserts if not a["ok"]]
        cost = float(((meta.get("acceptance") or {}).get("draft_cost_usd")) or 0.0)
        record.update({
            "status": status,
            "ok": not failed and status in ("ACCEPTED_DELIVERY", "DELIVERY_READY"),
            "asserts": asserts,
            "asserts_failed": [a["name"] for a in failed],
            "duration_min": round((time.time() - started) / 60, 2),
            "draft_cost_usd": cost,
            "aggregate": {k: agg.get(k) for k in ("cohort", "oracle", "misjudge")},
            "temp_base": str(base) if args.keep else "",
        })
        log("─" * 60)
        for a in asserts:
            log(f"  {'✅' if a['ok'] else '❌'} {a['name']}" + (f"（{a['detail']}）" if a["detail"] else ""))
        log(f"结论: {'PASS' if record['ok'] else 'FAIL'}｜状态={status}｜耗时={record['duration_min']}min")
        RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RESULTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log(f"结果已落档: {RESULTS_FILE}")
        return 0 if record["ok"] else 1
    finally:
        for p in (run_proc, web_proc):
            if p is not None and p.poll() is None:
                try:
                    p.send_signal(signal.SIGINT)
                    p.wait(timeout=15)
                except Exception:  # noqa: BLE001 - 收尾尽力而为
                    p.kill()
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            log(f"临时目录保留: {base}")


if __name__ == "__main__":
    sys.exit(main())
