import os
import json
import logging
import threading
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
        "plan_preflight_repair_enabled": True,  # 执行前确定性 Plan 修订（最多一次）
        "max_plan_repairs": 1,                # Plan preflight 自动修订上限
        "notify_on_complete": True,          # 任务完成时发桌面通知（macOS osascript）
        "notify_command": "",                # 自定义通知命令，如 "curl -X POST ..."
    },
    "verification": {
        "max_retries": 3,               # 验证失败后最大修复重试次数
        "retry_timeout": 300,           # 每次修复重试超时（秒）
        "run_timeout": 1800,            # 首跑硬超时基数（秒），按难度缩放（easy×1/medium×1.5/hard×2.5），0=禁用
        "block_on_failure": True,       # 验证失败是否阻断下游依赖（--no-verify-block 可关）
        "diverge_similarity_threshold": 0.3,  # 打地鼠检测：连续两次语义评估缺陷指纹相似度低于此值 → 提前终止重试
        "revert_threshold": 2,          # 回退/振荡检测：同一 worktree 累积 diff 状态出现次数 ≥ 此值 → 判定循环振荡终止
        # C3 局部重规划（PRD F-VERIFY-6 受控策略升级）：无进展（verify_revert/
        # divergence/失败模式重复）时生成一次 Plan 拆分建议。契约：最多一次、
        # 继承父预算（同一 sub_id 计量，L2 上限继续约束）、默认人工确认、
        # 不递归扩大任务图（拆分步只注入修复 prompt）。
        "replan": {
            "enabled": True,
            "auto_apply": False,        # headless/--yes 下免确认自动执行拆分修复（默认只记录建议）
            "max_children": 4,          # 拆分建议最大步数
            "repeat_similarity_threshold": 0.7,  # 失败模式重复判定：连续语义评估缺陷指纹相似度 ≥ 此值
        },
        # 独立只读审查 subagent（两阶段审查）：验证失败时，用独立模型做黑盒分析
        # （不参与实现，消除「实现者盲区」），审查意见注入修复 prompt。
        # 默认关闭（成本可控）；model 空 = 复用 evaluator.model。
        # skill：可选审查维度 skill 名（空 = 内置通用模板）。配置后加载
        # ~/.agent_go/skills/<name>/SKILL.md 的 body 作为审查维度指引，实现领域化审查。
        "readonly_review": {
            "enabled": False,
            # B5 循环智能 b：Reflexion 阈值化——retry_count ≥ threshold 才触发审查
            "threshold": 2,
            "model": "",
            "provider": "",
            "base_url": "",
            "skill": "",
            "max_tokens": 2048,
            "timeout_ms": 90000,
        },
        # M1.4 架构审查（SDD 最小治理闭环）：执行前生成最小 Architecture Decision
        # （边界 / 依赖方向 / 关键约束），由独立 LLM 审查产生
        # approved / rejected / changes_requested 决策。结果持久化到
        # meta.architecture_review。默认关闭（fail-open：未启用或失败不阻断执行）。
        # model 空 = 复用 plan_api.model。
        "architecture_review": {
            "enabled": False,
            "model": "",
            "provider": "",
            "base_url": "",
            "max_tokens": 2048,
            "timeout_ms": 90000,
        },
    },
    "goal": {
        "enabled": False,               # 是否在 TASK.md 注入 goal 指令（--goal 开启）
        "max_turns": 20,                # 单个 goal 循环最大 tool-call 轮数
        "timeout_seconds": 600,          # goal 循环全局超时（秒）
        "enable_goal_hook": False,      # 是否注入 Stop Hook（.claude/settings.local.json + verify-goal.sh）
        "policy": "off",                # Goal Policy 默认方向：off|auto|force|hook（用户 CLI 覆盖优先）
    },
    "agent_loop": {
        "enabled": False,               # 默认关闭，--agent-loop 开启
        "max_turns": 20,                # 最大对话轮数
        "max_duration": 600,            # 全局超时（秒）
        "api_timeout": 120,             # 单次 API 调用超时（秒）
        "stuck_repeat_threshold": 3,    # B2 stuck 检测：连续相同工具调用达阈值先提醒，再犯终止
        "no_progress_turns": 8,         # B2 no-progress 信号：连续 N 轮无成功写入则记录（不终止）
    },
    # C4 KnowledgeStore（A/B 实验臂）：修复重试时注入跨任务历史经验
    # （Problem/deviation/verify_state）。默认关闭 = A/B 对照臂；
    # bench --with-knowledge 或置 enabled=true 开启注入臂。
    # suppressed_ids：按 Problem id 屏蔽错误知识（可淘汰机制）。
    # resolution_llm：葬礼回写时用 LLM 把「失败报错+修复内容」总结为根因+做法
    # （根因级 resolution_summary，保护未来 A/B 判定效度）；fail-open，
    # 失败/关闭自动降级为 diffstat 级摘要。默认开启（单次调用成本可忽略）。
    # snapshot：KV-cache 稳定快照（C4 前置修订）——首次构建的非空知识块跨重试
    # 冻结复用，逐轮重建会打爆本地模型前缀缓存；置 false 退化为逐轮重建（对照/调试）。
    "knowledge": {
        "enabled": False,
        "max_items": 3,
        "suppressed_ids": [],
        "resolution_llm": True,
        "snapshot": True,
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
        "local_model_url": "http://localhost:4000/v1/chat/completions",
        "local_model_name": "claude-sonnet-4-6",
        "enable_rules": True
    },
    # 本地模型 TCO 成本口径（2026-08-12）：本地模型 metering 成本清零（$0），
    # 若直接进 $/pass 会让 gate 把它当"免费"失真。这里按"每次调用"估算本地推理
    # 的显式成本（电费 + 硬件折旧），键为本地模型名（actual_model 匹配）：
    #   "mlx-community/Qwen3.6-27B-4bit": 0.0007   # ~0.5$/h 硬件 TCO ÷ 平均调用时长
    # 留空 = 不折算（本地成本保持 0，用户配置后才纳入 TCO 评估）。
    "local_model_cost": {},
    "cost_control": {
        # S10/S12 成本控制三层。
        # 冷启动策略（无基线时）：
        #   - L1 单次调用上限（--max-budget-usd）：l1_enabled 独立控制。
        #     2026-08-08 改默认关：Claude CLI 2.1.224 的 --max-budget-usd 语义为"接近上限即拒绝"，
        #     导致 $0.13 实际成本触发 $0.20 预算上限（error_max_budget_usd），任务无法启动。
        #     L1 改为默认关，与 L2/L3 一致，待基线校准后显式开启。
        #   - L2 子任务累计 / L3 任务级熔断：依赖 enabled 总开关，默认关。
        #     这两层是"判死"机制，基线不可信时误杀率高，须用 eval cost-baseline 校准后才开。
        "enabled": False,              # L2/L3 总开关（依赖冻结基线，默认关）
        "l1_enabled": False,           # L1 独立开关（默认关：Claude CLI 预算语义会在干活前拒绝，待基线校准后开启）
        "max_budget_usd": 0.50,        # L3 任务总预算
        "per_subtask_budget_usd": {    # L1 单次调用上限（按难度）；冷启动宽松默认
            "easy": 0.20,              # 基线 P90×1.5 约 $0.10，冷启动取 2x 留余量
            "medium": 0.40,            # 基线 P90×1.5 约 $0.10-0.17，冷启动取 2x
            "hard": 1.00,              # 基线 P90×1.5 约 $0.19-0.36，冷启动取宽松上界
        },
        "subtask_multiplier": 2.5,     # L2 子任务累计 = 单次上限 × 系数
        # legacy（CR-TD）：on_exceed 已不再被代码读取——实际开关是下方 budget_mode。
        # 保留仅为兼容旧 config 文件（写入会被忽略，无副作用）。
        "on_exceed": "stop",
        # S12-P1 G3 per-task 预算策略：strict=超预算 block；degrade=切便宜模型继续；
        # ignore=关 L3（仅 L1/L2 生效）。与 --budget / Task Spec 字段配合。
        "budget_mode": "strict",
    },
    "skills": {
        "auto_discover": False,     # 是否自动匹配 Skill（基于任务描述）
        "max_auto_skills": 3       # 自动匹配时最多加载 N 个 Skill
    },
    "agents": {
        "default": "developer"      # 默认 Agent 类型
    },
    "artifact_dir": None,           # S9-B 产物导出目录；null = 不导出（产物留在 worktree，向后兼容）
    "pipeline": {
        # T09 本地模型自动限流（并发调度原则 2026-09-04 拍板：云端可并行、本地串行）。
        # True（默认）：路由到已验证本地后端（worker_base_url 指向本机且探测确认为本地模型）
        # 的子任务在波次内互斥串行；云端路由子任务不受影响，仍按 --parallel 并行。
        "local_model_serialize": True,
    },
    "worker_backend": "",           # B3 显式 worker backend（"pi" 等）；空 = 按既有策略解析（claude/agent_loop）
    # B4 声明式 backend 路由（空 = 不覆盖；非 claude 仅 headless 生效）。
    # 注意命名避开 deprecated 的 worker_backends（模型名→ANTHROPIC_BASE_URL 映射）。
    "worker_backend_by_difficulty": {
        "easy": "",
        "medium": "",
        "hard": "",
    },
    "worker_backend_by_type": {},   # B4 按 agent_type 路由（如 {"explore": "pi"}；优先级高于 by_difficulty）
    # 促销窗口路由：时间窗内且无显式 backend 声明时优先用指定 backend（需本机可用）。
    # 字段：backend / start / end（YYYY-MM-DD 闭区间）/ daily_start / daily_end（HH:MM，支持跨午夜）/ tz_offset（默认 +8）。
    # 例：GLM flash 夜间免费（仅 ZCode 本体）→ {"backend": "zcode", "start": "2026-09-04", "end": "2026-09-20", "daily_start": "23:00", "daily_end": "09:00"}
    "backend_promo": {},
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
    "worker_models_fallback_chain": {  # P0：失败后按 retry 顺序自动升级模型
        "easy": [],
        "medium": [],
        "hard": [],
    },
    "worker_models_degrades": {     # S12-P2：budget_mode=degrade 时的模型降级表（对称升级表）。
                                    # 键 = 当前难度，值 = 降级目标难度；空 = 降档到 claude 默认模型
        "easy": "",
        "medium": "easy",
        "hard": "medium",
    },
    "worker_models_by_type": {},    # CR-G3：任务类型→模型路由（优先于 difficulty）。
                                    # 键 = task_type（security/bugfix/refactor/test/docs/...，
                                    # 由 Spec `task_type:` 字段或 role_skill_map 关键词检测得出），
                                    # 值 = 该类型用的模型名。默认空 = 功能关闭，纯难度路由。
                                    # 例：{"security": "claude-opus-4-8", "docs": "claude-haiku-4-5"}
    "worker_models_by_cognitive": {},  # 认知模式→模型路由（异构模型路由：探索/实现/审查）。
                                       # 键 = cognitive_mode（explore/implement/review），
                                       # 值 = 该认知模式用的模型名。优先级最高（覆盖 task_type/difficulty）。
                                       # 认知模式来源：subtask.cognitive_mode（planner 可标注）
                                       # 或按 agent_type 推断（architect→explore, reviewer→review, 其余→implement）。
                                       # 例：{"explore": "claude-haiku-4-5", "review": "claude-opus-4-8",
                                       #      "implement": "claude-sonnet-4-6"}
    "local_model_names": {},        # 本地后端真实模型名映射（routed → 实际名，如
                                    # {"claude-haiku-4-5": "Qwen3.6-27B-4bit"}）；
                                    # 探测本地代理 /status 失败时的兜底
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
        #
        # Playwright MCP（UI 语义验证，Tier 2 补充，默认关闭）：
        # 微软官方 @playwright/mcp，用无障碍树（accessibility snapshot）而非 vision 模型，
        # 确定性高、比截图 LLM 便宜。启用后工具暴露为 mcp__playwright__browser_*。
        # 前置：worker 环境需有 npx + 网络（首次 npx @playwright/mcp@latest 下载）。
        # 定位：CLI 快照（Tier 1, npx playwright test）为主，MCP 为交互探索补充。
        # 详见 docs/design/ui-verification-research-2026-08-12.md §5
        "playwright": {
            "command": "npx",
            "args": ["@playwright/mcp@latest"],
            "enabled": False,
            "scope": "worker",
        },
    },
    "mcp_client": {
        # MCP 消费层工具注入模式（T08 代理工具模式，pi-mcp-adapter 借鉴）。
        # "proxy"(默认): agent_loop 路径只注入单个 mcp__proxy 代理工具（~200 token），
        #   模型经 op=list/describe/call 动态发现与调用，避免全量 schema 塞爆上下文；
        # "full": 回退为 mcp__{server}__{tool} 全量 schema 注入（T08 前行为）。
        # 仅影响 agent_loop（直接 API）路径；subtask 的 claude --mcp-config 透传不受影响。
        "tool_mode": "proxy",
    },
}

