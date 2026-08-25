from engine import FusionConfig, fuse


def sources(**overrides):
    wyckoff = {"signalId": "w1", "symbol": "BTCUSDT", "marketTime": "2026-08-25T00:04:59+00:00",
               "barId": "2026-08-25T00:00:00+00:00", "decision": "ALLOW_SHADOW", "direction": "LONG",
               "continuityOk": True, "executionActionable": False, "executionGatePassed": False}
    liquidity = {"observationId": "l1", "symbol": "BTCUSDT", "marketTime": "2026-08-25T00:04:59+00:00",
                 "barId": "2026-08-25T00:00:00+00:00", "decision": "ALLOW_SHADOW", "direction": "LONG",
                 "continuityOk": True, "executionActionable": False, "executionGatePassed": False}
    wyckoff.update(overrides.pop("wyckoff", {})); liquidity.update(overrides.pop("liquidity", {}))
    return wyckoff, liquidity


def test_failed_soak_is_hard_shadow_reject():
    wyckoff, liquidity = sources()
    result = fuse(wyckoff, liquidity, FusionConfig(liquidity_soak_status="FAIL"), 1787616600)
    assert result["decision"] == "REJECT_SHADOW"
    assert result["reason"] == "liquidity_soak_not_passed"
    assert not result["executionActionable"]


def test_passed_soak_allows_aligned_shadow_evidence_only():
    wyckoff, liquidity = sources()
    result = fuse(wyckoff, liquidity, FusionConfig(liquidity_soak_status="PASS"), 1787616600)
    assert result["decision"] == "ALLOW_SHADOW"
    assert result["reason"] == "wyckoff_liquidity_confluence"
    assert not result["executionGatePassed"]


def test_continuity_failure_rejects_before_confluence():
    wyckoff, liquidity = sources(liquidity={"continuityOk": False})
    result = fuse(wyckoff, liquidity, FusionConfig(liquidity_soak_status="PASS"), 1787616600)
    assert result["reason"] == "continuity_gate_failed"


def test_direction_conflict_rejects():
    wyckoff, liquidity = sources(liquidity={"direction": "SHORT"})
    result = fuse(wyckoff, liquidity, FusionConfig(liquidity_soak_status="PASS"), 1787616600)
    assert result["reason"] == "direction_conflict"


def test_id_is_deterministic():
    wyckoff, liquidity = sources()
    first = fuse(wyckoff, liquidity, FusionConfig(), 1787616600)
    second = fuse(wyckoff, liquidity, FusionConfig(), 1787616600)
    assert first["observationId"] == second["observationId"]
