# ADR 0002: WebSocket stream allowlist validation

## Status

Accepted for Phase 1.

## Context

The collector subscribes to a fixed set of Binance combined streams. A malformed or unexpected stream name must not reach path construction, metric labels, or the writer queue because it could terminate collection or create unintended filesystem paths.

## Decision

- Build an immutable allowlist from the configured subscription streams.
- Validate every received envelope against that allowlist before enqueueing it.
- Reject malformed or unexpected stream names as protocol errors.
- Increment an error metric and continue receiving without terminating the collector.
- Do not create partitions, metrics labels, or quarantine entries for untrusted stream names.

## Consequences

The collector fails closed for stream identity while remaining available for valid configured streams. Adding a new market stream requires an explicit configuration and parser update.
