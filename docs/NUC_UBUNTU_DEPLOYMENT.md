# Ubuntu / NUC Deployment Guide

Target: Ubuntu Server 24.04 LTS, Docker Engine with Compose v2, UTC host clock.

## Recommended baseline

- 4+ CPU cores, 16 GB RAM preferred, SSD with at least 250 GB usable.
- Wired LAN, DHCP reservation/static address, NTP enabled.
- Separate non-root deployment user with SSH key authentication.
- Full-disk encryption where unattended reboot requirements permit it.
- UPS recommended for the NUC and network equipment.

## Host preparation

```bash
sudo apt update
sudo apt install -y ca-certificates curl git jq chrony smartmontools
sudo systemctl enable --now chrony
timedatectl status
```

Install Docker Engine and the Compose plugin from Docker's official Ubuntu
repository. Add only the deployment user to the `docker` group; treat this as
root-equivalent access. Re-login before testing `docker version` and
`docker compose version`.

## Repository and data layout

```text
/opt/coin-platform-trading/       immutable Git checkout
/data/coin-platform/              runtime datasets and state
/srv/coin-platform-backup/        optional local encrypted staging
/etc/coin-platform/               host-only deployment metadata
```

```bash
sudo mkdir -p /opt/coin-platform-trading /data/coin-platform
sudo chown -R "$USER":"$USER" /opt/coin-platform-trading /data/coin-platform
git clone https://github.com/davidlukyvo/coin-platform-trading.git \
  /opt/coin-platform-trading
cd /opt/coin-platform-trading
git fetch --all --prune
git checkout --detach <approved-exact-sha>
cp .env.example .env
chmod 600 .env
```

Set unique PostgreSQL/Grafana credentials and environment-specific bind
addresses. Never copy Bamboo's `.env`, vault master key or exchange secret via
Git. Provision secrets out-of-band and keep permissions at `0600`.

## Preflight and deployment

```bash
cd /opt/coin-platform-trading
docker compose config -q
docker compose build
docker compose run --rm storage-init
docker compose up -d
docker compose ps -a
```

Never use `docker compose down -v`. Initial deployment remains public-data,
shadow and paper-only. Do not provision a trading-enabled exchange key.

## Network policy

- Expose Grafana/System Trading only to the trusted LAN or VPN.
- Keep service metrics internal to Docker unless a host check requires loopback.
- Deny unsolicited WAN ingress at the router and host firewall.
- Narrator requires outbound HTTPS to OpenAI only after Phase 4 approval.
- Research/narrator containers never join a future execution-only network.

## Validation checklist

1. All containers healthy with restart 0 and OOM false.
2. Prometheus targets UP and no firing rules.
3. Binance and BingX connected; queue/write errors zero.
4. Bronze and Silver increase; quarantine remains explained/empty.
5. Wyckoff/Hybrid `SHADOW_ONLY`; Paper `PAPER_ONLY`.
6. `liveTrading`, `executionActionable`, `executionGatePassed` all false.
7. No credential mount or published port on research services.
8. Root filesystem, inode, RAM, swap and load within budget.
9. Reboot test restores services without resetting journals/checkpoints.
10. Backup restore is proven on a disposable path before relying on it.

## Backup policy

- Back up Git-independent state: PostgreSQL, journals, checkpoints, research
  manifests, compact Silver history, Grafana state and encrypted vault material.
- Backups are encrypted, versioned and tested; master keys are backed up
  separately from ciphertext.
- Raw Bronze may use shorter retention, but only after replay requirements are
  met and the change is approved.

## Update and rollback

```bash
git fetch origin
git checkout --detach <new-approved-sha>
docker compose config -q
docker compose build <changed-service>
docker compose up -d --no-deps <changed-service>
```

Record the previous SHA and image digest before deployment. Rollback checks out
the previous exact SHA, rebuilds only the affected service and preserves all
volumes/journals. Do not reset checkpoints to force recovery.

## Bamboo to NUC promotion

Promote only after Bamboo soak passes. Compare exact SHA, Compose rendering,
service security settings, data schema versions, dashboard UIDs, target/rule
counts and resource budgets. NUC starts with its own fresh public market data;
historical datasets are transferred only through a checksummed, explicit import.
