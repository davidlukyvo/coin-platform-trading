# System Trading credential UI runbook

## Safety state

The initial service supports BingX credential storage and a read-only Spot balance test only. It has no code path for placing orders, withdrawing, transferring, or enabling live trading.

## Initialize local secrets

Run on the deployment host from the repository:

```bash
python3 scripts/init-system-trading-secrets.py
chmod 700 secrets secrets/system-trading
```

The command prompts for a dedicated UI password and creates:

- `secrets/system-trading/master.key`: Fernet master key, mode `0600`;
- `secrets/system-trading/admin-password.scrypt`: salted scrypt password hash, mode `0600`.

Neither file is tracked by Git. Back up `master.key` to a protected offline location. Do not copy either value into `.env`, chat, tickets, shell history, or documentation.

## Deploy and verify

Capture the current state first, then build only the new service:

```bash
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml build system-trading
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml up -d storage-init system-trading
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml ps system-trading
curl --fail http://127.0.0.1:8040/healthz
ss -lntp | grep '127.0.0.1:8040'
```

Expected health response includes `"trading_enabled":false` and `"dry_run":true`. The port must not bind to `0.0.0.0` or the Bamboo LAN address.

## Open the UI

From PowerShell on the laptop:

```powershell
ssh -N -L 8040:127.0.0.1:8040 trading-bamboo
```

Keep that terminal open and browse to `http://127.0.0.1:8040`. Traffic stays inside the SSH tunnel. Use the System Trading password, not the Grafana password.

## Configure BingX

Before entering a real key, verify in BingX that Withdraw is disabled and IP restriction is configured where supported. Paste the key directly into the UI; never send it through chat. The `Test connection` operation calls only the signed Spot balance endpoint and records only success/failure plus the count of non-zero assets.

## Rollback

Stopping the UI does not affect collectors or stored market data:

```bash
docker compose -f docker-compose.yml -f docker-compose.bamboo.yml stop system-trading
```

Do not delete `master.key` while an encrypted credential vault exists. To revoke access immediately, delete or disable the API key in BingX first, then remove the stored credential through the UI.
