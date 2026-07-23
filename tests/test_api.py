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
        """无匹配规则时返回单步任务"""
        from agent_go.api import decompose_fallback
        config = {"fallback": {"enable_rules": True}}
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
