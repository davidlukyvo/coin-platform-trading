# ADR 0003: Encrypted exchange credential vault

- Status: Accepted
- Date: 2026-08-09

## Context

The owner needs to configure a BingX credential without placing the API key or secret in Git, Docker Compose, `.env`, browser storage, Grafana, or application logs. Live trading is not approved. Sending credentials over plain LAN HTTP would expose them to network observers.

## Decision

Add a small `system-trading` service with these constraints:

- bind the host port to `127.0.0.1` and access it only through an SSH tunnel;
- authenticate with a dedicated scrypt-hashed administrator password;
- encrypt the BingX API key and secret with Fernet before an atomic file write;
- keep the master key in a root-protected, Git-ignored directory mounted read-only;
- never display the secret again and only show a masked API key;
- provide one signed, read-only Spot balance check;
- contain no order, cancellation, withdrawal, transfer, or live-trading endpoint;
- expose health and Prometheus metrics without credential or balance values;
- run as an unprivileged user with dropped capabilities and a read-only root filesystem.

The master key and encrypted vault must be backed up separately. Possession of both allows credential recovery; loss of the master key makes the vault unreadable.

## Consequences

This design protects secrets at rest and in Git, while the SSH tunnel protects them in transit. It does not protect credentials if the running container, Docker daemon, host root account, or both master key and vault backup are compromised. A future live execution service must remain separate and pass an independent risk engine review.
