"""AG-1 共享契约包测试：llama_contracts 结构 + 漂移检测脚本。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_go.llama_contracts import (
    CONTRACT_SOURCE,
    CONTRACT_VERSION,
    EscalationDecision,
    SignalSnapshot,
    build_escalation,
    build_signal_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIFT_SCRIPT = REPO_ROOT / "tools" / "check_llama_contracts.py"


class TestSignalSnapshot:
    def test_defaults_fail_open(self):
        snap = build_signal_snapshot()
        assert isinstance(snap, SignalSnapshot)
        assert snap["contract_version"] == CONTRACT_VERSION
        assert snap["reread_pressure"] == 0
        assert snap["ile"] is False
        assert snap["ile_kinds"] == []
        assert snap["h_be"] is None

    def test_json_serializable(self):
        snap = build_signal_snapshot(reread_pressure=2, session_key="s1", turn=5)
        roundtrip = json.loads(json.dumps(snap))
        assert roundtrip["reread_pressure"] == 2
        assert roundtrip["session_key"] == "s1"
        assert roundtrip["turn"] == 5


class TestEscalationDecision:
    def test_build_escalation(self):
        d = build_escalation(task_id="T1", action="retry", reason="test_failed",
                             attempt_count=1, signals={"reread_pressure": 0})
        assert isinstance(d, EscalationDecision)
        assert d["contract_version"] == CONTRACT_VERSION
        assert d["task_id"] == "T1"
        assert d["action"] == "retry"
        assert d["reason"] == "test_failed"
        assert d["attempt_count"] == 1
        assert d["signals_at_decision"] == {"reread_pressure": 0}
        assert d["refined_task"] is None

    def test_json_serializable(self):
        d = build_escalation("T1", "human", "max_retries_exceeded")
        assert json.loads(json.dumps(d))["action"] == "human"


class TestContractMeta:
    def test_source_version_consistent(self):
        assert CONTRACT_SOURCE["contract_version"] == CONTRACT_VERSION
        assert CONTRACT_SOURCE["files"] == ["signal_types.py", "protocol_types.py"]


# ═══════════════════════════════════════════════════════════════
# 漂移检测脚本（hermetic：用 tmp_path 伪造上游仓库）
# ═══════════════════════════════════════════════════════════════

_UPSTREAM_SIGNAL = '''CONTRACT_VERSION = {ver}
def build_signal_snapshot(h_be=None, h_be_trend=None, d_ledger=None,
                          retention=None, rationale_ratio=None, ile=False,
                          ile_kinds=None, view_reset=False, reread_pressure=0,
                          action_diversity=None, cognitive_load=0.0,
                          config_fingerprint="", session_key="", turn=0):
    pass
'''

_UPSTREAM_PROTOCOL = '''CONTRACT_VERSION = {ver}
def build_escalation(task_id, action, reason, attempt_count=0, signals=None, refined_task=None):
    pass
'''


def _make_fake_repo(tmp_path: Path, version: int = 1) -> Path:
    (tmp_path / "signal_types.py").write_text(_UPSTREAM_SIGNAL.format(ver=version))
    (tmp_path / "protocol_types.py").write_text(_UPSTREAM_PROTOCOL.format(ver=version))
    return tmp_path


def _run_drift(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "--repo", str(repo)],
        capture_output=True, text=True)


class TestDriftChecker:
    def test_match_passes(self, tmp_path):
        repo = _make_fake_repo(tmp_path, version=CONTRACT_VERSION)
        r = _run_drift(repo)
        assert r.returncode == 0
        assert "OK" in r.stdout

    def test_version_drift_fails(self, tmp_path):
        repo = _make_fake_repo(tmp_path, version=CONTRACT_VERSION + 1)
        r = _run_drift(repo)
        assert r.returncode == 1
        assert "CONTRACT_VERSION 漂移" in r.stdout

    def test_signature_drift_fails(self, tmp_path):
        repo = _make_fake_repo(tmp_path, version=CONTRACT_VERSION)
        # 篡改上游签名：新增参数
        (repo / "signal_types.py").write_text(
            _UPSTREAM_SIGNAL.format(ver=CONTRACT_VERSION).replace(
                "reread_pressure=0", "reread_pressure=0, new_field=None"))
        r = _run_drift(repo)
        assert r.returncode == 1
        assert "签名漂移" in r.stdout

    def test_missing_repo_skips(self, tmp_path):
        r = _run_drift(tmp_path / "nonexistent")
        assert r.returncode == 0
        assert "SKIP" in r.stdout

    @pytest.mark.skipif(
        not (Path.home() / "APP" / "llama.cpp" / "signal_types.py").is_file(),
        reason="本机无 llama-defender 仓库")
    def test_real_repo_no_drift(self):
        r = subprocess.run([sys.executable, str(DRIFT_SCRIPT)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
