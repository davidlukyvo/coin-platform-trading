import pandas as pd
from gate_v3 import add_features,objective
def test_fixed_regimes():
    f=pd.DataFrame({"market_time":pd.to_datetime(["2026-01-01T02:00Z","2026-01-01T15:00Z"]),"spreadRatio15m":[.7,1.4],"hourlyEMA20Slope":[.0001,.002]}); r=add_features(f)
    assert list(r.session)==["ASIA","US"] and list(r.volatility_regime.astype(str))==["COMPRESSED","EXPANDED"]
def test_requires_positive_segments():
    good={"closed":40,"netPnl":5,"maxDrawdown":2}; bad={"closed":20,"netPnl":-1,"maxDrawdown":3}
    assert objective(good,good)>-1e9 and objective(good,bad)==-1e9
