"""测试 plan_to_subtasks — Plan → 子任务转换"""

from agent_go import plan_to_subtasks


class TestPlanToSubtasks:
    """plan_to_subtasks 基础功能测试"""

    def test_basic_conversion(self, sample_plan, logger):
        """验证基本 Plan 转换为子任务"""
        subtasks = plan_to_subtasks(sample_plan, logger)
        assert len(subtasks) == 2
        assert subtasks[0]["id"] == "sub-1"
        assert subtasks[1]["id"] == "sub-2"

    def test_titles(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        assert subtasks[0]["title"] == "后端 JWT 认证"
        assert subtasks[1]["title"] == "前端登录页面"

    def test_files_hint(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        assert "src/auth/jwt.py" in subtasks[0]["files_hint"]
        assert subtasks[1]["files_hint"] == "src/pages/login.tsx"

    def test_dependencies(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        # step 2 depends on step 1
        assert subtasks[1]["depends_on"] == ["sub-1"]
        # step 1 has no dependencies
        assert subtasks[0]["depends_on"] == []

    def test_agent_prompt_injected(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        for st in subtasks:
            assert "agent_prompt" in st
        assert "JWT" in subtasks[0]["agent_prompt"]

    def test_verification_injected(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        assert subtasks[0]["verification"] == "pytest tests/test_auth.py"
        assert "验证命令" in subtasks[0]["description"]

    def test_risks_injected(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        assert subtasks[0]["risks"] == ["密钥管理"]
        assert "风险提示" in subtasks[0]["description"]

    def test_shared_resources_inject(self, sample_plan, logger):
        subtasks = plan_to_subtasks(sample_plan, logger)
        for st in subtasks:
            assert "共享资源清单" in st["description"]
            assert "https://github.com/user/repo.git" in st["description"]

    def test_no_files_uses_wildcard(self, sample_plan, logger):
        """steps 中 files 为空时使用 *"""
        plan = {
            "overview": "test",
            "steps": [{"id": 1, "title": "t", "description": "d"}],
            "shared_resources": {}
        }
        subtasks = plan_to_subtasks(plan, logger)
        assert subtasks[0]["files_hint"] == "*"

    def test_minimal_plan(self, minimal_plan, logger):
        """最小 Plan（无 dependencies/shared_resources）"""
        subtasks = plan_to_subtasks(minimal_plan, logger)
        assert len(subtasks) == 1
        assert subtasks[0]["depends_on"] == []

    def test_empty_steps(self, logger):
        plan = {"overview": "empty", "steps": []}
        subtasks = plan_to_subtasks(plan, logger)
        assert subtasks == []

    def test_default_title_fallback(self, logger):
        """steps 中无 title 时使用默认名"""
        plan = {
            "overview": "test",
            "steps": [{"id": 5, "description": "desc"}]
        }
        subtasks = plan_to_subtasks(plan, logger)
        assert subtasks[0]["title"] == "步骤 5"
