import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest

from engine import Thresholds, WyckoffShadowAdapter


os.umask(0o077)
LAST = Gauge("wyckoff_adapter_last_success_unixtime", "Last successful Wyckoff shadow evaluation")
SIGNALS = Gauge("wyckoff_adapter_signals", "Current Wyckoff shadow signals", ["symbol", "decision", "event"])
SCORE = Gauge("wyckoff_adapter_score", "Current Wyckoff shadow score", ["symbol"])
JOURNAL = Gauge("wyckoff_adapter_journal_rows", "Exactly-once Wyckoff signal journal rows")
RESEARCH_JOURNAL = Gauge("wyckoff_research_journal_rows", "Exactly-once Wyckoff VSA research observations")
PHASE = Gauge("wyckoff_market_phase_info", "Current quantified Wyckoff market phase", ["symbol", "phase", "explanation"])
PHASE_SCORE = Gauge("wyckoff_market_phase_confidence", "Rule confidence for current Wyckoff phase", ["symbol"])
INTENT = Gauge("wyckoff_composite_operator_intent_info", "Rule-based composite operator intent", ["symbol", "intent"])
VSA = Gauge("wyckoff_vsa_event_info", "Confirmed VSA events on latest closed candle", ["symbol", "event", "direction", "status", "explanation"])
VSA_SCORE = Gauge("wyckoff_vsa_event_score", "Rule score for confirmed VSA events", ["symbol", "event"])
FEATURE = Gauge("wyckoff_market_feature", "Bounded market-structure features", ["symbol", "feature"])
ERRORS = Counter("wyckoff_adapter_errors_total", "Wyckoff adapter cycle errors", ["type"])
state = {"healthy": False, "error": None, "last_success": 0.0}
thresholds = Thresholds(
    max_market_age_seconds=int(os.getenv("WYCKOFF_MAX_MARKET_AGE_SECONDS", "3900")),
    min_rr=float(os.getenv("WYCKOFF_MIN_RR", "2.0")), ready_score=int(os.getenv("WYCKOFF_READY_SCORE", "70")),
)
adapter = WyckoffShadowAdapter(
    Path(os.getenv("SILVER_ROOT", "/data/silver")), Path(os.getenv("WYCKOFF_OUTPUT_ROOT", "/data/wyckoff-signals")),
    os.getenv("WYCKOFF_DATA_EXCHANGE", "binance"),
    tuple(item.strip().upper() for item in os.getenv("WYCKOFF_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if item.strip()),
    thresholds,
)


def execute():
    try:
        result = adapter.run_once()
        SIGNALS.clear(); PHASE.clear(); PHASE_SCORE.clear(); INTENT.clear(); VSA.clear(); VSA_SCORE.clear(); FEATURE.clear()
        for signal in result["signals"]:
            SIGNALS.labels(signal["symbol"], signal["authorityDecision"], signal["eventCandidate"]).set(1)
            SCORE.labels(signal["symbol"]).set(signal["score"])
            PHASE.labels(signal["symbol"], signal["phaseCandidate"], signal["phaseExplanation"]).set(1)
            PHASE_SCORE.labels(signal["symbol"]).set(signal["phaseConfidence"])
            INTENT.labels(signal["symbol"], signal["compositeOperatorIntent"]).set(1)
            for event in signal["vsaEvents"]:
                VSA.labels(signal["symbol"], event["event"], event["direction"], event["status"], event["explanation"]).set(1)
                VSA_SCORE.labels(signal["symbol"], event["event"]).set(event["score"])
            for feature in ("relativeVolume15m", "spreadRatio15m", "closeLocation15m", "rangePosition", "rangeWidthATR"):
                FEATURE.labels(signal["symbol"], feature).set(signal["features"][feature])
        JOURNAL.set(result["journalRows"]); RESEARCH_JOURNAL.set(result["researchJournalRows"]); LAST.set(time.time())
        state.update(healthy=True, error=None, last_success=time.time())
    except Exception as exc:
        ERRORS.labels(type(exc).__name__).inc(); state.update(healthy=False, error=type(exc).__name__)


def scheduler():
    while True:
        time.sleep(int(os.getenv("WYCKOFF_INTERVAL_SECONDS", "60"))); execute()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics": body, code, kind = generate_latest(), 200, "text/plain"
        elif self.path == "/healthz":
            body = json.dumps({"status": "healthy" if state["healthy"] else "unhealthy", "mode": "SHADOW_ONLY",
                               "live_trading": False, "error": state["error"]}).encode()
            code, kind = (200 if state["healthy"] else 503), "application/json"
        else: body, code, kind = b"not found", 404, "text/plain"
        self.send_response(code); self.send_header("Content-Type", kind); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass


if __name__ == "__main__":
    execute(); threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8070), Handler).serve_forever()
