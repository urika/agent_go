#!/usr/bin/env python3
"""llama-defender Phase 0 缓存诊断（上下文工程设计 §5 Phase 0，2026-09-06）。

三件事：
  E1 analyze     被动解析 rapid-mlx 引擎日志，量化每请求 prefill 增量占比
                 （n_tokens/n_past 同构指标 = tokens_to_prefill/prompt_tokens）。
                 零干扰——批跑运行期间也可执行。
  E2 probe       绕过代理直发 append-only 序列到后端（隔离对照：验证引擎前缀
                 缓存本身可用）。会在 KV 缓存中产生新条目并可能在 Metal 压力下
                 驱逐在跑会话的缓存——**批跑/实验运行期间禁止**（日志活跃自动拒跑）。
  E3 probe-swa   固定前缀 + 变尾 ×N（前缀缓存语义验证，§4.8 SWA ⚠️ 项）。
                 同样受活跃守卫限制。

用法:
  python3 tools/phase0_cache_diag.py analyze <llama-server.log> [--json OUT] [--last N]
  python3 tools/phase0_cache_diag.py probe --url http://127.0.0.1:8081 --model M [--turns 8] [--force]
  python3 tools/phase0_cache_diag.py probe-swa --url http://127.0.0.1:8081 --model M [--rounds 10] [--force]

验收口径（设计文档 §5 Phase 0）：
  运行态增量占比中位数 < 0.1 → 缓存健康（线性成本成立）
  ≈ 1.0                     → 缓存击穿坐实（改写是元凶）
  混合                       → 看 MISS 事件 wasted tokens 占比定位击穿源
"""
import argparse
import json
import os
import random
import re
import string
import sys
import time
import urllib.request

# ── rapid-mlx 日志行模式（2026-09-06 实测校准）─────────────────────────────
# [schedule] request=2c842c34-98e uid=882 prompt_tokens=51449 tokens_to_prefill=65, 51384 cached max_tokens=32000 ...
# [schedule] request=83da7ce8-0c4 uid=883 prompt_tokens=41108 tokens_to_prefill=41108 max_tokens=160 ...
RE_SCHEDULE = re.compile(
    r"\[schedule\] request=(\S+) uid=(\d+) prompt_tokens=(\d+) tokens_to_prefill=(\d+)(?:, (\d+) cached)?")
# [cache_fetch] request=... HIT prompt_tokens=51449 cached=51384 remaining=65 time=0.005s
# [cache_fetch] request=... MISS prompt_tokens=41108 time=0.006s entries=14
RE_FETCH = re.compile(
    r"\[cache_fetch\] request=(\S+) (HIT|MISS) prompt_tokens=(\d+)(?: cached=(\d+))?")
# LCP unavailable: shared=40797 entry_len=41004 requested_len=41108 non_trimmable=True
RE_LCP = re.compile(
    r"LCP unavailable: shared=(\d+) entry_len=(\d+) requested_len=(\d+) non_trimmable=(\w+)")
# [prefix-pressure-evict] evicted 3 entries under Metal pressure (metal_cap=28.1GB, cache_max=7.7GB)
RE_EVICT = re.compile(r"prefix-pressure-evict\] evicted (\d+) entries")
# [REQUEST] POST /v1/chat/completions ... msgs=69 ... total_chars=122218 ...
RE_REQ = re.compile(r"\[REQUEST\] POST /v1/chat/completions.*?msgs=(\d+).*?total_chars=(\d+)")
# [cache_store] request=... tokens=51706 (51449 prompt + 257 output) stored=True ...
RE_STORE = re.compile(r"\[cache_store\] request=(\S+) tokens=(\d+) \((\d+) prompt \+ (\d+) output\) stored=(\w+)")
RE_FIRST_TOKEN = re.compile(r"first token after ([\d.]+)s")


