"""种子数据生成器 — 为 agent_go eval 子系统提供开箱即用的演示数据。

设计原则：
  - 不影响核心功能：仅写入 ~/.agent_go/task-demo-* 目录（前缀独立，不与真实任务冲突）
  - 可重置：重复运行会覆盖旧数据
  - 数据真实感：模拟多模型（Sonnet 5 / DeepSeek / Haiku）+ 成功/失败/重试/阻断场景
  - 纯数据：零依赖核心模块（只写 JSON，不 import pipeline/executor）

用法：
  python eval_suite/init_demo_data.py          # 创建种子数据
  python eval_suite/init_demo_data.py --reset  # 重置重新生成
  python eval_suite/init_demo_data.py --clean  # 删除所有 demo 数据

生成后立即可用：
  agent_go eval cost        # 多模型成本分布
  agent_go eval reliability # 16 子任务可靠性分析
  agent_go eval gate        # $/pass 门禁判定
  agent_go eval models --results eval_suite/results.jsonl  # 需要先 migrate
"""

import json
import sys
import shutil
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.config import AGENT_GO_DIR

NOW = datetime.now().isoformat()
DEMO_PREFIX = "task-demo-"


# ═══════════════════════════════════════════════════════════════
# 场景定义：5 个任务模拟真实多模型混合执行
# ═══════════════════════════════════════════════════════════════

