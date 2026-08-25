import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class FusionConfig:
    max_market_age_seconds: int = 3900
    continuity_bars: int = 12
    liquidity_soak_status: str = "FAIL"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


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
    def __init__(self, wyckoff_db: Path, liquidity_db: Path, output_root: Path,
                 symbols: tuple[str, ...], config: FusionConfig):
        self.wyckoff_db, self.liquidity_db = wyckoff_db, liquidity_db
        self.symbols, self.config = symbols, config
        self.journal = FusionJournal(output_root / "evidence-fusion.db")

    def _latest_wyckoff(self, symbol: str) -> dict | None:
        with _connect_read_only(self.wyckoff_db) as connection:
            row = connection.execute("""SELECT signal_id,market_time,bar_id,direction,decision,reason,
                execution_actionable,execution_gate_passed FROM signals WHERE symbol=?
                ORDER BY market_time DESC LIMIT 1""", (symbol,)).fetchone()
            times = [x[0] for x in connection.execute("""SELECT market_time FROM research_observations
                WHERE symbol=? ORDER BY market_time DESC LIMIT ?""", (symbol, self.config.continuity_bars))]
        if not row:
            return None
        times.reverse()
        return {"signalId": row[0], "marketTime": row[1], "barId": row[2], "symbol": symbol,
                "direction": row[3], "decision": row[4], "reason": row[5],
                "executionActionable": bool(row[6]), "executionGatePassed": bool(row[7]),
                "liveTrading": False, "continuityOk": _continuous(times)}

    def _latest_liquidity(self, symbol: str) -> dict | None:
        with _connect_read_only(self.liquidity_db) as connection:
            rows = connection.execute("""SELECT market_time,evidence_json FROM observations WHERE symbol=?
                ORDER BY market_time DESC LIMIT ?""", (symbol, self.config.continuity_bars)).fetchall()
        if not rows:
            return None
        evidence = json.loads(rows[0][1])
        times = [row[0] for row in reversed(rows)]
        evidence["continuityOk"] = _continuous(times)
        return evidence

    def run_once(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        signals, scheduler = [], []
        for symbol in self.symbols:
            wyckoff, liquidity = self._latest_wyckoff(symbol), self._latest_liquidity(symbol)
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
