"""测试 spec.py — Task Spec 解析、L1 准入审查、模板生成（S11-P0）。

测试隔离原则：用 tmp_path 构造临时 Spec 文件和临时 git 仓库，不改 agent_go 自身代码。
"""

import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.spec import (
    TaskSpec,
    parse_spec, validate_spec_l1, render_spec_template,
    extract_file_paths, _match_section_key, _cmd_matches_whitelist,
)


# ─── 完整的合法 Spec（供多数测试复用） ───────────────────────────────

VALID_SPEC = """# Task Spec: 用户邮箱验证

## 1. 目标（做什么）
为 User 模型添加 email_verified 字段和邮箱验证逻辑，包含数据库迁移、token 校验和 API 端点。

## 2. 动机（为什么）
当前注册无需邮箱验证，垃圾注册增加。相关 Issue: #142。

## 3. 范围（动哪里，不动哪里）
### 需要改动
- `src/models/user.py` — User 模型新增 email_verified 字段
- `src/auth/verify.py` — 新增验证 token 生成与校验逻辑
- `src/api/user.py` — 新增 /verify-email 端点

### 明确不动
- `src/api/admin.py` — 管理后台不改动
- 不引入邮件发送服务

## 4. 约束
- 数据库迁移必须可回滚（Alembic upgrade/downgrade 配对）
- 验证 token 有效期 24 小时
- Python 3.9+，不新增 PyPI 包

## 5. 验收标准（怎么算做完）
- [ ] `python -m pytest tests/test_auth.py::test_email_verification -v` 全部通过
- [ ] 新创建的 User 实例 email_verified 默认为 False
- [ ] 验证 token 过期后（>24h）拒绝验证请求

## 6. 参考资料
- 设计文档：docs/design/email-verification.md
- 类似实现：commit a1b2c3d（密码重置 token）

## 7. 已知风险
- User 表 ~50w 行，迁移需注意锁表时间（建议 batch update）
- TokenManager 当前仅支持 password_reset，扩展需兼容性验证
"""


# ═══ 解析测试 ════════════════════════════════════════════════════════

class TestParseSpec:
    """Task Spec 7 章节解析。"""

    def test_parse_complete_spec(self):
        spec = parse_spec(VALID_SPEC)
        assert spec is not None
        assert spec.title == "Task Spec: 用户邮箱验证"
        assert "email_verified" in spec.goal
        assert "垃圾注册" in spec.motivation
        assert "src/models/user.py" in spec.scope
        assert "可回滚" in spec.constraint
        assert "pytest" in spec.acceptance
        assert "email-verification.md" in spec.reference
        assert "锁表" in spec.risk
        assert spec.is_complete is True

    def test_parse_from_path(self, tmp_path):
        f = tmp_path / "task.md"
        f.write_text(VALID_SPEC, encoding="utf-8")
        spec = parse_spec(f)
        assert spec is not None
        assert spec.source_path == f
        assert "email_verified" in spec.goal

    def test_parse_nonexistent_path(self, tmp_path):
        spec = parse_spec(tmp_path / "nonexistent.md")
        assert spec is None

    def test_parse_missing_required_sections(self):
        """缺 §5 验收标准 → is_complete False。"""
        text = """# Task Spec: 不完整

## 1. 目标
做一件事，描述足够长以通过长度检查。

## 3. 范围
改 src/main.py，不动其他文件，描述也够长了。
"""
        spec = parse_spec(text)
        assert spec.goal != ""
        assert spec.motivation == ""  # 缺 §2
        assert spec.acceptance == ""  # 缺 §5
        assert spec.is_complete is False

    def test_parse_section_alias(self):
        """章节标题用别名也能识别。"""
        text = """# Spec

## Goal
实现用户登录功能，描述足够长以通过长度检查。

## Why
为了安全，背景说明。

## Scope
改 src/auth.py 一个文件，范围描述足够长了。

## Acceptance
pytest tests/test_auth.py -v 全绿。
"""
        spec = parse_spec(text)
        assert "登录" in spec.goal
        assert "安全" in spec.motivation
        assert "auth.py" in spec.scope
        assert "pytest" in spec.acceptance
        assert spec.is_complete is True

    def test_parse_section_without_dot(self):
        """标题「## 1 目标」无点号也能识别。"""
        text = """# Spec

## 1 目标
做一件事，描述足够长以通过长度检查。

## 2 动机
为了某个原因，背景说明。

## 3 范围
改 src/main.py 一个文件，范围描述足够长了。

## 5 验收标准
pytest tests/ -v 全绿。
"""
        spec = parse_spec(text)
        assert spec.is_complete is True

    def test_parse_empty_sections(self):
        """章节存在但内容空。"""
        text = """# Spec

## 1. 目标


## 2. 动机


## 3. 范围


## 5. 验收标准

"""
        spec = parse_spec(text)
        assert spec.is_complete is False
        assert spec.goal == ""


# ═══ L1 准入审查测试 ═════════════════════════════════════════════════

