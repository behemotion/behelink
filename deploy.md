# behelink — deploy runbook

Deployed **2026-07-21** to the dedicated public server (Hostkey panel name
`hostkey82312`):

| | |
|---|---|
| Host | `46.17.103.230` (hostname `behelink`), Ubuntu 26.04 LTS |
| Access | `ssh build@46.17.103.230` — key + pinned host key in the operator's access package (`~/Srv/hostkey/behelink-access/`, **not** in this repo); `build` has passwordless sudo |
| App checkout | `/home/build/apps/behelink/` (rsync'd working tree; `uv sync --extra dev`) |
| State | `/home/build/apps/behelink-data/behelink.db` (SQLite, WAL) |
| Service | systemd **user** unit `behelink.service` (linger enabled — survives logout, starts at boot) |
| Front | Caddy (Ubuntu package) on `:80` → `reverse_proxy 127.0.0.1:47150` |

## Shape

```
internet ──:80 (plain HTTP for now)──► Caddy
                    └─ reverse_proxy 127.0.0.1:47150 ──► uvicorn/behelink
                                                           └─ SQLite (~/apps/behelink-data/)
```

- behelink's listener binds `127.0.0.1:47150`; verified unreachable from
  outside. The public property lives entirely in Caddy.
- uvicorn runs with `proxy_headers=True, forwarded_allow_ips="127.0.0.1"`
  (the `behelink` entrypoint's defaults): the observed client IP is taken
  from Caddy's `X-Forwarded-For`, trusted only from localhost. Spoofed
  `X-Forwarded-For` from external clients is ignored (verified live —
  uvicorn walks the header right-to-left past trusted proxies only).

> **TLS gap (open):** the server has an IP but no domain yet, so ACME TLS is
> not possible and Caddy serves plain HTTP on :80. Resolve/owner tokens
> therefore transit unencrypted — acceptable for the current test phase only.
> When a domain lands: point DNS at 46.17.103.230, replace `:80` in the
> Caddyfile with the domain, `sudo systemctl reload caddy` — Caddy handles the
> cert automatically. Then rotate all previously issued tokens.

## Server-side layout

`~/.config/systemd/user/behelink.service`:

```ini
[Unit]
Description=behelink NAT rendezvous service
After=network.target

[Service]
WorkingDirectory=/home/build/apps/behelink
Environment=BEHELINK_DATABASE_PATH=/home/build/apps/behelink-data/behelink.db
ExecStart=/home/build/apps/behelink/.venv/bin/behelink
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

`/etc/caddy/Caddyfile`:

```caddyfile
:80 {
	reverse_proxy 127.0.0.1:47150
}
```

## Redeploy

From a behelink checkout (key flags abbreviated — see access package):

```sh
rsync -az --delete --exclude .venv --exclude __pycache__ \
      --exclude .pytest_cache --exclude '*.db*' \
      ./ build@46.17.103.230:apps/behelink/
ssh build@46.17.103.230 \
    'cd ~/apps/behelink && ~/.local/bin/uv sync --extra dev \
     && ~/.local/bin/uv run pytest && systemctl --user restart behelink'
```

The database lives outside the checkout, so `--delete` is safe.

## Ops

- Health: `curl http://46.17.103.230/healthz` → `{"status":"ok"}`.
- Logs: `journalctl --user -u behelink` (app), `sudo journalctl -u caddy`.
- Admin is SQLite-file-direct in v1:
  `sqlite3 ~/apps/behelink-data/behelink.db 'SELECT id, ip, port, last_seen FROM links'`.
- `BEHELINK_REGISTRATION_RATE_PER_HOUR` (default 10/IP) is active; tune in
  the unit's `Environment=` lines before wide announcement.
- Single instance by design (in-memory rate limiter, SQLite); HA out of
  scope for v1.

## Live verification (2026-07-21)

- Full pytest suite (34 tests) green on the server itself.
- T0-style end-to-end from an outside NAT'd network, 14/14 passed:
  healthz → register (201) → duplicate (409) → heartbeat (observed IP ==
  the client's real egress IP) → resolve (correct `{ip, port}`) → 401/404
  auth behavior → rotate (old token dead, new works) → delete (204, id
  freed) → port 47150 closed externally → `X-Forwarded-For` spoof ignored.
