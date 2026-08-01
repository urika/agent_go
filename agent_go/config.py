import os, json, logging, threading
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from .console import _LazyConsole

console = _LazyConsole()

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
        "temperature": 0.2,
        "timeout_ms": 180000,
        "worker_base_url": "",
        "worker_max_tokens": 0,
        "local_models": []
    },
    "planner_api": {},              # Plan 生成专用 API 配置（非空时覆盖 plan_api，直连 LLM 不走 proxy）
    "behavior": {
        "auto_confirm_plan": False,         # 默认同意 Plan 方案
        "auto_confirm_subtasks": False,     # 默认同意子任务列表
        "auto_verify_subtask": False,       # 默认同意子任务验证结果
        "show_agent_prompt": True,          # 展示给 Agent 的 Prompt
        "show_resource_map": True,           # 展示共享资源清单
        "max_plan_iterations": 5,            # 最大 Plan 重生成次数
        "notify_on_complete": True,          # 任务完成时发桌面通知（macOS osascript）
        "notify_command": "",                # 自定义通知命令，如 "curl -X POST ..."
    },
    "verification": {
        "max_retries": 3,               # 验证失败后最大修复重试次数
        "retry_timeout": 300,            # 每次修复重试超时（秒）
        "block_on_failure": True,        # 验证失败是否阻断下游依赖（--no-verify-block 可关）
    },
    "goal": {
        "enabled": False,               # 是否在 TASK.md 注入 goal 指令（--goal 开启）
        "max_turns": 20,                # 单个 goal 循环最大 tool-call 轮数
        "timeout_seconds": 600,          # goal 循环全局超时（秒）
        "enable_goal_hook": False,      # 是否注入 Stop Hook（.claude/settings.json + verify-goal.sh）
    },
    "agent_loop": {
        "enabled": False,               # 默认关闭，--agent-loop 开启
        "max_turns": 20,                # 最大对话轮数
        "max_duration": 600,            # 全局超时（秒）
        "api_timeout": 120,             # 单次 API 调用超时（秒）
    },
    "evaluator": {
        "enabled": False,               # 默认关闭（向后兼容 + 成本可控）
        "fail_closed": False,           # 评估器 API 失败时是否阻断流程（true=标记失败，false=默认通过）
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "base_url": "https://api.anthropic.com/v1/messages",
        "api_key": "",                  # 空字符串 = 复用 AGENT_GO_API_KEY
        "prompt_template": "default",   # 可扩展 prompt 模板名
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
    "artifact_dir": None,           # S9-B 产物导出目录；null = 不导出（产物留在 worktree，向后兼容）
    "worker_models": {
        "easy": "",                 # S4 复杂度双通道：空 = claude CLI 默认模型
        "medium": "",
        "hard": "",                 # 如 "claude-opus-4-20250514"，hard 子任务走强模型
    },
    "worker_models_fallback": {     # retry/timeout 时的模型升级表（空 = 不升级）
        "easy": "",
        "medium": "",
        "hard": "",
    },
    "cache": {
        "enabled": True,
        "plan_ttl": 86400,          # Plan 缓存有效期（秒），默认 24h
        "max_entries": 100          # 最大缓存条目数
    },
    "router": {
        "enabled": False,           # 默认关闭，向后兼容
        "roles": {},                # 角色路由配置，格式见 docs/in/role-aware-routing-design.md
        "agent_type_mapping": {     # Agent 类型 → 路由角色
            "developer": "worker",
            "architect": "planner",
            "reviewer": "reviewer",
            "tester": "worker",
        },
        "circuit_breaker": {
            "failure_threshold": 5,         # 连续 N 次可用性失败 → 熔断
            "cooldown_seconds": 60,          # 熔断 N 秒后半开试探
            "half_open_requests": 2,         # 半开时允许通过的请求数
        },
    },
    "mcp_servers": {
        # 外部 MCP server 配置（S9-A 消费层）。
        # 格式: "server_key": {"command": "uvx", "args": [...], "env": {}, "enabled": True, "tool_filter": [...], "scope": "worker"}
        # scope: "worker"(默认，仅执行子任务可见) | "planner_only" | "always"
        # 工具暴露为 mcp__{server_key}__{tool_name}，避免与原生工具重名
        # 详见 docs/design/office-capability-extension.md §2
    },
}

