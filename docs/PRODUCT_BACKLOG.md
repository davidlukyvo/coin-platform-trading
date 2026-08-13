# Product and Engineering Backlog

Priority is evidence quality before feature breadth. Status values are
`NEXT`, `PLANNED`, `BLOCKED`, and `DONE`.

## Phase 0 — Safety and platform baseline

- DONE — Binance/BingX collectors, Bronze/Silver, quarantine and quality metrics.
- DONE — Hybrid and Wyckoff shadow boundaries.
- DONE — Paper Spot/Futures, isolated risk engine and read-only administration.
- DONE — Prometheus/Grafana and protected Zabbix watcher.
- NEXT — Add automated architecture-boundary tests proving narrator/research
  containers have no credential mounts, host ports or execution functions.

Exit: all safety boundaries verified in CI and deployment audit.

## Phase 1 — Continuous research truth

### P0: Closed-bar research scheduler

Problem: current Wyckoff observations can be 15–45 minutes apart, so the journal
is not a continuous 5m backtest series.

Deliverables:

- Track per-symbol last processed closed bar.
- Process every unseen complete 5m candle in order, not only the latest candle.
- Build aligned 15m/1h context without future leakage.
- Persist one deterministic observation per symbol/closed 5m bar.
- Export missing research bars, processing lag and replay counters.
- Preserve the current journal; migration is additive and idempotent.

Acceptance:

- 24-hour run has expected 288 observations per symbol, excluding documented
  upstream gaps.
- duplicate IDs zero, missed research bars zero, replay is byte-equivalent.
- Silver remains read-only and live flags remain false.

### P0: Data manifest and replay contract

- Snapshot Silver file hashes, min/max event time, gaps and rule/config SHA.
- Add an event-time clock and prohibit wall-clock access in backtest logic.
- Produce reproducibility report for each run.

### P1: Funding/OI public-data adapter

- Binance and BingX public funding, open interest and liquidation data.
- Exchange-specific normalized schema plus missing/unavailable status.
- Freshness, continuity and cross-source discrepancy monitoring.

Exit: a deterministic market snapshot can be rebuilt at any historical `asOf`.

## Phase 2 — Market Scanner v1

- Start with BTCUSDT and ETHUSDT; then a liquidity-governed allowlist of 10–20.
- Rank changes in phase, confirmed VSA events, volume anomaly, freshness and RR.
- Separate `interesting for research` from `eligible for paper`.
- Store scan reason, score components and invalidation.
- Rate-limit symbol churn and GPT narration requests.

Acceptance: the same snapshots always produce the same ranking; stale or
incomplete symbols cannot rank as actionable.

## Phase 3 — Replay and backtest

- Replay Wyckoff/VSA and EMA using closed-bar event time.
- Fee, slippage, funding, isolated margin and liquidation models.
- Train/calibration, validation, OOS and monthly walk-forward runs.
- Baselines: buy/hold, EMA, random entry with identical risk, Wyckoff without VSA.
- Reports: expectancy, profit factor, drawdown, MAE/MFE, stability by phase/event,
  parameter sensitivity and sample sufficiency.

Acceptance: at least 100–200 OOS closed trades per candidate (300 preferred),
positive expectancy after costs, PF >1.2 as a research threshold, no material
collapse under cost stress, and no dependence on a few outliers.

## Phase 4 — GPT Market Narrator

- Define `market_snapshot_v1` and `market_narrative_v1` JSON Schemas.
- First build a deterministic template narrator and golden evaluation set.
- Add prompt-injection-safe renderer and output validator.
- Store snapshot ID, prompt hash, model, output, evidence references and verdict.
- Invoke only on closed 15m bars or meaningful thesis changes.
- Fallback to deterministic narrative when API fails.
- Dashboard card and evidence drill-down; no execution tool or exchange key.

Acceptance:

- 100% numeric claims match the source snapshot.
- 100% narratives contain invalidation and missing-evidence disclosure.
- zero unsupported events, phases, positions or probabilities in the golden set.
- narrator outage has no effect on ingestion, research or paper trading.

## Phase 5 — Paper validation

- Shadow evidence 7–14 days.
- Paper x1 for logic validation, then isolated Futures x10 comparison.
- EMA and Wyckoff/VSA retain separate virtual accounts.
- Daily and weekly reports compare backtest versus paper drift.
- Minimum 30-day Futures soak and risk rejection audit.

## Phase 6 — NUC productionization

- Ubuntu host hardening, encrypted backup and restore rehearsal.
- Exact-SHA release process and Bamboo-to-NUC parity report.
- Resource budgets, retention jobs, UPS/reboot recovery and health notifications.
- Morning Briefing, thesis-change update, post-trade review and weekly notes.

## Explicitly blocked

- BLOCKED — Live execution.
- BLOCKED — GPT-generated signals, position sizing, TP/SL or risk overrides.
- BLOCKED — Broad coin expansion before Phase 1 continuity and storage budgets.
- BLOCKED — Treating rule confidence as calibrated probability.

## Immediate sprint

1. Implement the closed-bar research scheduler.
2. Add replay/continuity unit tests and Prometheus metrics.
3. Soak for 24 hours on Bamboo.
4. Produce the first complete per-candle Wyckoff/VSA evidence manifest.

This sprint has higher value than connecting GPT now: it creates the trustworthy
history that both backtesting and the future narrator require.
