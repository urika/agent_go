"""大模型定价表与档位元数据。

定价来源：2026-07 厂商官网 + 聚合平台公开数据。汇率 1 USD ≈ 7.2 CNY。
更新时间见各模型注释；每次更新同步修改本行日期。
详情见 docs/design/model-evaluation-and-tiering.md。

过期旧模型保留（供旧 metering 日志兼容），标注 ⚰️。
"""

__all__ = ["MODEL_PRICES", "PROVIDER_DEFAULT_MODEL", "LEGACY_PROVIDER_DEFAULT_MODEL", "PROVIDER_DEFAULT_MODEL_CUTOFF", "MODEL_TIER",
           "resolve_price", "missing_price_models", "format_price_for_report", "infer_provider"]

from typing import Optional  # noqa: E402

# ═══════════════════════════════════════════════════════════════
# 定价表（USD / 百万 tokens，标准 API 价，非 batch/cache 价）
# ═══════════════════════════════════════════════════════════════

MODEL_PRICES = {
    # ── Anthropic（2026-07，benchlm / Anthropic 官网） ──
    "claude-fable-5":          {"prompt": 10.0, "completion": 50.0},   # 最高能力级（Opus 之上），1M ctx
    "claude-opus-4-8":         {"prompt": 5.0,  "completion": 25.0},   # 最新 Opus（May 2026），最复杂任务
    "claude-opus-4-7":         {"prompt": 5.0,  "completion": 25.0},   # 上一代 Opus
    "claude-opus-4-6":         {"prompt": 5.0,  "completion": 25.0},   # ⚠️ 已被 4.8 取代
    "claude-sonnet-5":         {"prompt": 2.0,  "completion": 10.0},   # 新默认（Jun 2026）intro 价至 Aug 31
                                                                        #   intro 结束后 $3/$15
    "claude-sonnet-4-6":       {"prompt": 3.0,  "completion": 15.0},   # Sonnet 4.6 稳定版
    "claude-sonnet-4":         {"prompt": 3.0,  "completion": 15.0},   # ⚠️ Sonnet 4（与 4.6 同价）
    "claude-sonnet-4-20250514": {"prompt": 3.0, "completion": 15.0},   # ⚠️ Sonnet 4 精确版本号
    "sonnet[1m]":            {"prompt": 2.0,  "completion": 10.0},   # 代理路由别名：本地代理 → sonnet[1m] = Sonnet 5 intro 价
    "claude-haiku-4-5":        {"prompt": 1.0,  "completion": 5.0},    # 轻量，当前最新 Haiku
    "claude-haiku-4-5-20251001": {"prompt": 1.0, "completion": 5.0},   # 同款，版本号精确

    # ── Anthropic 旧版（⚰️ 保留兼容） ──
    "claude-opus-4-1":         {"prompt": 15.0, "completion": 75.0},   # ⚰️ 已被 Fable 5 / Opus 4.8 取代

    # ── OpenAI（2026-07，OpenAI 官网 / openai-cost crate） ──
    "gpt-5":                   {"prompt": 1.25, "completion": 10.0},   # GPT-5 基座（Q2 2026）
    "gpt-5-mini":              {"prompt": 0.25, "completion": 2.0},    # GPT-5 小号
    "gpt-5-nano":              {"prompt": 0.05, "completion": 0.4},    # GPT-5 最轻
    "gpt-5.7":                 {"prompt": 1.0,  "completion": 6.0},    # Jul 2026 旗舰（Vercel），1.1M ctx
    "gpt-4.1":                 {"prompt": 2.0,  "completion": 8.0},    # GPT-4.1 主力
    "gpt-4.1-mini":            {"prompt": 0.4,  "completion": 1.6},    # GPT-4.1 小号
    "gpt-4.1-nano":            {"prompt": 0.1,  "completion": 0.4},    # GPT-4.1 最轻
    "gpt-4o":                  {"prompt": 2.5,  "completion": 10.0},   # ⚠️ 旧旗舰（保留兼容）
    "gpt-4o-mini":             {"prompt": 0.15, "completion": 0.6},    # ⚠️ 旧轻量
    "o4-mini":                 {"prompt": 1.1,  "completion": 4.4},    # 推理模型
    "o3":                      {"prompt": 2.0,  "completion": 8.0},    # 推理模型

    # ── Google Gemini（2026-06，ai.google.dev） ──
    "gemini-2.5-pro":          {"prompt": 1.25, "completion": 10.0},   # 旗舰（≤200K ctx）
    "gemini-2.5-flash":        {"prompt": 0.3,  "completion": 2.5},    # 主力轻量
    "gemini-2.5-flash-lite":   {"prompt": 0.1,  "completion": 0.4},    # 最轻
    "gemini-3.1-pro":          {"prompt": 2.0,  "completion": 12.0},   # 新一代 Pro
    "gemini-3-flash":          {"prompt": 0.5,  "completion": 3.0},    # 新一代 Flash
    "gemini-3.5-flash":        {"prompt": 1.5,  "completion": 9.0},    # 最新 Flash

    # ── DeepSeek（2026-07，官方文档） ──
    "deepseek-chat":           {"prompt": 0.14, "completion": 0.28},   # V3.2 性价比旗舰（$0.28/$0.28）
    "deepseek-v3.2":           {"prompt": 0.14, "completion": 0.28},   # 同款别名
    "deepseek-v4-flash":       {"prompt": 0.14, "completion": 0.28},   # V4 Flash
    "deepseek-v4-pro":         {"prompt": 0.435, "completion": 0.87},  # V4 Pro（2026-07 官网价）
    "deepseek-reasoner":       {"prompt": 0.42, "completion": 0.83},   # R1 推理（¥3/6）

    # ── 阿里云百炼 Qwen（2026-07，help.aliyun.com） ──
    "qwen-max":                {"prompt": 2.78, "completion": 8.33},   # 旗舰（¥20/60）
    "qwen3-max":               {"prompt": 0.78, "completion": 3.90},   # Qwen3 Max 降价后
    "qwen-plus":               {"prompt": 0.11, "completion": 0.28},   # 主力（¥0.8/2）

    # ── 字节火山方舟 Doubao（2026-07，volcengine.com） ──
    "doubao-1.5-pro-32k":      {"prompt": 0.11, "completion": 0.28},   # 主力（¥0.8/2）
    "doubao-lite":             {"prompt": 0.04, "completion": 0.08},   # 最轻（¥0.3/0.6）

    # ── 月之暗面 Kimi（2026-07，platform.kimi.com） ──
    "kimi-k2":                 {"prompt": 0.56, "completion": 2.22},   # K2 旗舰（¥4/16）
    "kimi-k2.5":               {"prompt": 0.95, "completion": 4.0},    # K2.5（前端专精，256K ctx）

    # ── 智谱 GLM（2026-07，bigmodel.cn） ──
    "glm-5":                   {"prompt": 0.07, "completion": 0.14},   # 最新旗舰（¥0.5~1）
    "glm-4.6":                 {"prompt": 0.69, "completion": 2.08},   # ⚠️ 上一代（¥5/15）
    "glm-4.7-air":             {"prompt": 0.07, "completion": 0.14},   # GLM-4.7 Air（¥0.5）
    "glm-4.7":                 {"prompt": 0.5556, "completion": 2.2222},  # GLM-4.7（¥4/16，2026-07 实测 claude-* 路由实际后端）
    "glm-5.1":                 {"prompt": 0.8333, "completion": 3.3333},  # GLM-5.1（¥6/24，阿里云百炼=智谱官网）
    "glm-5.2":                 {"prompt": 1.1111, "completion": 3.8889},  # GLM-5.2（¥8/28，智谱官方，settings 默认 sonnet）
    "glm-4.5-air":             {"prompt": 0.1111, "completion": 0.2778},  # GLM-4.5-Air（¥0.8/2，2025-07-29 官方）

    # ── 旧版保留（⚰️ 供旧 metering 日志兼容） ──
    "deepseek-v4-flash-old":   {"prompt": 0.27, "completion": 1.1},
    "deepseek-v4-pro-old":     {"prompt": 0.55, "completion": 2.19},
    "deepseek-chat-old":       {"prompt": 0.27, "completion": 1.1},
    "deepseek-reasoner-old":   {"prompt": 0.55, "completion": 2.19},
}