DECOMPOSE_RULES: list[dict[str, Any]] = [
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
    Web 操作台（M1）：env 未设置时读 ~/.agent_go/.current_profile（config local/cloud 写入）。
    """
    if not config_path:
        profile = os.environ.get("AGENT_GO_PROFILE", "")
        if not profile:
            marker = AGENT_GO_DIR / ".current_profile"
            try:
                profile = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
            except OSError:
                profile = ""
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


def write_censored_event(metering_path: Any, level: str, sub_id: str = "",
                         spent: float = 0.0, budget: float = 0.0,
                         reason: str = "") -> None:
    """写入删失（censored）计量事件到 metering.jsonl（测量/控制解耦）。

    成本控制熔断时调用：真实成本实际是「≥ spent」，熔断只是停止了继续花费，
    不改变「已花费」这一事实。写入 censored 事件让基线统计能识别右删失记录，
    避免把「被截断的成本」当成「自然成本」用于预测。

    Args:
        metering_path: metering.jsonl 路径（空则跳过）
        level: 熔断层级 L1/L2/L3
        sub_id: 子任务 id（任务级为 ""）
        spent: 熔断时已累计成本（右删失下限）
        budget: 触发的预算上限
        reason: 熔断原因描述
    """
    if not metering_path:
        return
    meter_event(metering_path, {
        "event": "cost_censored",
        "level": level,
        "sub_id": sub_id,
        "cost_usd": round(float(spent or 0.0), 6),
        "budget_usd": round(float(budget or 0.0), 6),
        "censored": True,          # 右删失标记：真实成本 ≥ cost_usd
        "reason": reason,
    })


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
