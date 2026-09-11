from __future__ import annotations
import hashlib,itertools,json,os
from datetime import UTC,datetime
from pathlib import Path
import numpy as np
import pandas as pd
from loss_engine import ResearchConfig,stats

ROOT=Path(os.getenv("REPLAY_DATA_ROOT","/data/replay")); VERSION="wyckoff_gate_v4_confirmation_retest"

def load_development():
    source=json.loads((ROOT/"reports/latest.json").read_text()); cutoff=pd.Timestamp(source["holdoutStart"])
    s=pd.read_parquet(ROOT/"reports/allow-signals.parquet"); s["market_time"]=pd.to_datetime(s.market_time,utc=True)
    s=s[s.market_time<cutoff].copy().reset_index(drop=True)
    files=sorted((ROOT/"parquet").glob("month=*/bars.parquet")); m=pd.concat([pd.read_parquet(p) for p in files],ignore_index=True)
    m["open_time"]=pd.to_datetime(m.open_time,utc=True); m=m.sort_values("open_time").drop_duplicates("open_time")
    m=m.set_index("open_time"); b=m.resample("5min").agg(open=("open","first"),high=("high","max"),low=("low","min"),close=("close","last"),volume=("volume","sum")).dropna().reset_index()
    b["atr"]=pd.concat([(b.high-b.low),(b.high-b.close.shift()).abs(),(b.low-b.close.shift()).abs()],axis=1).max(axis=1).rolling(14).mean()
    b["volume_median20"]=b.volume.rolling(20).median(); return s,b,cutoff,source

def discover(setups,bars,window,volume_ratio,retest_bars,target_rr,hold_bars,config):
    opens=pd.DatetimeIndex(bars.open_time); rows=[]
    for setup in setups.itertuples(index=False):
        start=int(opens.searchsorted(setup.market_time,side="right")); scan=bars.iloc[start:start+window]
        if len(scan)<3: continue
        side=setup.direction; breakout=None; level=None
        for j in range(2,len(scan)):
            prior=scan.iloc[max(0,j-3):j]
            level=float(prior.high.max() if side=="LONG" else prior.low.min()); bar=scan.iloc[j]
            crossed=bar.close>level if side=="LONG" else bar.close<level
            body_ok=bar.close>bar.open if side=="LONG" else bar.close<bar.open
            volume_ok=bar.volume>=bar.volume_median20*volume_ratio
            if crossed and body_ok and volume_ok: breakout=start+j; break
        if breakout is None: continue
        confirm=None
        for j in range(breakout+1,min(breakout+1+retest_bars,len(bars)-1)):
            bar=bars.iloc[j]; tolerance=max(float(bar.atr)*.2,level*.0002)
            touched=bar.low<=level+tolerance if side=="LONG" else bar.high>=level-tolerance
            held=bar.close>=level if side=="LONG" else bar.close<=level
            if touched and held: confirm=j; break
        if confirm is None: continue
        entry_index=confirm+1
        if entry_index>=len(bars) or bars.iloc[entry_index].open_time>=pd.Timestamp(setups.market_time.max()).ceil("D")+pd.Timedelta(days=2): continue
        entry=float(bars.iloc[entry_index].open)*(1+(config.slippage_bps if side=="LONG" else -config.slippage_bps)/10000)
        original_stop=float(setup.stop); risk=entry-original_stop if side=="LONG" else original_stop-entry
        if risk<=0 or risk/entry>.03: continue
        target=entry+(risk*target_rr if side=="LONG" else -risk*target_rr); exit_price=None; exit_reason="TIME_EXIT"
        future=bars.iloc[entry_index:entry_index+hold_bars]
        for bar in future.itertuples(index=False):
            stop_hit=bar.low<=original_stop if side=="LONG" else bar.high>=original_stop
            target_hit=bar.high>=target if side=="LONG" else bar.low<=target
            if stop_hit or target_hit:
                exit_price=original_stop if stop_hit else target; exit_time=bar.open_time; exit_reason="STOP" if stop_hit else "TARGET"; break
        if exit_price is None:
            if future.empty: continue
            exit_price=float(future.iloc[-1].close); exit_time=future.iloc[-1].open_time
        exit_price*=1+(-config.slippage_bps if side=="LONG" else config.slippage_bps)/10000
        qty=config.notional/entry; exit_notional=qty*exit_price; gross=qty*(exit_price-entry)*(1 if side=="LONG" else -1)
        fees=(config.notional+exit_notional)*config.fee_bps/10000; taxes=(exit_notional if side=="LONG" else config.notional)*config.tax_bps/10000
        rows.append({"market_time":bars.iloc[entry_index].open_time,"resolved":True,"exit_time":exit_time,"exit_reason":exit_reason,"net_pnl":gross-fees-taxes,"gross_pnl":gross,"fees":fees,"taxes":taxes})
    return pd.DataFrame(rows)

