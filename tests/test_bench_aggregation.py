from pathlib import Path

from agent_go.bench import analyze_model_productivity


def test_stress_records_are_reported_separately(tmp_path: Path):
    path = tmp_path / "results.jsonl"
    common = {
        "model": "m1", "completed": 1, "total_subtasks": 1,
        "pass_rate": 1.0, "total_cost_usd": 1.0, "total_retries": 0,
    }
    path.write_text(
        "\n".join([
            str({**common, "task_id": "core"}).replace("'", '"'),
            str({**common, "task_id": "stress", "suite": "stress", "total_cost_usd": 10.0}).replace("'", '"'),
        ]) + "\n",
        encoding="utf-8",
    )
    result = analyze_model_productivity(path)
    assert result["ordinary_runs"] == 1
    assert result["stress_runs"] == 1
    assert result["frozen_metrics"]["valid_cost_usd"] == 1.0
    assert result["stress_metrics"]["valid_cost_usd"] == 10.0
