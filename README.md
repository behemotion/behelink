# behelink

Hosted NAT rendezvous service for the BEHEMOTION harness.

behelink answers exactly one question — *"where is server X reachable right
now?"* — and gets out of the way. A client's self-hosted
[behetask](https://github.com/behemotion/behetask)-server registers with
behelink and heartbeats its current public `{ip, port}`; a behetask CLI
anywhere resolves that address through behelink, then connects **directly** to
the client's server.

behelink is rendezvous-first: it never proxies task-server traffic. It also
brokers NAT hole-punching (candidate exchange and a UDP self-STUN reflector),
but only the signaling — the punched connection itself never touches behelink.
One narrow exception to "never proxies" exists: an opt-in **Telegram webhook
relay** that queues inbound Telegram updates for a NAT'd behetask-server to
collect, so a bot works with no public ingress on the client side. See
[`docs/superpowers/specs/2026-07-28-behelink-telegram-webhook-relay-design.md`](docs/superpowers/specs/2026-07-28-behelink-telegram-webhook-relay-design.md).

BEHEMOTION operates one public instance by default —
**https://link.behemotion.com** (see [`DEPLOY.md`](DEPLOY.md)); anyone can
self-host their own behelink and point their deployment at it instead.

## Design

The authoritative design spec:

- [`docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md`](docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md)
- [`docs/superpowers/specs/2026-07-28-behelink-telegram-webhook-relay-design.md`](docs/superpowers/specs/2026-07-28-behelink-telegram-webhook-relay-design.md)
  — amends the above spec's "no data-plane relaying" Non-Goal

Implementation plans:

- [`docs/superpowers/plans/2026-07-21-behelink-service.md`](docs/superpowers/plans/2026-07-21-behelink-service.md)
- [`docs/superpowers/plans/2026-07-28-behelink-telegram-webhook-relay.md`](docs/superpowers/plans/2026-07-28-behelink-telegram-webhook-relay.md)

## API

All endpoints under bare `/v1`; errors are RFC 9457 `application/problem+json`.

| Endpoint | Auth | Result |
|---|---|---|
| `POST /v1/links` `{id, port}` | none (rate-limited per IP) | `201 {owner_token, resolve_token}` — shown **once**; `409` if `id` taken |
| `PATCH /v1/links/{id}` `{port?}` | Bearer `owner_token` | heartbeat: re-captures observed IP, refreshes `last_seen` → `200 {ip, port, last_seen}` |
| `GET /v1/links/{id}` | Bearer `resolve_token` | `200 {ip, port, last_seen}`; `404` if unknown **or** stale (no distinction, by design) |
| `DELETE /v1/links/{id}` | Bearer `owner_token` | deregister → `204` |
| `POST /v1/links/{id}:rotateResolveToken` | Bearer `owner_token` | `200 {resolve_token}` — old resolve token invalidated |
| `POST /v1/links/{id}:requestConnect` `{port}` | Bearer `resolve_token` | queues a pending-connect (short TTL); `200 {ip, port}` — the link's own current candidate, same round trip |
| `GET /v1/links/{id}/pending-connect?wait=N` | Bearer `owner_token` | long-poll; `200 {ip, port}` once a connect request lands, else `204` after `min(N, BEHELINK_PENDING_CONNECT_WAIT_MAX_SECONDS)` |
| `POST /v1/links/{id}:enableTelegramRelay` | Bearer `owner_token` | `200 {webhook_url, path_token}` — opt in; idempotent while the process lives |
| `POST /v1/links/{id}:rotateWebhookPathToken` | Bearer `owner_token` | `200 {webhook_url, path_token}` — old token invalid immediately |
| `POST /v1/links/{id}:disableTelegramRelay` | Bearer `owner_token` | `204` — purges queued updates |
| `POST /v1/tg/{id}/{path_token}` | path token | Telegram-facing ingress → `200 {"ok": true}` once committed; `503` when the queue is full so Telegram retries |
| `GET /v1/links/{id}/telegram/updates?wait=N&limit=M` | Bearer `owner_token` | long-poll; `200 {updates:[{seq, received_at, secret_header, payload}]}` or `204` |
| `POST /v1/links/{id}/telegram/ack` `{through_seq}` | Bearer `owner_token` | `204` — deletes this link's rows through `seq` |
| `GET /v1/links/{id}/telegram` | Bearer `owner_token` | `200 {enabled, depth, oldest_seq, last_enqueue_at, dropped_total}` |
| `GET /healthz` | none | `200 {"status": "ok"}` (DB ping only) |

A link is **stale** when it hasn't heartbeated within
`BEHELINK_HEARTBEAT_TTL_SECONDS` (default 180s = 3 × the ~60s heartbeat).

For hole-punch usage (behetask Mode D), `port` in `PATCH`/`POST /v1/links` means the caller's
self-STUN'd candidate port, not a forwarded listening port — self-STUN against the UDP reflector
below. `ip` is still always server-observed, never client-declared, on every endpoint including
the two above.

behelink also runs a UDP self-STUN reflector on `BEHELINK_REFLECTOR_PORT`: send `{"token": "<owner_token
or resolve_token>"}` as JSON; a valid token gets `{"ip", "port"}` echoing the observed source
address back. An invalid/missing token gets no reply.

Link IDs: 3–63 chars, lowercase letters/digits with inner hyphens
(`acme-tasks`).

### Telegram webhook relay

A behetask-server behind NAT has no public HTTPS URL to give `setWebhook`, so it
has to long-poll `getUpdates` — and Telegram allows exactly one `getUpdates`
consumer per bot token, so a second one silently steals updates. The relay
removes that: the server calls `:enableTelegramRelay`, registers the returned
`webhook_url` with Telegram itself, then long-polls
`GET /v1/links/{id}/telegram/updates` and acks what it has processed.

Delivery is **at-least-once and durable** — the row is committed before behelink
answers Telegram `200`, because Telegram only redelivers on a non-2xx. Only the
`owner_token` may read the queue, so there is exactly one consumer by
construction. Since the `path_token` is stored hashed, the cleartext copy that
makes re-enable idempotent lives in process memory: after a behelink restart,
re-enabling mints a fresh token, so callers should treat `:enableTelegramRelay`
as authoritative on startup and re-register whatever URL it returns.

## Configuration

Env vars, `BEHELINK_` prefix:

| Variable | Default | Meaning |
|---|---|---|
| `BEHELINK_DATABASE_PATH` | `behelink.db` | SQLite file path |
| `BEHELINK_HEARTBEAT_TTL_SECONDS` | `180` | staleness cutoff for resolution |
| `BEHELINK_REGISTRATION_RATE_PER_HOUR` | `10` | per-IP `POST /v1/links` limit |
| `BEHELINK_HOST` | `127.0.0.1` | listener bind (Caddy fronts it) |
| `BEHELINK_PORT` | `47150` | listener port (umbrella port registry) |
| `BEHELINK_REFLECTOR_HOST` | `0.0.0.0` | UDP reflector bind (no Caddy in front — see `DEPLOY.md`) |
| `BEHELINK_REFLECTOR_PORT` | `47151` | UDP reflector port (umbrella port registry) |
| `BEHELINK_REFLECTOR_RATE_PER_MINUTE` | `20` | per-source-IP reflector probe limit |
| `BEHELINK_REFLECTOR_MAX_PAYLOAD_BYTES` | `512` | oversized probes are dropped, no reply |
| `BEHELINK_REQUEST_CONNECT_RATE_PER_MINUTE` | `10` | per-resolve_token `:requestConnect` limit |
| `BEHELINK_PENDING_CONNECT_TTL_SECONDS` | `10.0` | how long a queued connect request stays valid |
| `BEHELINK_PENDING_CONNECT_WAIT_MAX_SECONDS` | `25.0` | server-side clamp on the long-poll `wait` query param |
| `BEHELINK_PUBLIC_BASE_URL` | `https://link.behemotion.com` | base for the `webhook_url` handed back by `:enableTelegramRelay` |
| `BEHELINK_TELEGRAM_RELAY_ENABLED` | `True` | master switch; off makes enable/rotate and the ingress `404` |
| `BEHELINK_TELEGRAM_QUEUE_MAX_DEPTH` | `1000` | per-link queue depth before the ingress sheds with `503` |
| `BEHELINK_TELEGRAM_QUEUE_TTL_SECONDS` | `3600` | unacked row lifetime; expiry is counted in `dropped_total` |
| `BEHELINK_TELEGRAM_UPDATES_WAIT_MAX_SECONDS` | `25.0` | clamp on the updates long-poll `wait` |
| `BEHELINK_TELEGRAM_UPDATES_MAX_BATCH` | `100` | clamp on the updates `limit` |
| `BEHELINK_TELEGRAM_WEBHOOK_RATE_PER_MINUTE` | `120` | ingress limit, applied per link **and** per source IP |
| `BEHELINK_TELEGRAM_MAX_PAYLOAD_BYTES` | `1048576` | ingress body cap (`413` above it) |

## Development

```sh
uv sync --extra dev
uv run pytest
uv run behelink      # serves 127.0.0.1:47150
```

Testing a client (behetask-server, a script, etc.) against a real public
instance instead of a local one? See
[`docs/connecting-a-dev-build.md`](docs/connecting-a-dev-build.md).

## Security notes

- Tokens are returned exactly once at registration and stored only as SHA-256
  hashes (unsalted over CSPRNG secrets — behetask API-key precedent); compares
  are constant-time.
- A wrong bearer token on an existing link answers `404`, identical to a
  nonexistent link — resolvers and probers can't enumerate IDs or distinguish
  "exists but not yours". Only a missing/malformed `Authorization` header gets
  `401`.
- Owner-token compromise is a *redirection/DoS* risk only (an attacker can
  point the ID at their own box); behetask's application-level API-key auth
  still gates every request a CLI would then make.
- For the core rendezvous path behelink stores only
  `{id, port, observed ip, token hashes, timestamps}` — never task data.
- The Telegram relay is the one exception, and it is opt-in per link. While a
  link has it enabled, inbound Telegram update payloads are stored in behelink's
  database until the owner acks them (typically seconds) or the TTL sweeps them.
  **They are not encrypted at rest in v1** — a behelink operator, or anyone with
  database access, can read queued message text. Encrypting payloads to a
  behetask-held key is a named follow-up. Operators who find this unacceptable
  self-host behelink; BEHEMOTION's instance then sees nothing.
- behelink never holds a bot token and never calls `setWebhook`; behetask
  registers its own webhook and validates the `X-Telegram-Bot-Api-Secret-Token`
  that behelink only stores opaquely, so a compromised behelink cannot forge
  updates behetask will accept.
