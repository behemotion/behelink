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
| Front | Caddy (official caddy apt repo, v2.11.4), vhost `link.behemotion.com`, auto-TLS (Let's Encrypt, obtained 2026-07-21, auto-renews) → `reverse_proxy 127.0.0.1:47150` |
| Firewall | ufw: default-deny inbound; `22/tcp` (rate-limited), `80/tcp`, `443/tcp+udp`, `47151/udp` (hole-punch reflector, added 2026-07-21) |
| Brute-force | fail2ban, `sshd` jail (systemd backend, incremental bans up to 48h) |
| Updates | unattended-upgrades: security + `-updates` pockets, auto-reboot 04:30 when required |

## Shape

```
internet ──ufw (22 limit / 80 / 443)──► Caddy vhost link.behemotion.com
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

`~/.config/systemd/user/behelink.service` (hardened 2026-07-21):

```ini
[Unit]
Description=behelink NAT rendezvous service
After=network.target

[Service]
WorkingDirectory=/home/build/apps/behelink
Environment=BEHELINK_DATABASE_PATH=/home/build/apps/behelink-data/behelink.db
ExecStart=/home/build/apps/behelink/.venv/bin/behelink
Restart=always
RestartSec=2
NoNewPrivileges=yes
PrivateTmp=yes
MemoryHigh=768M
MemoryMax=1G

[Install]
WantedBy=default.target
```

`/etc/caddy/Caddyfile` (Caddy from the official
`dl.cloudsmith.io/public/caddy/stable` apt repo; the packaged unit runs with
`ProtectSystem=full`, so a drop-in `/etc/systemd/system/caddy.service.d/logdir.conf`
adds `LogsDirectory=caddy` + `ReadWritePaths=/var/log/caddy` for the access
log):

```caddyfile
link.behemotion.com {
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		-Server
		-Via
		defer
	}

	request_body {
		max_size 16KB
	}

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

Host hardening (all applied 2026-07-21):

- **sshd** — `/etc/ssh/sshd_config.d/00-hardening.conf`: `PermitRootLogin no`,
  `PasswordAuthentication no`, `AllowUsers build`, `MaxAuthTries 3`,
  `LoginGraceTime 30`, `X11Forwarding no`, keepalive 300s×2. (`00-` sorts
  before cloud-init's `50-` file; sshd first-match wins.) Recovery if locked
  out: Hostkey panel VNC console — root's local password is intentionally
  still set for that path.
- **ufw** — default deny incoming / allow outgoing; `limit 22/tcp`,
  `allow 80/tcp`, `allow 443/tcp`, `allow 443/udp` (HTTP/3).
- **fail2ban** — `/etc/fail2ban/jail.local`: systemd backend, sshd jail in
  aggressive mode, 1h bans escalating to 48h (`bantime.increment`).
- **unattended-upgrades** — `/etc/apt/apt.conf.d/52unattended-upgrades-local`
  adds the `-updates` pocket, unused-dependency cleanup, and
  `Automatic-Reboot 04:30`.
- **Backups** — user timer `behelink-backup.timer` (daily, persistent,
  randomized ≤1h) runs `~/apps/behelink-data/backup.sh`: `sqlite3 .backup` to
  `~/apps/behelink-data/backups/`, pruned after 14 days.
- **Docker/containerd** are installed and running but unused (no containers);
  disabling them was deferred — see "Open items".

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
- Logs: `journalctl --user -u behelink` (app), `sudo journalctl -u caddy`
  (server), `sudo tail /var/log/caddy/link.behemotion.com-access.log` (JSON
  access log, 50 MiB × 10 rolls, 30-day retention).
- Bans: `sudo fail2ban-client status sshd`; firewall: `sudo ufw status verbose`.
- Backups: `systemctl --user list-timers behelink-backup.timer`; restore =
  copy a `~/apps/behelink-data/backups/behelink-*.db` snapshot over
  `behelink.db` and restart the unit.
- Admin is SQLite-file-direct in v1:
  `sqlite3 ~/apps/behelink-data/behelink.db 'SELECT id, ip, port, last_seen FROM links'`.
- `BEHELINK_REGISTRATION_RATE_PER_HOUR` (default 10/IP) is active; tune in
  the unit's `Environment=` lines before wide announcement.
- Single instance by design (in-memory rate limiter, SQLite); HA out of
  scope for v1.

## Hole-punch signaling (live since 2026-07-21)

Adds a UDP listener on `BEHELINK_REFLECTOR_PORT` (default `47151`) in the same process — no new
systemd unit, no Caddy change (Caddy can't front raw UDP).

- `sudo ufw allow 47151/udp` applied on the live box (mirrors the existing `allow 443/udp` rule).
- `BEHELINK_REFLECTOR_HOST=0.0.0.0` (the default, no override needed) — unlike the HTTP listener,
  this one binds a public interface directly, by design (Caddy can't proxy it).
- Verified with a UDP probe sending `{"token":"<a live token>"}` from an outside network and
  confirming a JSON `{"ip", "port"}` reply — see "Live verification" below.

## Hole-punch signaling live verification (2026-07-21)

- Redeployed via the standard rsync + `uv sync` + `uv run pytest` + `systemctl --user restart`
  flow above; full pytest suite (60 tests) green on the server itself.
- `sudo ufw allow 47151/udp` applied; `ss -ulnp` confirmed the reflector bound `0.0.0.0:47151`.
- End-to-end from an outside network against `https://link.behemotion.com`: register (201) →
  `POST :requestConnect` with the resolve token (`200 {ip, port}`, the link's own candidate) →
  `GET /pending-connect` with the owner token (`200`, returned the queued candidate `{ip, port}`)
  → wrong-token `pending-connect` (`404`) → deregister (`204`).
- UDP self-STUN reflector probed directly at `46.17.103.230:47151` from outside: valid token →
  JSON reply echoing the prober's observed `(ip, port)`; invalid token → no reply (timeout), as
  designed.

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

## Security hardening verification (2026-07-21, second pass)

- ufw active; nmap-visible surface is 22/80/443 only; ssh `limit` rule live.
- sshd: root login + password auth refused (effective config confirmed via
  `sshd -T`); only key-auth as `build` accepted.
- fail2ban picked up 142 historical failed SSH attempts on activation and
  immediately banned 3 actively brute-forcing IPs.
- Full API e2e re-run through the hardened stack from an outside network:
  register 201 → heartbeat (correct egress IP) → resolve → delete 204.
- 32 KiB request body → 413 (Caddy `request_body max_size 16KB`).
- Response headers: HSTS + nosniff present; `Server`/`Via` stripped.
- Daily DB backup ran once manually; snapshot verified in
  `~/apps/behelink-data/backups/`.

## Open items

- **Docker/containerd**: installed (with git as a dependency) but unused —
  zero containers/images. Candidate for `systemctl disable --now docker
  docker.socket containerd` + package purge if nothing plans to use it;
  deferred to the operator since it was installed deliberately post-provision.
- No external uptime monitoring yet (the box can't observe its own outages);
  a probe on `https://link.behemotion.com/healthz` from any monitor would
  close that gap.
