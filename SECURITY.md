# Security Policy

## Supported Version

Only the current `main` branch is supported. This project is experimental and
does not provide a guaranteed security response time.

## Reporting

Do not open a public issue containing passwords, API keys, packet captures,
radio IDs tied to people, precise station coordinates, or private logs. Contact
the repository owner privately through GitHub instead.

## Runtime Secrets

The following files belong in `/home/quantar/quantar-runtime`, never in Git:

- `quantarbridge.yml` with the BrandMeister device password
- `bm_api.key`
- `dashboard-auth.json`
- `tetrapack-brew-bridge.json` when BREW credentials are configured
- packet captures, SMS queues, logs, identity caches, and backups

The installer creates the runtime directory with restrictive permissions.
Dashboard passwords are stored as salted PBKDF2-SHA256 records. BrandMeister
and BREW passwords are not returned by the dashboard API.

## Network Exposure

The dashboard is intended for a trusted management network. Bind it to a
management address or place it behind an authenticated TLS reverse proxy before
exposing it beyond the LAN. Keep FNE, UDP audio, RPC, and packet-data ports on
loopback unless a deliberate multi-host design requires otherwise.

## Before Publishing Changes

Run:

```bash
python3 scripts/audit_public_tree.py .
gitleaks dir . --no-banner --redact
```

If a credential ever enters Git history, revoke it first. Removing the visible
line in a later commit is not sufficient.
