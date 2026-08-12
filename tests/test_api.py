"""测试 call_api — LLM API 调用

通过 mock urllib.request 避免真实网络请求。
"""

from unittest.mock import patch, MagicMock
import json
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go.api import call_api
from agent_go.config import get_api_key


class MockResponse:
    """模拟 urllib.request.urlopen 返回值的上下文管理器"""

    def __init__(self, json_data, status=200):
        self._json_data = json_data
        self.status = status

    def __enter__(self):
        self.body = io.BytesIO()
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self._json_data).encode("utf-8")


class TestCallApi:
    """call_api 基础功能测试"""

    ANTHROPIC_CONFIG = {
        "plan_api": {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com/v1/messages",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "temperature": 0.2,
            "api_key": "sk-ant-test-key"
        }
    }

    OPENAI_CONFIG = {
        "plan_api": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-4o",
            "max_tokens": 4096,
            "temperature": 0.2,
            "api_key": "sk-openai-test-key"
        }
    }

    DEEPSEEK_CONFIG = {
        "plan_api": {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1/chat/completions",
            "model": "deepseek-chat",
            "max_tokens": 4096,
            "temperature": 0.2,
            "api_key": "sk-deepseek-test-key"
        }
    }

    @patch("urllib.request.urlopen")
    def test_anthropic_provider(self, mock_urlopen, logger):
        """Anthropic 格式的请求/响应"""
        mock_resp = MockResponse({
            "content": [{"text": "测试响应内容"}]
        })
        mock_urlopen.return_value = mock_resp

        result = call_api(self.ANTHROPIC_CONFIG, [{"role": "user", "content": "hi"}], logger)
        assert result == "测试响应内容"

        # 验证请求头（urllib 会自动 title-case key 名）
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.headers["X-api-key"] == "sk-ant-test-key"
        assert call_args.headers["Anthropic-version"] == "2023-06-01"

    @patch("urllib.request.urlopen")
    def test_openai_provider(self, mock_urlopen, logger):
        """OpenAI 格式的请求/响应"""
        mock_resp = MockResponse({
            "choices": [{"message": {"content": "OpenAI 响应"}}]
        })
        mock_urlopen.return_value = mock_resp

        result = call_api(self.OPENAI_CONFIG, [{"role": "user", "content": "hi"}], logger)
        assert result == "OpenAI 响应"

        # 验证请求头
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.headers["Authorization"] == "Bearer sk-openai-test-key"

    @patch("urllib.request.urlopen")
    def test_deepseek_provider(self, mock_urlopen, logger):
        """DeepSeek (OpenAI-compatible) 格式"""
        mock_resp = MockResponse({
            "choices": [{"message": {"content": "DeepSeek 响应"}}]
        })
        mock_urlopen.return_value = mock_resp

        result = call_api(self.DEEPSEEK_CONFIG, [{"role": "user", "content": "hi"}], logger)
        assert result == "DeepSeek 响应"

        # 验证请求头
        call_args = mock_urlopen.call_args[0][0]
        assert call_args.headers["Authorization"] == "Bearer sk-deepseek-test-key"

    @patch("urllib.request.urlopen")
    def test_custom_base_url(self, mock_urlopen, logger):
        """自定义 base_url"""
        config = {
            "plan_api": {
                "provider": "custom",
                "base_url": "https://my-custom-endpoint.com/v1/chat",
                "model": "my-model",
                "max_tokens": 4096,
                "temperature": 0.2,
                "api_key": "sk-custom-key"
            }
        }
        mock_resp = MockResponse({
            "choices": [{"message": {"content": "custom response"}}]
        })
        mock_urlopen.return_value = mock_resp

        result = call_api(config, [{"role": "user", "content": "hi"}], logger)
        assert result == "custom response"

        call_args = mock_urlopen.call_args[0][0]
        assert call_args.full_url == "https://my-custom-endpoint.com/v1/chat"

    def test_missing_api_key(self, logger):
        """无 API Key 时抛出 RuntimeError"""
        config = {
            "plan_api": {
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1/messages",
                "model": "test",
                "api_key": ""
            }
        }
        import os
        # 确保环境变量中也没有 key
        saved = os.environ.pop("AGENT_GO_API_KEY", None)
        try:
            import pytest
            with pytest.raises(RuntimeError, match="API Key 未配置"):
                call_api(config, [{"role": "user", "content": "hi"}], logger)
        finally:
            if saved is not None:
                os.environ["AGENT_GO_API_KEY"] = saved


# ═══════════════════════════════════════════════════════════════
# Plan 缓存
# ═══════════════════════════════════════════════════════════════

