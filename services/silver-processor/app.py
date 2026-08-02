from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest

from processor import Checkpoint, SilverProcessor

logging.basicConfig(level=os.getenv("SILVER_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("silver")
PROCESSED = Counter("silver_records_processed_total", "Input Bronze records")
WRITTEN = Counter("silver_records_written_total", "Silver records written")
REJECTED = Counter("silver_records_rejected_total", "Rejected records")
DUPLICATES = Counter("silver_duplicates_total", "Duplicate records")
BATCHES = Counter("silver_batches_total", "Completed batches")
DURATION = Histogram("silver_batch_duration_seconds", "Batch duration")
LAG = Gauge("silver_lag_seconds", "Lag from latest written exchange event")
LAST_SUCCESS = Gauge("silver_last_success_unixtime", "Last successful batch")
CHECKPOINT_OFFSET = Gauge("silver_checkpoint_offset", "Highest committed source offset")
FILES_WRITTEN = Counter("silver_files_written_total", "Parquet files written")
WRITE_ERRORS = Counter("silver_write_errors_total", "Silver write errors")

stop_event = threading.Event()
runtime = {"fatal": None, "last_success": None}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/metrics":
            body, status, content = generate_latest(), 200, CONTENT_TYPE_LATEST
        elif self.path == "/healthz":
            healthy = runtime["fatal"] is None
            body = json.dumps({"status": "healthy" if healthy else "unhealthy", **runtime}).encode()
            status, content = (200 if healthy else 503), "application/json"
        else:
            body, status, content = b"not found", 404, "text/plain"
        self.send_response(status); self.send_header("Content-Type", content); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def settings() -> tuple[SilverProcessor, int, int]:
    checkpoint = Checkpoint(Path(os.getenv("SILVER_CHECKPOINT", "/data/checkpoints/silver/state.db")))
    processor = SilverProcessor(
        Path(os.getenv("BRONZE_ROOT", "/data/bronze")), Path(os.getenv("SILVER_ROOT", "/data/silver")),
        Path(os.getenv("SILVER_QUARANTINE_ROOT", "/data/silver-quarantine")),
        Path(os.getenv("SILVER_REPORT_ROOT", "/data/reports/silver")), checkpoint,
        int(os.getenv("SILVER_BATCH_MAX_BYTES", str(16 * 1024 * 1024))),
        int(os.getenv("SILVER_MIN_BATCH_BYTES", str(4 * 1024 * 1024))),
    )
    return processor, int(os.getenv("SILVER_INTERVAL_SECONDS", "60")), int(os.getenv("SILVER_METRICS_PORT", "8010"))


def execute(processor: SilverProcessor, dry_run: bool = False, use_checkpoint: bool = True,
            start_date: str | None = None, end_date: str | None = None) -> None:
    started = time.monotonic()
    try:
        results = processor.run_once(dry_run=dry_run, use_checkpoint=use_checkpoint,
                                     start_date=start_date, end_date=end_date)
        for result in results:
            PROCESSED.inc(result.input_rows); WRITTEN.inc(result.written_rows); REJECTED.inc(result.rejected_rows)
            DUPLICATES.inc(result.duplicates); FILES_WRITTEN.inc(result.files_written); CHECKPOINT_OFFSET.set(result.offset)
            if result.max_event_time:
                LAG.set(max(0, time.time() - result.max_event_time.timestamp()))
        BATCHES.inc(); LAST_SUCCESS.set(time.time()); runtime["last_success"] = time.time()
        logger.info("batch files=%s input=%s written=%s rejected=%s duplicates=%s", len(results), sum(x.input_rows for x in results), sum(x.written_rows for x in results), sum(x.rejected_rows for x in results), sum(x.duplicates for x in results))
    except Exception:
        WRITE_ERRORS.inc(); logger.exception("Silver batch failed"); raise
    finally:
        DURATION.observe(time.monotonic() - started)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--backfill-start")
    parser.add_argument("--backfill-end")
    parser.add_argument("--backfill-commit-checkpoint", action="store_true")
    args = parser.parse_args()
    processor, interval, port = settings()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: stop_event.set())
    while not stop_event.is_set():
        try:
            backfill = bool(args.backfill_start or args.backfill_end)
            execute(processor, args.dry_run or args.validate_only,
                    use_checkpoint=(not backfill or args.backfill_commit_checkpoint),
                    start_date=args.backfill_start, end_date=args.backfill_end)
        except Exception as exc:
            runtime["fatal"] = repr(exc)
        if args.once: break
        stop_event.wait(interval)
    server.shutdown()


if __name__ == "__main__":
    main()
