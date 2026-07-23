"""测试 config.py — safe_input / setup_logger / log_event / DECOMPOSE_RULES

覆盖 config.py 中尚未被 test_config.py 覆盖的内部函数。
"""

import os
import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent_go.config import (
    safe_input,
    setup_logger,
    log_event,
    DECOMPOSE_RULES,
    DEFAULT_CONFIG,
)


# ═══════════════════════════════════════════════════════════════
# safe_input
# ═══════════════════════════════════════════════════════════════

class TestSafeInput:
    """safe_input 包装测试"""

    def test_normal_input(self):
        with patch("builtins.input", return_value="yes"):
            result = safe_input("Confirm? ")
        assert result == "yes"

    def test_eof_error_returns_empty(self):
        """EOFError（非交互模式）返回空字符串"""
        with patch("builtins.input", side_effect=EOFError):
            result = safe_input("Confirm? ")
        assert result == ""

    def test_empty_input(self):
        with patch("builtins.input", return_value=""):
            result = safe_input("")
        assert result == ""

    def test_unicode_input(self):
        with patch("builtins.input", return_value="确认"):
            result = safe_input("确认吗？")
        assert result == "确认"


# ═══════════════════════════════════════════════════════════════
# setup_logger
# ═══════════════════════════════════════════════════════════════

class TestSetupLogger:
    """setup_logger 日志初始化测试"""

    def test_creates_logger_with_name(self, tmp_path):
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        logger = setup_logger("task-001", task_dir)
        assert logger.name == "agent_go.task-001"
        assert logger.level == logging.DEBUG

    def test_creates_log_file(self, tmp_path):
        task_dir = tmp_path / "task-002"
        task_dir.mkdir()
        logger = setup_logger("task-002", task_dir)
        log_file = task_dir / "execution.log"
        logger.info("test message")
        assert log_file.exists()

    def test_adds_file_handler(self, tmp_path):
        task_dir = tmp_path / "task-003"
        task_dir.mkdir()
        logger = setup_logger("task-003", task_dir)
        # 至少有一个 FileHandler
        has_file_handler = any(
            isinstance(h, logging.FileHandler) for h in logger.handlers
        )
        assert has_file_handler

    def test_adds_stream_handler(self, tmp_path):
        task_dir = tmp_path / "task-004"
        task_dir.mkdir()
        logger = setup_logger("task-004", task_dir)
        has_stream_handler = any(
            isinstance(h, logging.StreamHandler) for h in logger.handlers
        )
        assert has_stream_handler

    def test_clears_existing_handlers(self, tmp_path):
        """重复 setup 时清除旧 handlers"""
        task_dir = tmp_path / "task-005"
        task_dir.mkdir()
        logger1 = setup_logger("task-005", task_dir)
        handler_count_before = len(logger1.handlers)
        logger2 = setup_logger("task-005", task_dir)
        # 旧的 handlers 被清除，重新创建
        assert len(logger2.handlers) == handler_count_before

    def test_file_handler_is_debug_level(self, tmp_path):
        task_dir = tmp_path / "task-006"
        task_dir.mkdir()
        logger = setup_logger("task-006", task_dir)
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                assert h.level == logging.DEBUG

    def test_stream_handler_is_info_level(self, tmp_path):
        task_dir = tmp_path / "task-007"
        task_dir.mkdir()
        logger = setup_logger("task-007", task_dir)
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                assert h.level == logging.INFO

    def test_utf8_encoding(self, tmp_path):
        """日志文件支持 UTF-8 编码"""
        task_dir = tmp_path / "task-008"
        task_dir.mkdir()
        logger = setup_logger("task-008", task_dir)
        logger.info("中文日志消息")
        log_file = task_dir / "execution.log"
        content = log_file.read_text(encoding="utf-8")
        assert "中文日志消息" in content


# ═══════════════════════════════════════════════════════════════
# log_event
# ═══════════════════════════════════════════════════════════════

