# Evidence Fusion Shadow v1

## Purpose

The adapter combines closed-bar Wyckoff/VSA and Liquidity Structure research evidence without granting execution authority. It is the quality-gated bridge toward the future GPT Market Narrator.

## Safety boundary

- Mode is always `SHADOW_ONLY`.
- `liveTrading`, `executionActionable`, and `executionGatePassed` are hard-coded false.
- The service has no Paper Futures mount, exchange credential, account API, or published host port.
- Wyckoff and Liquidity SQLite journals are mounted read-only; only the fusion journal is writable.
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
- Both input mounts are read-only and output is the only writable persistent mount.
- Deterministic IDs and exactly-once journal rows.
- All three symbols READY with current bar alignment.
- Errors, unsafe flags, duplicates, and gaps remain zero during the validation window.
- Existing Paper Futures, Wyckoff, Liquidity, collectors, Prometheus, and host remain healthy.

## Rollback

Stop and remove only `evidence-fusion-adapter`. The input journals and all existing trading services are untouched. Retain the fusion SQLite journal for audit unless the owner explicitly authorizes deletion.