class TestPlanCache:
    """缓存 Key 生成、保存、加载、过期清理"""

    def test_get_cache_key(self, tmp_path):
        """缓存 key 是 SHA256 hex 字符串"""
        from agent_go.api import get_cache_key
        with patch("agent_go.api.analyze_project", return_value="file1.py\nfile2.py\n"):
            with patch("agent_go.api.get_git_info", return_value={
                "remote": "origin", "branch": "main", "commit": "abc"
            }):
                key1 = get_cache_key("hello", tmp_path)
        assert len(key1) == 64
        assert all(c in "0123456789abcdef" for c in key1)

    def test_cache_key_different_for_different_tasks(self, tmp_path):
        """不同 task 产生不同的 key"""
        from agent_go.api import get_cache_key
        with patch("agent_go.api.analyze_project", return_value=""):
            with patch("agent_go.api.get_git_info", return_value={
                "remote": "", "branch": "", "commit": ""
            }):
                k1 = get_cache_key("task A", tmp_path)
                k2 = get_cache_key("task B", tmp_path)
        assert k1 != k2

    def test_cache_key_different_repos(self, tmp_path):
        """不同 repo 产生不同的 key"""
        from agent_go.api import get_cache_key
        repo2 = tmp_path / "other_repo"
        with patch("agent_go.api.analyze_project", return_value="file1.py"):
            with patch("agent_go.api.get_git_info", return_value={
                "remote": "r1", "branch": "main", "commit": "a"
            }):
                k1 = get_cache_key("task", tmp_path)
            with patch("agent_go.api.get_git_info", return_value={
                "remote": "r2", "branch": "main", "commit": "a"
            }):
                k2 = get_cache_key("task", repo2)
        assert k1 != k2

    def test_save_and_load_cached_plan(self, tmp_path, logger):
        """保存后应能正确加载"""
        from agent_go.api import save_cached_plan, load_cached_plan, get_cache_key
        config = {"cache": {"enabled": True, "plan_ttl": 86400}}

        with patch("agent_go.api.analyze_project", return_value="files"):
            with patch("agent_go.api.get_git_info", return_value={
                "remote": "", "branch": "main", "commit": ""
            }):
                key = get_cache_key("test task", tmp_path)

        plan = {"overview": "test", "steps": [{"id": 1, "title": "step1"}]}

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            save_cached_plan(key, plan, "test task", tmp_path, config)
            loaded = load_cached_plan(key, "test task", config, logger)

        assert loaded is not None
        assert loaded["overview"] == "test"

    def test_cache_disabled_does_not_save(self, tmp_path, logger):
        """cache.enabled=False 时不保存"""
        from agent_go.api import save_cached_plan, get_cache_key
        config = {"cache": {"enabled": False}}

        with patch("agent_go.api.analyze_project", return_value=""):
            with patch("agent_go.api.get_git_info", return_value={
                "remote": "", "branch": "", "commit": ""
            }):
                key = get_cache_key("task", tmp_path)

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            save_cached_plan(key, {}, "task", tmp_path, config)
            # 应无文件创建
            cache_dir = tmp_path / "cache" / "plans"
            assert not cache_dir.exists()

    def test_load_expired_cache(self, tmp_path, logger):
        """过期的缓存返回 None 并删除文件"""
        import time
        from agent_go.api import load_cached_plan

        config = {"cache": {"enabled": True, "plan_ttl": 1}}  # 1 秒 TTL

        # 创建一个过期缓存（直接写入）
        cache_dir = tmp_path / "cache" / "plans"
        sub_dir = cache_dir / "ab"
        sub_dir.mkdir(parents=True)
        cache_file = sub_dir / "abcdef123456.json"
        cache_file.write_text(json.dumps({
            "cache_key": "abcdef123456",
            "plan": {"overview": "old", "steps": [{"id": 1}]},
            "meta": {
                "created_at": "2020-01-01T00:00:00",  # 已过期
                "last_hit_at": "2020-01-01T00:00:00",
                "hit_count": 0,
                "task": "old task",
                "ttl": 1,
            },
        }), encoding="utf-8")

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            result = load_cached_plan("abcdef123456", "old task", config, logger)
        assert result is None, "过期缓存应返回 None"
        assert not cache_file.exists(), "过期文件应被删除"

    def test_cache_task_mismatch(self, tmp_path, logger):
        """缓存 task 不匹配时跳过缓存"""
        from agent_go.api import load_cached_plan
        config = {"cache": {"enabled": True, "plan_ttl": 86400}}

        cache_dir = tmp_path / "cache" / "plans"
        sub_dir = cache_dir / "ab"
        sub_dir.mkdir(parents=True)
        cache_file = sub_dir / "abcdef123456.json"
        from datetime import datetime
        cache_file.write_text(json.dumps({
            "cache_key": "abcdef123456",
            "plan": {"overview": "old task plan", "steps": [{"id": 1}]},
            "meta": {
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "task": "different task",
                "ttl": 86400,
            },
        }), encoding="utf-8")

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            result = load_cached_plan("abcdef123456", "my new task", config, logger)
        assert result is None

    def test_list_cache_entries(self, tmp_path):
        """列出缓存条目"""
        from agent_go.api import list_cache_entries
        cache_dir = tmp_path / "cache" / "plans"
        sub_dir = cache_dir / "aa"
        sub_dir.mkdir(parents=True)
        (sub_dir / "aaa.json").write_text(json.dumps({
            "cache_key": "aaa",
            "plan": {},
            "meta": {"created_at": "2026-01-01T00:00:00"},
        }), encoding="utf-8")

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            entries = list_cache_entries()
        assert len(entries) >= 1

    def test_clean_expired_cache(self, tmp_path):
        """清理过期缓存"""
        from agent_go.api import clean_expired_cache

        cache_dir = tmp_path / "cache" / "plans"
        sub_dir = cache_dir / "bb"
        sub_dir.mkdir(parents=True)
        (sub_dir / "bbb.json").write_text(json.dumps({
            "cache_key": "bbb",
            "plan": {"steps": [{"id": 1}]},
            "meta": {"created_at": "2020-01-01T00:00:00"},
        }), encoding="utf-8")

        config = {"cache": {"plan_ttl": 1}}

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            removed = clean_expired_cache(config)
        assert removed >= 1