class TestLogEvent:
    """log_event 结构化事件日志测试"""

    def test_writes_json_event(self, tmp_path):
        """log_event 写入 JSON 格式的 DEBUG 事件"""
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        logger = setup_logger("task-001", task_dir)

        log_event(logger, "plan_complete", {"duration_ms": 500, "iteration": 1})

        log_file = task_dir / "execution.log"
        content = log_file.read_text(encoding="utf-8")

        # 验证 JSON 事件结构
        assert "plan_complete" in content
        assert "duration_ms" in content
        assert "500" in content

    def test_event_has_timestamp(self, tmp_path):
        task_dir = tmp_path / "task-002"
        task_dir.mkdir()
        logger = setup_logger("task-002", task_dir)

        log_event(logger, "test_event", {"data": "value"})

        log_file = task_dir / "execution.log"
        content = log_file.read_text(encoding="utf-8")
        assert "timestamp" in content
        assert "test_event" in content

    def test_multiple_events(self, tmp_path):
        task_dir = tmp_path / "task-003"
        task_dir.mkdir()
        logger = setup_logger("task-003", task_dir)

        for i in range(3):
            log_event(logger, "step", {"step": i})

        log_file = task_dir / "execution.log"
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3

    def test_event_with_empty_data(self, tmp_path):
        task_dir = tmp_path / "task-004"
        task_dir.mkdir()
        logger = setup_logger("task-004", task_dir)

        log_event(logger, "empty", {})

        log_file = task_dir / "execution.log"
        content = log_file.read_text(encoding="utf-8")
        assert "empty" in content

    def test_unicode_in_event_data(self, tmp_path):
        task_dir = tmp_path / "task-005"
        task_dir.mkdir()
        logger = setup_logger("task-005", task_dir)

        log_event(logger, "task_start", {"title": "中文任务名称"})

        log_file = task_dir / "execution.log"
        content = log_file.read_text(encoding="utf-8")
        assert "中文任务名称" in content


# ═══════════════════════════════════════════════════════════════
# DECOMPOSE_RULES 结构验证
# ═══════════════════════════════════════════════════════════════

class TestDecomposeRules:
    """DECOMPOSE_RULES fallback 规则结构测试"""

    def test_has_rules(self):
        assert len(DECOMPOSE_RULES) >= 2

    def test_each_rule_has_patterns(self):
        for rule in DECOMPOSE_RULES:
            assert "patterns" in rule
            assert isinstance(rule["patterns"], list)
            assert len(rule["patterns"]) > 0

    def test_each_rule_has_subtasks(self):
        for rule in DECOMPOSE_RULES:
            assert "subtasks" in rule
            assert isinstance(rule["subtasks"], list)
            assert len(rule["subtasks"]) > 0

    def test_each_subtask_has_required_fields(self):
        for rule in DECOMPOSE_RULES:
            for st in rule["subtasks"]:
                assert "id" in st
                assert "title" in st
                assert "description" in st

    def test_subtask_ids_are_unique_per_rule(self):
        for rule in DECOMPOSE_RULES:
            ids = [st["id"] for st in rule["subtasks"]]
            assert len(ids) == len(set(ids)), f"Duplicate ids in rule: {rule['patterns']}"

    def test_jwt_rule_content(self):
        jwt_rule = next(r for r in DECOMPOSE_RULES if "JWT" in r["patterns"])
        assert jwt_rule is not None
        subtask_titles = [st["title"] for st in jwt_rule["subtasks"]]
        assert any("JWT" in t for t in subtask_titles)
        assert any("测试" in t for t in subtask_titles)


# ═══════════════════════════════════════════════════════════════
# DEFAULT_CONFIG 结构验证
# ═══════════════════════════════════════════════════════════════

class TestDefaultConfig:
    """DEFAULT_CONFIG 完整性验证"""

    def test_has_required_sections(self):
        required = ["plan_api", "behavior", "fallback", "skills", "agents", "cache"]
        for section in required:
            assert section in DEFAULT_CONFIG, f"Missing section: {section}"

    def test_plan_api_fields(self):
        api = DEFAULT_CONFIG["plan_api"]
        assert "provider" in api
        assert "base_url" in api
        assert "model" in api
        assert "max_tokens" in api
        assert "temperature" in api

    def test_behavior_fields(self):
        behavior = DEFAULT_CONFIG["behavior"]
        assert "auto_confirm_plan" in behavior
        assert "auto_confirm_subtasks" in behavior
        assert "max_plan_iterations" in behavior

    def test_fallback_fields(self):
        fallback = DEFAULT_CONFIG["fallback"]
        assert "local_model_url" in fallback
        assert "local_model_name" in fallback
        assert "enable_rules" in fallback

    def test_skills_fields(self):
        skills = DEFAULT_CONFIG["skills"]
        assert "auto_discover" in skills
        assert "max_auto_skills" in skills

    def test_agents_fields(self):
        agents = DEFAULT_CONFIG["agents"]
        assert "default" in agents

    def test_cache_fields(self):
        cache = DEFAULT_CONFIG["cache"]
        assert "enabled" in cache
        assert "plan_ttl" in cache
        assert "max_entries" in cache
