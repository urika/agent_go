"""验收测试（spec-to-test）CLI/调度调用面的覆盖补充（近期提交 471163d/e4e5785）。

补的是 `cli.py` 人审门辅助函数与 `eval acceptance` 聚合入口的未覆盖分支；
不重复 `test_spec_test.py` / `test_cmd_eval.py` 的既有覆盖。
"""

import argparse
import json
from pathlib import Path

import pytest

from agent_go.config import DEFAULT_CONFIG


def _config(**behavior):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.setdefault("spec_test", {})
    cfg["behavior"].update(behavior)
    return cfg


def _draft():
    return {
        "files": [{"path": "test_acceptance_foo.py", "content": "assert True\n"}],
        "commands": ["pytest tests/acceptance/test_acceptance_foo.py -v"],
        "notes": "验收",
        "model": "test-model",
    }


class _StubTTY:
    def isatty(self):
        return True


class _BrokenTTY:
    def isatty(self):
        raise OSError("no tty")


class TestAcceptanceReviewPossible:
    def test_web_confirm_plan_enables_review(self):
        from agent_go import cli as cli_mod
        assert cli_mod._acceptance_review_possible(_config(web_confirm_plan=True), headless=True) is True

    def test_headless_without_web_is_false(self):
        from agent_go import cli as cli_mod
        assert cli_mod._acceptance_review_possible(_config(), headless=True) is False

    def test_tty_detection(self, monkeypatch):
        from agent_go import cli as cli_mod
        monkeypatch.setattr("sys.stdin", _StubTTY())
        assert cli_mod._acceptance_review_possible(_config(), headless=False) is True

    def test_tty_exception_falls_back_to_false(self, monkeypatch):
        from agent_go import cli as cli_mod
        monkeypatch.setattr("sys.stdin", _BrokenTTY())
        assert cli_mod._acceptance_review_possible(_config(), headless=False) is False


class TestPrepareDraftSavesDraft:
    def test_draft_is_saved_to_task_dir(self, tmp_path, logger, monkeypatch):
        from agent_go import cli as cli_mod
        from agent_go import spec_test
        monkeypatch.setattr(spec_test, "draft_acceptance", lambda *a, **k: _draft())
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["spec_test"].update(enabled=True, require_review=False)
        task_dir = tmp_path / "task-1"
        task_dir.mkdir()
        draft = cli_mod._prepare_acceptance_draft("任务", cfg, logger, headless=True,
                                                  task_dir=task_dir, repo=tmp_path)
        assert draft is not None
        saved = json.loads((task_dir / "acceptance" / "DRAFT.json").read_text(encoding="utf-8"))
        assert saved["_saved_reason"] == "drafted" and saved["files"][0]["path"].endswith(".py")


class TestFreezeAndMetaHelpers:
    def test_freeze_without_draft_returns_none(self, tmp_path, logger):
        from agent_go import cli as cli_mod
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        assert cli_mod._freeze_acceptance_after_review(task_dir=tmp_path, draft=None, state={},
                                                       config=cfg, logger=logger) is None

    def test_meta_block_with_manifest_delegates(self, logger, tmp_path):
        from agent_go import cli as cli_mod
        from agent_go import spec_test
        manifest = spec_test.freeze(tmp_path, _draft(), reviewed=True, logger=logger, review_channel="cli")
        assert manifest is not None
        block = cli_mod._spec_test_meta_block(manifest, {}, None)
        assert block == spec_test.meta_block(manifest)
        assert block["frozen"] is True and block["commands"]


