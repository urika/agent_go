"""模型实体注册表（① Model Registry，模型实体三层设计 P1）。

模型是单一实体：**固有属性一次注册，全局复用**——端点、推理特性（thinking）、
输出特性（JSON 遵从）、成本（pricing/TCO）、能力标签。接入新模型只需在
`~/.agent_go/models.json` 加一条记录，**零代码改动**（thinking/JSON 声明式适配）。

三层设计（docs/design/model-entity-config-design.md）：
  ① Model Registry（本模块，模型固有）
  ② Role Binding（router.roles，场景使用，引用 ① id）
  ③ Deployment Topology（部署拓扑，代理侧）

加载策略（fallback 兼容）：
  - `~/.agent_go/models.json` 存在 → 加载注册表
  - 缺失/损坏 → 空注册表（现有 plan_api/worker_models 逻辑不受影响）
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import AGENT_GO_DIR

logger = logging.getLogger(__name__)

MODELS_PATH = AGENT_GO_DIR / "models.json"


@dataclass
class ThinkingSpec:
    """模型推理特性（① 固有）：deepseek-v4-pro / GLM 等推理模型必须 thinking enabled
    否则返回空（实测根因）。format 区分 Anthropic / OpenAI 两种 thinking 参数格式。"""
    format: str = "openai"          # "anthropic" | "openai"
    required: bool = False          # 是否必须 thinking enabled（v4-pro/GLM=true）
    budget_param: str = "budget_tokens"   # anthropic 格式的预算参数名
    budget_tokens: int = 8192

    @classmethod
    def from_dict(cls, data: dict) -> "ThinkingSpec":
        return cls(
            format=data.get("format", "openai"),
            required=bool(data.get("required", False)),
            budget_param=data.get("budget_param", "budget_tokens"),
            budget_tokens=int(data.get("budget_tokens", 8192)),
        )


@dataclass
class OutputSpec:
    """模型输出特性（① 固有）：JSON 遵从度。strict（GLM）直接输出合法 JSON；
    loose（deepseek）需 response_format=json_object 辅助；poor 输出不可靠。"""
    json_compliance: str = "loose"      # "strict" | "loose" | "poor"
    needs_response_format: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "OutputSpec":
        return cls(
            json_compliance=data.get("json_compliance", "loose"),
            needs_response_format=bool(data.get("needs_response_format", False)),
        )


@dataclass
class CostSpec:
    """模型成本（① 固有）：pricing 引用 pricing.py 价格表名；tco_per_call 是本地
    模型每次调用的真实拥有成本（电费+硬件折旧，原 local_model_cost 迁入）。"""
    pricing: Optional[str] = None      # pricing.py 价格表 key（云端），None=本地免费表外
    tco_per_call: float = 0.0          # 本地 TCO/次（USD）

    @classmethod
    def from_dict(cls, data: dict) -> "CostSpec":
        return cls(
            pricing=data.get("pricing"),
            tco_per_call=float(data.get("tco_per_call", 0.0) or 0.0),
        )


@dataclass
class ModelEntity:
    """模型实体（① 固有属性全集）。接入新模型 = models.json 加一条本结构。"""
    id: str
    provider: str                       # "anthropic" | "openai" | "deepseek" | "custom"
    base_url: str = ""
    key_ref: str = ""                   # env 变量名或 secret 引用（**不存明文**）
    thinking: ThinkingSpec = field(default_factory=ThinkingSpec)
    output: OutputSpec = field(default_factory=OutputSpec)
    context_chars: int = 200000         # 实际上下文上限（压缩/路由阈值用）
    cost: CostSpec = field(default_factory=CostSpec)
    quality_tags: list[str] = field(default_factory=list)  # plan_strong/eval_strong/code_strong/cheap
    tier: str = ""                      # MODEL_TIER 档（可选，pricing.py 已有）

    @classmethod
    def from_dict(cls, model_id: str, data: dict) -> "ModelEntity":
        endpoint = data.get("endpoint", {}) or {}
        return cls(
            id=model_id,
            provider=data.get("provider", "custom"),
            base_url=endpoint.get("base_url", data.get("base_url", "")),
            key_ref=endpoint.get("key_ref", data.get("key_ref", "")),
            thinking=ThinkingSpec.from_dict(data.get("reasoning", {}).get("thinking", {}) or {}),
            output=OutputSpec.from_dict(data.get("output", {}) or {}),
            context_chars=int(data.get("limits", {}).get("context_chars", 200000)),
            cost=CostSpec.from_dict(data.get("cost", {}) or {}),
            quality_tags=list(data.get("quality_tags", []) or []),
            tier=data.get("tier", ""),
        )


# ── key_ref 解析（P1.2，不存明文）────────────────────────────

def resolve_key(key_ref: str) -> str:
    """把 key_ref 解析为实际 API key。

    支持：
      - 环境变量名：`env:GLM_API_KEY` 或裸 `GLM_API_KEY` → os.environ
      - secret 引用：`secret:<path>#<field>`（代理 configs/secret.local.conf 风格，预留）
      - 空 → 空串（本地模型/无 key 场景）
    绝不接受明文 key（安全：models.json 不存明文）。
    """
    if not key_ref:
        return ""
    ref = key_ref.strip()
    if ref.startswith("env:"):
        return os.environ.get(ref[4:], "")
    if ref.startswith("secret:"):
        # 预留：从 secret 文件读取（代理 secret.local.conf 风格 export KEY="..."）
        body = ref[7:]
        path, _, field_name = body.partition("#")
        field_name = field_name or "PROXY_CLOUD_API_KEY"
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("export ") and field_name in line and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return val
        except OSError as e:
            logger.warning("key_ref secret 读取失败 %s: %s", path, e)
        return ""
    # 裸环境变量名（无 env: 前缀）
    return os.environ.get(ref, "")


# ── 注册表加载与查询 ─────────────────────────────────────────

_registry_cache: Optional[dict[str, ModelEntity]] = None
_registry_mtime: float = 0.0


def load_registry(force_reload: bool = False) -> dict[str, ModelEntity]:
    """加载模型注册表（models.json）。缺失/损坏 → 空 dict（fallback，不影响现有逻辑）。

    带 mtime 缓存：文件变更自动重载（配置中心编辑后新任务生效）。
    """
    global _registry_cache, _registry_mtime
    if not MODELS_PATH.exists():
        _registry_cache = {}
        _registry_mtime = 0.0
        return {}
    try:
        mtime = MODELS_PATH.stat().st_mtime
    except OSError:
        return _registry_cache or {}
    if not force_reload and _registry_cache is not None and mtime == _registry_mtime:
        return _registry_cache
    try:
        raw = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("models.json 读取失败（%s），回退空注册表: %s", MODELS_PATH, e)
        _registry_cache = {}
        _registry_mtime = 0.0
        return {}
    if not isinstance(raw, dict):
        logger.warning("models.json 顶层应为 JSON 对象，回退空注册表")
        _registry_cache = {}
        return {}
    registry: dict[str, ModelEntity] = {}
    for model_id, data in raw.items():
        if isinstance(data, dict):
            registry[model_id] = ModelEntity.from_dict(model_id, data)
    _registry_cache = registry
    _registry_mtime = mtime
    return registry


def get_model(model_id: str) -> Optional[ModelEntity]:
    """按 id 查模型实体。未注册返回 None（调用方 fallback 到现有配置）。"""
    return load_registry().get(model_id)


def list_models() -> list[ModelEntity]:
    """全部已注册模型（models list CLI / 配置中心展示用）。"""
    return list(load_registry().values())