class TestDecomposeFallback:
    """decompose_fallback 降级拆解"""

    def test_rule_based_jwt(self, logger):
        """JWT 关键词匹配规则拆解"""
        from agent_go.api import decompose_fallback
        config = {"fallback": {"enable_rules": True}}
        with patch("agent_go.api.re") as mock_re:
            result = decompose_fallback("implement JWT auth", Path("/tmp"), config, logger)
        # 即使 regex 匹配失败，应走 DECOMPOSE_RULES
        assert len(result) >= 1

    def test_rule_based_test(self, logger):
        """测试相关关键词"""
        from agent_go.api import decompose_fallback
        config = {"fallback": {"enable_rules": True}}
        result = decompose_fallback("add unit tests", Path("/tmp"), config, logger)
        assert len(result) >= 1

    def test_fallback_default(self, logger):
        """本地模型不可达时返回单步任务（规则兜底）。"""
        from agent_go.api import decompose_fallback
        config = {"fallback": {"enable_rules": True,
                               "local_model_url": "http://localhost:9999/v1/chat/completions",
                               "local_model_name": "claude-sonnet-4-6"}}
        result = decompose_fallback("do something random", Path("/tmp"), config, logger)
        assert len(result) == 1
        assert result[0]["id"] == "sub-1"
        assert result[0]["title"] == "执行主任务"

    def test_local_model_fallback(self, logger):
        """本地模型 API 失败后的规则兜底"""
        from agent_go.api import decompose_fallback
        config = {
            "fallback": {
                "local_model_url": "http://localhost:9999/v1/chat/completions",
                "local_model_name": "qwen",
                "enable_rules": True,
            }
        }
        # 本地模型不可达时应降级到 DECOMPOSE_RULES
        result = decompose_fallback("test JWT auth", Path("/tmp"), config, logger)
        assert len(result) >= 1

    def test_local_reachable_uses_llm(self, logger):
        """本地代理可达（localhost:4000）时用本地 LLM 分解（2026-08-12 修复 2）。"""
        from unittest.mock import patch
        from agent_go.api import decompose_fallback
        config = {"fallback": {"local_model_url": "http://localhost:4000/v1/chat/completions",
                               "local_model_name": "claude-sonnet-4-6",
                               "enable_rules": True}}
        fake_body = b'{"choices": [{"message": {"content": "[{\\"title\\": \\"A\\", \\"description\\": \\"d\\", \\"files_hint\\": \\"*\\", \\"agent_prompt\\": \\"p\\"}, {\\"title\\": \\"B\\", \\"description\\": \\"d2\\", \\"files_hint\\": \\"*\\", \\"agent_prompt\\": \\"p2\\"}]"}}]}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_body
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_ctx) as mock_open:
            result = decompose_fallback("do complex task", Path("/tmp"), config, logger)
        assert len(result) == 2
        assert result[0]["title"] == "A"
        mock_open.assert_called_once()

    def test_subtask_id_format(self, logger):
        """子任务 ID 格式 sub-1, sub-2, ..."""
        from agent_go.api import decompose_fallback
        config = {"fallback": {"enable_rules": True}}
        result = decompose_fallback("implement JWT token auth", Path("/tmp"), config, logger)
        for i, st in enumerate(result):
            assert st["id"] == f"sub-{i+1}"


