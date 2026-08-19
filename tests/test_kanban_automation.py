"""W1 看板任务分类器测试。"""
import pytest


@pytest.fixture
def kanban_env(tmp_path, monkeypatch):
    import agent_go.kanban as kb
    monkeypatch.setattr(kb, "AGENT_GO_DIR", tmp_path)
    return tmp_path


class TestClassifyAutomation:
    def test_arch_signals_manual(self):
        from agent_go.kanban import classify_automation
        assert classify_automation("重构系统架构", "", "") == "manual"
        assert classify_automation("Fix race condition", "", "") == "manual"
        assert classify_automation("", "跨文件 performance 优化", "") == "manual"

    def test_spec_auto(self):
        from agent_go.kanban import classify_automation
        assert classify_automation("添加用户登录", "", "docs/spec.md") == "auto"

    def test_pending_default(self):
        from agent_go.kanban import classify_automation
        assert classify_automation("添加用户登录", "", "") == "pending"


class TestCardAutomationField:
    def test_create_card_auto_automation(self, kanban_env):
        from agent_go.kanban import create_card
        card = create_card("添加用户登录", "implementation", repo="/tmp/r")
        assert card["automation"] == "pending"
        c2 = create_card("重构系统架构", "implementation", repo="/tmp/r")
        assert c2["automation"] == "manual"
        c3 = create_card("加登录", "implementation", repo="/tmp/r", spec_path="s.md")
        assert c3["automation"] == "auto"

    def test_update_automation_allowed(self, kanban_env):
        from agent_go.kanban import create_card, update_card, get_card
        card = create_card("添加用户登录", "implementation", repo="/tmp/r")
        assert card["automation"] == "pending"
        update_card(card["id"], automation="auto")
        assert get_card(card["id"])["automation"] == "auto"
