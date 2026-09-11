from __future__ import annotations
import hashlib,itertools,json,os
from datetime import UTC,datetime
from pathlib import Path
import numpy as np
import pandas as pd
from loss_engine import ResearchConfig,stats

ROOT=Path(os.getenv("REPLAY_DATA_ROOT","/data/replay")); VERSION="strategy_benchmark_v1"

def load():
    source=json.loads((ROOT/"reports/latest.json").read_text()); cutoff=pd.Timestamp(source["holdoutStart"])
    files=sorted((ROOT/"parquet").glob("month=*/bars.parquet")); m=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
    m["open_time"]=pd.to_datetime(m.open_time,utc=True); m=m[m.open_time<cutoff].sort_values("open_time").drop_duplicates("open_time").set_index("open_time")
    b=m.resample("15min").agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum")).dropna().reset_index()
    tr=pd.concat([(b.high-b.low),(b.high-b.close.shift()).abs(),(b.low-b.close.shift()).abs()],axis=1).max(axis=1); b["atr"]=tr.rolling(14).mean()
    return b,cutoff,source

def entries(bars,family,p):
    close=bars.close
    if family=="TREND":
        fast=close.ewm(span=p[0],adjust=False).mean(); slow=close.ewm(span=p[1],adjust=False).mean(); slope=slow.pct_change(4)
        long=(fast>slow)&(slope>p[2]); short=(fast<slow)&(slope<-p[2])
    elif family=="BREAKOUT":
        high=bars.high.shift(1).rolling(p[0]).max(); low=bars.low.shift(1).rolling(p[0]).min()
        long=close>high; short=close<low
    else:
        mean=close.rolling(p[0]).mean(); std=close.rolling(p[0]).std(); z=(close-mean)/std
        long=z<-p[1]; short=z>p[1]
    raw=np.select([long,short],["LONG","SHORT"],default="NONE")
    return pd.Series(raw,index=bars.index).where(pd.Series(raw,index=bars.index)!=pd.Series(raw,index=bars.index).shift(),"NONE")

def simulate(bars,signals,atr_mult,target_rr,max_hold,config):
    rows=[]; busy=-1
    for i,side in enumerate(signals):
        if side=="NONE" or i+1<=busy or i+1>=len(bars) or not np.isfinite(bars.atr.iloc[i]): continue
        entry_i=i+1; entry=float(bars.open.iloc[entry_i])*(1+(config.slippage_bps if side=="LONG" else -config.slippage_bps)/10000)
        risk=float(bars.atr.iloc[i])*atr_mult; stop=entry-risk if side=="LONG" else entry+risk; target=entry+risk*target_rr if side=="LONG" else entry-risk*target_rr
        future=bars.iloc[entry_i:min(entry_i+max_hold,len(bars))]; exit_price=None; reason="TIME_EXIT"
        for j,bar in enumerate(future.itertuples(index=False)):
            stop_hit=bar.low<=stop if side=="LONG" else bar.high>=stop; target_hit=bar.high>=target if side=="LONG" else bar.low<=target
            if stop_hit or target_hit: exit_price=stop if stop_hit else target; exit_time=bar.open_time; reason="STOP" if stop_hit else "TARGET"; busy=entry_i+j; break
        if exit_price is None:
            if future.empty: continue
            exit_price=float(future.close.iloc[-1]); exit_time=future.open_time.iloc[-1]; busy=entry_i+len(future)-1
        exit_price*=1+(-config.slippage_bps if side=="LONG" else config.slippage_bps)/10000; qty=config.notional/entry; exit_notional=qty*exit_price
        gross=qty*(exit_price-entry)*(1 if side=="LONG" else -1); fees=(config.notional+exit_notional)*config.fee_bps/10000; taxes=(exit_notional if side=="LONG" else config.notional)*config.tax_bps/10000
        rows.append({"market_time":bars.open_time.iloc[entry_i],"resolved":True,"exit_time":exit_time,"exit_reason":reason,"gross_pnl":gross,"fees":fees,"taxes":taxes,"net_pnl":gross-fees-taxes})
    return pd.DataFrame(rows,columns=["market_time","resolved","exit_time","exit_reason","gross_pnl","fees","taxes","net_pnl"])

def assess(trades,start,end):
    bounds=pd.date_range(start.floor("D"),end.ceil("D"),periods=5); segments=[stats(trades[(trades.market_time>=bounds[i])&(trades.market_time<bounds[i+1])]) for i in range(4)]
    overall=stats(trades); positive=sum(x["closed"]>=15 and x["netPnl"]>0 for x in segments)
    qualified=overall["closed"]>=100 and overall["netPnl"]>0 and overall["profitFactor"]>=1.15 and positive>=3 and max(x["maxDrawdown"] for x in segments)<=20
    return overall,segments,positive,qualified

def main():
    bars,cutoff,source=load(); config=ResearchConfig(); candidates=[]
    definitions=[]
    for fast,slow,slope in itertools.product((12,24),(48,96),(0,.0005)): definitions.append(("TREND",(fast,slow,slope)))
    for lookback in (20,40,80): definitions.append(("BREAKOUT",(lookback,)))
    for lookback,z in itertools.product((20,40),(2,2.5)): definitions.append(("MEAN_REVERSION",(lookback,z)))
    for family,p in definitions:
        signal=entries(bars,family,p)
        for atr,rr,hold in itertools.product((1.5,2.0),(1.5,2.0,3.0),(32,64)):
            trades=simulate(bars,signal,atr,rr,hold,config); overall,segments,positive,qualified=assess(trades,bars.open_time.min(),cutoff)
            candidates.append({"family":family,"signalParameters":p,"risk":{"atrStop":atr,"targetRR":rr,"maxHolding15m":hold},"overall":overall,"segments":segments,"positiveSegments":positive,"qualified":qualified})
    qualified=[x for x in candidates if x["qualified"]]; ranked=sorted(candidates,key=lambda x:(x["positiveSegments"],x["overall"]["netPnl"],x["overall"]["closed"]),reverse=True)
    report={"version":VERSION,"reportId":hashlib.sha256(f"{VERSION}:{source['reportId']}:{cutoff}".encode()).hexdigest(),"updatedAt":datetime.now(UTC).isoformat(),"symbol":source["symbol"],"mode":"OFFLINE_RESEARCH_ONLY","liveTrading":False,"executionActionable":False,"executionGatePassed":False,"promotionAllowed":False,"excludedHoldoutStart":cutoff.isoformat(),"holdoutRowsAccessed":0,"entryPolicy":"NEXT_15M_OPEN_CAUSAL","sameBarPolicy":"STOP_FIRST_CONSERVATIVE","costs":{"feeBps":config.fee_bps,"taxBps":config.tax_bps,"slippageBps":config.slippage_bps,"funding":"NOT_INSTRUMENTED"},"candidateCount":len(candidates),"qualifiedCandidates":qualified,"bestResearchCandidates":ranked[:10],"recommendation":"CROSS_ASSET_CONFIRMATION_REQUIRED" if qualified else "NO_BENCHMARK_EDGE"}
    out=ROOT/"reports/benchmark-v1-latest.json"; tmp=out.with_suffix(".json.tmp"); tmp.write_text(json.dumps(report,indent=2)+"\n"); tmp.replace(out)
    print(json.dumps({k:report[k] for k in ("reportId","symbol","candidateCount","holdoutRowsAccessed","qualifiedCandidates","bestResearchCandidates","recommendation")},separators=(",",":")))
if __name__=="__main__": main()