class TestGeneratePlan:
    """generate_plan — prompt 构建与缓存逻辑"""

    def test_requires_api_key(self, logger):
        """无 API key 时抛出错误"""
        from agent_go.api import generate_plan
        config = {"plan_api": {"api_key": "", "provider": "anthropic",
                                "base_url": "https://api.anthropic.com/v1/messages",
                                "model": "test", "max_tokens": 100, "temperature": 0}}
        # Mock cache to return None (avoid cache loading path)
        with patch("agent_go.api.load_cached_plan", return_value=None):
            with patch("agent_go.api.get_cache_key", return_value="testkey"):
                with patch("agent_go.api.analyze_project", return_value=""):
                    with patch("agent_go.api.get_git_info", return_value={
                        "remote": "", "branch": "", "commit": ""
                    }):
                        with patch("agent_go.api.get_resource_map", return_value={
                            "directories": [], "key_files": []
                        }):
                            with patch("agent_go.api.list_skills", return_value=[]):
                                with patch("agent_go.api.load_role_skill_map", return_value={}):
                                    with pytest.raises(RuntimeError, match="API Key"):
                                        generate_plan("task", Path("/tmp"), config, logger)

    def test_local_url_skips_api_key_check(self, logger):
        """2026-08-12 纯本地模式：base_url 指向本机时无需 API key。"""
        import os
        saved = os.environ.pop("AGENT_GO_API_KEY", None)
        try:
            config = {
                "plan_api": {
                    "provider": "openai",
                    "base_url": "http://localhost:4000/v1/chat/completions",
                    "model": "claude-sonnet-4-6",
                    "api_key": "",
                }
            }
            fake_body = b'{"choices": [{"message": {"content": "ok"}}]}'
            mock_resp = MagicMock()
            mock_resp.read.return_value = fake_body
            mock_ctx = MagicMock()
            mock_ctx.__enter__.return_value = mock_resp
            with patch("urllib.request.urlopen", return_value=mock_ctx) as mock_open:
                content = call_api(config, [{"role": "user", "content": "hi"}], logger)
            assert content == "ok"
            mock_open.assert_called_once()
        finally:
            if saved is not None:
                os.environ["AGENT_GO_API_KEY"] = saved

    def test_cache_hit_on_first_iteration(self, logger):
        """第一次迭代且无补充/文档时检查缓存"""
        from agent_go.api import generate_plan
        config = {
            "plan_api": {"api_key": "sk-test", "provider": "anthropic",
                          "base_url": "https://api.anthropic.com/v1/messages",
                          "model": "test", "max_tokens": 100, "temperature": 0},
            "cache": {"enabled": True, "plan_ttl": 86400},
        }

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.load_cached_plan", return_value={
                "overview": "cached", "steps": [{"id": 1, "title": "step"}]
            }):
                with patch("agent_go.api.analyze_project", return_value=""):
                    with patch("agent_go.api.get_git_info", return_value={
                        "remote": "", "branch": "", "commit": ""
                    }):
                        with patch("agent_go.api.call_api") as mock_call:
                            result = generate_plan("task", Path("/tmp"), config, logger)

        assert result["overview"] == "cached"
        # 缓存命中时不应调用 API
        mock_call.assert_not_called()

    def test_project_files_truncated(self, logger):
        """超过 100 个文件时截断"""
        from agent_go.api import generate_plan
        many_files = "\n".join([f"file{i}.py" for i in range(150)])
        config = {
            "plan_api": {"api_key": "sk-test", "provider": "anthropic",
                          "base_url": "https://api.anthropic.com/v1/messages",
                          "model": "test", "max_tokens": 100, "temperature": 0},
            "cache": {"enabled": False},
        }

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.call_api") as mock_call:
                mock_call.return_value = '{"overview": "test", "steps": []}'
                with patch("agent_go.api.analyze_project", return_value=many_files):
                    with patch("agent_go.api.get_git_info", return_value={
                        "remote": "", "branch": "", "commit": ""
                    }):
                        with patch("agent_go.api.get_resource_map", return_value={
                            "directories": [], "key_files": []
                        }):
                            generate_plan("task", Path("/tmp"), config, logger, no_cache=True)

                # verify call_api had the truncated file list
                call_args = mock_call.call_args[0]
                user_content = call_args[1][1]["content"]
                assert "file0.py" in user_content
                assert "file149.py" not in user_content  # beyond 100

    def test_skill_context_truncated(self, logger):
        """Skill 上下文超过 system prompt 预算时截断"""
        from agent_go.api import generate_plan
        config = {
            "plan_api": {"api_key": "sk-test", "provider": "anthropic",
                          "base_url": "https://api.anthropic.com/v1/messages",
                          "model": "test", "max_tokens": 100, "temperature": 0},
            "cache": {"enabled": False},
        }

        # 非常大的 skill context
        long_skill = "x" * 10000

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.call_api") as mock_call:
                mock_call.return_value = '{"overview": "test", "steps": []}'
                with patch("agent_go.api.analyze_project", return_value=""):
                    with patch("agent_go.api.get_git_info", return_value={
                        "remote": "", "branch": "", "commit": ""
                    }):
                        with patch("agent_go.api.get_resource_map", return_value={
                            "directories": [], "key_files": []
                        }):
                            with patch("agent_go.api.list_skills", return_value=[]):
                                with patch("agent_go.api.load_role_skill_map", return_value={}):
                                    generate_plan("task", Path("/tmp"), config, logger,
                                                  skill_context=long_skill, no_cache=True)
        # 不应抛异常
        assert True

    def test_supplement_and_docs_passed(self, logger):
        """supplement 和 reference_docs 被正确传递"""
        from agent_go.api import generate_plan
        config = {
            "plan_api": {"api_key": "sk-test", "provider": "anthropic",
                          "base_url": "https://api.anthropic.com/v1/messages",
                          "model": "test", "max_tokens": 4096, "temperature": 0},
            "cache": {"enabled": False},
        }

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.call_api") as mock_call:
                mock_call.return_value = '{"overview": "test", "steps": []}'
                with patch("agent_go.api.analyze_project", return_value=""):
                    with patch("agent_go.api.get_git_info", return_value={
                        "remote": "", "branch": "", "commit": ""
                    }):
                        with patch("agent_go.api.get_resource_map", return_value={
                            "directories": [], "key_files": []
                        }):
                            with patch("agent_go.api.list_skills", return_value=[]):
                                with patch("agent_go.api.load_role_skill_map", return_value={}):
                                    generate_plan("task", Path("/tmp"), config, logger,
                                                  supplement="extra info",
                                                  reference_docs="## Docs\ncontent",
                                                  no_cache=True)

                call_args = mock_call.call_args[0]
                user_content = call_args[1][1]["content"]
                assert "extra info" in user_content
                assert "Docs" in user_content

    def test_router_enabled_uses_config_task_id(self, logger):
        """router.enabled=true 时走 call_with_role，task_id 取自 config（回归：api.py 曾引用未定义的 task_id 变量导致 NameError）"""
        from agent_go.api import generate_plan
        config = {
            "plan_api": {"api_key": "sk-test", "provider": "anthropic",
                          "base_url": "https://api.anthropic.com/v1/messages",
                          "model": "test", "max_tokens": 100, "temperature": 0},
            "cache": {"enabled": False},
            "router": {"enabled": True},
            "_task_id": "task-xyz",
        }
        from types import SimpleNamespace
        fake_route = SimpleNamespace(
            role="planner",
            primary=SimpleNamespace(provider="anthropic", model="test"),
        )

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.resolve_provider", return_value=fake_route):
                with patch("agent_go.api.call_with_role") as mock_route_call:
                    mock_route_call.return_value = ('{"overview": "routed", "steps": []}', {"role": "planner"})
                    with patch("agent_go.api.analyze_project", return_value=""):
                        with patch("agent_go.api.get_git_info", return_value={
                            "remote": "", "branch": "", "commit": ""
                        }):
                            with patch("agent_go.api.get_resource_map", return_value={
                                "directories": [], "key_files": []
                            }):
                                with patch("agent_go.api.list_skills", return_value=[]):
                                    with patch("agent_go.api.load_role_skill_map", return_value={}):
                                        result = generate_plan("task", Path("/tmp"), config, logger, no_cache=True)

        assert result["overview"] == "routed"
        assert mock_route_call.call_args.kwargs["task_id"] == "task-xyz"