# ═══════════════════════════════════════════════════════════════
# 模型档位（供 difficulty 路由 + 质量门参考，2026-07 更新）
# ═══════════════════════════════════════════════════════════════

MODEL_TIER: dict[str, list[str]] = {
    "frontier": [
        # 顶级旗舰 — 只有最高风险和最高回报的任务用
        "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
        "gpt-5.7", "gpt-5",
        "gemini-3.1-pro",
        "qwen-max",
        "glm-5.2", "glm-5.1",
    ],
    "value": [
        # 主力性价比 — 大部分 production 任务
        "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4", "claude-sonnet-4-20250514",
        "gpt-4.1", "gpt-4o",
        "gemini-2.5-pro",
        "deepseek-chat", "deepseek-v3.2", "deepseek-v4-flash", "deepseek-v4-pro",
        "qwen3-max", "qwen-plus",
        "doubao-1.5-pro-32k",
        "kimi-k2", "kimi-k2.5",
        "glm-5", "glm-4.6", "glm-4.7",
    ],
    "lite": [
        # 轻量 — 高频、低成本、延迟敏感
        "claude-haiku-4-5", "claude-haiku-4-5-20251001",
        "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini",
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-flash", "gemini-3.5-flash",
        "deepseek-reasoner",
        "doubao-lite",
        "glm-4.7-air", "glm-4.5-air",
    ],
}

