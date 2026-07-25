"""桥接脚本：将历史 agent_go 任务目录转为 bench results.jsonl 格式。

用法：
  python eval_suite/migrate_history.py > eval_suite/results.jsonl

这会按 actual_model 分组，把每个历史任务的 subtask 结果转为 bench result 条目，
让 analyze_model_productivity 和 eval models 直接可用。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.config import AGENT_GO_DIR


def main():
    tasks = sorted(AGENT_GO_DIR.glob("task-*"), reverse=True)
    if not tasks:
        print("[]")
        return

    for td in tasks:
        meta_path = td / "meta.json"
        metering_path = td / "metering.jsonl"

        if not meta_path.exists() or not metering_path.exists():
            continue

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        results = meta.get("results", [])

        # 按 actual_model 聚合 metering
        model_events: dict[str, list[dict]] = {}
        for line in metering_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = ev.get("actual_model") or ev.get("virtual_model", "unknown")
            model_events.setdefault(model, []).append(ev)

        # 每个 model 产出一条 bench result
        for model, events in model_events.items():
            total_cost = sum(e.get("cost_usd", 0) or 0 for e in events)
            total_latency = sum(e.get("latency_ms", 0) or 0 for e in events)
            completed = sum(1 for r in results if r.get("status") == "completed")
            failed = sum(1 for r in results if r.get("status") == "failed")

            entry = {
                "task_id": td.name[:30],
                "model": model,
                "task_dir": str(td),
                "elapsed_sec": results[0].get("duration_sec", 0) if results else 0,
                "subprocess_exit": 0,
                "completed": completed,
                "failed": failed,
                "total_subtasks": len(results),
                "pass_rate": round(completed / len(results), 4) if results else 0,
                "all_verify_ok": all(r.get("verify_ok") for r in results if r.get("status") == "completed"),
                "total_retries": sum(r.get("retry_count", 0) for r in results),
                "total_cost_usd": round(total_cost, 6),
                "total_latency_ms": round(total_latency, 2),
                "dollar_per_pass": round(total_cost / completed, 6) if completed else None,
                "stderr_tail": "",
            }
            print(json.dumps(entry, ensure_ascii=False))


if __name__ == "__main__":
    main()