class TestCacheEnabledRead:
    """cache.enabled 对读取路径的约束（回归 docs/ISSUES.md ISSUE-10）"""

    def test_cache_disabled_does_not_load(self, tmp_path, logger):
        """cache.enabled=False 时 load_cached_plan 直接返回 None"""
        from agent_go.api import save_cached_plan, load_cached_plan
        key = "ab" * 32
        plan = {"overview": "test", "steps": [{"id": 1, "title": "step1"}]}
        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            save_cached_plan(key, plan, "task", tmp_path,
                             {"cache": {"enabled": True, "plan_ttl": 86400}})
            # 文件确实已写入
            assert (tmp_path / "cache" / "plans" / key[:2] / f"{key}.json").exists()
            loaded = load_cached_plan(key, "task", {"cache": {"enabled": False}}, logger)
        assert loaded is None

    def test_cache_enabled_loads_normally(self, tmp_path, logger):
        """cache.enabled=True（默认）时读取不受影响"""
        from agent_go.api import save_cached_plan, load_cached_plan
        key = "cd" * 32
        plan = {"overview": "test", "steps": [{"id": 1, "title": "step1"}]}
        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            save_cached_plan(key, plan, "task", tmp_path,
                             {"cache": {"enabled": True, "plan_ttl": 86400}})
            loaded = load_cached_plan(key, "task", {"cache": {"enabled": True, "plan_ttl": 86400}}, logger)
        assert loaded is not None
        assert loaded["overview"] == "test"


