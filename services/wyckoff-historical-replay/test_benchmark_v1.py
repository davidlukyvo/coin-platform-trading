import pandas as pd
from benchmark_v1 import entries
def test_entries_are_delayed_and_discrete():
    b=pd.DataFrame({"close":[1.0]*100,"high":[1.0]*100,"low":[1.0]*100})
    assert set(entries(b,"BREAKOUT",(20,)).unique())=={"NONE"}
