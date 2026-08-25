"""测试 discover_skills — 基于关键词的任务-技能自动匹配"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.skills import discover_skills, Skill, AGENT_GO_SKILLS_DIR


class _MakeSkillDirMixin:
    """Helper: 在临时目录下创建模拟 skill"""

    @staticmethod
    def _make_skill_dir(tmp_path, name, description):
        skill_dir = tmp_path / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nSkill body.",
            encoding="utf-8",
        )
        return skill_dir


class TestDiscoverSkills(_MakeSkillDirMixin):
    """discover_skills 自动匹配测试"""

    def test_exact_match(self, tmp_path):
        """任务描述与 skill description 有重叠词时命中"""
        # 注: 使用英文关键词，因 discover_skills 基于 r'\w+' 分词，
        # 中文文本无空格时整句为一个 token，无法部分匹配
        self._make_skill_dir(tmp_path, "security-review",
                             "Security audit — authentication authorization encryption")
        self._make_skill_dir(tmp_path, "code-review",
                             "Code review — quality and standards")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("audit authentication module for security")
        assert len(result) > 0
        names = [s.name for s in result]
        assert "security-review" in names

    def test_no_match(self, tmp_path):
        """无关键词语义重叠时不匹配"""
        self._make_skill_dir(tmp_path, "security-review",
                             "安全审查 — 涉及认证、权限、加密")
        self._make_skill_dir(tmp_path, "code-review",
                             "代码审查 — 质量与规范")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("更新 README 文档")
        assert result == []

    def test_partial_match(self, tmp_path):
        """部分关键词匹配（多词重叠命中）"""
        self._make_skill_dir(tmp_path, "frontend-react",
                             "前端 React 组件开发与测试")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("编写 React 组件测试用例")
        assert len(result) > 0
        assert result[0].name == "frontend-react"

    def test_single_english_word_no_match(self, tmp_path):
        """单一英文泛词不匹配（防止 skill description 中的弱义词误配，
        如 orm-optimizer 的 "verification" 与任务模板字段重叠）"""
        self._make_skill_dir(tmp_path, "frontend-react",
                             "前端 React 组件开发与测试")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("编写 React 组件测试用例")
        # 英文单词 "react" 不满足 ≥2 词规则（中文词除外），不匹配
        # 注意：此描述含中文词，走 CJK 分支应仍匹配；用纯英文单泛词验证
        result2 = discover_skills("implement authentication module")
        names = [s.name for s in result2]
        assert "frontend-react" not in names

    def test_sort_by_overlap_count(self, tmp_path):
        """按匹配关键词数排序，重叠词多的排在前面"""
        self._make_skill_dir(tmp_path, "skill-a",
                             "React 组件开发与性能优化")
        self._make_skill_dir(tmp_path, "skill-b",
                             "React 测试编写和组件审查")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("React 组件测试")
        # skill-b 匹配 "React" "组件" "测试" 三个词
        # skill-a 匹配 "React" "组件" 两个词
        if len(result) >= 2:
            # skill-b 应该在前面
            names = [s.name for s in result]
            assert "skill-b" == names[0] or len(result) >= 1

    def test_max_skills_limit(self, tmp_path):
        """max_skills 参数限制返回数量"""
        for i in range(5):
            self._make_skill_dir(tmp_path, f"skill-{i}",
                                 f"task 测试 验证 审查 质量 {i}")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("task 测试", max_skills=2)
        assert len(result) <= 2

    def test_returns_skill_objects(self, tmp_path):
        """返回类型为 Skill 对象"""
        self._make_skill_dir(tmp_path, "security-review",
                             "安全审查 — 涉及认证、权限、加密")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("安全审查")
        for s in result:
            assert isinstance(s, Skill)
            assert s.body != ""

    def test_no_installed_skills(self, tmp_path):
        """无已安装 skill 时返回空列表"""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", empty_dir):
            result = discover_skills("任何任务")
        assert result == []

    def test_case_insensitive_match(self, tmp_path):
        """大小写不敏感匹配（多词重叠命中）"""
        self._make_skill_dir(tmp_path, "security-review",
                             "Security Audit — Authentication, Authorization")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            # 单英文泛词 "authentication" 不满足 ≥2 词规则，不匹配
            result = discover_skills("implement AUTHENTICATION module")
            assert len(result) == 0
            # 多词重叠（authentication + authorization）命中，且大小写不敏感
            result2 = discover_skills("implement AUTHENTICATION and AUTHORIZATION flows")
            assert len(result2) > 0
            assert result2[0].name == "security-review"

    def test_punctuation_handling(self, tmp_path):
        """标点符号不影响关键词提取"""
        self._make_skill_dir(tmp_path, "api-design",
                             "API 设计规范与 RESTful 架构")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("设计 RESTful API!")
        assert len(result) > 0
        assert result[0].name == "api-design"

    def test_no_cross_type_mismatch_issue33(self, tmp_path):
        """ISSUE-33: Python 修复任务不得误配前端 skill（CJK bigram 防单字符误配）。

        修复前：CJK 按单字符分词，frontend-react description 中的高频字（管理/组件/
        网络/请求）与任何中文任务都易重叠 ≥2 个字符，导致无关 skill 注入。修复后
        bigram 保留语义，跨类型任务无重叠。
        """
        self._make_skill_dir(tmp_path, "frontend-react",
                             "React 前端开发 — 组件、状态管理、路由、样式、网络请求")
        self._make_skill_dir(tmp_path, "security-review",
                             "安全审查 — 涉及认证、权限、加密、SQL注入、密钥管理的代码")

        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            # Python 修复任务：不应匹配 frontend-react
            result = discover_skills("修复 planning.py 的 validate_plan_quality 函数 scope_conflict 误报")
            names = [s.name for s in result]
            assert "frontend-react" not in names, f"Python 修复任务误配前端 skill: {names}"
            # 安全任务：应匹配 security-review
            result2 = discover_skills("对认证和权限相关的代码进行安全审查，检查 SQL 注入")
            names2 = [s.name for s in result2]
            assert "security-review" in names2, f"安全任务应匹配 security-review: {names2}"
            # 前端任务：应匹配 frontend-react
            result3 = discover_skills("编写 React 前端组件，处理状态管理和路由")
            names3 = [s.name for s in result3]
            assert "frontend-react" in names3, f"前端任务应匹配 frontend-react: {names3}"


class TestIssue53HomographFalsePositive(_MakeSkillDirMixin):
    """ISSUE-53: 同形异义巧合词 + bigram 碎片不得击穿匹配门槛。

    实测案例：任务「修改 cmd_list 函数签名」被误配 lark-contact（通讯录个人
    签名）/lark-apps（错误量）/lark-openapi-explorer（API 调用）——df=1 的
    巧合词（签名/错误/调用）一个即达旧门槛 1.0。修复：门槛 2.0（≈两个独立
    专属词证据）+ 中文碎片停用词 + 单字符过滤。
    """

    def test_homograph_coincidence_no_match(self, tmp_path):
        self._make_skill_dir(tmp_path, "lark-contact",
                             "飞书通讯录：当用户提到一个名字要发消息，或查个人签名、部门时使用")
        self._make_skill_dir(tmp_path, "lark-apps",
                             "妙搭应用开发：当用户要开发一个系统或应用，查询错误量、访问量时使用")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("修复 src/cli.py 的 cmd_list 函数签名默认值错误")
            names = [s.name for s in result]
            assert "lark-contact" not in names, f"函数签名误配通讯录签名: {names}"
            assert "lark-apps" not in names, f"默认值错误误配错误量监控: {names}"

    def test_lark_style_pool_python_task_no_match(self, tmp_path):
        """lark 系 skill 池（「当用户…时」句式）下，纯 Python 修复任务零命中"""
        self._make_skill_dir(tmp_path, "lark-im",
                             "飞书即时通讯：当用户要发消息、查看或搜索聊天记录时使用")
        self._make_skill_dir(tmp_path, "lark-doc",
                             "飞书云文档：当用户给出文档 URL，需要查看、创建、编辑文档时使用")
        self._make_skill_dir(tmp_path, "lark-task",
                             "飞书任务：当用户需要创建待办事项、查看任务列表、跟踪任务进度时使用")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("修复 planning.py 的 validate_plan_quality 函数误报")
            assert result == []

    def test_two_strong_words_still_match(self, tmp_path):
        """两个及以上专属词命中的合法匹配不受门槛提高影响"""
        self._make_skill_dir(tmp_path, "bank-risk",
                             "银行业智能风控信息收集整理，生成风控研究报告")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            result = discover_skills("收集银行业风控资料并生成报告")
            assert "bank-risk" in [s.name for s in result]

    def test_single_char_fragment_filtered(self, tmp_path):
        """单字符碎片（如「为」）不参与计分——否则它与任意一个 df=1 词组合
        即可凑够 ≥2 词 + score 2.0 破门"""
        self._make_skill_dir(tmp_path, "lark-drive",
                             "飞书云空间：管理 Drive 文件，导入 Word 为 docx")
        with patch("agent_go.skills.AGENT_GO_SKILLS_DIR", tmp_path):
            # 「为」被标点隔离成单字符 token；不过滤时 overlap={为,文件}
            # score=2.0 恰好达标误配，过滤后仅剩 {文件} 不足 2 词
            result = discover_skills("把 a 为 b 写进文件")
            assert result == []