def evaluate(trades):
    if trades.empty: return {"overall":stats(trades),"segments":[],"positiveSegments":0,"qualified":False}
    bounds=pd.date_range(trades.market_time.min().floor("D"),trades.market_time.max().ceil("D"),periods=5); segments=[]
    for i in range(4): segments.append(stats(trades[(trades.market_time>=bounds[i])&(trades.market_time<bounds[i+1])]))
    overall=stats(trades); positive=sum(x["closed"]>=10 and x["netPnl"]>0 for x in segments)
    qualified=overall["closed"]>=60 and overall["netPnl"]>0 and overall["profitFactor"]>=1.15 and positive>=3
    return {"overall":overall,"segments":segments,"positiveSegments":positive,"qualified":qualified}

def main():
    setups,bars,cutoff,source=load_development(); config=ResearchConfig(); results=[]
    grid=itertools.product((6,12),(1.0,1.25),(3,6),(1.5,2.0,2.5),(36,72,144))
    for window,volume,retest,rr,hold in grid:
        trades=discover(setups,bars,window,volume,retest,rr,hold,config); result=evaluate(trades)
        results.append({"parameters":{"confirmationWindow5m":window,"minimumVolumeRatio":volume,"retestWindow5m":retest,"targetRR":rr,"maxHolding5m":hold},**result})
    qualified=[x for x in results if x["qualified"]]; ranked=sorted(results,key=lambda x:(x["positiveSegments"],x["overall"]["netPnl"],x["overall"]["closed"]),reverse=True)
    report={"version":VERSION,"reportId":hashlib.sha256(f"{VERSION}:{source['reportId']}:{cutoff}".encode()).hexdigest(),"updatedAt":datetime.now(UTC).isoformat(),"mode":"OFFLINE_RESEARCH_ONLY","liveTrading":False,"executionActionable":False,"executionGatePassed":False,"promotionAllowed":False,"symbol":source["symbol"],"developmentSetups":len(setups),"excludedHoldoutStart":cutoff.isoformat(),"holdoutRowsAccessed":0,"causalEntry":"next_5m_open_after_breakout_and_retest_confirmation","sameBarPolicy":"STOP_FIRST_CONSERVATIVE","costs":{"feeBps":config.fee_bps,"taxBps":config.tax_bps,"slippageBps":config.slippage_bps,"funding":"NOT_INSTRUMENTED"},"candidateCount":len(results),"qualifiedCandidates":qualified,"bestResearchCandidates":ranked[:10],"recommendation":"QUALIFIED_FOR_NEW_UNSEEN_SHADOW" if qualified else "NO_TRADE_RESEARCH_LOCK"}
    out=ROOT/"reports/gate-v4-latest.json"; tmp=out.with_suffix(".json.tmp"); tmp.write_text(json.dumps(report,indent=2)+"\n"); tmp.replace(out)
    print(json.dumps({k:report[k] for k in ("reportId","developmentSetups","holdoutRowsAccessed","candidateCount","qualifiedCandidates","bestResearchCandidates","recommendation")},separators=(",",":")))
if __name__=="__main__": main()
