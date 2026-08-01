# ADR 0001: RAW write failure handling

## Status

Accepted for Phase 1.

## Context

The market-data collector must not silently discard an event when the primary RAW append fails. A transient disk error should be retried, while an event that still cannot be written must remain visible for operator recovery.

## Decision

1. RAW writes use a dedicated single writer outside the WebSocket receive loop.
2. Each failed RAW append is retried with bounded exponential delay.
3. After retries are exhausted, the complete source payload and error context are appended to a separate quarantine NDJSON path.
4. If both RAW and quarantine storage fail, the collector marks a fatal error, reports unhealthy, and initiates shutdown so container restart policy can recover after storage is repaired.
5. Unexpected WebSocket stream names are rejected before entering the write queue.
6. No synthetic event or silent repair is permitted.

## Consequences

- A transient filesystem fault does not immediately lose the event.
- Quarantined records require a later replay/reconciliation workflow.
- RAW and quarantine directories currently share the same host data root; a complete disk failure can affect both. A later milestone may place the emergency spool on separate storage.
- Runtime failure injection remains mandatory before merging the bootstrap PR.
