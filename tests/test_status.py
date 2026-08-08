from agent_go.status import TASK_STATES, migrate_meta_status, normalize_task_status, set_task_status


def test_all_canonical_states_are_accepted():
    assert normalize_task_status("DELIVERY_READY") == "DELIVERY_READY"
    assert len(TASK_STATES) == 13


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