SCENARIOS = [
    {
        "task_id": "task-demo-001",
        "task": "新增用户认证 API（post /login /logout）",
        "status": "completed",
        "subtasks": [
            {"id": "sub-1", "title": "JWT 签发中间件", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "greywall", "duration": 45.0},
            {"id": "sub-2", "title": "登录接口实现", "status": "completed", "retry": 1,
             "verify_ok": True, "sandbox": "greywall", "duration": 92.0},
            {"id": "sub-3", "title": "登出 + token 黑名单", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "greywall", "duration": 38.0},
        ],
        "metering": [
            {"role": "planner", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "prompt_tokens": 2000, "completion_tokens": 1200, "cost_usd": 0.016},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "difficulty": "easy", "prompt_tokens": 3000, "completion_tokens": 2500, "cost_usd": 0.031, "subtask_id": "sub-1"},
            {"role": "worker", "actual_provider": "deepseek", "actual_model": "deepseek-chat",
             "difficulty": "medium", "prompt_tokens": 5000, "completion_tokens": 4000, "cost_usd": 0.001, "subtask_id": "sub-2"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "difficulty": "easy", "prompt_tokens": 2000, "completion_tokens": 1500, "cost_usd": 0.019, "subtask_id": "sub-3"},
        ],
    },
    {
        "task_id": "task-demo-002",
        "task": "重构数据库连接池为 async/await 模式",
        "status": "completed",
        "subtasks": [
            {"id": "sub-1", "title": "迁移连接池核心类", "status": "completed", "retry": 2,
             "verify_ok": True, "sandbox": "greywall", "duration": 210.0},
            {"id": "sub-2", "title": "更新所有 DAO 调用方", "status": "completed", "retry": 1,
             "verify_ok": True, "sandbox": "greywall", "duration": 145.0},
            {"id": "sub-3", "title": "添加连接超时兜底", "status": "no_changes", "retry": 0,
             "verify_ok": True, "sandbox": "greywall", "duration": 12.0},
        ],
        "metering": [
            {"role": "planner", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "prompt_tokens": 3500, "completion_tokens": 2000, "cost_usd": 0.027},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-opus-4-8",
             "difficulty": "hard", "prompt_tokens": 8000, "completion_tokens": 6000, "cost_usd": 0.190, "subtask_id": "sub-1"},
            {"role": "worker", "actual_provider": "deepseek", "actual_model": "deepseek-chat",
             "difficulty": "medium", "prompt_tokens": 6000, "completion_tokens": 5000, "cost_usd": 0.002, "subtask_id": "sub-2"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-haiku-4-5",
             "difficulty": "easy", "prompt_tokens": 1500, "completion_tokens": 800, "cost_usd": 0.005, "subtask_id": "sub-3"},
        ],
    },
    {
        "task_id": "task-demo-003",
        "task": "实现文件上传 API（S3 直传 + 本地回退）",
        "status": "failed",
        "subtasks": [
            {"id": "sub-1", "title": "S3 签名生成服务", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "greywall", "duration": 55.0},
            {"id": "sub-2", "title": "本地存储回退实现", "status": "failed", "retry": 3,
             "verify_ok": False, "sandbox": "greywall", "duration": 320.0},
            {"id": "sub-3", "title": "文件校验与缩略图", "status": "blocked", "retry": 0,
             "verify_ok": False, "sandbox": "greywall", "duration": 0.0},
        ],
        "metering": [
            {"role": "planner", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "prompt_tokens": 2500, "completion_tokens": 1800, "cost_usd": 0.023},
            {"role": "worker", "actual_provider": "deepseek", "actual_model": "deepseek-chat",
             "difficulty": "medium", "prompt_tokens": 4000, "completion_tokens": 3000, "cost_usd": 0.001, "subtask_id": "sub-1"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "difficulty": "hard", "prompt_tokens": 6000, "completion_tokens": 5000, "cost_usd": 0.065, "subtask_id": "sub-2", "note": "attempt 1"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "difficulty": "hard", "prompt_tokens": 4000, "completion_tokens": 3000, "cost_usd": 0.039, "subtask_id": "sub-2", "note": "attempt 2"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-opus-4-8",
             "difficulty": "hard", "prompt_tokens": 5000, "completion_tokens": 4000, "cost_usd": 0.125, "subtask_id": "sub-2", "note": "attempt 3", "fallback_reason": "primary_timeout"},
        ],
    },
    {
        "task_id": "task-demo-004",
        "task": "添加请求日志中间件 + Prometheus metrics",
        "status": "completed",
        "subtasks": [
            {"id": "sub-1", "title": "日志中间件实现", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "greywall", "duration": 28.0},
            {"id": "sub-2", "title": "Prometheus metrics 端点", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "greywall", "duration": 35.0},
            {"id": "sub-3", "title": "Grafana dashboard JSON", "status": "completed", "retry": 1,
             "verify_ok": True, "sandbox": "greywall", "duration": 55.0},
        ],
        "metering": [
            {"role": "planner", "actual_provider": "anthropic", "actual_model": "claude-haiku-4-5",
             "prompt_tokens": 1800, "completion_tokens": 1000, "cost_usd": 0.006},
            {"role": "worker", "actual_provider": "deepseek", "actual_model": "deepseek-chat",
             "difficulty": "easy", "prompt_tokens": 2000, "completion_tokens": 1500, "cost_usd": 0.0005, "subtask_id": "sub-1"},
            {"role": "worker", "actual_provider": "deepseek", "actual_model": "deepseek-chat",
             "difficulty": "easy", "prompt_tokens": 2500, "completion_tokens": 2000, "cost_usd": 0.0007, "subtask_id": "sub-2"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "difficulty": "medium", "prompt_tokens": 3000, "completion_tokens": 2500, "cost_usd": 0.031, "subtask_id": "sub-3"},
        ],
    },
    {
        "task_id": "task-demo-005",
        "task": "前端组件库升级 React 19 → React 19.5",
        "status": "completed",
        "subtasks": [
            {"id": "sub-1", "title": "升级 package.json + 锁定版本", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "native", "duration": 18.0},
            {"id": "sub-2", "title": "修复 breaking changes（3 个组件）", "status": "completed", "retry": 2,
             "verify_ok": True, "sandbox": "native", "duration": 180.0},
            {"id": "sub-3", "title": "更新 SSR 配置", "status": "completed", "retry": 0,
             "verify_ok": True, "sandbox": "native", "duration": 42.0},
            {"id": "sub-4", "title": "跑全量端到端测试", "status": "completed", "retry": 1,
             "verify_ok": True, "sandbox": "native", "duration": 90.0},
        ],
        "metering": [
            {"role": "planner", "actual_provider": "google", "actual_model": "gemini-2.5-pro",
             "prompt_tokens": 2200, "completion_tokens": 1500, "cost_usd": 0.018},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-sonnet-5",
             "difficulty": "easy", "prompt_tokens": 1000, "completion_tokens": 500, "cost_usd": 0.006, "subtask_id": "sub-1"},
            {"role": "worker", "actual_provider": "anthropic", "actual_model": "claude-opus-4-8",
             "difficulty": "hard", "prompt_tokens": 7000, "completion_tokens": 5500, "cost_usd": 0.173, "subtask_id": "sub-2"},
            {"role": "worker", "actual_provider": "openai", "actual_model": "gpt-4.1",
             "difficulty": "medium", "prompt_tokens": 2500, "completion_tokens": 2000, "cost_usd": 0.021, "subtask_id": "sub-3"},
            {"role": "worker", "actual_provider": "deepseek", "actual_model": "deepseek-chat",
             "difficulty": "easy", "prompt_tokens": 1500, "completion_tokens": 1000, "cost_usd": 0.0004, "subtask_id": "sub-4"},
        ],
    },
]