# CR-G2：模型名 → tier 反查表（advisory 路由校验用；MODEL_TIER 此前零消费者）
_MODEL_TO_TIER: dict[str, str] = {m: tier for tier, mods in MODEL_TIER.items() for m in mods}


def model_tier(name: str) -> Optional[str]:
    """模型名 → tier（frontier/value/lite）。复用 resolve_price 的后缀剥离。
    未分级模型（自定义/本地）返回 None——不视为错配。
    """
    if not name:
        return None
    _m = name.strip()
    if _m in _MODEL_TO_TIER:
        return _MODEL_TO_TIER[_m]
    for _suf in ("-20251001", "-20250514", "-v2", "-v3"):
        if _m.endswith(_suf) and _m[: -len(_suf)] in _MODEL_TO_TIER:
            return _MODEL_TO_TIER[_m[: -len(_suf)]]
    return None


def validate_worker_tier(worker_models: dict) -> list[tuple[str, str, str, str]]:
    """CR-G2：advisory 校验 worker_models 与 MODEL_TIER 的明显错配。

    Returns [(slot, model, tier, msg), ...] 错配列表（空 = 无错配）。
      - hard 槽用 lite 模型 → 能力不足
      - easy 槽用 frontier 模型 → 过贵
    未分级模型（model_tier=None）不报（自定义/本地模型合法）。advisory，不阻断。
    """
    _issues: list[tuple[str, str, str, str]] = []
    if not isinstance(worker_models, dict):
        return _issues
    _hard = worker_models.get("hard", "") or ""
    if model_tier(_hard) == "lite":
        _issues.append(("hard", _hard, "lite", "hard 槽用 lite 模型，能力恐不足（hard 任务建议 value/frontier）"))
    _easy = worker_models.get("easy", "") or ""
    if model_tier(_easy) == "frontier":
        _issues.append(("easy", _easy, "frontier", "easy 槽用 frontier 模型，过贵（easy 任务建议 lite/value）"))
    return _issues

# ═══════════════════════════════════════════════════════════════
# provider → 默认模型（旧日志缺 model 字段时回退用）
# ═══════════════════════════════════════════════════════════════

PROVIDER_DEFAULT_MODEL = {
    "anthropic":  "claude-sonnet-5",          # 2026-07 新默认（Sonnet 5 取代 Sonnet 4）
    "openai":     "gpt-4.1",
    "deepseek":   "deepseek-chat",
    "google":     "gemini-2.5-pro",
    "moonshot":   "kimi-k2",
    "volcengine": "doubao-1.5-pro-32k",
    "zhipu":      "glm-5",
}

# ═══════════════════════════════════════════════════════════════
# 历史默认（2026-07 模型升级前的 provider 默认值）
# 用于向后兼容：旧 metering 日志缺 model 字段时使用更准确的历史默认，
# 避免历史 $/pass rate 被新默认价（往往更便宜）拉低
# ═══════════════════════════════════════════════════════════════

