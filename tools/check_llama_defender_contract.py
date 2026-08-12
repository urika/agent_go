#!/usr/bin/env python3
"""llama-defender 集成契约验收脚本（纯 stdlib）。

对运行中的 llama-defender 执行 R1-R7 接口契约检查。
默认 safe 模式（只读）；--full 执行变更操作与故障注入用例。

用法：
    python3 tools/check_llama_defender_contract.py [--full] [--json]
        [--proxy-url http://127.0.0.1:4000]
        [--manage-script /path/to/manage.sh]

退出码：任一 P0 用例 FAIL → 1；否则 0。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_ENUM = {"healthy", "starting", "backend_down", "proxy_down", "model_drift", "down"}
REQUIRED_STATUS_FIELDS = ["proxy", "backend", "active_profile", "state", "ready"]

results: list[dict] = []


def record(check_id: str, req: str, priority: str, name: str, ok: bool | None, detail: str = "") -> None:
    """ok: True=PASS, False=FAIL, None=SKIP"""
    status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
    results.append({"id": check_id, "req": req, "priority": priority,
                    "name": name, "status": status, "detail": detail})
    mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ "}[status]
    print(f"{mark} [{check_id}] ({req}/{priority}) {name}" + (f" — {detail}" if detail else ""))


def http_get(url: str, timeout: float = 2.0) -> tuple[int, dict | str, float]:
    """返回 (status_code, json_or_text, elapsed_sec)。网络错误返回 (0, err, elapsed)。"""
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            elapsed = time.time() - start
            try:
                return resp.status, json.loads(body), elapsed
            except json.JSONDecodeError:
                return resp.status, body, elapsed
    except urllib.error.HTTPError as e:
        return e.code, str(e), time.time() - start
    except Exception as e:
        return 0, str(e), time.time() - start


def run_manage(manage: Path, *args: str, timeout: int = 10) -> tuple[int, str, str, float]:
    """非交互执行 manage.sh，返回 (exit, stdout, stderr, elapsed)。"""
    start = time.time()
    try:
        proc = subprocess.run(
            ["bash", str(manage), *args],
            stdin=subprocess.DEVNULL,  # R3 非交互保证：不得等待输入
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr, time.time() - start
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout>{timeout}s", time.time() - start


def get_status(proxy: str) -> tuple[int, dict | str, float, str]:
    """优先 /api/status，备选 /status?format=json。返回 (code, body, elapsed, endpoint)。"""
    for ep in ("/api/status", "/status?format=json"):
        code, body, elapsed = http_get(proxy + ep, timeout=2.0)
        if code == 200 and isinstance(body, dict):
            return code, body, elapsed, ep
    return code, body, elapsed, ep


# ── A 组：接口契约（safe） ─────────────────────────────────────────

def check_a1_status_endpoint(proxy: str) -> None:
    code, body, _, ep = get_status(proxy)
    ok = code == 200 and isinstance(body, dict)
    record("A1", "R1", "P0", "结构化状态端点存在", ok,
           f"{ep} → {code}" + ("" if ok else "（未实现，预期基线 FAIL）"))


def check_a2_required_fields(proxy: str) -> None:
    code, body, _, _ = get_status(proxy)
    if not (code == 200 and isinstance(body, dict)):
        record("A2", "R1", "P0", "必含字段", None, "端点缺失，跳过")
        return
    missing = [f for f in REQUIRED_STATUS_FIELDS if f not in body]
    nested = []
    if isinstance(body.get("proxy"), dict) and "alive" not in body["proxy"]:
        nested.append("proxy.alive")
    if isinstance(body.get("backend"), dict):
        for f in ("model_name", "alive"):
            if f not in body["backend"]:
                nested.append(f"backend.{f}")
    missing += nested
    record("A2", "R1", "P0", "必含字段", not missing,
           "齐全" if not missing else f"缺失: {missing}")


def check_a3_state_enum(proxy: str) -> None:
    code, body, _, _ = get_status(proxy)
    if not (code == 200 and isinstance(body, dict)):
        record("A3", "R1", "P0", "state 枚举合法", None, "端点缺失，跳过")
        return
    state = body.get("state")
    record("A3", "R1", "P0", "state 枚举合法", state in STATE_ENUM,
           f"state={state!r}")


def check_a4_latency(proxy: str) -> None:
    code, _, elapsed, ep = get_status(proxy)
    if code != 200:
        record("A4", "R1", "P0", "状态端点时延 <1s", None, "端点缺失，跳过")
        return
    record("A4", "R1", "P0", "状态端点时延 <1s", elapsed < 1.0, f"{elapsed:.2f}s @ {ep}")


def check_a6_ready_semantics(proxy: str) -> None:
    code, body, _, _ = get_status(proxy)
    if not (code == 200 and isinstance(body, dict)):
        record("A6", "R2", "P0", "ready 语义一致", None, "端点缺失，跳过")
        return
    ready, state = body.get("ready"), body.get("state")
    ok = isinstance(ready, bool) and (state != "healthy" or ready is True)
    record("A6", "R2", "P0", "ready 语义一致（healthy→ready=true）", ok,
           f"state={state!r} ready={ready!r}")


def check_a7_noninteractive(manage: Path) -> None:
    fails = []
    for cmd in ("status", "current", "list"):
        code, _, err, elapsed = run_manage(manage, cmd, timeout=10)
        if code != 0 or elapsed >= 10:
            fails.append(f"{cmd}: exit={code} {elapsed:.1f}s {err[:60]}")
    record("A7", "R3", "P0", "manage.sh 只读命令非交互", not fails,
           "均 <10s exit 0" if not fails else "; ".join(fails))


def check_a8_invalid_cmd_exit(manage: Path) -> None:
    code, out, err, _ = run_manage(manage, "__nonexistent_cmd__", timeout=10)
    record("A8", "R3", "P0", "非法命令退出码非 0 + 错误输出", code != 0 and bool((out + err).strip()),
           f"exit={code}")


def check_a9_readonly_idempotent(manage: Path) -> None:
    codes = [run_manage(manage, "status", timeout=10)[0] for _ in range(2)]
    record("A9", "R3", "P0", "只读命令幂等", all(c == 0 for c in codes),
           f"exits={codes}")


def check_a10_watchdog_status(manage: Path) -> None:
    code, out, err, _ = run_manage(manage, "watchdog-status", timeout=10)
    text = (out + err).lower()
    ok = code == 0 and any(k in text for k in ("enabled", "running", "last", "restart", "disabled"))
    record("A10", "R5", "P1", "watchdog 状态可查", ok if code == 0 else ok,
           f"exit={code}" + ("" if ok else "（输出缺少结构化字段）"))


def check_a11_profiles_endpoint(proxy: str) -> None:
    code, body, _ = http_get(proxy + "/api/profiles", timeout=2.0)
    if code == 404 or code == 0:
        record("A11", "R6", "P1", "profile 列表端点（可选）", None, "未实现，SKIP")
        return
    ok = code == 200 and isinstance(body, (list, dict))
    record("A11", "R6", "P1", "profile 列表端点（可选）", ok, f"→ {code}")


def check_a12_lifecycle_events(manage: Path) -> None:
    log = manage.parent / "logs" / "lifecycle_events.jsonl"
    if not log.exists():
        record("A12", "R7", "P2", "生命周期事件（可选）", None, "文件不存在，SKIP")
        return
    try:
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()][:5]
        ok = all("ts" in line or "timestamp" in line for line in lines) if lines else True
        record("A12", "R7", "P2", "生命周期事件（可选）", ok, f"{len(lines)} 行样本")
    except json.JSONDecodeError as e:
        record("A12", "R7", "P2", "生命周期事件（可选）", False, f"JSON 解析失败: {e}")


def check_b1_readiness(proxy: str) -> None:
    code1, _, e1 = http_get(proxy + "/v1/models", timeout=2.0)
    code2, body2, e2, _ = get_status(proxy)
    ready = body2.get("ready") if isinstance(body2, dict) else None
    ok = code1 == 200 and (e1 + e2) < 2.0
    record("B1", "R1/R2", "P0", "就绪检查（S1）", ok,
           f"/v1/models={code1} {e1:.2f}s; status ready={ready}")


def check_b6_metrics(proxy: str) -> None:
    code, body, _ = http_get(proxy + "/metrics", timeout=2.0)
    ok = code == 200 and isinstance(body, dict) and "total" in body
    record("B6", "协议", "P0", "metrics 字段稳定（S7）", ok,
           f"→ {code}" + ("" if ok else "（缺 total 或端点异常）"))


# ── full 模式（变更操作/故障注入） ──────────────────────────────────

def check_d2_start_idempotent(manage: Path) -> None:
    codes = [run_manage(manage, "start", timeout=120)[0] for _ in range(2)]
    record("D2", "R3", "P1", "start 幂等（full）", all(c == 0 for c in codes), f"exits={codes}")


def check_d3_reload_idempotent(manage: Path) -> None:
    codes = [run_manage(manage, "reload", timeout=30)[0] for _ in range(2)]
    record("D3", "R3", "P1", "reload 幂等（full）", all(c == 0 for c in codes), f"exits={codes}")


def check_d1_lock_exclusion(manage: Path) -> None:
    lock = manage.parent / ".manage.lock"
    hold = subprocess.Popen(["bash", "-c", f"exec 9>{lock} && flock -x 9 && sleep 8"],
                            stdin=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        code, _, err, elapsed = run_manage(manage, "reload", timeout=15)
        ok = code != 0 and elapsed < 5.0
        record("D1", "R4", "P1", "变更锁互斥（full）", ok,
               f"exit={code} {elapsed:.1f}s" + ("" if ok else "（未互斥或等待过长）"))
    finally:
        hold.terminate()
        hold.wait(timeout=5)


def check_c1_backend_down(proxy: str, manage: Path) -> None:
    code0, _, _ = run_manage(manage, "stop-backend", timeout=60)
    time.sleep(1)
    _, body, _, _ = get_status(proxy)
    state = body.get("state") if isinstance(body, dict) else None
    ok = state == "backend_down"
    record("C1", "S3/S4", "P1", "故障注入：backend_down 诊断（full）", ok, f"state={state!r}")
    code1, _, _ = run_manage(manage, "start-backend", timeout=600)
    record("C1-recovery", "S4", "P1", "恢复：start-backend", code1 == 0, f"exit={code1}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    parser.add_argument("--manage-script",
                        default="/Users/jinsongwang/APP/llama.cpp/manage.sh")
    parser.add_argument("--full", action="store_true",
                        help="执行变更操作与故障注入用例（需无活跃 agent_go 任务）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    proxy = args.proxy_url.rstrip("/")
    manage = Path(args.manage_script)

    print(f"llama-defender 契约验收  proxy={proxy}  manage={manage}  mode={'FULL' if args.full else 'SAFE'}\n")

    if not manage.exists():
        print(f"manage.sh 不存在: {manage}")
        return 2

    check_a1_status_endpoint(proxy)
    check_a2_required_fields(proxy)
    check_a3_state_enum(proxy)
    check_a4_latency(proxy)
    check_a6_ready_semantics(proxy)
    check_a7_noninteractive(manage)
    check_a8_invalid_cmd_exit(manage)
    check_a9_readonly_idempotent(manage)
    check_a10_watchdog_status(manage)
    check_a11_profiles_endpoint(proxy)
    check_a12_lifecycle_events(manage)
    check_b1_readiness(proxy)
    check_b6_metrics(proxy)

    if args.full:
        print("\n── full 模式：变更/故障注入用例 ──")
        check_d2_start_idempotent(manage)
        check_d3_reload_idempotent(manage)
        check_d1_lock_exclusion(manage)
        check_c1_backend_down(proxy, manage)
    else:
        print("\n（safe 模式：C/D 组变更与故障注入用例跳过，加 --full 执行）")

    p0_fails = [r for r in results if r["status"] == "FAIL" and r["priority"] == "P0"]
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_skip = sum(1 for r in results if r["status"] == "SKIP")

    print(f"\n汇总: {n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP；P0 FAIL: {len(p0_fails)}")

    if args.json:
        print(json.dumps({"results": results,
                          "summary": {"pass": n_pass, "fail": n_fail, "skip": n_skip,
                                      "p0_fail": len(p0_fails)}}, ensure_ascii=False, indent=2))

    return 1 if p0_fails else 0


if __name__ == "__main__":
    sys.exit(main())
