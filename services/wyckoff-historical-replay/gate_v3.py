from __future__ import annotations
import hashlib,itertools,json,os
from dataclasses import replace
from datetime import UTC,datetime
from pathlib import Path
import numpy as np
import pandas as pd
from loss_engine import ResearchConfig,candidate_masks,outcomes,stats

ROOT=Path(os.getenv("REPLAY_DATA_ROOT","/data/replay")); VERSION="wyckoff_gate_v3_nested_walk_forward"

def load_development():
    source=json.loads((ROOT/"reports/latest.json").read_text()); cutoff=pd.Timestamp(source["holdoutStart"])
    signals=pd.read_parquet(ROOT/"reports/allow-signals.parquet"); signals["market_time"]=pd.to_datetime(signals.market_time,utc=True)
    signals=signals[signals.market_time<cutoff].copy().reset_index(drop=True)
    files=sorted((ROOT/"parquet").glob("month=*/bars.parquet"))
    bars=pd.concat([pd.read_parquet(p,columns=["open_time","high","low","close"]) for p in files],ignore_index=True)
    bars["open_time"]=pd.to_datetime(bars.open_time,utc=True)
    return signals,bars.sort_values("open_time").reset_index(drop=True),cutoff,source

def add_features(frame):
    r=frame.copy(); hour=r.market_time.dt.hour
    r["session"]=np.select([hour<7,hour<13,hour<21],["ASIA","EUROPE","US"],default="LATE_US")
    r["volatility_regime"]=pd.cut(pd.to_numeric(r["spreadRatio15m"],errors="coerce"),[-np.inf,.8,1.2,2,np.inf],labels=["COMPRESSED","NORMAL","EXPANDED","EXTREME"])
    r["trend_strength"]=pd.cut(pd.to_numeric(r["hourlyEMA20Slope"],errors="coerce").abs(),[-np.inf,.0005,.0015,np.inf],labels=["FLAT","MODERATE","STRONG"])
    return r

def close_timeouts(trades,signals,bars,config):
    r=trades.copy(); opens=pd.DatetimeIndex(bars.open_time)
    for i in r.index[~r.resolved]:
        s=signals.loc[i]; start=int(opens.searchsorted(s.market_time,side="right")); end=min(start+config.max_holding_bars,len(bars))-1
        if end<start: continue
        side=s.direction; entry=float(s.entry)*(1+(config.slippage_bps if side=="LONG" else -config.slippage_bps)/10000)
        exit_price=float(bars.iloc[end].close)*(1+(-config.slippage_bps if side=="LONG" else config.slippage_bps)/10000)
        qty=config.notional/entry; exit_notional=qty*exit_price
        gross=qty*(exit_price-entry)*(1 if side=="LONG" else -1)
        fees=(config.notional+exit_notional)*config.fee_bps/10000; taxes=(exit_notional if side=="LONG" else config.notional)*config.tax_bps/10000
        r.loc[i,["resolved","exit_time","exit_reason","gross_pnl","fees","taxes","net_pnl"]]=[True,bars.iloc[end].open_time,"TIME_EXIT",gross,fees,taxes,gross-fees-taxes]
    return r

def universe(frame):
    atoms=candidate_masks(frame)
    for col in ("session","volatility_regime","trend_strength"):
        for value in frame[col].dropna().unique(): atoms[f"{col}={value}"]=frame[col].astype(str)==str(value)
    result=dict(atoms); names=sorted(n for n in atoms if n!="all")
    for left,right in itertools.combinations(names,2):
        if "=" in left and "=" in right and left.split("=")[0]==right.split("=")[0]: continue
        result[f"{left} AND {right}"]=atoms[left]&atoms[right]
    return result

def objective(train,validation):
    if train["closed"]<35 or validation["closed"]<12 or train["netPnl"]<=0 or validation["netPnl"]<=0: return -1e9
    return validation["netPnl"]-.5*validation["maxDrawdown"]+.15*train["netPnl"]

def analyze_variant(trades,hold_bars):
    candidates=universe(trades); boundaries=pd.date_range(trades.market_time.min().floor("D"),trades.market_time.max().ceil("D"),periods=8)
    folds=[]; selections={}
    for i in range(3,7):
        train=trades.market_time<boundaries[i]; valid=(trades.market_time>=boundaries[i])&(trades.market_time<boundaries[i+1]); ranked=[]
        for name,mask in candidates.items():
            a,b=stats(trades[train&mask]),stats(trades[valid&mask]); ranked.append((objective(a,b),name,a,b))
        best=max(ranked,key=lambda x:x[0]); chosen=best[1] if best[0]>-1e9 else "NO_TRADE"
        if chosen!="NO_TRADE": selections[chosen]=selections.get(chosen,0)+1
        folds.append({"trainEnd":boundaries[i].isoformat(),"validationEnd":boundaries[i+1].isoformat(),"gate":chosen,"score":best[0],"train":best[2],"validation":best[3]})
    stable=[]
    for name,count in selections.items():
        overall=stats(trades[candidates[name]]); positive=sum(f["gate"]==name and f["validation"]["netPnl"]>0 for f in folds)
        if count>=2 and positive>=2 and overall["closed"]>=60 and overall["netPnl"]>0:
            stable.append({"gate":name,"selectedFolds":count,"positiveSelectedFolds":positive,"development":overall})
    return {"maxHoldingMinutes":hold_bars,"folds":folds,"stableCandidates":sorted(stable,key=lambda x:x["development"]["netPnl"],reverse=True)}

def main():
    signals,bars,cutoff,source=load_development(); config=ResearchConfig(); variants=[]
    for hold in (60,180,360,720,1440):
        c=replace(config,max_holding_bars=hold); trades=add_features(close_timeouts(outcomes(signals,bars,c),signals,bars,c)); variants.append(analyze_variant(trades,hold))
    stable=[{"maxHoldingMinutes":v["maxHoldingMinutes"],**x} for v in variants for x in v["stableCandidates"]]
    report={"version":VERSION,"reportId":hashlib.sha256(f"{VERSION}:{source['reportId']}:{cutoff}".encode()).hexdigest(),"updatedAt":datetime.now(UTC).isoformat(),"mode":"OFFLINE_RESEARCH_ONLY","liveTrading":False,"executionActionable":False,"executionGatePassed":False,"promotionAllowed":False,"symbol":"BTCUSDT","developmentSignals":len(signals),"developmentLastMarketTime":signals.market_time.max().isoformat(),"excludedHoldoutStart":cutoff.isoformat(),"holdoutRowsAccessed":0,"selection":"nested_anchored_walk_forward_development_only","costs":{"feeBps":config.fee_bps,"taxBps":config.tax_bps,"slippageBps":config.slippage_bps,"funding":"NOT_INSTRUMENTED"},"variants":variants,"stableCandidates":stable,"recommendation":"CONTINUE_SHADOW_VALIDATION" if stable else "NO_TRADE_RESEARCH_LOCK"}
    output=ROOT/"reports/gate-v3-latest.json"; temp=output.with_suffix(".json.tmp"); temp.write_text(json.dumps(report,indent=2)+"\n"); temp.replace(output)
    print(json.dumps({k:report[k] for k in ("reportId","developmentSignals","excludedHoldoutStart","holdoutRowsAccessed","stableCandidates","recommendation")},separators=(",",":")))
if __name__=="__main__": main()
