from benchmark_v2 import assess
def test_short_sample_not_qualified():
    import pandas as pd
    f=pd.DataFrame(columns=["market_time","resolved","exit_time","gross_pnl","fees","taxes","net_pnl"])
    assert assess(f,pd.Timestamp("2025-01-01",tz="UTC"),pd.Timestamp("2026-01-01",tz="UTC"))[3] is False
