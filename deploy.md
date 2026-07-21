# behelink — deploy runbook

Deployed **2026-07-21** to the dedicated public server (Hostkey panel name
`hostkey82312`):

| | |
|---|---|
| URL | **https://link.behemotion.com** (HTTP 308-redirects to HTTPS) |
| Host | `46.17.103.230` (hostname `behelink`), Ubuntu 26.04 LTS |
| Access | `ssh build@46.17.103.230` — key + pinned host key in the operator's access package (`~/Srv/hostkey/behelink-access/`, **not** in this repo); `build` has passwordless sudo |
| App checkout | `/home/build/apps/behelink/` (rsync'd working tree; `uv sync --extra dev`) |
| State | `/home/build/apps/behelink-data/behelink.db` (SQLite, WAL) |
| Service | systemd **user** unit `behelink.service` (linger enabled — survives logout, starts at boot) |
| Front | Caddy (Ubuntu package), vhost `link.behemotion.com`, auto-TLS (Let's Encrypt, obtained 2026-07-21, auto-renews) → `reverse_proxy 127.0.0.1:47150` |

## Shape

```
internet ──TLS──► Caddy vhost link.behemotion.com
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

> TLS gap resolved 2026-07-21: `link.behemotion.com` DNS landed, Caddy
> obtained the Let's Encrypt cert automatically (TLS-ALPN challenge). The
> plain-HTTP window issued only throwaway test tokens, all deleted before the
> switch (links table verified empty) — nothing needed rotation.

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
link.behemotion.com {
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

- Health: `curl https://link.behemotion.com/healthz` → `{"status":"ok"}`.
- Logs: `journalctl --user -u behelink` (app), `sudo journalctl -u caddy`.
- Admin is SQLite-file-direct in v1:
  `sqlite3 ~/apps/behelink-data/behelink.db 'SELECT id, ip, port, last_seen FROM links'`.
- `BEHELINK_REGISTRATION_RATE_PER_HOUR` (default 10/IP) is active; tune in
  the unit's `Environment=` lines before wide announcement.
- Single instance by design (in-memory rate limiter, SQLite); HA out of
  scope for v1.

## Live verification (2026-07-21)

- Full pytest suite (34 tests) green on the server itself.
- T0-style end-to-end from an outside NAT'd network, 14/14 passed — run
  twice, over plain HTTP pre-domain and again over
  `https://link.behemotion.com`: healthz → register (201) → duplicate (409)
  → heartbeat (observed IP == the client's real egress IP) → resolve
  (correct `{ip, port}`) → 401/404 auth behavior → rotate (old token dead,
  new works) → delete (204, id freed) → port 47150 closed externally →
  `X-Forwarded-For` spoof ignored.
- HTTP→HTTPS 308 redirect and Let's Encrypt cert chain verified from
  outside.