def analyze(log_path: str, json_out: str = "", last_n: int = 200,
            from_offset: int = 0, from_marker: str = "") -> int:
    reqs = []          # per-request: dict(req, uid, prompt, prefill, cached, ratio)
    lcp_events = []    # (shared, entry_len, requested_len, non_trimmable)
    evictions = []     # evicted entry counts
    ctx_sizes = []     # (msgs, total_chars) per [REQUEST]
    ttfts = []         # first token latency (s)
    stores = 0
    warmups = []       # 每会话/每前缀族首个请求（验收口径排除项，近似：连续 MISS 后首个 HIT/大 prompt）
    started = False

    with open(log_path, "r", errors="replace") as f:
        pos = 0
        for line in f:
            line_offset = pos
            pos += len(line.encode("utf-8", errors="replace"))
            # 窗口定位：--from-offset 字节起点 / --from-marker 首次出现行之后
            if not started:
                if from_offset and line_offset >= from_offset:
                    started = True
                elif from_marker and from_marker in line:
                    started = True
                elif not from_offset and not from_marker:
                    started = True
                else:
                    continue
            m = RE_SCHEDULE.search(line)
            if m:
                prompt = int(m.group(3))
                prefill = int(m.group(4))
                cached = int(m.group(5)) if m.group(5) else prompt - prefill
                reqs.append({
                    "req": m.group(1), "uid": int(m.group(2)),
                    "prompt": prompt, "prefill": prefill, "cached": cached,
                    "ratio": (prefill / prompt) if prompt else 0.0,
                })
                continue
            m = RE_LCP.search(line)
            if m:
                lcp_events.append((int(m.group(1)), int(m.group(2)),
                                   int(m.group(3)), m.group(4) == "True"))
                continue
            m = RE_EVICT.search(line)
            if m:
                evictions.append(int(m.group(1)))
                continue
            m = RE_REQ.search(line)
            if m:
                ctx_sizes.append((int(m.group(1)), int(m.group(2))))
                continue
            m = RE_FIRST_TOKEN.search(line)
            if m:
                ttfts.append(float(m.group(1)))
                continue
            if RE_STORE.search(line):
                stores += 1

    if not reqs:
        print("未解析到 [schedule] 行——日志格式不匹配或文件为空")
        return 1

    def pct(vals, p):
        if not vals:
            return 0
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * p / 100))]

    total_prompt = sum(r["prompt"] for r in reqs)
    total_prefill = sum(r["prefill"] for r in reqs)
    incremental = 1 - total_prefill / total_prompt if total_prompt else 0
    hits = [r for r in reqs if r["cached"] > 0]
    misses = [r for r in reqs if r["cached"] == 0]
    miss_prompt = sum(r["prompt"] for r in misses)
    lcp_miss_prompt = sum(e[2] for e in lcp_events)  # LCP unavailable 的整段重算
    lcp_shared = sum(e[0] for e in lcp_events)       # 其中本可复用的部分
    ratios = [r["ratio"] for r in reqs]
    recent = reqs[-last_n:]

    print(f"=== E1 现状诊断：{log_path} ===")
    print(f"请求数                 {len(reqs)}（HIT {len(hits)} / MISS {len(misses)}，"
          f"请求级命中率 {len(hits)/len(reqs)*100:.1f}%）")
    print(f"prompt tokens 总量     {total_prompt:,}")
    print(f"实算 prefill tokens    {total_prefill:,}")
    print(f"增量占比（整体）        {total_prefill/total_prompt:.4f}  "
          f"← 核心指标（<0.1 线性健康 / ≈1.0 击穿）")
    print(f"ratio 分布 P50/P90/P95 {pct(ratios,50):.3f} / {pct(ratios,90):.3f} / {pct(ratios,95):.3f}")
    b = {"<=0.1": 0, "0.1-0.5": 0, "0.5-0.9": 0, ">0.9": 0}
    for r in ratios:
        b["<=0.1" if r <= 0.1 else "0.1-0.5" if r <= 0.5 else "0.5-0.9" if r <= 0.9 else ">0.9"] += 1
    print(f"ratio 桶               {b}")
    print(f"\n── 击穿源分解 ──")
    print(f"LCP unavailable        {len(lcp_events)} 次，整段重算 {lcp_miss_prompt:,} tokens，"
          f"其中本可复用（shared）{lcp_shared:,}（{(lcp_shared/max(1,lcp_miss_prompt))*100:.1f}%）")
    print(f"  → 非 trimmable 的 MISS：条目与请求在消息内部发散时整条作废，"
          f"99%+ 相同也全量重算——Phase 1 的头号优化目标")
    print(f"压力驱逐事件           {len(evictions)} 次，驱逐条目 {sum(evictions)} 个"
          f"（Metal 压力下缓存条目被挤出，hit 变 miss 的第二来源）")
    print(f"cache_store 成功       {stores} 次")
    if ctx_sizes:
        chars = [c for _, c in ctx_sizes]
        msgs = [m for m, _ in ctx_sizes]
        print(f"\n── 上下文增长（{len(ctx_sizes)} 个 REQUEST）──")
        print(f"total_chars P50/P95/max {pct(chars,50):,} / {pct(chars,95):,} / {max(chars):,}")
        print(f"msgs P50/P95/max        {pct(msgs,50)} / {pct(msgs,95)} / {max(msgs)}")
    if ttfts:
        print(f"首 token 延迟 P50/P95   {pct(ttfts,50):.1f}s / {pct(ttfts,95):.1f}s")
    if len(recent) >= 10:
        rp = sum(r["prompt"] for r in recent)
        rf = sum(r["prefill"] for r in recent)
        print(f"\n── 最近 {len(recent)} 请求窗口 ──")
        print(f"增量占比               {rf/rp:.4f}（对比全量历史 {total_prefill/total_prompt:.4f}）")

    verdict = ("缓存击穿坐实（改写/发散是元凶）" if total_prefill / total_prompt > 0.5 else
               "混合态：命中时增量健康，MISS 事件（LCP unavailable / 驱逐）造成整段全量重算"
               if total_prefill / total_prompt > 0.1 else
               "缓存健康（线性成本成立）")
    print(f"\n判定：{verdict}")

    if json_out:
        with open(json_out, "w") as f:
            json.dump({
                "schema": "phase0-e1/v1", "log": log_path,
                "requests": len(reqs), "hits": len(hits), "misses": len(misses),
                "total_prompt_tokens": total_prompt, "total_prefill_tokens": total_prefill,
                "incremental_ratio": round(total_prefill / total_prompt, 5),
                "ratio_p50_p90_p95": [round(pct(ratios, p), 4) for p in (50, 90, 95)],
                "ratio_buckets": b,
                "lcp_events": len(lcp_events), "lcp_miss_tokens": lcp_miss_prompt,
                "lcp_shared_tokens": lcp_shared,
                "evict_events": len(evictions), "evicted_entries": sum(evictions),
                "cache_stores": stores,
                "ctx_chars_p50_p95_max": [pct(chars, 50), pct(chars, 95), max(chars)] if ctx_sizes else [],
                "ttft_p50_p95": [round(pct(ttfts, 50), 1), round(pct(ttfts, 95), 1)] if ttfts else [],
                "recent_window": {"n": len(recent),
                                  "incremental_ratio": round(rf / rp, 5) if rp else None},
                "verdict": verdict,
            }, f, ensure_ascii=False, indent=2)
        print(f"JSON 已写入 {json_out}")
    return 0


