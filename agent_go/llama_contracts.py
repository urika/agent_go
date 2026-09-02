"""llama_contracts.py — llama-defender 共享契约包（ vendored，stdlib only）。

来源（AG-1，2026-09-02 引入）：
- llama-defender 仓库 `signal_types.py`（SignalSnapshot）
- llama-defender 仓库 `protocol_types.py`（EscalationDecision）

只 vendor 双端共享的两个契约（见 docs/in/protocol-layer-ownership-review-feedback-20260902.md §5.2）：
- SignalSnapshot：代理生产 / agent_go 消费（信号面，R17 草案）
- EscalationDecision：双端共享，**任务级语义**（轮次级策略不复用此类型）

纪律：
- 与上游以 CONTRACT_VERSION 对齐；只加 Optional 字段不 +1，删/改字段才 +1。
- 跨仓库漂移检测：`python3 tools/check_llama_contracts.py`（CI 或手工运行）。
- 所有字段 Optional / 有默认值（fail-open）；值 JSON 可序列化。
- 本模块为叶子模块：禁止 import 其他 agent_go 模块。
"""
from typing import Dict, List, Optional

# 与 llama-defender signal_types.py / protocol_types.py 对齐
CONTRACT_VERSION = 1

# vendored 来源标识（漂移检测脚本据此比对）
CONTRACT_SOURCE = {
    "repo": "llama-defender",
    "files": ["signal_types.py", "protocol_types.py"],
    "vendored_at": "2026-09-02",
    "contract_version": CONTRACT_VERSION,
}


# ═══════════════════════════════════════════════════════════════
# SignalSnapshot（信号面：代理生产，agent_go 消费）
# ═══════════════════════════════════════════════════════════════

class SignalSnapshot(dict):
    """某时刻的完整信号快照——升级决策的输入。

    由 llama-defender Signal Layer（ifc_metrics.build_ifc_section 等）聚合生产，
    经 R17 `GET /api/session/<key>/signals` 端点交付。

    设计约束（与上游一致）：
    - 所有字段 Optional → 信号层故障时消费方以默认值运行（fail-open）
    - 不可变（值对象，创建后不应修改）
    - JSON 可序列化
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class EscalationDecision(dict):
    """验证失败后的策略切换决策——任务级语义，双端共享。

    agent_go 侧由 replan 确定性决策层（AG-3）生产；
    llama-defender 侧由 P5 escalate 生产。轮次级策略不复用此类型。
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════
# 工厂函数（与上游签名一致）
# ═══════════════════════════════════════════════════════════════

def build_signal_snapshot(
    h_be: Optional[float] = None,
    h_be_trend: Optional[float] = None,
    d_ledger: Optional[float] = None,
    retention: Optional[float] = None,
    rationale_ratio: Optional[float] = None,
    ile: bool = False,
    ile_kinds: Optional[List[str]] = None,
    view_reset: bool = False,
    reread_pressure: int = 0,
    action_diversity: Optional[float] = None,
    cognitive_load: float = 0.0,
    config_fingerprint: str = "",
    session_key: str = "",
    turn: int = 0,
) -> SignalSnapshot:
    """构建 SignalSnapshot——所有参数有默认值（fail-open）。"""
    return SignalSnapshot(
        contract_version=CONTRACT_VERSION,
        h_be=h_be,
        h_be_trend=h_be_trend,
        d_ledger=d_ledger,
        retention=retention,
        rationale_ratio=rationale_ratio,
        ile=ile,
        ile_kinds=ile_kinds or [],
        view_reset=view_reset,
        reread_pressure=reread_pressure,
        action_diversity=action_diversity,
        cognitive_load=cognitive_load,
        config_fingerprint=config_fingerprint,
        session_key=session_key,
        turn=turn,
    )


def build_escalation(task_id: str, action: str, reason: str,
                     attempt_count: int = 0,
                     signals: Optional[Dict] = None,
                     refined_task: Optional[dict] = None) -> EscalationDecision:
    """构建 EscalationDecision——与上游 build_escalation 签名一致。"""
    return EscalationDecision(
        contract_version=CONTRACT_VERSION,
        task_id=task_id, action=action, reason=reason,
        signals_at_decision=signals or {},
        attempt_count=attempt_count,
        refined_task=refined_task,
    )


__all__ = [
    "CONTRACT_VERSION", "CONTRACT_SOURCE",
    "SignalSnapshot", "EscalationDecision",
    "build_signal_snapshot", "build_escalation",
]
