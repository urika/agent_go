"""大模型定价表与档位元数据。

定价来源：2026-07 厂商官网 + 聚合平台公开数据。汇率 1 USD ≈ 7.2 CNY。
更新时间见各模型注释；每次更新同步修改本行日期。
详情见 docs/design/model-evaluation-and-tiering.md。

过期旧模型保留（供旧 metering 日志兼容），标注 ⚰️。
"""

__all__ = ["MODEL_PRICES", "PROVIDER_DEFAULT_MODEL", "LEGACY_PROVIDER_DEFAULT_MODEL", "PROVIDER_DEFAULT_MODEL_CUTOFF", "MODEL_TIER"]

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
    "deepseek-v4-pro":         {"prompt": 0.55, "completion": 1.1},    # V4 Pro
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
        "glm-5", "glm-4.6",
    ],
    "lite": [
        # 轻量 — 高频、低成本、延迟敏感
        "claude-haiku-4-5", "claude-haiku-4-5-20251001",
        "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini",
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-flash", "gemini-3.5-flash",
        "deepseek-reasoner",
        "doubao-lite",
        "glm-4.7-air",
    ],
}

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

# 模型升级生效日期：日志时间戳早于这个日期的使用 LEGACY_PROVIDER_DEFAULT_MODEL
PROVIDER_DEFAULT_MODEL_CUTOFF = "2026-07-25"
