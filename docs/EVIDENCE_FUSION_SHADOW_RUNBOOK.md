# Evidence Fusion Shadow v1

## Purpose

The adapter combines closed-bar Wyckoff/VSA and Liquidity Structure research evidence without granting execution authority. It is the quality-gated bridge toward the future GPT Market Narrator.

## Safety boundary

- Mode is always `SHADOW_ONLY`.
- `liveTrading`, `executionActionable`, and `executionGatePassed` are hard-coded false.
- The service has no Paper Futures mount, exchange credential, account API, or published host port.
- Wyckoff and Liquidity projections are consumed over the private Compose network; only the fusion journal is mounted.
- `LIQUIDITY_SOAK_STATUS` defaults to `FAIL`. Until a clean validation window is explicitly promoted to `PASS`, every otherwise-valid confluence is recorded as `REJECT_SHADOW/liquidity_soak_not_passed`.

## Fusion gates

1. Reject unsafe source flags.
2. Reject stale evidence older than `FUSION_MAX_MARKET_AGE_SECONDS`.
3. Reject if either source's recent closed 5-minute bars are not continuous.
4. Wait until both sources refer to the same closed bar.
5. Reject while the Liquidity soak status is not PASS.
6. After a future explicit PASS, allow shadow evidence only when both sources independently produce `ALLOW_SHADOW` in the same direction.

An `ALLOW_SHADOW` result is research evidence only and is never an order.

## Acceptance checks

- Container healthy, restart 0, OOM false, UID 10011, read-only root filesystem.
- No host port or credential mount.
- No source database mounts; output is the only writable persistent mount.
- Deterministic IDs and exactly-once journal rows.
- All three symbols READY with current bar alignment.
- Errors, unsafe flags, duplicates, and gaps remain zero during the validation window.

## Liquidity continuity re-soak

The Liquidity adapter persists a `continuity_baseline` in its existing journal on the
first cycle after this capability is deployed. It does not reset observations and it
does not backfill historical gaps. Only internal gaps whose first missing interval is
after that baseline can fail the new soak.

Continuity and freshness are separate gates:

- `UPSTREAM_MINUTE_GAP` records a missing closed one-minute candle between two
  observed candles.
- `COMPLETE_5M_GAP` records a missing complete five-minute research bar.
- `freshnessSeconds` and `freshnessStatus` describe trailing/batch delay and never
  create a continuity event by themselves.

Continuity events use deterministic IDs and remain in the journal across restarts.
Promote the Liquidity soak only after a complete validation window has zero new
events for every symbol, no unresolved source alignment, no unsafe flags, and no
stale source. Promotion remains a separate owner decision; it must not automatically
change `LIQUIDITY_SOAK_STATUS` or connect Evidence Fusion to Paper Futures.
- Existing Paper Futures, Wyckoff, Liquidity, collectors, Prometheus, and host remain healthy.

## Rollback

Stop and remove only `evidence-fusion-adapter`. The input journals and all existing trading services are untouched. Retain the fusion SQLite journal for audit unless the owner explicitly authorizes deletion.
