"""测试 plan_to_subtasks — Plan → 子任务转换"""

from agent_go.ui import plan_to_subtasks


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


# ═══════════════════════════════════════════════════════════════
# 覆盖补强：CR-G3 task_type Spec override > 关键词检测
# ═══════════════════════════════════════════════════════════════

def test_task_type_override_wins_over_keyword_detection(tmp_path, logger):
    """CR-G3：Spec 显式 task_type（task_type_override）优先于 role_skill_map 关键词检测。
    标题含 security/auth → 关键词判 security；override='refactor' → 胜出为 refactor。"""
    from agent_go.ui import plan_to_subtasks
    plan = {"steps": [{
        "id": 1, "title": "修复 auth 认证越权", "description": "security 漏洞修复",
        "agent_prompt": "", "verification": "",
    }], "dependencies": {}}
    # 无 override：关键词检测 → security
    subs = plan_to_subtasks(plan, logger, repo=tmp_path)
    assert subs[0]["task_type"] == "security", "无 override 时应关键词检测为 security"
    # 有 override：Spec 显式胜出
    subs2 = plan_to_subtasks(plan, logger, repo=tmp_path, task_type_override="refactor")
    assert subs2[0]["task_type"] == "refactor", "Spec override 应胜出于关键词检测"


def test_task_type_none_when_no_match_no_override(tmp_path, logger):
    """无 override + 无关键词匹配 → task_type=None（回退难度路由）。"""
    from agent_go.ui import plan_to_subtasks
    plan = {"steps": [{
        "id": 1, "title": "实现一个普通功能", "description": "常规开发",
        "agent_prompt": "", "verification": "",
    }], "dependencies": {}}
    subs = plan_to_subtasks(plan, logger, repo=tmp_path)


def test_cognitive_and_permission_passthrough(tmp_path, logger):
    """异构模型路由 + 权限最小化字段透传：cognitive_mode / allowed_tools / permission_mode。"""
    from agent_go.ui import plan_to_subtasks
    plan = {"steps": [{
        "id": 1, "title": "只读审查", "description": "审查代码",
        "agent_prompt": "", "verification": "",
        "cognitive_mode": "review",
        "allowed_tools": ["Read", "Grep", "Glob"],
        "permission_mode": "default",
    }], "dependencies": {}}
    subs = plan_to_subtasks(plan, logger, repo=tmp_path)
    assert subs[0]["cognitive_mode"] == "review"
    assert subs[0]["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert subs[0]["permission_mode"] == "default"


def test_cognitive_and_permission_defaults(tmp_path, logger):
    """未声明 cognitive/allowed_tools/permission_mode → 空默认值（不破坏既有行为）。"""
    from agent_go.ui import plan_to_subtasks
    plan = {"steps": [{
        "id": 1, "title": "实现", "description": "实现功能",
        "agent_prompt": "", "verification": "",
    }], "dependencies": {}}
    subs = plan_to_subtasks(plan, logger, repo=tmp_path)
    assert subs[0]["cognitive_mode"] == ""
    assert subs[0]["allowed_tools"] == []
    assert subs[0]["permission_mode"] == ""
    assert subs[0]["task_type"] is None


class TestSkillBackfillRecheck:
    """ISSUE-53：default_skills（任务级自动匹配）回填前需通过子任务级相关性复检。

    实测案例：Python CLI 修复任务被任务级匹配的三个 lark skill 全量回填进
    子任务。修复后：子任务文本复检不命中的不回填，命中的才回填。
    """

    @staticmethod
    def _make_skill(tmp_path, name, description):
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody",
            encoding="utf-8")

    @staticmethod
    def _plan(title, desc):
        return {"shared_resources": {}, "dependencies": {},
                "steps": [{"id": 1, "title": title, "description": desc,
                           "files": ["src/cli.py"], "verification": "",
                           "skills": [], "agent_prompt": ""}]}

    def test_unrelated_default_skills_not_backfilled(self, tmp_path, logger):
        from unittest.mock import patch
        self._make_skill(tmp_path, "lark-im",
                         "飞书即时通讯：当用户要发消息、查看或搜索聊天记录时使用")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            subtasks = plan_to_subtasks(
                self._plan("修复 cmd_list 默认状态", "修改 src/cli.py 的 cmd_list 函数签名"),
                logger, repo=None, default_skills=["lark-im"])
        assert subtasks[0]["skills"] == []

    def test_related_default_skills_backfilled(self, tmp_path, logger):
        from unittest.mock import patch
        self._make_skill(tmp_path, "lark-im",
                         "飞书即时通讯：当用户要发消息、查看或搜索聊天记录时使用")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            subtasks = plan_to_subtasks(
                self._plan("发送飞书消息通知", "完成后发送飞书消息通知相关人员"),
                logger, repo=None, default_skills=["lark-im"])
        assert subtasks[0]["skills"] == ["lark-im"]

    def test_recheck_failure_skips_backfill(self, tmp_path, logger):
        """复检异常时不回填（注入是可选增强，宁缺毋滥）"""
        from unittest.mock import patch
        self._make_skill(tmp_path, "lark-im",
                         "飞书即时通讯：当用户要发消息、查看或搜索聊天记录时使用")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path), \
                patch("agent_go.skills.discover_skills", side_effect=RuntimeError("boom")):
            subtasks = plan_to_subtasks(
                self._plan("发送飞书消息通知", "完成后发送飞书消息通知相关人员"),
                logger, repo=None, default_skills=["lark-im"])
        assert subtasks[0]["skills"] == []
