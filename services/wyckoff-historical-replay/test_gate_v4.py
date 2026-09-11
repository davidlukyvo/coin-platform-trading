import pandas as pd
from gate_v4 import evaluate
def test_empty_candidate_never_qualifies():
    r=evaluate(pd.DataFrame(columns=["market_time","resolved","exit_time","net_pnl","gross_pnl","fees","taxes"]))
    assert r["qualified"] is False
