import hashlib
import json
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class FusionConfig:
    max_market_age_seconds: int = 3900
    continuity_bars: int = 12
    liquidity_soak_status: str = "FAIL"


def _iso_epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _continuous(times: list[str]) -> bool:
    if len(times) < 2:
        return False
    epochs = [_iso_epoch(value) for value in times]
    return all(abs((right - left) - 300.0) < 0.01 for left, right in zip(epochs, epochs[1:]))


def fuse(wyckoff: dict, liquidity: dict, config: FusionConfig, now: float) -> dict:
    symbol = wyckoff["symbol"]
    market_time = max(wyckoff["marketTime"], liquidity["marketTime"])
    raw_id = f"evidence_fusion_v1|{symbol}|{wyckoff['signalId']}|{liquidity['observationId']}"
    observation_id = hashlib.sha256(raw_id.encode()).hexdigest()
    unsafe = any((
        wyckoff.get("executionActionable"), wyckoff.get("executionGatePassed"),
        liquidity.get("executionActionable"), liquidity.get("executionGatePassed"),
        wyckoff.get("liveTrading"), liquidity.get("liveTrading"),
    ))
    age = max(0.0, now - _iso_epoch(market_time))
    bar_match = wyckoff.get("barId") == liquidity.get("barId")
    continuity_ok = bool(wyckoff.get("continuityOk") and liquidity.get("continuityOk"))
    decision, reason = "WAIT", "confirmation_pending"
    if unsafe:
        decision, reason = "REJECT_SHADOW", "unsafe_source_flag"
    elif age > config.max_market_age_seconds:
        decision, reason = "REJECT_SHADOW", "stale_evidence"
    elif not continuity_ok:
        decision, reason = "REJECT_SHADOW", "continuity_gate_failed"
    elif not bar_match:
        decision, reason = "WAIT", "bar_alignment_pending"
    elif config.liquidity_soak_status != "PASS":
        decision, reason = "REJECT_SHADOW", "liquidity_soak_not_passed"
    else:
        wy_decision = wyckoff.get("decision", "WAIT")
        liq_decision = liquidity.get("decision", "WAIT")
        wy_direction = wyckoff.get("direction", "NONE")
        liq_direction = liquidity.get("direction", "NONE")
        if wy_decision == "ALLOW_SHADOW" and liq_decision == "ALLOW_SHADOW":
            if wy_direction == liq_direction and wy_direction in {"LONG", "SHORT"}:
                decision, reason = "ALLOW_SHADOW", "wyckoff_liquidity_confluence"
            else:
                decision, reason = "REJECT_SHADOW", "direction_conflict"
        elif wy_decision.startswith("REJECT") or liq_decision.startswith("REJECT"):
            decision, reason = "REJECT_SHADOW", "source_rejected"
    return {
        "observationId": observation_id, "strategy": "evidence_fusion_shadow_v1",
        "symbol": symbol, "marketTime": market_time, "barId": wyckoff.get("barId"),
        "decision": decision, "direction": wyckoff.get("direction", "NONE"), "reason": reason,
        "wyckoffDecision": wyckoff.get("decision"), "liquidityDecision": liquidity.get("decision"),
        "barAligned": bar_match, "continuityOk": continuity_ok, "marketAgeSeconds": age,
        "liquiditySoakStatus": config.liquidity_soak_status,
        "mode": "SHADOW_ONLY", "liveTrading": False, "executionActionable": False,
        "executionGatePassed": False,
    }


class FusionJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS fusion_observations(
            observation_id TEXT PRIMARY KEY, market_time TEXT NOT NULL, symbol TEXT NOT NULL,
            evidence_json TEXT NOT NULL, created_at REAL NOT NULL)""")
        self.connection.commit()

    def insert(self, evidence: dict) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO fusion_observations VALUES(?,?,?,?,?)",
            (evidence["observationId"], evidence["marketTime"], evidence["symbol"],
             json.dumps(evidence, separators=(",", ":")), time.time()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM fusion_observations").fetchone()[0])


class EvidenceFusionAdapter:
    def __init__(self, wyckoff_url: str, liquidity_url: str, output_root: Path,
                 symbols: tuple[str, ...], config: FusionConfig):
        self.wyckoff_url, self.liquidity_url = wyckoff_url, liquidity_url
        self.symbols, self.config = symbols, config
        self.journal = FusionJournal(output_root / "evidence-fusion.db")

    @staticmethod
    def _projection(url: str) -> dict:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.load(response)

    @staticmethod
    def _latest(projection: dict, symbol: str, identity: str) -> dict | None:
        scheduler = next((x for x in projection.get("scheduler", []) if x.get("symbol") == symbol), {})
        item = next((x for x in projection.get("signals", []) if x.get("symbol") == symbol), None)
        if not item:
            return None
        item = dict(item)
        item[identity] = item.get(identity) or item.get("signalId") or item.get("observationId")
        if identity == "signalId":
            item["decision"] = item.get("authorityDecision", item.get("decision", "WAIT"))
            item["reason"] = item.get("authorityReason", item.get("reason", "unknown"))
        item["continuityOk"] = bool(scheduler.get("continuityOk", scheduler.get("status") == "READY"))
        return item

    def run_once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        wyckoff_projection = self._projection(self.wyckoff_url)
        liquidity_projection = self._projection(self.liquidity_url)
        signals, scheduler = [], []
        for symbol in self.symbols:
            wyckoff = self._latest(wyckoff_projection, symbol, "signalId")
            liquidity = self._latest(liquidity_projection, symbol, "observationId")
            if not wyckoff or not liquidity:
                scheduler.append({"symbol": symbol, "status": "WARMING_UP", "reason": "source_missing"})
                continue
            evidence = fuse(wyckoff, liquidity, self.config, now)
            self.journal.insert(evidence)
            signals.append(evidence)
            scheduler.append({"symbol": symbol, "status": "READY", "reason": evidence["reason"]})
        projection = {"updatedAt": datetime.now(UTC).isoformat(), "mode": "SHADOW_ONLY",
                      "liveTrading": False, "executionActionable": False,
                      "executionGatePassed": False, "signals": signals, "scheduler": scheduler,
                      "journalRows": self.journal.count()}
        return projection