# ── 主动探针（E2/E3）：批跑运行期禁用 ──────────────────────────────────────

# 真实推理事件的日志标记（区别于 GET /v1/models 健康轮询与未匹配路由的 4xx）
_INFERENCE_MARKERS = ("[schedule]", "[REQUEST] POST", "[cache_fetch]", "prompt_cache_save")


def _last_inference_marker_pos(log_path: str) -> int:
    """返回日志中最后一条推理标记行的起始字节位置（无则 -1）。"""
    size = os.path.getsize(log_path)
    offset = max(0, size - 2_000_000)
    pos = -1
    with open(log_path, "rb") as fh:
        fh.seek(offset)
        acc = 0
        for line in fh.read().split(b"\n"):
            if any(m.encode() in line for m in _INFERENCE_MARKERS):
                pos = offset + acc
            acc += len(line) + 1
    return pos


def _busy_guard(log_path: str, force: bool, quiet_sec: float = 90.0) -> bool:
    """双采样静默检查：采样间隔内无新增推理事件 → 视为空闲。

    不能用日志 mtime 判断——健康轮询（GET /v1/models）与未匹配路由的 4xx
    轰炸都会持续刷新 mtime，令守卫永远关闭（2026-09-06 实测）。
    探针会在 Metal 压力下驱逐在跑会话的 KV 缓存条目，因此只在确认
    「quiet_sec 窗口内零推理事件」后放行。
    """
    if force:
        print("⚠️ --force：跳过活跃守卫（请确认批跑/实验未在运行）")
        return True
    if not os.path.exists(log_path):
        print(f"拒绝：引擎日志不存在 {log_path}")
        return False
    size0 = os.path.getsize(log_path)
    pos0 = _last_inference_marker_pos(log_path)
    print(f"静默采样 {quiet_sec:.0f}s×2（确认窗口内零推理事件）…")
    for _ in range(2):
        time.sleep(quiet_sec)
        size1 = os.path.getsize(log_path)
        pos1 = _last_inference_marker_pos(log_path)
        # 窗口内出现新推理事件（标记位置推进超过采样前文件大小）→ 不空闲
        if pos1 >= size0 and pos1 > pos0:
            print(f"拒绝：采样窗口内检测到新推理事件（标记 @ {pos1}，采样前大小 {size0}）。"
                  f"探针会驱逐在跑会话的 KV 缓存条目。确认空闲后重试或 --force。")
            return False
        size0 = size1
    print("✅ 静默确认（窗口内零推理事件）")
    return True