# ═══════════════════════════════════════════════════════════════
# call_api 错误路径
# ═══════════════════════════════════════════════════════════════

import urllib.error

from agent_go.config import DEFAULT_CONFIG


class MockRawResponse:
    """返回原始字节内容的响应（用于非 JSON 响应体测试）"""

    def __init__(self, raw_body, status=200):
        self._raw_body = raw_body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self._raw_body


class TestCallApiErrors:
    """call_api 错误路径：HTTP 4xx/5xx、网络错误、超时/IO、响应解析失败"""

    MESSAGES = [{"role": "user", "content": "hi"}]

    @staticmethod
    def _config(provider="anthropic"):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["plan_api"]["provider"] = provider
        config["plan_api"]["api_key"] = "sk-test-key"
        return config

    @patch("urllib.request.urlopen")
    def test_http_error_4xx_with_body(self, mock_urlopen, logger):
        """HTTP 4xx：错误 body 被读取并包含在异常信息中"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 400, "Bad Request",
            None, io.BytesIO(b'{"error": {"message": "invalid request"}}'))

        with pytest.raises(RuntimeError) as exc_info:
            call_api(self._config(), self.MESSAGES, logger)
        assert "HTTP 400" in str(exc_info.value)
        assert "invalid request" in str(exc_info.value)

    @patch("urllib.request.urlopen")
    def test_http_error_5xx_with_body(self, mock_urlopen, logger):
        """HTTP 5xx：错误 body 被读取并包含在异常信息中"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 500, "Internal Server Error",
            None, io.BytesIO(b"Internal Server Error"))

        with pytest.raises(RuntimeError) as exc_info:
            call_api(self._config(), self.MESSAGES, logger)
        assert "HTTP 500" in str(exc_info.value)
        assert "Internal Server Error" in str(exc_info.value)

    @patch("urllib.request.urlopen")
    def test_http_error_body_read_failure(self, mock_urlopen, logger):
        """HTTP 错误 body 读取失败时降级为 str(e)，仍抛出含状态码的 RuntimeError"""
        broken_fp = MagicMock()
        broken_fp.read.side_effect = OSError("stream broken")
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 429, "Too Many Requests",
            None, broken_fp)

        with pytest.raises(RuntimeError) as exc_info:
            call_api(self._config(), self.MESSAGES, logger)
        assert "HTTP 429" in str(exc_info.value)

    @patch("urllib.request.urlopen")
    def test_url_error(self, mock_urlopen, logger):
        """URLError 网络错误"""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        with pytest.raises(RuntimeError, match="网络错误"):
            call_api(self._config(), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_timeout_error(self, mock_urlopen, logger):
        """超时（TimeoutError / socket.timeout）"""
        mock_urlopen.side_effect = TimeoutError("timed out")

        with pytest.raises(RuntimeError, match="连接超时或 IO 错误"):
            call_api(self._config(), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_os_error(self, mock_urlopen, logger):
        """其他 IO 错误（OSError 子类，如连接被重置）"""
        mock_urlopen.side_effect = ConnectionResetError("Connection reset by peer")

        with pytest.raises(RuntimeError, match="连接超时或 IO 错误"):
            call_api(self._config(), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_invalid_json_response(self, mock_urlopen, logger):
        """响应体无法解析为 JSON"""
        mock_urlopen.return_value = MockRawResponse(b"<html>502 Bad Gateway</html>")

        with pytest.raises(RuntimeError, match="无法解析为 JSON"):
            call_api(self._config(), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_anthropic_response_missing_content(self, mock_urlopen, logger):
        """Anthropic 响应缺少 content 字段"""
        mock_urlopen.return_value = MockResponse({"id": "msg_1", "usage": {}})

        with pytest.raises(RuntimeError, match="响应结构异常"):
            call_api(self._config(), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_anthropic_response_empty_content(self, mock_urlopen, logger):
        """Anthropic 响应 content 为空数组（IndexError 路径）"""
        mock_urlopen.return_value = MockResponse({"content": []})

        with pytest.raises(RuntimeError, match="响应结构异常"):
            call_api(self._config(), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_openai_response_missing_choices(self, mock_urlopen, logger):
        """OpenAI 响应缺少 choices 字段"""
        mock_urlopen.return_value = MockResponse({"id": "chatcmpl-1"})

        with pytest.raises(RuntimeError, match="响应结构异常"):
            call_api(self._config(provider="openai"), self.MESSAGES, logger)

    @patch("urllib.request.urlopen")
    def test_response_not_a_dict(self, mock_urlopen, logger):
        """响应 JSON 不是对象（TypeError 路径）"""
        mock_urlopen.return_value = MockRawResponse(b'["not", "a", "dict"]')

        with pytest.raises(RuntimeError, match="响应结构异常"):
            call_api(self._config(), self.MESSAGES, logger)


# ═══════════════════════════════════════════════════════════════
# generate_plan：路由失败传播与缓存写入
# ═══════════════════════════════════════════════════════════════

class TestGeneratePlanRouterFailure:
    """router.enabled=true 时 call_with_role 抛错的传播路径"""

    @staticmethod
    def _config():
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["plan_api"]["api_key"] = "sk-test"
        config["router"] = {"enabled": True}
        return config

    @staticmethod
    def _fake_route():
        from types import SimpleNamespace
        return SimpleNamespace(
            role="planner",
            primary=SimpleNamespace(provider="anthropic", model="test"),
        )

    def test_call_with_role_error_propagates(self, logger):
        """call_with_role 失败时异常向上传播（由调用方 cli.py 重试/降级），
        generate_plan 内部不静默降级到 call_api"""
        from agent_go.api import generate_plan
        config = self._config()

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.resolve_provider", return_value=self._fake_route()):
                with patch("agent_go.api.call_with_role",
                           side_effect=RuntimeError("路由调用失败：primary 不可用")):
                    with patch("agent_go.api.call_api") as mock_call_api:
                        with patch("agent_go.api.analyze_project", return_value=""):
                            with patch("agent_go.api.get_git_info", return_value={
                                "remote": "", "branch": "", "commit": ""
                            }):
                                with patch("agent_go.api.get_resource_map", return_value={
                                    "directories": [], "key_files": []
                                }):
                                    with patch("agent_go.api.list_skills", return_value=[]):
                                        with patch("agent_go.api.load_role_skill_map", return_value={}):
                                            with pytest.raises(RuntimeError, match="路由调用失败"):
                                                generate_plan("task", Path("/tmp"), config, logger,
                                                              no_cache=True)
        # 路由失败后不应回退到非路由的 call_api
        mock_call_api.assert_not_called()

    def test_call_with_role_error_skips_cache_write(self, logger):
        """路由调用失败时不写入 Plan 缓存"""
        from agent_go.api import generate_plan
        config = self._config()
        config["cache"] = {"enabled": True, "plan_ttl": 86400}

        with patch("agent_go.api.get_api_key", return_value="sk-test"):
            with patch("agent_go.api.resolve_provider", return_value=self._fake_route()):
                with patch("agent_go.api.call_with_role",
                           side_effect=RuntimeError("路由调用失败：primary 不可用")):
                    with patch("agent_go.api.get_cache_key", return_value="deadbeef"):
                        with patch("agent_go.api.load_cached_plan", return_value=None):
                            with patch("agent_go.api.save_cached_plan") as mock_save:
                                with patch("agent_go.api.analyze_project", return_value=""):
                                    with patch("agent_go.api.get_git_info", return_value={
                                        "remote": "", "branch": "", "commit": ""
                                    }):
                                        with patch("agent_go.api.get_resource_map", return_value={
                                            "directories": [], "key_files": []
                                        }):
                                            with patch("agent_go.api.list_skills", return_value=[]):
                                                with patch("agent_go.api.load_role_skill_map", return_value={}):
                                                    with pytest.raises(RuntimeError, match="路由调用失败"):
                                                        generate_plan("task", Path("/tmp"), config, logger)
        mock_save.assert_not_called()


class TestGeneratePlanCacheWrite:
    """generate_plan 成功后的 Plan 缓存写入路径"""

    @staticmethod
    def _config():
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["plan_api"]["api_key"] = "sk-test"
        config["cache"] = {"enabled": True, "plan_ttl": 86400}
        return config

    def test_successful_plan_saved_to_cache(self, tmp_path, logger):
        """首次生成成功后写入缓存；二次调用命中缓存、不再请求 API"""
        from agent_go.api import generate_plan
        config = self._config()
        plan_json = '{"overview": "fresh plan", "steps": [{"id": 1, "title": "step1"}]}'

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            with patch("agent_go.api.get_api_key", return_value="sk-test"):
                with patch("agent_go.api.call_api", return_value=plan_json) as mock_call:
                    with patch("agent_go.api.analyze_project", return_value="file1.py"):
                        with patch("agent_go.api.get_git_info", return_value={
                            "remote": "", "branch": "main", "commit": ""
                        }):
                            with patch("agent_go.api.get_resource_map", return_value={
                                "directories": [], "key_files": []
                            }):
                                with patch("agent_go.api.list_skills", return_value=[]):
                                    with patch("agent_go.api.load_role_skill_map", return_value={}):
                                        plan1 = generate_plan("task", tmp_path, config, logger)
                                        plan2 = generate_plan("task", tmp_path, config, logger)

        assert plan1["overview"] == "fresh plan"
        assert plan2["overview"] == "fresh plan"
        # 第二次调用命中缓存，API 只被请求一次
        assert mock_call.call_count == 1
        # 缓存文件确实已写入
        cache_files = list((tmp_path / "cache" / "plans").glob("*/*.json"))
        assert len(cache_files) == 1

    def test_no_cache_skips_cache_write(self, tmp_path, logger):
        """no_cache=True 时不读写缓存"""
        from agent_go.api import generate_plan
        config = self._config()
        plan_json = '{"overview": "fresh plan", "steps": [{"id": 1, "title": "step1"}]}'

        with patch("agent_go.api.AGENT_GO_DIR", tmp_path):
            with patch("agent_go.api.get_api_key", return_value="sk-test"):
                with patch("agent_go.api.call_api", return_value=plan_json):
                    with patch("agent_go.api.analyze_project", return_value="file1.py"):
                        with patch("agent_go.api.get_git_info", return_value={
                            "remote": "", "branch": "main", "commit": ""
                        }):
                            with patch("agent_go.api.get_resource_map", return_value={
                                "directories": [], "key_files": []
                            }):
                                with patch("agent_go.api.list_skills", return_value=[]):
                                    with patch("agent_go.api.load_role_skill_map", return_value={}):
                                        plan = generate_plan("task", tmp_path, config, logger,
                                                             no_cache=True)

        assert plan["overview"] == "fresh plan"
        assert not (tmp_path / "cache" / "plans").exists()


# ═══════════════════════════════════════════════════════════════
# 覆盖补强（P0-3）：planner_api 隔离（PRD 铁律：planner 不降级到弱模型）
# ═══════════════════════════════════════════════════════════════

class TestPlannerApiIsolation:
    """planner_api 覆盖 plan_api 仅用于 plan 生成；不走 worker proxy。
    回归会让 planner 流量走弱模型/代理 → 规划质量降、worker 成本膨胀。"""

    @patch("urllib.request.urlopen")
    def test_planner_api_overrides_plan_api(self, mock_urlopen, logger):
        """planner_api 配置 → 请求走 planner_api 的 base_url + model（非 plan_api）。"""
        import json as _json
        mock_urlopen.return_value = MockResponse({"choices": [{"message": {"content": "plan"}}]})
        config = {
            "plan_api": {"provider": "openai", "base_url": "http://proxy:4000/v1/chat",
                         "model": "weak-proxy-model", "api_key": "k"},
            "planner_api": {"provider": "openai", "base_url": "http://direct-llm/v1/chat",
                            "model": "strong-planner-model", "api_key": "k"},
        }
        call_api(config, [{"role": "user", "content": "hi"}], logger)
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://direct-llm/v1/chat"
        assert _json.loads(req.data.decode("utf-8"))["model"] == "strong-planner-model"

    @patch("urllib.request.urlopen")
    def test_planner_api_empty_falls_back_to_plan_api(self, mock_urlopen, logger):
        """planner_api 未配置/空 → 回退 plan_api（向后兼容）。"""
        import json as _json
        mock_urlopen.return_value = MockResponse({"choices": [{"message": {"content": "plan"}}]})
        config = {
            "plan_api": {"provider": "openai", "base_url": "http://proxy:4000/v1/chat",
                         "model": "fallback-model", "api_key": "k"},
            "planner_api": {},  # 空 → 回退
        }
        call_api(config, [{"role": "user", "content": "hi"}], logger)
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://proxy:4000/v1/chat"
        assert _json.loads(req.data.decode("utf-8"))["model"] == "fallback-model"

    @patch("urllib.request.urlopen")
    def test_planner_api_absent_falls_back_to_plan_api(self, mock_urlopen, logger):
        """config 无 planner_api 键 → 回退 plan_api。"""
        import json as _json
        mock_urlopen.return_value = MockResponse({"choices": [{"message": {"content": "plan"}}]})
        config = {
            "plan_api": {"provider": "openai", "base_url": "http://proxy:4000/v1/chat",
                         "model": "fallback-model", "api_key": "k"},
        }
        call_api(config, [{"role": "user", "content": "hi"}], logger)
        req = mock_urlopen.call_args[0][0]
        assert _json.loads(req.data.decode("utf-8"))["model"] == "fallback-model"
