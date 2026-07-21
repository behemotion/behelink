# behelink — deploy runbook

> Target host: **TBD — public server to be provided.** behelink is the
> harness's one deliberately public-facing service; it deploys on
> BEHEMOTION-controlled infrastructure with a real public IP + domain, *not*
> dev-4 (which stays LAN-only). Pattern mirrors behetask's `deploy.md`
> (rootless Podman + systemd-user + Caddy); fill in host specifics once the
> server exists.

## Shape

```
internet ──TLS──► Caddy vhost (public domain, real cert via ACME)
                    └─ reverse_proxy 127.0.0.1:47150 ──► uvicorn/behelink
                                                           └─ SQLite file (volume)
```

- behelink's own listener binds `127.0.0.1:47150` — the "public" property
  lives entirely in Caddy's vhost.
- uvicorn runs with `proxy_headers=True, forwarded_allow_ips="127.0.0.1"`
  (already the `behelink` entrypoint's defaults), so the observed client IP is
  the real public address from `X-Forwarded-For`, trusted only from local
  Caddy.

## Container

```sh
podman build -t behelink .          # Containerfile: TBD with host setup
# or run straight from a checkout:
BEHELINK_DATABASE_PATH=/var/lib/behelink/behelink.db uv run behelink
```

Persist `BEHELINK_DATABASE_PATH` on a mounted volume — it is the only state
the service has (WAL mode, so `.db`, `.db-wal`, `.db-shm` live together).

## Caddy vhost (sketch)

```caddyfile
link.example.org {          # real domain TBD
    reverse_proxy 127.0.0.1:47150
}
```

## Environment

See README's Configuration table. Production notes:

- `BEHELINK_REGISTRATION_RATE_PER_HOUR` (default 10/IP) is the pre-launch
  id-squatting guard required by the design spec; tune before announcing.
- No secrets are needed by behelink itself — all credentials are minted per
  link and stored hashed.

## Ops

- Health: `curl https://<domain>/healthz` → `{"status":"ok"}`.
- Admin access is SQLite-file-direct in v1 (`sqlite3 behelink.db 'SELECT id,
  ip, port, last_seen FROM links'`); a dedicated ops CLI is an open follow-up
  in the design spec.
- Single instance by design (in-memory rate limiter, SQLite); HA is out of
  scope for v1.