class TestValidateSpecL1:
    """L1 硬门禁 4 项检查。"""

    def test_valid_spec_no_violations(self):
        spec = parse_spec(VALID_SPEC)
        violations = validate_spec_l1(spec, repo=None)
        assert violations == []

    def test_missing_required_section(self):
        """检查 1：必填章节缺失。"""
        spec = parse_spec("""# Spec

## 1. 目标
做一件事，描述足够长。

## 3. 范围
改 src/main.py 一个文件，范围描述也够长了。
""")
        # 缺 §2 动机、§5 验收标准
        violations = validate_spec_l1(spec, repo=None)
        checks = {(v.check, v.section) for v in violations}
        assert ("required", "2") in checks
        assert ("required", "5") in checks

    def test_section_too_short(self):
        """检查 2：章节长度下限（敷衍检测）。"""
        spec = parse_spec("""# Spec

## 1. 目标
修 bug

## 2. 动机
要修

## 3. 范围
改代码

## 5. 验收标准
能跑
""")
        violations = validate_spec_l1(spec, repo=None)
        length_violations = [v for v in violations if v.check == "length"]
        assert len(length_violations) >= 3  # 多个章节都太短
        sections = {v.section for v in length_violations}
        assert "1" in sections and "2" in sections

    def test_path_validation_existing_files(self, tmp_path):
        """检查 3：文件路径有效性 — 路径存在时不报错。"""
        # 构造临时 git 仓库
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "models").mkdir()
        (repo / "src" / "models" / "user.py").write_text("# user model")
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo),
                       capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

        spec = parse_spec("""# Spec

## 1. 目标
改 user 模型加字段，描述够长。

## 2. 动机
需要这个字段，背景说明。

## 3. 范围
- `src/models/user.py`

## 5. 验收标准
python -m pytest tests/test_user.py -v
""")
        violations = validate_spec_l1(spec, repo=repo)
        # user.py 存在，不应有 path 违规
        path_violations = [v for v in violations if v.check == "path"]
        assert path_violations == []

    def test_path_validation_nonexistent_file(self, tmp_path):
        """检查 3：路径不存在时报违规 + 建议最近似匹配。"""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "models").mkdir()
        (repo / "src" / "models" / "user.py").write_text("# user")
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo),
                       capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

        # 故意拼错：usr.py（缺 e），应建议 user.py
        spec = parse_spec("""# Spec

## 1. 目标
改一个拼错路径的文件，描述够长。

## 2. 动机
测试路径校验，背景说明。

## 3. 范围
- `src/models/usr.py`

## 5. 验收标准
python -m pytest tests/ -v
""")
        violations = validate_spec_l1(spec, repo=repo)
        path_violations = [v for v in violations if v.check == "path"]
        assert len(path_violations) == 1
        assert "usr.py" in path_violations[0].message
        # 应建议最近似匹配（user.py，编辑距离最近）
        assert "user.py" in path_violations[0].suggestion

    def test_whitelist_violation(self):
        """检查 4：验证命令不在白名单。"""
        spec = parse_spec("""# Spec

## 1. 目标
跑一个不在白名单的命令，描述够长。

## 2. 动机
测试白名单校验，背景说明。

## 3. 范围
改 src/main.py 一个文件，范围描述也够长了。

## 5. 验收标准
```bash
rm -rf /
```
""")
        violations = validate_spec_l1(spec, repo=None)
        wl = [v for v in violations if v.check == "whitelist"]
        assert len(wl) == 1
        assert "rm" in wl[0].message

    def test_whitelist_accepts_pytest(self):
        """pytest 在白名单内。"""
        spec = parse_spec("""# Spec

## 1. 目标
正常跑测试，描述够长以通过检查。

## 2. 动机
验证功能正确，背景说明。

## 3. 范围
改 src/main.py 一个文件，范围描述够长。

## 5. 验收标准
```bash
python -m pytest tests/ -v
```
""")
        violations = validate_spec_l1(spec, repo=None)
        wl = [v for v in violations if v.check == "whitelist"]
        assert wl == []


# ═══ 工具函数测试 ════════════════════════════════════════════════════

class TestExtractFilePaths:
    """文件路径提取。"""

    def test_extract_backtick_paths(self):
        text = "改 `src/models/user.py` 和 `src/api/user.py`"
        paths = extract_file_paths(text)
        assert "src/models/user.py" in paths
        assert "src/api/user.py" in paths

    def test_extract_bare_paths(self):
        text = "修改 src/auth/verify.py 文件"
        paths = extract_file_paths(text)
        assert "src/auth/verify.py" in paths

    def test_exclude_urls(self):
        text = "参考 https://example.com/page.py 和 src/main.py"
        paths = extract_file_paths(text)
        assert "src/main.py" in paths
        assert all("example.com" not in p for p in paths)

    def test_dedup(self):
        text = "`src/main.py` 和 `src/main.py`"
        paths = extract_file_paths(text)
        assert paths.count("src/main.py") == 1


