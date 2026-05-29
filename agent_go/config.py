import sys, os, subprocess, json, re, time, threading, shlex, signal, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

__all__ = [
    "AGENT_GO_DIR", "CONFIG_PATH", "DEFAULT_CONFIG", "DECOMPOSE_RULES",
    "safe_input", "load_config", "get_api_key", "setup_logger", "log_event",
]

AGENT_GO_DIR = Path.home() / ".agent_go"
AGENT_GO_DIR.mkdir(exist_ok=True)
CONFIG_PATH = AGENT_GO_DIR / "config.json"

DEFAULT_CONFIG = {
    "plan_api": {
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com/v1/messages",
        "api_key": "",
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "temperature": 0.2
    },
    "behavior": {
        "auto_confirm_plan": False,         # 默认同意 Plan 方案
        "auto_confirm_subtasks": False,     # 默认同意子任务列表
        "auto_verify_subtask": False,       # 默认同意子任务验证结果
        "show_agent_prompt": True,          # 展示给 Agent 的 Prompt
        "show_resource_map": True,           # 展示共享资源清单
        "max_plan_iterations": 5             # 最大 Plan 重生成次数
    },
    "fallback": {
        "local_model_url": "http://localhost:8000/v1/chat/completions",
        "local_model_name": "qwen",
        "enable_rules": True
    },
    "skills": {
        "auto_discover": False,     # 是否自动匹配 Skill（基于任务描述）
        "max_auto_skills": 3       # 自动匹配时最多加载 N 个 Skill
    },
    "agents": {
        "default": "developer"      # 默认 Agent 类型
    },
    "cache": {
        "enabled": True,
        "plan_ttl": 86400,          # Plan 缓存有效期（秒），默认 24h
        "max_entries": 100          # 最大缓存条目数
    }
}

DECOMPOSE_RULES = [
    {
        "patterns": ["JWT", "jwt", "auth", "认证", "token"],
        "subtasks": [
            {"id": "sub-1", "title": "后端JWT签名迁移", "description": "将后端JWT签名算法从HS256迁移至RS256，生成RSA密钥对并更新签名/验证逻辑", "files_hint": "src/auth/**"},
            {"id": "sub-2", "title": "前端登录适配", "description": "前端适配新的公钥获取流程，更新登录页JWT解析和验证逻辑", "files_hint": "src/pages/login/**"},
            {"id": "sub-3", "title": "测试补充", "description": "补充RS256相关的单元测试和端到端测试", "files_hint": "tests/**"},
        ]
    },
    {
        "patterns": ["test", "测试", "coverage"],
        "subtasks": [
            {"id": "sub-1", "title": "分析现有测试覆盖", "description": "识别当前测试未覆盖的模块和函数", "files_hint": "tests/**, src/**"},
            {"id": "sub-2", "title": "编写补充测试", "description": "为未覆盖模块添加单元测试和集成测试", "files_hint": "tests/**"},
        ]
    },
]

def safe_input(prompt=""):
    """包装 input()，在非交互模式下返回空字符串（触发默认确认路径）。"""
    try:
        return input(prompt)
    except EOFError:
        print()
        return ""

def load_config():
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"⚙️  已创建默认配置: {CONFIG_PATH}")
    return DEFAULT_CONFIG

def get_api_key(config):
    return os.environ.get("AGENT_GO_API_KEY", "") or config.get("plan_api", {}).get("api_key", "")

def setup_logger(task_id, task_dir):
    logger = logging.getLogger(f"agent_go.{task_id}")
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fh = logging.FileHandler(task_dir / "execution.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

def log_event(logger, event, data):
    logger.debug(json.dumps({"timestamp": datetime.now().isoformat(), "event": event, **data}, ensure_ascii=False))
