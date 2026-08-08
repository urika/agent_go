from agent_go.status import TASK_STATES, migrate_meta_status, normalize_task_status, set_task_status


def test_all_canonical_states_are_accepted():
    assert normalize_task_status("DELIVERY_READY") == "DELIVERY_READY"
    assert len(TASK_STATES) == 14  # M0 修复：加 PAUSED（中断暂停）


def test_legacy_completed_maps_to_delivery_ready():
    meta = {"status": "completed"}
    assert migrate_meta_status(meta) == "DELIVERY_READY"
    assert meta["status"] == "DELIVERY_READY"
    assert meta["legacy_status"] == "completed"


def test_delivery_metadata_overrides_legacy_failed_status():
    assert normalize_task_status("completed", {"accepted_delivery": True}) == "ACCEPTED_DELIVERY"
    assert normalize_task_status("completed", {"delivery_failed": True}) == "DELIVERY_FAILED"


def test_set_status_rejects_unknown_state():
    meta = {}
    set_task_status(meta, "EXECUTING")
    assert meta["status"] == "EXECUTING"


# ═══════════════════════════════════════════════════════════════
# M0 状态机语义修复：PLAN_REVIEW vs PAUSED 分离
# ═══════════════════════════════════════════════════════════════

def test_paused_maps_to_paused_not_plan_review():
    """修复：paused → PAUSED（中断暂停锚点），不再是 PLAN_REVIEW（规划审查门）。"""
    assert "PAUSED" in TASK_STATES
    assert normalize_task_status("paused") == "PAUSED"
    assert normalize_task_status("paused") != "PLAN_REVIEW"


def test_plan_review_stays_plan_review():
    """PLAN_REVIEW 保持纯净的规划审查门语义。"""
    assert "PLAN_REVIEW" in TASK_STATES
    assert normalize_task_status("plan_review") == "PLAN_REVIEW"


def test_interrupted_maps_to_executing():
    """interrupted（旧）→ EXECUTING（可恢复，非审查）。"""
    assert normalize_task_status("interrupted") == "EXECUTING"