LEGACY_PROVIDER_DEFAULT_MODEL = {
    "anthropic":  "claude-sonnet-4-20250514", # 2026-07 前的旧默认（$3/$15）
    "openai":     "gpt-4o",                   # 2026-07 前的旧默认（$2.5/$10）
    "deepseek":   "deepseek-chat-old",        # 旧 V2 价（$0.27/$1.1）
    "google":     "gemini-2.5-pro",           # 未变更
    "moonshot":   "kimi-k2",                  # 未变更
    "volcengine": "doubao-1.5-pro-32k",       # 未变更
    "zhipu":      "glm-5",                    # 未变更
}

# ═══════════════════════════════════════════════════════════════
# 运行前模型-价格预检（S12 冷启动/基线校验）
# ═══════════════════════════════════════════════════════════════


def resolve_price(model: str) -> Optional[dict[str, float]]:
    """解析模型定价。优先精确匹配；其次剥离版本号后缀匹配
    （如 glm-4.7 的响应名带精度后缀时回退基础名）。找不到返回 None。
    """
    if not model:
        return None
    _m = model.strip()
    if _m in MODEL_PRICES:
        return MODEL_PRICES[_m]
    # 剥离已知后缀回退：xxx-yyyyMMdd / xxx-vN 等
    for _suffix in ("-20251001", "-20250514", "-v2", "-v3"):
        if _m.endswith(_suffix) and _m[: -len(_suffix)] in MODEL_PRICES:
            return MODEL_PRICES[_m[: -len(_suffix)]]
    return None


def missing_price_models(models: list[str]) -> list[str]:
    """返回缺少定价的模型列表（用于运行前预检）。"""
    return [m for m in models if m and resolve_price(m) is None]


# 模型名前缀 → provider 反查表（P1 router recommend / advisory 校验用）。
# 顺序敏感：先匹配更特异的前缀（如 claude-*），否则 doubao/doubao-lite 会误入。
_MODEL_PROVIDER_PREFIXES: list[tuple[str, str]] = [
    ("claude-", "anthropic"), ("claude[", "anthropic"), ("sonnet[", "anthropic"),
    ("fable", "anthropic"), ("haiku", "anthropic"), ("opus", "anthropic"),
    ("gpt-", "openai"), ("o3", "openai"), ("o4-", "openai"),
    ("gemini-", "google"),
    ("deepseek-", "deepseek"),
    ("qwen-", "aliyun"),
    ("doubao-", "volcengine"),
    ("kimi-", "moonshot"),
    ("glm-", "zhipu"),
    ("llama", "local"),
    ("qwen3", "local"),
    ("qwen2", "local"),
    ("deepseek-r1", "deepseek"),
]


def infer_provider(model: str) -> Optional[str]:
    """模型名 → provider 反查（前缀匹配，支持版本号后缀剥离）。

    P1 router recommend 需要"同源铁律"校验（reviewer 与 worker 不同 provider），
    而 bench results 里只有模型名、没有 provider 记录——此函数按定价表前缀还原。

    Args:
        model: 模型名（如 "deepseek-chat" / "claude-opus-4-8" / "qwen-max"）。

    Returns:
        provider 名（anthropic/openai/google/deepseek/aliyun/volcengine/moonshot/zhipu/local），
        无法识别返回 None（自定义模型不阻断，advisory 语义）。
    """
    if not model:
        return None
    _m = model.strip()
    for _suf in ("-20251001", "-20250514", "-v2", "-v3", "-old"):
        if _m.endswith(_suf):
            _base = _m[: -len(_suf)]
            if resolve_price(_base):
                _m = _base
            break
    for _prefix, _prov in _MODEL_PROVIDER_PREFIXES:
        if _m.startswith(_prefix):
            return _prov
    return None


def format_price_for_report(model: str) -> str:
    """报告用：返回模型定价的人类可读串，无定价标注 ⚠️ 缺价。"""
    p = resolve_price(model)
    if not p:
        return f"{model} ⚠️ 缺定价"
    return f"{model} ($ {p.get('prompt', 0)}/{p.get('completion', 0)})"

# 模型升级生效日期：日志时间戳早于这个日期的使用 LEGACY_PROVIDER_DEFAULT_MODEL
PROVIDER_DEFAULT_MODEL_CUTOFF = "2026-07-25"