class TestConfirmPlanChannelWeb:
    def test_web_confirm_x_exits(self, tmp_path, logger, monkeypatch):
        from agent_go import cli as cli_mod
        from agent_go import web_confirm as web_confirm_mod
        monkeypatch.setattr(web_confirm_mod, "web_confirm", lambda *a, **k: "X")
        with pytest.raises(SystemExit) as excinfo:
            cli_mod._confirm_plan_channel({"steps": []}, _config(web_confirm_plan=True),
                                          repo=None, logger=logger, iteration=1, task="t",
                                          plan_dir=tmp_path)
        assert excinfo.value.code == 0

    def test_web_confirm_y_and_r(self, tmp_path, logger, monkeypatch):
        from agent_go import cli as cli_mod
        from agent_go import web_confirm as web_confirm_mod
        monkeypatch.setattr(web_confirm_mod, "web_confirm", lambda *a, **k: "Y")
        plan = {"steps": []}
        confirmed, _docs = cli_mod._confirm_plan_channel(plan, _config(web_confirm_plan=True),
                                                         repo=None, logger=logger, iteration=1,
                                                         task="t", plan_dir=tmp_path)
        assert confirmed == plan
        monkeypatch.setattr(web_confirm_mod, "web_confirm", lambda *a, **k: "R")
        regenerated, _docs2 = cli_mod._confirm_plan_channel(plan, _config(web_confirm_plan=True),
                                                            repo=None, logger=logger, iteration=1,
                                                            task="t", plan_dir=tmp_path)
        assert regenerated is None

    def test_web_receipt_merged_into_draft(self, tmp_path, logger, monkeypatch):
        from agent_go import cli as cli_mod
        from agent_go import web_confirm as web_confirm_mod
        state = {"decision": "approved", "edits": 1,
                 "files": [{"path": "test_acceptance_foo.py", "content": "assert 1\n"}]}
        monkeypatch.setattr(web_confirm_mod, "web_confirm", lambda *a, **k: "Y")
        acceptance = _draft()
        cli_mod._confirm_plan_channel({"steps": []}, _config(web_confirm_plan=True), repo=None,
                                      logger=logger, iteration=1, task="t", plan_dir=tmp_path,
                                      acceptance=acceptance, acceptance_state=state)
        assert acceptance["files"][0]["content"] == "assert 1\n"  # 回执覆盖草稿内容


def _seed_task(root: Path, name: str, acc: dict, results: list) -> Path:
    td = root / name
    td.mkdir(parents=True, exist_ok=True)
    (td / "meta.json").write_text(json.dumps({
        "status": "DELIVERY_READY", "accepted_delivery": True,
        "acceptance": acc, "results": results,
    }), encoding="utf-8")
    return td


class TestEvalAcceptanceSurface:
    def _args(self, **over):
        base = {"subcommand": "acceptance", "task_id": None, "eval_all": False,
                "json_mode": False, "window_days": None}
        base.update(over)
        return argparse.Namespace(**base)

    def test_json_mode_emits_parseable_report(self, tmp_path, monkeypatch, capsys):
        import agent_go.config as config_mod
        from agent_go.eval import cmd_eval
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        _seed_task(tmp_path, "task-20261005-130000-001-aaaa", {"enabled": True}, [
            {"subtask_id": "s1", "verification_results": [
                {"type": "acceptance", "exit_code": 0, "attempt": 1}]}])
        cmd_eval(self._args(json_mode=True))
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{"):])
        assert payload["cohort"]["tasks"] == 1
        assert payload["oracle"]["first_attempt_pass_rate"] == 1.0

    def test_text_report_and_window(self, tmp_path, monkeypatch, capsys):
        import agent_go.config as config_mod
        from agent_go.eval import cmd_eval
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        _seed_task(tmp_path, "task-20261005-120000-001-old1", {"enabled": True}, [])
        _seed_task(tmp_path, "task-20261005-130000-002-new2", {"enabled": True, "frozen": True}, [])
        cmd_eval(self._args(window_days=1))
        out = capsys.readouterr().out
        assert "spec-to-test 验收测试口径" in out and "最近 1 天" in out
        assert "首次验证通过率" in out

    def test_empty_dir_still_prints_report(self, tmp_path, monkeypatch, capsys):
        import agent_go.config as config_mod
        from agent_go.eval import cmd_eval
        monkeypatch.setattr(config_mod, "AGENT_GO_DIR", tmp_path)
        cmd_eval(self._args())
        out = capsys.readouterr().out
        assert "spec-to-test 验收测试口径" in out and "启用队列:          0 任务" in out
