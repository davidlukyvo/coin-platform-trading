# Repository Blueprint

The current repository remains a deployable monorepo. New components should
follow this target layout incrementally; do not perform a disruptive bulk move.

```text
coin-platform-trading/
├── contracts/                    versioned JSON/Parquet schemas
│   ├── market_snapshot_v1.json
│   └── market_narrative_v1.json
├── services/
│   ├── collector/
│   ├── silver-processor/
│   ├── silver-quality-exporter/
│   ├── feature-builder/          planned
│   ├── wyckoff-shadow-adapter/
│   ├── market-scanner/           planned
│   ├── market-narrator/          planned
│   ├── replay-backtest/          planned
│   ├── paper-trading/
│   ├── paper-futures/
│   └── system-trading/
├── libs/                         pure, reusable deterministic logic
│   ├── market_contracts/
│   ├── event_clock/
│   ├── wyckoff_vsa/
│   └── risk_models/
├── monitoring/
│   ├── prometheus/
│   └── grafana/
├── tests/
│   ├── contracts/
│   ├── replay/
│   ├── security_boundaries/
│   └── integration/
├── scripts/                      audits, migration, backup, restore
├── docs/
│   ├── decisions/                ADRs
│   ├── CTO_MASTER_PLAN.md
│   ├── PRODUCT_BACKLOG.md
│   └── NUC_UBUNTU_DEPLOYMENT.md
├── docker-compose.yml
└── docker-compose.bamboo.yml
```

Rules:

- Business logic belongs in pure modules; HTTP/metrics layers remain thin.
- Each service has runtime and test Docker targets, health endpoint and bounded
  resource/security settings.
- Schema changes are additive first, versioned and migration-tested.
- Dashboards are provisioned files with stable UIDs.
- Every service documents inputs, outputs, failure modes, replay semantics,
  metrics, alerts, security boundary and rollback.
- Environment overlays may change binds/resources, never business rules silently.