def _post_chat(url: str, model: str, messages: list, max_tokens: int = 8) -> dict:
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens, "stream": False,
                       "temperature": 0}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    data["_wall_s"] = round(time.time() - t0, 2)
    return data


def _parse_new_cache_lines(log_path: str, offset: int) -> list:
    """探针结束后回读日志新增段，提取本批请求的 cache_fetch/schedule 行。"""
    out = []
    with open(log_path, "r", errors="replace") as f:
        f.seek(offset)
        for line in f:
            m = RE_SCHEDULE.search(line)
            if m:
                out.append({"kind": "schedule", "req": m.group(1),
                            "prompt": int(m.group(3)), "prefill": int(m.group(4)),
                            "cached": int(m.group(5)) if m.group(5) else 0})
                continue
            m = RE_FETCH.search(line)
            if m:
                out.append({"kind": "fetch", "req": m.group(1), "hit": m.group(2) == "HIT",
                            "prompt": int(m.group(3)),
                            "cached": int(m.group(4)) if m.group(4) else 0})
    return out


def _filler(tokens_approx: int) -> str:
    """生成确定性填充文本（每行 ~12 词，行数按粗略 token 目标换算；内容稳定）。"""
    rng = random.Random(42)
    line = " ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(5))
                    for _ in range(12))
    lines_needed = max(1, tokens_approx // 15)
    return "\n".join(line for _ in range(lines_needed))


def probe(args) -> int:
    """E2：append-only 直发对照——引擎前缀缓存本身是否可用。"""
    if not _busy_guard(args.log, args.force):
        return 2
    url = args.url.rstrip("/") + "/v1/chat/completions"
    messages = [{"role": "system", "content": _filler(args.prefix_tokens)},
                {"role": "user", "content": "turn 1: reply with the single word OK"}]
    offset = os.path.getsize(args.log)
    print(f"E2 append-only：{args.turns} 轮，前缀 ~{args.prefix_tokens} tokens，直发 {url}")
    for i in range(1, args.turns + 1):
        r = _post_chat(url, args.model, messages)
        u = r.get("usage", {})
        print(f"  turn {i}: prompt_tokens={u.get('prompt_tokens')} "
              f"wall={r['_wall_s']}s")
        messages.append({"role": "assistant", "content": "OK"})
        messages.append({"role": "user",
                         "content": f"turn {i+1}: reply with the single word OK"})
    time.sleep(1)
    lines = _parse_new_cache_lines(args.log, offset)
    scheds = [l for l in lines if l["kind"] == "schedule"]
    print("\n引擎侧缓存行为（本轮探针请求）：")
    for s in scheds:
        ratio = s["prefill"] / s["prompt"] if s["prompt"] else 0
        print(f"  {s['req']}: prompt={s['prompt']} prefill={s['prefill']} "
              f"cached={s['cached']} ratio={ratio:.4f}")
    inc = [s for s in scheds[1:] if s["prompt"]]
    if inc:
        med = sorted(s["prefill"] / s["prompt"] for s in inc)[len(inc) // 2]
        print(f"\n判定：turn≥2 增量占比中位数 {med:.4f} → "
              + ("引擎前缀缓存可用（击穿源在代理层）✅" if med < 0.2
                 else "引擎缓存本身不可用/语义异常 ❌"))
    return 0


def probe_swa(args) -> int:
    """E3：固定前缀 + 变尾——前缀缓存语义验证（SWA/架构异常检测）。"""
    if not _busy_guard(args.log, args.force):
        return 2
    url = args.url.rstrip("/") + "/v1/chat/completions"
    base = [{"role": "system", "content": _filler(args.prefix_tokens)},
            {"role": "user", "content": "Remember the number 47. Reply OK."}]
    messages = base + [{"role": "assistant", "content": "OK"}]
    offset = os.path.getsize(args.log)
    print(f"E3 SWA 验证：固定前缀 ~{args.prefix_tokens} tokens + 变尾 ×{args.rounds}，直发 {url}")
    for i in range(1, args.rounds + 1):
        probe_msgs = messages + [{"role": "user",
                                  "content": f"probe {i} nonce {os.urandom(4).hex()}: reply OK"}]
        r = _post_chat(url, args.model, probe_msgs)
        print(f"  round {i}: prompt_tokens={r.get('usage', {}).get('prompt_tokens')} wall={r['_wall_s']}s")
    time.sleep(1)
    lines = _parse_new_cache_lines(args.log, offset)
    scheds = [l for l in lines if l["kind"] == "schedule"]
    print("\n引擎侧缓存行为：")
    misses = partials = snaps = 0
    for s in scheds:
        ratio = s["prefill"] / s["prompt"] if s["prompt"] else 0
        tag = "FULL-MISS" if ratio > 0.9 else "PARTIAL" if ratio > 0.1 else "HIT"
        print(f"  {s['req']}: prompt={s['prompt']} prefill={s['prefill']} "
              f"cached={s['cached']} ratio={ratio:.4f} [{tag}]")
        if ratio > 0.9:
            misses += 1
        elif ratio > 0.1:
            partials += 1
        else:
            snaps += 1
    print(f"\n判定：全量 MISS {misses} / 部分重算 {partials} / 高复用 {snaps} → "
          + ("前缀缓存语义正常 ✅（部分重算=SWA 异常信号，未出现）" if partials == 0
             else "存在部分前缀重算（SWA/架构缓存异常信号，需人工复核）⚠️"))
    if misses:
        print("  注：FULL-MISS 轮为候选选择落在无检查点条目（多探针混合条目"
              "边缘形态，P1b 段链去重的系统性解法范畴），非缓存语义异常。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="E1：被动解析引擎日志（零干扰）")
    p.add_argument("log", nargs="?",
                   default="/Users/jinsongwang/APP/llama.cpp/logs/llama-server.log")
    p.add_argument("--json", default="", help="结果 JSON 输出路径")
    p.add_argument("--last", type=int, default=200, help="最近窗口请求数")
    p.add_argument("--from-offset", type=int, default=0,
                   help="只分析该字节偏移之后（Gate B1 验收：轮次起点的日志位置）")
    p.add_argument("--from-marker", default="",
                   help="只分析该子串首次出现行之后（如 --from-marker 'LCP snapdown'）")
    p.set_defaults(fn=lambda a: analyze(a.log, a.json, a.last,
                                        a.from_offset, a.from_marker))

    for name, fn in (("probe", probe), ("probe-swa", probe_swa)):
        p = sub.add_parser(name, help=fn.__doc__.split("——")[0])
        p.add_argument("--url", default="http://127.0.0.1:8081")
        p.add_argument("--model", default="pyros-vault/Ornith-1.5-35B-A3B-oQ4e-fixed-mtp")
        p.add_argument("--log", default="/Users/jinsongwang/APP/llama.cpp/logs/llama-server.log")
        p.add_argument("--force", action="store_true", help="跳过活跃守卫")
        if name == "probe":
            p.add_argument("--turns", type=int, default=8)
            p.add_argument("--prefix-tokens", type=int, default=2000)
        else:
            p.add_argument("--rounds", type=int, default=10)
            p.add_argument("--prefix-tokens", type=int, default=2000)
        p.set_defaults(fn=fn)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
