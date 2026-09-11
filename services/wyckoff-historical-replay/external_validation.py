from __future__ import annotations
import hashlib,json,os
from datetime import UTC,datetime
from pathlib import Path
import numpy as np
import pandas as pd
from benchmark_v1 import entries,simulate
from loss_engine import ResearchConfig,stats
ROOT=Path(os.getenv("REPLAY_DATA_ROOT","/data/replay/BTCUSDT_EXTERNAL_4Y")); VERSION="btc_4h_trend_external_validation_v1"
FROZEN={"timeframe":"4h","family":"TREND","signalParameters":[12,96,.001],"atrStop":3.0,"targetRR":3.0,"maxHoldingBars":16}
def main():
    manifest=json.loads((ROOT/"manifest.json").read_text()); files=sorted((ROOT/"parquet").glob("month=*/bars.parquet"))
    m=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True); m["open_time"]=pd.to_datetime(m.open_time,utc=True); m=m.sort_values("open_time").drop_duplicates("open_time").set_index("open_time")
    b=m.resample("4h").agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum"),minute_count=("close","count")).dropna().reset_index()
    incomplete=int((b.minute_count!=240).sum()); b=b[b.minute_count==240].reset_index(drop=True)
    tr=pd.concat([(b.high-b.low),(b.high-b.close.shift()).abs(),(b.low-b.close.shift()).abs()],axis=1).max(axis=1); b["atr"]=tr.rolling(14).mean()
    backward_return=b.close.pct_change(6*90); b["regime"]=np.select([backward_return>.20,backward_return<-.20],["BULL","BEAR"],default="SIDEWAYS")
    trades=simulate(b,entries(b,"TREND",tuple(FROZEN["signalParameters"])),3.0,3.0,16,ResearchConfig())
    regime=pd.merge_asof(trades.sort_values("market_time"),b[["open_time","regime"]].sort_values("open_time"),left_on="market_time",right_on="open_time",direction="backward")
    yearly={str(year):stats(part) for year,part in trades.groupby(trades.market_time.dt.year)}; regimes={name:stats(part) for name,part in regime.groupby("regime")}
    overall=stats(trades); positive_years=sum(x["closed"]>=20 and x["netPnl"]>0 for x in yearly.values()); covered=sum(x["closed"]>=20 for x in regimes.values()); positive_regimes=sum(x["closed"]>=20 and x["netPnl"]>0 for x in regimes.values())
    passed=overall["closed"]>=120 and overall["netPnl"]>0 and overall["profitFactor"]>=1.2 and positive_years>=3 and covered>=2 and positive_regimes>=2 and overall["maxDrawdown"]<=50
    report={"version":VERSION,"reportId":hashlib.sha256(f"{VERSION}:{manifest['months'][0]['sha256']}:{manifest['months'][-1]['sha256']}:{FROZEN}".encode()).hexdigest(),"updatedAt":datetime.now(UTC).isoformat(),"symbol":"BTCUSDT","period":manifest["requestedMonths"],"mode":"OFFLINE_RESEARCH_ONLY","liveTrading":False,"executionActionable":False,"executionGatePassed":False,"promotionAllowed":False,"parametersFrozenBeforeDataset":True,"overlapWithSelectionDataset":False,"dataQuality":{"internalMinuteGaps":manifest["internalMinuteGaps"],"excludedIncomplete4hBars":incomplete},"entryPolicy":"NEXT_4H_OPEN_CAUSAL","sameBarPolicy":"STOP_FIRST_CONSERVATIVE","regimePolicy":"CAUSAL_TRAILING_90D_RETURN_20_PERCENT","parameters":FROZEN,"costs":{"feeBps":5,"taxBps":10,"slippageBps":2,"funding":"NOT_INSTRUMENTED"},"overall":overall,"yearly":yearly,"regimes":regimes,"positiveYears":positive_years,"coveredRegimes":covered,"positiveRegimes":positive_regimes,"strictPass":passed,"recommendation":"SHADOW_SIGNAL_CANARY" if passed else "REJECT_CANDIDATE"}
    out=ROOT/"external-validation.json"; tmp=out.with_suffix(".json.tmp"); tmp.write_text(json.dumps(report,indent=2)+"\n"); tmp.replace(out); print(json.dumps(report,separators=(",",":")))
if __name__=="__main__": main()