DECOMPOSE_RULES = [
    {
        # 注意：pattern 不能太宽泛，否则 "10 万 tokens" 这种常见词会误触发
        "patterns": ["JWT 签名", "jwt 签名", "auth 模块", "登录认证", "access token", "refresh token", "OAuth"],
        "subtasks": [
            {"id": "sub-1", "title": "后端JWT签名迁移", "description": "将后端JWT签名算法从HS256迁移至RS256，生成RSA密钥对并更新签名/验证逻辑", "files_hint": "src/auth/**"},
            {"id": "sub-2", "title": "前端登录适配", "description": "前端适配新的公钥获取流程，更新登录页JWT解析和验证逻辑", "files_hint": "src/pages/login/**"},
            {"id": "sub-3", "title": "测试补充", "description": "补充RS256相关的单元测试和端到端测试", "files_hint": "tests/**"},
        ]
    },
    {
        # pattern 加 "覆盖率报告" 等具体词，避免误触发
        "patterns": ["测试覆盖率", "test coverage", "unit test", "编写测试", "补充测试"],
        "subtasks": [
            {"id": "sub-1", "title": "分析现有测试覆盖", "description": "识别当前测试未覆盖的模块和函数", "files_hint": "tests/**, src/**"},
            {"id": "sub-2", "title": "编写补充测试", "description": "为未覆盖模块添加单元测试和集成测试", "files_hint": "tests/**"},
        ]
    },
]

def safe_input(prompt: str = "") -> str:
    """包装 input()，在非交互模式下返回空字符串（触发默认确认路径）。"""
    try:
        return input(prompt)
    except EOFError:
        console.print()
        return ""

def load_config(config_path: Optional[str] = None) -> dict[str, Any]:
    """加载配置。config_path 非空时读取指定文件（bench 临时 config），否则读 ~/.agent_go/config.json。

    Profile 支持（R-3）：--profile <name> / AGENT_GO_PROFILE 环境变量 →
    优先读 ~/.agent_go/profiles/<name>.json，其次 ~/.agent_go/config.<name>.json；
    不存在时回退默认配置并警告。--config（config_path）优先级高于 profile。
    """
    if not config_path:
        profile = os.environ.get("AGENT_GO_PROFILE", "")
        if profile:
            candidates = [
                AGENT_GO_DIR / "profiles" / f"{profile}.json",
                AGENT_GO_DIR / f"config.{profile}.json",
            ]
            found = next((c for c in candidates if c.exists()), None)
            if found:
                config_path = str(found)
            else:
                console.warning(f"Profile '{profile}' 不存在（查找 {', '.join(str(c) for c in candidates)}），回退默认配置。")

    if config_path:
        target = Path(config_path)
        if not target.exists():
            console.warning(f"指定配置文件不存在: {target}，回退默认配置。")
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            saved = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            console.warning(f"指定配置文件读取失败 ({target}): {e}，回退默认配置。")
            return json.loads(json.dumps(DEFAULT_CONFIG))
        if not isinstance(saved, dict):
            console.warning(f"指定配置文件格式无效 ({target}): 顶层应为 JSON 对象，回退默认配置。")
            return json.loads(json.dumps(DEFAULT_CONFIG))
        merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            console.warning(f"配置文件损坏或无法读取 ({CONFIG_PATH}): {e}，已回退默认配置。请检查或删除该文件。")
            return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        if not isinstance(saved, dict):
            console.warning(f"配置文件格式无效 ({CONFIG_PATH}): 顶层应为 JSON 对象，已回退默认配置。请检查或删除该文件。")
            return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        merged = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    console.print(f"⚙️  已创建默认配置: {CONFIG_PATH}")
    return DEFAULT_CONFIG

def get_api_key(config: dict[str, Any]) -> str:
    key = os.environ.get("AGENT_GO_API_KEY", "") or config.get("plan_api", {}).get("api_key", "")
    if isinstance(key, str) and "${" in key:
        import re
        key = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.environ.get(m.group(1), m.group(0)), key)
    return key

def setup_logger(task_id: str, task_dir: Path) -> logging.Logger:
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

_metering_lock = threading.Lock()


def log_event(logger: logging.Logger, event: str, data: dict[str, Any]) -> None:
    logger.debug(json.dumps({"timestamp": datetime.now().isoformat(), "event": event, **data}, ensure_ascii=False))


def meter_event(metering_path: Any, event: dict[str, Any]) -> None:
    """写入结构化计量事件到 metering.jsonl（P1 配套）。

    Args:
        metering_path: Path-like 对象或字符串；为空时不写入
        event: 计量事件字典，会被追加 ts 字段
    """
    if not metering_path:
        return
    from pathlib import Path
    path = Path(metering_path)
    event["ts"] = datetime.now().isoformat()
    try:
        with _metering_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as e:
        logging.getLogger(__name__).debug(f"meter_event 写入失败: {e}")
