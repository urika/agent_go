"""测试 slugify_branch_name — 标题转合法 git 分支名"""

from agent_go.utils import slugify_branch_name


def test_slugify_normal_title():
    """普通标题：转小写，空白替换为横杠"""
    assert slugify_branch_name("Hello World") == "hello-world"


def test_slugify_uppercase_and_spaces():
    """含大写和连续空白：小写并合并空白为单个横杠，保留点号"""
    assert slugify_branch_name("API  v2.0") == "api-v2.0"


def test_slugify_special_characters():
    """含特殊字符：移除非法字符（斜杠/感叹号/@），仅保留合法分支名字符"""
    assert slugify_branch_name("Bug/fix! @login") == "bugfix-login"


def test_slugify_long_title_truncated():
    """超长标题：超过 40 字符时截断到 40 字符以内"""
    result = slugify_branch_name("x" * 100)
    assert result == "x" * 40
    assert len(result) <= 40


def test_slugify_empty_or_whitespace_input():
    """空输入或纯空白输入：返回 'unnamed'"""
    assert slugify_branch_name("") == "unnamed"
    assert slugify_branch_name("   ") == "unnamed"
    assert slugify_branch_name("\t  ") == "unnamed"


# ═══════════════════════════════════════════════════════════════
# A2 函数级验收契约：classify_verification_scope
# ═══════════════════════════════════════════════════════════════

from agent_go.utils import classify_verification_scope


def test_scope_function_level_nodeid():
    """function 级：pytest nodeid（:: 语法）"""
    assert classify_verification_scope("pytest tests/test_storage.py::test_add") == "function"
    assert classify_verification_scope("pytest tests/test_x.py::TestFoo::test_bar") == "function"
    assert classify_verification_scope("python -m pytest tests/test_x.py::test_add -v") == "function"


def test_scope_function_level_k_selector():
    """function 级：-k 选择器（函数名过滤）"""
    assert classify_verification_scope("pytest tests/test_x.py -k test_add") == "function"
    assert classify_verification_scope("pytest -k 'not slow'") == "function"


def test_scope_file_level():
    """file 级：指向具体测试文件"""
    assert classify_verification_scope("pytest tests/test_storage.py") == "file"
    assert classify_verification_scope("pytest tests/test_storage.py -v") == "file"
    assert classify_verification_scope("jest src/auth.test.ts") == "file"
    assert classify_verification_scope("python -m pytest tests/test_auth.py") == "file"


def test_scope_suite_level():
    """suite 级：整仓/整目录测试（A2 的弱锚定来源）"""
    assert classify_verification_scope("pytest") == "suite"
    assert classify_verification_scope("pytest tests/") == "suite"
    assert classify_verification_scope("pytest tests") == "suite"
    assert classify_verification_scope("npm test") == "suite"
    assert classify_verification_scope("cargo test") == "suite"
    assert classify_verification_scope("go test ./...") == "suite"


def test_scope_static_level():
    """static 级：仅静态检查，不运行测试"""
    assert classify_verification_scope("ruff check src/") == "static"
    assert classify_verification_scope("npm run lint") == "static"
    assert classify_verification_scope("tsc --noEmit") == "static"


def test_scope_none_and_prefixes():
    """none 级 + 前缀剥离（cd/环境变量）"""
    assert classify_verification_scope("") == "none"
    assert classify_verification_scope("   ") == "none"
    assert classify_verification_scope(None) == "none"
    assert classify_verification_scope("cd repo && pytest tests/test_x.py") == "file"
    assert classify_verification_scope("PYTHONPATH=. pytest tests/") == "suite"
