"""测试 call_api — LLM API 调用

通过 mock urllib.request 避免真实网络请求。
"""

from unittest.mock import patch, MagicMock
import json
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go import call_api, get_api_key


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