class TestMatchSectionKey:
    def test_numeric_prefix(self):
        assert _match_section_key("1. 目标") == "1"
        assert _match_section_key("2、动机") == "2"

    def test_alias(self):
        assert _match_section_key("goal") == "1"
        assert _match_section_key("验收标准") == "5"

    def test_unknown(self):
        assert _match_section_key("附录") is None


class TestCmdMatchesWhitelist:
    def test_pytest(self):
        assert _cmd_matches_whitelist("python -m pytest tests/ -v") is True

    def test_rm(self):
        assert _cmd_matches_whitelist("rm -rf /") is False

    def test_empty(self):
        assert _cmd_matches_whitelist("") is True

    def test_env_prefix(self):
        """前导环境变量赋值应被剥离。"""
        assert _cmd_matches_whitelist("FOO=bar python -m pytest tests/") is True


# ═══ 模板生成测试 ════════════════════════════════════════════════════

class TestRenderSpecTemplate:
    def test_template_has_all_sections(self):
        tpl = render_spec_template()
        for marker in ["## 1. 目标", "## 2. 动机", "## 3. 范围",
                       "## 4. 约束", "## 5. 验收标准", "## 6. 参考", "## 7. 已知风险"]:
            assert marker in tpl, f"模板缺少章节: {marker}"

    def test_template_no_typo(self):
        """确认模板中没有笔误（醇收 → 验收）。"""
        tpl = render_spec_template()
        assert "醇收" not in tpl
        assert "验收标准" in tpl

    def test_template_with_repo(self, tmp_path):
        """提供 repo 时预填目录提示。"""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("# main")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_main.py").write_text("# test")
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo),
                       capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        tpl = render_spec_template(repo)
        assert "src" in tpl  # 预填了目录提示

    def test_template_nonexistent_repo(self, tmp_path):
        """repo 不存在时回退到通用模板。"""
        tpl = render_spec_template(tmp_path / "nonexistent")
        assert "## 1. 目标" in tpl


# ═══ TaskSpec 数据类测试 ═════════════════════════════════════════════

class TestTaskSpecDataclass:
    def test_default_empty(self):
        spec = TaskSpec()
        assert spec.is_complete is False
        assert spec.goal == ""

    def test_is_complete_requires_all_four(self):
        spec = TaskSpec(goal="g", motivation="m", scope="s", acceptance="a")
        assert spec.is_complete is True
        spec.acceptance = ""
        assert spec.is_complete is False


# ═══════════════════════════════════════════════════════════════
# CR-G3: Spec task_type 字段解析
# ═══════════════════════════════════════════════════════════════

class TestParseSpecTaskType:
    _BASE = "# Task Spec: x\n\n## 1. 目标*\n\ng\n\n## 2. 动机*\n\nm\n\n## 3. 范围*\n\ns\n\n## 5. 验收标准*\n\na\n"

    def test_explicit_task_type(self):
        spec = parse_spec(self._BASE + "\ntask_type: security\n")
        assert spec.task_type == "security"

    def test_inline_comment_tolerated(self):
        """行内注释（task_type: security  # ...）仍能解析。"""
        spec = parse_spec(self._BASE + "\ntask_type: bugfix  # 修复型任务\n")
        assert spec.task_type == "bugfix"

    def test_placeholder_yields_empty(self):
        """模板占位行（task_type:  # 可选...）因 # 非字首不匹配 → 空。"""
        spec = parse_spec(self._BASE + "\ntask_type:  # 可选。任务类型\n")
        assert spec.task_type == ""

    def test_no_task_type_field(self):
        """无 task_type 行 → 空（交关键词检测）。"""
        spec = parse_spec(self._BASE)
        assert spec.task_type == ""

    def test_case_lowercased(self):
        spec = parse_spec(self._BASE + "\ntask_type: Security\n")
        assert spec.task_type == "security"

    def test_template_includes_task_type(self):
        """生成的 Spec 模板含 task_type 元数据行（可发现性）。"""
        tpl = render_spec_template()
        assert "task_type:" in tpl


# ═══════════════════════════════════════════════════════════════
# CR-TD：Spec budget 字段（任务级成本预算 USD）
# ═══════════════════════════════════════════════════════════════

class TestParseSpecBudget:
    _BASE = "# Task Spec: x\n\n## 1. 目标*\n\ng\n\n## 2. 动机*\n\nm\n\n## 3. 范围*\n\ns\n\n## 5. 验收标准*\n\na\n"

    def test_explicit_budget(self):
        spec = parse_spec(self._BASE + "\nbudget: 0.30\n")
        assert spec.budget == 0.30

    def test_budget_integer(self):
        spec = parse_spec(self._BASE + "\nbudget: 1\n")
        assert spec.budget == 1.0

    def test_budget_placeholder_yields_none(self):
        """占位行（budget:  # 可选...）→ None（交 config/CLI）。"""
        spec = parse_spec(self._BASE + "\nbudget:  # 可选。任务级成本预算\n")
        assert spec.budget is None

    def test_no_budget_field(self):
        assert parse_spec(self._BASE).budget is None

    def test_template_includes_budget(self):
        assert "budget:" in render_spec_template()
