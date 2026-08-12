"""测试 goal_policy.py — Goal Policy Resolver（用户覆盖 > config > 系统策略 > Planner 建议）。"""
from agent_go.goal_policy import GOAL_MODES, resolve_goal_policy


def _subtasks(difficulty="medium", verification="pytest tests"):
    return [{"id": "sub-1", "difficulty": difficulty, "verification": verification}]


class TestGoalModes:
    def test_modes_defined(self):
        assert GOAL_MODES == ("off", "auto", "force", "hook")


class TestUserOverride:
    def test_force_enables_goal(self):
        r = resolve_goal_policy("force", subtasks=_subtasks(), headless=True)
        assert r["enabled"] is True
        assert r["enable_hook"] is False
        assert r["mode"] == "force"
        assert "user_override" in r["reason_codes"]

    def test_off_disables(self):
        r = resolve_goal_policy("off", subtasks=_subtasks(), headless=True)
        assert r["enabled"] is False
        assert r["mode"] == "off"

    def test_hook_enables_hook(self):
        r = resolve_goal_policy("hook", subtasks=_subtasks(), headless=True)
        assert r["enabled"] is True
        assert r["enable_hook"] is True
        assert r["backend"] == "claude_cli"

    def test_user_override_beats_config_policy(self):
        r = resolve_goal_policy("off", config_policy="force",
                                subtasks=_subtasks(), headless=True)
        assert r["mode"] == "off"
        assert r["enabled"] is False


class TestConfigPolicy:
    def test_config_force_applies_without_user_override(self):
        r = resolve_goal_policy(None, config_policy="force",
                                subtasks=_subtasks(), headless=True)
        assert r["mode"] == "force"
        assert r["enabled"] is True
        assert "config_policy" in r["reason_codes"]

    def test_config_off_falls_through_to_system_rules(self):
        r = resolve_goal_policy(None, config_policy="off",
                                subtasks=_subtasks(), headless=True)
        assert "config_policy" not in r["reason_codes"]


class TestAutoRules:
    def test_headless_medium_with_verification_enables(self):
        r = resolve_goal_policy(None, subtasks=_subtasks("medium"), headless=True)
        assert r["mode"] == "auto"
        assert r["enabled"] is True
        assert "headless_task" in r["reason_codes"]
        assert "clear_verification" in r["reason_codes"]

    def test_easy_task_stays_off(self):
        r = resolve_goal_policy(None, subtasks=_subtasks("easy"), headless=True)
        assert r["enabled"] is False
        assert "simple_task" in r["reason_codes"]

    def test_no_verification_stays_off(self):
        r = resolve_goal_policy(None, subtasks=_subtasks("hard", ""), headless=True)
        assert r["enabled"] is False
        assert "no_completion_evidence" in r["reason_codes"]

    def test_interactive_stays_off(self):
        r = resolve_goal_policy(None, subtasks=_subtasks("hard"), headless=False)
        assert r["enabled"] is False
        assert "interactive_or_default_off" in r["reason_codes"]

    def test_planner_recommendation_recorded_but_ignored_when_off(self):
        r = resolve_goal_policy(None, goal_recommendation={"mode": "force"},
                                subtasks=_subtasks("easy"), headless=True)
        assert r["enabled"] is False
        assert "planner_suggested_force_ignored" in r["reason_codes"]
