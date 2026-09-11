import pandas as pd

from download import months
from replay import rolling_walk_forward
from loss_engine import ResearchConfig


def test_month_range_is_closed_and_deterministic():
    assert months("2025-09", "2026-08") == [str(pd.Period("2025-09", "M") + i) for i in range(12)]


def test_rolling_report_never_promotes():
    rows = []
    start = pd.Timestamp("2025-01-01", tz="UTC")
    for i in range(80):
        rows.append({"market_time": start + pd.Timedelta(days=i), "resolved": True,
                     "exit_time": start + pd.Timedelta(days=i, hours=1), "net_pnl": -1.0,
                     "gross_pnl": -0.6, "fees": 0.2, "taxes": 0.2, "direction": "LONG",
                     "event": "SPRING", "phase": "ACCUMULATION", "score": 80, "rr": 2.0})
    result = rolling_walk_forward(pd.DataFrame(rows), ResearchConfig())
    assert result["promotionAllowed"] is False
    assert result["holdoutPolicy"] == "selected_on_development_then_revealed_once"
    baseline = next(item for item in result["shortlist"] if item["gate"] == "all")
    assert baseline["frozenHoldout"]["closed"] > 0
    assert baseline["development"]["closed"] > baseline["frozenHoldout"]["closed"]
