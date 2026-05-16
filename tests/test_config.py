"""测试 load_config / get_api_key — 配置加载与 API Key 解析"""

import os
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_go import load_config, get_api_key, AGENT_GO_DIR, CONFIG_PATH, DEFAULT_CONFIG


class TestLoadConfig:
    """load_config 功能测试"""

    def setup_method(self):
        """测试前保存现场"""
        self._saved_config = None
        if CONFIG_PATH.exists():
            self._saved_config = CONFIG_PATH.read_text(encoding="utf-8")

    def teardown_method(self):
        """测试后恢复现场"""
        if self._saved_config is not None:
            CONFIG_PATH.write_text(self._saved_config, encoding="utf-8")
        elif CONFIG_PATH.exists():
            CONFIG_PATH.unlink()

    def test_default_config_creation(self, monkeypatch):
        """当 ~/.agent_go/config.json 不存在时创建默认配置"""
        # 确保配置不存在
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        # 清理 AGENT_GO_DIR 缓存
        config = load_config()
        assert config["plan_api"]["provider"] == "anthropic"
        assert CONFIG_PATH.exists()
        # 验证文件权限
        mode = os.stat(CONFIG_PATH).st_mode & 0o777
        assert mode == 0o600, f"权限应为 600，实际: {oct(mode)}"

    def test_load_existing_config(self, monkeypatch):
        """读取已有配置"""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "plan_api": {"provider": "openai", "model": "gpt-4o"}
        }, ensure_ascii=False), encoding="utf-8")

        config = load_config()
        # 用户配置覆盖
        assert config["plan_api"]["provider"] == "openai"
        assert config["plan_api"]["model"] == "gpt-4o"
        # 默认值保留
        assert config["plan_api"]["max_tokens"] == 4096
        assert config["behavior"]["auto_confirm_plan"] == False

    def test_merge_nested_dicts(self, monkeypatch):
        """深层字典合并"""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "behavior": {"auto_confirm_plan": True}
        }), encoding="utf-8")
        config = load_config()
        # 用户值覆盖
        assert config["behavior"]["auto_confirm_plan"] == True
        # 默认值保留
        assert config["behavior"]["auto_confirm_subtasks"] == False

    def test_new_key_added_to_config(self, monkeypatch):
        """新字段加入 DEFAULT_CONFIG 时向前兼容"""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "behavior": {"auto_confirm_plan": True}
        }), encoding="utf-8")
        config = load_config()
        # 即使旧 config 没有这些字段，返回中也应有默认值
        assert "auto_verify_subtask" in config["behavior"]
        assert "show_agent_prompt" in config["behavior"]


class TestGetApiKey:
    """get_api_key 功能测试"""

    def test_from_env_var(self):
        """环境变量 AGENT_GO_API_KEY 优先级最高"""
        os.environ["AGENT_GO_API_KEY"] = "env-key-123"
        config = {"plan_api": {"api_key": "config-key-456"}}
        try:
            assert get_api_key(config) == "env-key-123"
        finally:
            del os.environ["AGENT_GO_API_KEY"]

    def test_from_config_when_no_env(self):
        """无环境变量时使用配置文件中的 key"""
        saved = os.environ.pop("AGENT_GO_API_KEY", None)
        try:
            config = {"plan_api": {"api_key": "config-key-789"}}
            assert get_api_key(config) == "config-key-789"
        finally:
            if saved is not None:
                os.environ["AGENT_GO_API_KEY"] = saved

    def test_empty_when_no_key(self):
        """无任何 key 配置时返回空字符串"""
        saved = os.environ.pop("AGENT_GO_API_KEY", None)
        try:
            config = {"plan_api": {}}
            assert get_api_key(config) == ""
        finally:
            if saved is not None:
                os.environ["AGENT_GO_API_KEY"] = saved
