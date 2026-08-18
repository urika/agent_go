"""M6.2 决策记录（decision log）测试。"""
from pathlib import Path

import pytest

import agent_go.config as cfg
import agent_go.decision_log as dl


@pytest.fixture
def log_env(tmp_path: Path, monkeypatch) -> Path:
    adir = tmp_path / "agent_go"
    adir.mkdir()
    (adir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "AGENT_GO_DIR", adir)
    monkeypatch.setattr(cfg, "CONFIG_PATH", adir / "config.json")
    monkeypatch.setattr(dl, "AGENT_GO_DIR", adir)
    return adir


class TestRecordDecision:
    def test_record_and_list(self, log_env):
        from agent_go.decision_log import record_decision, list_decisions, decision_count
        record_decision(change="router recommend --apply", source="router recommend --apply",
                        confirmer="cli", goal="hard 通过率≥95%", evidence_refs=["eval_suite/baselines/m4-mixB-hard"])
        record_decision(change="config 字段修改: worker_models", confirmer="web", source="config put")
        assert decision_count() == 2
        recs = list_decisions()
        assert len(recs) == 2
        assert recs[0]["change"] == "config 字段修改: worker_models"  # 最新在前
        assert recs[1]["source"] == "router recommend --apply"
        assert recs[1]["goal"] == "hard 通过率≥95%"
        assert recs[1]["evidence_refs"] == ["eval_suite/baselines/m4-mixB-hard"]
        assert recs[0]["_event_type"] == "decision"

    def test_empty_log(self, log_env):
        from agent_go.decision_log import list_decisions, decision_count
        assert list_decisions() == []
        assert decision_count() == 0

    def test_limit(self, log_env):
        from agent_go.decision_log import record_decision, list_decisions
        for i in range(5):
            record_decision(change=f"决策{i}", confirmer="cli")
        recs = list_decisions(limit=3)
        assert len(recs) == 3
        assert recs[0]["change"] == "决策4"  # 最新


class TestCliDecision:
    def test_cmd_decision_log(self, log_env, capsys):
        from agent_go.decision_log import record_decision
        record_decision(change="test change", source="cli", goal="目标X")
        import agent_go.cli as cli
        cli.cmd_decision(type("A", (), {"decision_subcommand": "log"})())
        out = capsys.readouterr().out
        assert "test change" in out
        assert "决策记录" in out

    def test_cmd_decision_empty(self, log_env, capsys):
        import agent_go.cli as cli
        cli.cmd_decision(type("A", (), {"decision_subcommand": "log"})())
        out = capsys.readouterr().out
        assert "暂无决策记录" in out
