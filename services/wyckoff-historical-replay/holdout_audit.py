from __future__ import annotations
import hashlib,json,os
from datetime import UTC,datetime
from pathlib import Path
import pandas as pd
from benchmark_v1 import entries,simulate
from loss_engine import ResearchConfig,stats
ROOT=Path(os.getenv("REPLAY_DATA_ROOT","/data/replay")); VERSION="btc_4h_trend_frozen_holdout_audit_v1"
PARAMETERS={"timeframe":"4h","family":"TREND","signalParameters":[12,96,.001],"atrStop":3.0,"targetRR":3.0,"maxHoldingBars":16}
def main():
    source=json.loads((ROOT/"reports/latest.json").read_text()); cutoff=pd.Timestamp(source["holdoutStart"])
    files=sorted((ROOT/"parquet").glob("month=*/bars.parquet")); m=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
    m["open_time"]=pd.to_datetime(m.open_time,utc=True); m=m.sort_values("open_time").drop_duplicates("open_time").set_index("open_time")
    b=m.resample("4h").agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum")).dropna().reset_index()
    tr=pd.concat([(b.high-b.low),(b.high-b.close.shift()).abs(),(b.low-b.close.shift()).abs()],axis=1).max(axis=1); b["atr"]=tr.rolling(14).mean()
    signal=entries(b,"TREND",tuple(PARAMETERS["signalParameters"])); trades=simulate(b,signal,3.0,3.0,16,ResearchConfig())
    holdout=trades[trades.market_time>=cutoff]; result=stats(holdout)
    passed=result["closed"]>=8 and result["netPnl"]>0 and result["profitFactor"]>=1.15 and result["maxDrawdown"]<=10
    report={"version":VERSION,"reportId":hashlib.sha256(f"{VERSION}:{source['reportId']}:{PARAMETERS}".encode()).hexdigest(),"updatedAt":datetime.now(UTC).isoformat(),"symbol":source["symbol"],"mode":"OFFLINE_RESEARCH_ONLY","liveTrading":False,"executionActionable":False,"executionGatePassed":False,"promotionAllowed":False,"selectionFrozenBeforeHoldout":True,"holdoutStart":cutoff.isoformat(),"parameters":PARAMETERS,"costs":{"feeBps":5,"taxBps":10,"slippageBps":2,"funding":"NOT_INSTRUMENTED"},"holdout":result,"strictPass":passed,"recommendation":"SHADOW_CANARY_CANDIDATE" if passed else "REJECT_CANDIDATE"}
    out=ROOT/"reports/holdout-audit-latest.json"; tmp=out.with_suffix(".json.tmp"); tmp.write_text(json.dumps(report,indent=2)+"\n"); tmp.replace(out); print(json.dumps(report,separators=(",",":")))
if __name__=="__main__": main()