# ═══════════════════════════════════════════════════════════════
# 写入函数
# ═══════════════════════════════════════════════════════════════

def _write_task(base_dir: Path, scenario: dict) -> Path:
    td = base_dir / scenario["task_id"]
    if td.exists():
        shutil.rmtree(td)
    td.mkdir(parents=True)

    # meta.json
    results = []
    for st in scenario["subtasks"]:
        results.append({
            "subtask_id": st["id"],
            "status": st["status"],
            "retry_count": st["retry"],
            "verify_ok": st["verify_ok"],
            "sandbox_type": st["sandbox"],
            "duration_sec": st["duration"],
            "summary": f"{st['title']} ({st['status']})",
            "timing": {"claude_execute_ms": int(st["duration"] * 1000)},
            "verification_results": [{"command": "pytest", "exit_code": 0 if st["verify_ok"] else 1, "duration_ms": 500}],
            "verification_confidence": {"level": "medium", "warning": ""},
        })
    meta = {
        "task_id": scenario["task_id"],
        "task": scenario["task"],
        "status": scenario["status"],
        "subtasks": [{"id": st["id"], "title": st["title"]} for st in scenario["subtasks"]],
        "results": results,
        "created": NOW,
        "base_branch": "main",
    }
    (td / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # metering.jsonl
    lines = []
    for ev in scenario["metering"]:
        ev["result"] = ev.get("result", "success")
        ev["fallback_reason"] = ev.get("fallback_reason", "")
        ev["latency_ms"] = ev.get("latency_ms", 3000)
        ev["task_id"] = scenario["task_id"]
        ev.setdefault("subtask_id", "")
        ev.setdefault("difficulty", "")
        lines.append(json.dumps(ev, ensure_ascii=False))
    (td / "metering.jsonl").write_text("\n".join(lines), encoding="utf-8")

    # execution.log（最小）
    (td / "execution.log").write_text(
        json.dumps({"timestamp": NOW, "event": "subtask_headless_start", "id": "sub-1"}) + "\n",
        encoding="utf-8")

    return td


def cmd_init(reset: bool = False):
    """创建种子数据。"""
    existing = sorted(AGENT_GO_DIR.glob(f"{DEMO_PREFIX}*"))
    if existing and not reset:
        print(f"📦 种子数据已存在 ({len(existing)} 任务)。用 --reset 重新生成。")
        print(f"   目录: {AGENT_GO_DIR}/task-demo-*")
        return

    if reset and existing:
        for td in existing:
            shutil.rmtree(td)
        print(f"🗑️  已清除 {len(existing)} 个旧种子任务")

    AGENT_GO_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for s in SCENARIOS:
        td = _write_task(AGENT_GO_DIR, s)
        created.append(td.name)

    # 统计
    total_cost = sum(
        sum(ev["cost_usd"] for ev in s["metering"]) for s in SCENARIOS)
    total_subtasks = sum(len(s["subtasks"]) for s in SCENARIOS)
    completed = sum(
        sum(1 for st in s["subtasks"] if st["status"] == "completed") for s in SCENARIOS)
    failed = sum(
        sum(1 for st in s["subtasks"] if st["status"] == "failed") for s in SCENARIOS)
    blocked = sum(
        sum(1 for st in s["subtasks"] if st["status"] == "blocked") for s in SCENARIOS)

    print(f"\n✅ 种子数据已生成: {len(created)} 任务, {total_subtasks} 子任务")
    print(f"   completed: {completed}  |  failed: {failed}  |  blocked: {blocked}")
    print(f"   累计成本:  ${total_cost:.4f}")
    print(f"   估计 $/pass: ${total_cost / max(completed, 1):.4f}")
    print(f"\n现在可以运行:")
    print(f"   agent_go eval cost")
    print(f"   agent_go eval reliability")
    print(f"   agent_go eval gate")
    print(f"   agent_go eval gate --check-regression")
    print(f"\n   提示：真实任务数据不受影响（种子前缀 {DEMO_PREFIX}*，与真实 task-* 隔离）")


def cmd_clean():
    """删除所有种子数据。"""
    existing = sorted(AGENT_GO_DIR.glob(f"{DEMO_PREFIX}*"))
    if not existing:
        print("📦 无种子数据")
        return
    for td in existing:
        shutil.rmtree(td)
    print(f"🗑️  已删除 {len(existing)} 个种子任务")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        cmd_clean()
    else:
        cmd_init(reset="--reset" in sys.argv)
