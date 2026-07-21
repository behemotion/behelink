# behelink

Hosted NAT rendezvous service for the BEHEMOTION harness.

behelink answers exactly one question — *"where is server X reachable right
now?"* — and gets out of the way. A client's self-hosted
[behetask](https://github.com/behemotion/behetask)-server registers with
behelink and heartbeats its current public `{ip, port}`; a behetask CLI
anywhere resolves that address through behelink, then connects **directly** to
the client's server. behelink is pure rendezvous: it never proxies task-server
traffic (no data-plane relaying, no NAT hole-punching).

BEHEMOTION operates one public instance by default; anyone can self-host their
own behelink and point their deployment at it instead.

## Design

The authoritative design spec:

- [`docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md`](docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md)

Implementation plan:

- [`docs/superpowers/plans/2026-07-21-behelink-service.md`](docs/superpowers/plans/2026-07-21-behelink-service.md)

## API

All endpoints under bare `/v1`; errors are RFC 9457 `application/problem+json`.

| Endpoint | Auth | Result |
|---|---|---|
| `POST /v1/links` `{id, port}` | none (rate-limited per IP) | `201 {owner_token, resolve_token}` — shown **once**; `409` if `id` taken |
| `PATCH /v1/links/{id}` `{port?}` | Bearer `owner_token` | heartbeat: re-captures observed IP, refreshes `last_seen` → `200 {ip, port, last_seen}` |
| `GET /v1/links/{id}` | Bearer `resolve_token` | `200 {ip, port, last_seen}`; `404` if unknown **or** stale (no distinction, by design) |
| `DELETE /v1/links/{id}` | Bearer `owner_token` | deregister → `204` |
| `POST /v1/links/{id}:rotateResolveToken` | Bearer `owner_token` | `200 {resolve_token}` — old resolve token invalidated |
| `GET /healthz` | none | `200 {"status": "ok"}` (DB ping only) |

A link is **stale** when it hasn't heartbeated within
`BEHELINK_HEARTBEAT_TTL_SECONDS` (default 180s = 3 × the ~60s heartbeat).

Link IDs: 3–63 chars, lowercase letters/digits with inner hyphens
(`acme-tasks`).

## Configuration

Env vars, `BEHELINK_` prefix:

| Variable | Default | Meaning |
|---|---|---|
| `BEHELINK_DATABASE_PATH` | `behelink.db` | SQLite file path |
| `BEHELINK_HEARTBEAT_TTL_SECONDS` | `180` | staleness cutoff for resolution |
| `BEHELINK_REGISTRATION_RATE_PER_HOUR` | `10` | per-IP `POST /v1/links` limit |
| `BEHELINK_HOST` | `127.0.0.1` | listener bind (Caddy fronts it) |
| `BEHELINK_PORT` | `47150` | listener port (umbrella port registry) |

## Development

```sh
uv sync --extra dev
uv run pytest
uv run behelink      # serves 127.0.0.1:47150
```

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
- behelink stores only `{id, port, observed ip, token hashes, timestamps}` —
  never task data; the data plane never touches it.
