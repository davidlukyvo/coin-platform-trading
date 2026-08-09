# System Trading credential UI runbook

## Safety state

The service supports BingX credential storage, a read-only connection test, and a read-only portfolio view for Spot balances, Perpetual balances, and open Perpetual positions. It has no code path for placing orders, withdrawing, transferring, or enabling live trading.

## Initialize local secrets

Run on the deployment host from the repository:

```bash
python3 scripts/init-system-trading-secrets.py
```

The command prompts for a dedicated UI password and creates:

- `secrets/system-trading/master.key`: Fernet master key, root-owned and group-readable only by service GID `10004`;
- `secrets/system-trading/admin-password.scrypt`: salted scrypt password hash with the same restricted ownership.

The directory is mode `0750` and both files are mode `0640`. This lets the non-root container UID/GID `10004` read the mounted secrets without making them world-readable.

Neither file is tracked by Git. Back up `master.key` to a protected offline location. Do not copy either value into `.env`, chat, tickets, shell history, or documentation.

## Initialize LAN HTTPS

Generate a private CA and a server certificate containing the Bamboo LAN IP as a Subject Alternative Name:

```bash
sh scripts/init-system-trading-tls.sh 172.26.12.120
```

The CA private key stays under `secrets/system-trading-ca` and is not mounted into the container. Back it up securely. Copy only `secrets/system-trading-ca/ca.crt` to administrator laptops and install it in the trusted root store.

For Bamboo, set these local `.env` values:

```dotenv
SYSTEM_TRADING_BIND_ADDRESS=172.26.12.120
SYSTEM_TRADING_PORT=8443
```

## Deploy and verify

Capture the current state first, then build only the new service:

```bash
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml build system-trading
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml up -d storage-init system-trading
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml ps system-trading
curl --fail --cacert secrets/system-trading-ca/ca.crt https://172.26.12.120:8443/healthz
ss -lntp | grep '172.26.12.120:8443'
```

Expected health response includes `"trading_enabled":false` and `"dry_run":true`. The port must bind to `172.26.12.120`, never `0.0.0.0`.

## Open the UI from the LAN

After trusting the private CA on the laptop, browse to `https://172.26.12.120:8443`. Use the System Trading password, not the Grafana password. Plain HTTP is intentionally unavailable.

## Configure BingX

Before entering a real key, verify in BingX that Withdraw is disabled and IP restriction is configured where supported. Paste the key directly into the UI; never send it through chat. The `Test connection` operation calls only the signed Spot balance endpoint and records only success/failure plus the count of non-zero assets.

`Refresh portfolio` calls these authenticated read-only endpoints:

- `/openApi/spot/v1/account/balance`;
- `/openApi/swap/v3/user/balance`;
- `/openApi/swap/v2/user/positions`.

Account values are rendered only in the current HTTPS response. They are not written to the vault, audit journal, logs, cookies, database, or Prometheus. The audit journal stores only result status and row counts.

## Rollback

Stopping the UI does not affect collectors or stored market data:

```bash
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml stop system-trading
```

Do not delete `master.key` while an encrypted credential vault exists. To revoke access immediately, delete or disable the API key in BingX first, then remove the stored credential through the UI.
