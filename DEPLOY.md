# DEPLOY — behelink

> Runbook: what an operator runs today. Ports per
> [CONVENTIONS §7](../docs/CONVENTIONS.md#7-service-packaging--deploy); topology, host
> address and SSH access in `CONNECTIONS.md` (local, not published). Dated verification logs
> live in `docs/history/`, not here.

behelink is one of the two exceptions to the harness's rootless-Podman rule: it runs as a
systemd **user** unit directly on a dedicated public server (no container runtime in use there).

## Shape

| | |
|---|---|
| Host | dedicated public server, hostname `behelink`, Ubuntu LTS (reached as `link.behemotion.com`) |
| Runtime | systemd-user unit `behelink.service` (linger enabled — survives logout, starts at boot) |
| App (HTTP) | `127.0.0.1:47150` — never reachable from outside |
| UDP reflector | `0.0.0.0:47151/udp` — public by design, same process (Caddy cannot front raw UDP) |
| DB | SQLite (WAL) at `~/apps/behelink-data/behelink.db`, outside the checkout |
| Vhost | `https://link.behemotion.com` (Caddy, auto-TLS via Let's Encrypt; HTTP 308-redirects to HTTPS) |
| Firewall | ufw default-deny inbound; `22/tcp` (rate-limited), `80/tcp`, `443/tcp`, `443/udp` (HTTP/3), `47151/udp` (reflector) |

```
internet ──ufw (22 limit / 80 / 443 tcp+udp)──► Caddy vhost link.behemotion.com
                    └─ reverse_proxy 127.0.0.1:47150 ──► uvicorn/behelink
                                                           └─ SQLite (~/apps/behelink-data/)
internet ──ufw (47151/udp)──────────────────────► behelink UDP reflector (0.0.0.0:47151, same process)
```

- uvicorn runs with `proxy_headers=True, forwarded_allow_ips="127.0.0.1"` (the `behelink`
  entrypoint's defaults, `src/behelink/__main__.py`): the observed client IP is taken from
  Caddy's `X-Forwarded-For`, trusted only from localhost. Spoofed `X-Forwarded-For` from external
  clients is ignored (uvicorn walks the header right-to-left past trusted proxies only).
- `BEHELINK_REFLECTOR_HOST=0.0.0.0` is the default — unlike the HTTP listener, the reflector binds
  a public interface directly, by design.
- Single instance by design (in-memory rate limiters and pending-connect queue, SQLite); HA is out
  of scope for v1.

## Prerequisites

- SSH access to the host as the deploy user (key + pinned host key from the operator's access
  package — never in this repo; see `CONNECTIONS.md`). The deploy user has passwordless sudo.
- `uv` installed for the deploy user (`~/.local/bin/uv`).
- Caddy from the official `dl.cloudsmith.io/public/caddy/stable` apt repo.

### Server-side layout

`~/.config/systemd/user/behelink.service` (`%h` is the deploy user's home directory; the live
unit spells it out):

```ini
[Unit]
Description=behelink NAT rendezvous service
After=network.target

[Service]
WorkingDirectory=%h/apps/behelink
Environment=BEHELINK_DATABASE_PATH=%h/apps/behelink-data/behelink.db
ExecStart=%h/apps/behelink/.venv/bin/behelink
Restart=always
RestartSec=2
NoNewPrivileges=yes
PrivateTmp=yes
MemoryHigh=768M
MemoryMax=1G

[Install]
WantedBy=default.target
```

`/etc/caddy/Caddyfile` (the packaged Caddy unit runs with `ProtectSystem=full`, so a drop-in
`/etc/systemd/system/caddy.service.d/logdir.conf` adds `LogsDirectory=caddy` +
`ReadWritePaths=/var/log/caddy` for the access log):

```caddyfile
link.behemotion.com {
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		-Server
		-Via
		defer
	}

	# request_body — two mutually exclusive handlers (see "Caddy body cap" below):
	#   /v1/tg/*            → max_size 2MB
	#   not path /v1/tg/*   → max_size 16KB

	log {
		output file /var/log/caddy/link.behemotion.com-access.log {
			roll_size 50MiB
			roll_keep 10
			roll_keep_for 720h
		}
	}

	reverse_proxy 127.0.0.1:47150
}
```

**Caddy body cap.** A single global `request_body max_size 16KB` is too small for the Telegram
ingress: a legitimate 4096-character Telegram message made of 4-byte glyphs (emoji, CJK) is
~49 KB, and Telegram retries non-2xx, so a Caddy `413` would wedge the webhook with a climbing
`pending_update_count` while the bot looked healthy. So `/v1/tg/*` is capped at 2MB and the 16KB
cap applies to everything else via a `not path /v1/tg/*` matcher, leaving behelink's own
`BEHELINK_TELEGRAM_MAX_PAYLOAD_BYTES` (1048576) as the binding limit with a proper RFC 9457 body.

**The exclusion matcher is load-bearing.** Every matching `request_body` handler runs in order and
the last one wins, so a bare catch-all silently overrides the larger route-specific cap. Verify
with `sudo caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile` and confirm the two
handlers' matchers are mutually exclusive — a `curl` probe is the only way to catch this, since
the pytest suite talks to the ASGI app directly and never traverses Caddy.

### Host hardening (in place)

- **sshd** — `/etc/ssh/sshd_config.d/00-hardening.conf`: `PermitRootLogin no`,
  `PasswordAuthentication no`, `AllowUsers` the deploy user only, `MaxAuthTries 3`,
  `LoginGraceTime 30`, `X11Forwarding no`, keepalive 300s×2. (`00-` sorts before cloud-init's
  `50-` file; sshd first-match wins.) Recovery if locked out: the hosting provider's panel VNC
  console — root's local password is intentionally still set for that path.
- **ufw** — default deny incoming / allow outgoing; `limit 22/tcp`, `allow 80/tcp`,
  `allow 443/tcp`, `allow 443/udp` (HTTP/3), `allow 47151/udp` (UDP reflector).
- **fail2ban** — `/etc/fail2ban/jail.local`: systemd backend, sshd jail in aggressive mode, 1h
  bans escalating to 48h (`bantime.increment`).
- **unattended-upgrades** — `/etc/apt/apt.conf.d/52unattended-upgrades-local` adds the `-updates`
  pocket, unused-dependency cleanup, and `Automatic-Reboot 04:30`.
- **Backups** — user timer `behelink-backup.timer` (daily, persistent, randomized ≤1h) runs
  `~/apps/behelink-data/backup.sh`: `sqlite3 .backup` to `~/apps/behelink-data/backups/`, pruned
  after 14 days.

## Deploy

From a behelink checkout (`<host>` = the SSH target in `CONNECTIONS.md`; key flags from the
access package):

```sh
rsync -az --delete --exclude .venv --exclude __pycache__ \
      --exclude .pytest_cache --exclude '*.db*' \
      ./ <host>:apps/behelink/
ssh <host> \
    'cd ~/apps/behelink && ~/.local/bin/uv sync --extra dev \
     && ~/.local/bin/uv run pytest && systemctl --user restart behelink'
```

The database lives outside the checkout, so `--delete` is safe.

Schema changes: `db.connect()` applies its `ALTER TABLE links ADD COLUMN` / `CREATE TABLE`
migrations idempotently on every connect, so the restart applies them. Before deploying a
release that changes the schema, take a backup first: `bash ~/apps/behelink-data/backup.sh`.

A Caddyfile change: back up `/etc/caddy/Caddyfile`, edit, run the `caddy adapt` check above, then
`sudo systemctl reload caddy`.

## Verify

```sh
curl -fsS https://link.behemotion.com/healthz     # → {"status":"ok"}
```

- From an outside network: register (`201`) → heartbeat (observed IP = the client's egress IP)
  → resolve → delete (`204`); port 47150 must be closed externally.
- UDP reflector: a probe `{"token":"<a live token>"}` to `link.behemotion.com:47151` gets a JSON
  `{"ip", "port"}` reply; an invalid token gets no reply.
- After any Caddy change: a 32 KB body to `/v1/links` → `413` from Caddy; a ~49 KB body to
  `/v1/tg/...` reaches behelink.

## Rollback

- Code: check out the previous good commit locally and rerun the Deploy step (rsync + `uv sync` +
  pytest + restart).
- Data: if a schema migration ran, stop the unit, copy the pre-deploy
  `~/apps/behelink-data/backups/behelink-*.db` snapshot over `behelink.db`, then start the unit.
- Caddy: restore the backed-up Caddyfile and `sudo systemctl reload caddy`.

## Configuration & secrets

behelink holds **no static secrets**: per-link tokens are generated at registration and stored only
as SHA-256 hashes in SQLite; TLS certificates are obtained and renewed by Caddy.

| Env var | Purpose | Where the value lives |
|---|---|---|
| `BEHELINK_DATABASE_PATH` | SQLite file path | unit `Environment=` line |
| `BEHELINK_REGISTRATION_RATE_PER_HOUR` | per-IP registration limit (default 10) — tune before wide announcement | default; override via unit `Environment=` |
| `BEHELINK_HOST` / `BEHELINK_PORT` | HTTP listener (`127.0.0.1:47150`) | defaults |
| `BEHELINK_REFLECTOR_HOST` / `BEHELINK_REFLECTOR_PORT` | UDP reflector (`0.0.0.0:47151`) | defaults |
| `BEHELINK_PUBLIC_BASE_URL` | base of the Telegram `webhook_url` (`https://link.behemotion.com`) | default |
| `BEHELINK_TELEGRAM_*`, `BEHELINK_*_RATE_*`, `BEHELINK_PENDING_CONNECT_*` | relay switch, caps, rate limits, TTLs | defaults — full list in `README.md` § Configuration |

## Ops

- Logs: `journalctl --user -u behelink` (app), `sudo journalctl -u caddy` (server),
  `sudo tail /var/log/caddy/link.behemotion.com-access.log` (JSON access log, 50 MiB × 10 rolls,
  30-day retention).
- Restart: `systemctl --user restart behelink`.
- Bans: `sudo fail2ban-client status sshd`; firewall: `sudo ufw status verbose`;
  listeners: `ss -ulnp` / `ss -tlnp`.
- Backups: `systemctl --user list-timers behelink-backup.timer`; restore = copy a
  `~/apps/behelink-data/backups/behelink-*.db` snapshot over `behelink.db` and restart the unit.
- Admin is SQLite-file-direct in v1:
  `sqlite3 ~/apps/behelink-data/behelink.db 'SELECT id, ip, port, last_seen FROM links'`.
- After a behelink restart the Telegram relay's in-memory `path_token` cache is gone:
  `:enableTelegramRelay` mints a fresh token and the old `webhook_url` answers `404` — clients
  re-register on startup (see `README.md`).

Open items:

- **Docker/containerd** are installed (with git as a dependency) but unused — zero
  containers/images. Candidate for `systemctl disable --now docker docker.socket containerd` +
  package purge if nothing plans to use it; deferred to the operator since it was installed
  deliberately post-provision.
- No external uptime monitoring yet (the box can't observe its own outages); a probe on
  `https://link.behemotion.com/healthz` from any monitor would close that gap.
