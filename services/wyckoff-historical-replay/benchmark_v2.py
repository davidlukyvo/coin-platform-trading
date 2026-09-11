from __future__ import annotations
import hashlib,itertools,json,os
from datetime import UTC,datetime
from pathlib import Path
import pandas as pd
from benchmark_v1 import entries,simulate
from loss_engine import ResearchConfig,stats

ROOT=Path(os.getenv("REPLAY_DATA_ROOT","/data/replay")); VERSION="strategy_benchmark_v2_low_turnover"
def load(timeframe):
    source=json.loads((ROOT/"reports/latest.json").read_text()); cutoff=pd.Timestamp(source["holdoutStart"])
    files=sorted((ROOT/"parquet").glob("month=*/bars.parquet")); m=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
    m["open_time"]=pd.to_datetime(m.open_time,utc=True); m=m[m.open_time<cutoff].sort_values("open_time").drop_duplicates("open_time").set_index("open_time")
    b=m.resample(timeframe).agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum")).dropna().reset_index()
    tr=pd.concat([(b.high-b.low),(b.high-b.close.shift()).abs(),(b.low-b.close.shift()).abs()],axis=1).max(axis=1); b["atr"]=tr.rolling(14).mean()
    return b,cutoff,source
def assess(trades,start,end):
    bounds=pd.date_range(start.floor("D"),end.ceil("D"),periods=5); seg=[stats(trades[(trades.market_time>=bounds[i])&(trades.market_time<bounds[i+1])]) for i in range(4)]
    overall=stats(trades); positive=sum(x["closed"]>=8 and x["netPnl"]>0 for x in seg)
    qualified=overall["closed"]>=40 and overall["netPnl"]>0 and overall["profitFactor"]>=1.15 and positive>=3 and overall["maxDrawdown"]<=20
    return overall,seg,positive,qualified
def main():
    config=ResearchConfig(); all_results=[]; source=None; cutoff=None
    for timeframe in ("1h","4h"):
        bars,cutoff,source=load(timeframe); definitions=[]
        for fast,slow,slope in itertools.product((8,12,24),(32,48,96),(0,.001)): definitions.append(("TREND",(fast,slow,slope)))
        for lookback in (20,40,80): definitions.append(("BREAKOUT",(lookback,)))
        for lookback,z in itertools.product((20,40),(2,2.5)): definitions.append(("MEAN_REVERSION",(lookback,z)))
        for family,p in definitions:
            signal=entries(bars,family,p)
            for atr,rr,hold in itertools.product((1.5,2.0,3.0),(2.0,3.0,4.0),(16,32)):
                trades=simulate(bars,signal,atr,rr,hold,config); overall,segments,positive,qualified=assess(trades,bars.open_time.min(),cutoff)
                all_results.append({"timeframe":timeframe,"family":family,"signalParameters":p,"risk":{"atrStop":atr,"targetRR":rr,"maxHoldingBars":hold},"overall":overall,"segments":segments,"positiveSegments":positive,"qualified":qualified})
    qualified=[x for x in all_results if x["qualified"]]; ranked=sorted(all_results,key=lambda x:(x["positiveSegments"],x["overall"]["netPnl"],x["overall"]["closed"]),reverse=True)
    report={"version":VERSION,"reportId":hashlib.sha256(f"{VERSION}:{source['reportId']}:{cutoff}".encode()).hexdigest(),"updatedAt":datetime.now(UTC).isoformat(),"symbol":source["symbol"],"mode":"OFFLINE_RESEARCH_ONLY","liveTrading":False,"executionActionable":False,"executionGatePassed":False,"promotionAllowed":False,"excludedHoldoutStart":cutoff.isoformat(),"holdoutRowsAccessed":0,"timeframes":["1h","4h"],"entryPolicy":"NEXT_BAR_OPEN_CAUSAL","sameBarPolicy":"STOP_FIRST_CONSERVATIVE","costs":{"feeBps":config.fee_bps,"taxBps":config.tax_bps,"slippageBps":config.slippage_bps,"funding":"NOT_INSTRUMENTED"},"candidateCount":len(all_results),"qualifiedCandidates":qualified,"bestResearchCandidates":ranked[:10],"recommendation":"CROSS_ASSET_CONFIRMATION_REQUIRED" if qualified else "NO_LOW_TURNOVER_EDGE"}
    out=ROOT/"reports/benchmark-v2-latest.json"; tmp=out.with_suffix(".json.tmp"); tmp.write_text(json.dumps(report,indent=2)+"\n"); tmp.replace(out)
    print(json.dumps({k:report[k] for k in ("reportId","symbol","candidateCount","holdoutRowsAccessed","qualifiedCandidates","bestResearchCandidates","recommendation")},separators=(",",":")))
if __name__=="__main__": main()
