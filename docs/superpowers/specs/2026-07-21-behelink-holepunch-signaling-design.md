# behelink — NAT Hole-Punch Signaling (Design)

## Overview

`2026-07-21-behelink-hosted-relay-design.md` (the shipped v1) made behelink a pure
address-rendezvous service: a client's behetask-server still configures port-forwarding on its own
router, and behelink only ever answers "where is server X reachable right now." behetask's Mode D
now wants **zero port-forwarding**, which requires NAT hole-punching — behelink brokers a handshake
so behetask-server and behetask-cli connect **directly**, then behelink is out of the picture. Full
context and behetask's own design: `docs/handoffs/from-behetask/MODE-D-HOLEPUNCH-RELAY-CONTRACT.md`
(behelink's copy of the contract) and `behetask/docs/superpowers/specs/2026-07-21-behelink-holepunch-mode-d-design.md`
(behetask's full spec). behelink's reaction and answers to that handoff's open questions are
recorded at `behetask/docs/handoffs/from-behelink/MODE-D-HOLEPUNCH-RELAY-CONTRACT-REPLY.md`.

This spec is the behelink-side design for building what that reply committed to: additive, not
replacing, the shipped `/v1/links` CRUD. behelink's own nature doesn't change — still a small,
single-instance signaling store that never sees or carries behetask API traffic.

## Goals

- Broker a NAT hole-punch handshake for behetask Mode D without ever becoming a data-plane relay.
- No bandwidth- or CPU-proportional cost as behetask usage grows — the added surface (a UDP
  reflector, a connect-request queue) is O(1) per attempt, not O(traffic).
- Introduce **no new client-trusted-address surface**: `ip` stays server-observed exactly as today;
  only what the existing client-declared `port` field *means* widens.
- Additive under `/v1`, no `links` table schema change, no new SQLite table.

## Non-Goals

- **No traffic-relay fallback, ever** — carried over from behetask's own binding non-goal. If
  hole-punching fails, behelink never starts proxying bytes.
- **No NAT-type classification, no RFC 5389 STUN wire-format compliance.** Only behetask-server and
  behetask-cli ever speak to the reflector; the wire shape is behelink's choice.
- **No change to Modes 0/A/B/C or the shipped hosted-relay contract**, beyond widening what `port`
  means for a link that opts into hole-punch usage.
- **No relaxing the `ip` trust boundary.** `HeartbeatRequest` gets no new `ip` field; `:requestConnect`
  never trusts a client-declared `ip` either — see Key Decisions.
- **No persistence for pending-connect records.** In-memory, single-instance, disposable by design.

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Take on the role at all | Yes | Confirmed with behelink's owner — accepted in principle, additive to the existing CRUD. |
| `ip` trust model | Stays server-observed via `_client_ip()`, exactly as today. Only the client-declared `port` field's *meaning* shifts (declared-forwarded-port → self-STUN'd candidate port). No new `HeartbeatRequest.ip` field; `:requestConnect` also uses the HTTP-observed IP, never a client-declared one. | A NAT box's external IP is the same across virtually every socket it owns (cone and typical symmetric NAT both preserve it; only the mapped port varies per-socket). Trusting client-declared IP would be a genuinely new, avoidable trust surface for no practical benefit. |
| UDP reflector placement | Same process, same systemd unit — a second `asyncio.DatagramProtocol` listener started via `loop.create_datagram_endpoint` on uvicorn's existing event loop, not a separate service. | No second process/container to deploy or supervise; matches the "no second container for a tiny service" reasoning the shipped design already used for SQLite over Postgres. |
| UDP port exposure | New dedicated UDP port, directly `ufw`-allowed (not Caddy-fronted — Caddy can't proxy raw UDP the way this needs). Exact port number is an Open Implementation Flag. | Confirmed with behelink's owner: keeps the reflector distinguishable from `443/udp` (HTTP/3 QUIC) in logs/firewall rules, and keeps the existing `47150/tcp` HTTP port untouched. |
| Reflector auth | Requires a bearer token in the UDP payload — `owner_token` **or** `resolve_token`, hash-checked against either column. | Confirmed with behelink's owner: every other behelink surface but `/healthz` requires a credential; a vanilla open STUN-alike would be the one exception. Both parties already hold a valid token before they ever need to self-STUN, so no new secret is minted. |
| Pending-connect storage | In-memory dict keyed by `link_id`, with an `asyncio.Event` per entry so the long-poll wakes instantly instead of polling. No new SQLite table. | Confirmed with behelink's owner: matches the existing in-memory `RateLimiter` precedent (`ratelimit.py`) — single-instance, records live only seconds, a restart losing an in-flight attempt is fine since the CLI retries. |
| Rate limiting | Two new `RateLimiter` instances (same class, different constructor params) — reflector probes keyed by UDP source IP, `:requestConnect` keyed by the resolve_token identity. behelink builds this in itself. | Confirmed with behelink's owner: the reflector is a public UDP surface and needs its own abuse control; token-keying `:requestConnect` (already authenticated) is more precise than IP-keying and doesn't punish multiple legit users behind one NAT. |
| Versioning | Additive under `/v1`, no version bump. | Confirmed with behelink's owner: matches the existing bare `/v1` + AIP colon-method convention already in use for `:rotateResolveToken`. |

## Architecture

```
behetask-server (NAT'd)                              behetask-cli (anywhere)
  │                                                        │
  ├─ self-STUN: UDP probe {token: owner_token} to          ├─ self-STUN: UDP probe
  │  behelink's reflector, from the exact socket that       │  {token: resolve_token}, from the
  │  will later punch → learns own external (ip, port)      │  exact socket that will later punch
  │                                                        │
  ├─ PATCH /v1/links/{id} (existing endpoint) — port        │
  │  now means "self-STUN'd candidate port"; ip is still     │
  │  server-observed, unchanged                              │
  │                                                        │
  ├─ GET /v1/links/{id}/pending-connect?wait=N (long-poll,  ├─ POST /v1/links/{id}:requestConnect
  │  Bearer owner_token) — asyncio.Event-driven wake-up,     │  {port} (Bearer resolve_token) → stored
  │  not polling                                             │  as an in-memory pending-connect;
  │                                                        │  response carries the server's
  │                                                        │  current {ip, port} in the same round trip
  │                                                        │
  └─────────────── punch burst fires both directions once behelink's role ends ───────────────┘

behelink itself: still a single-instance signaling store. The reflector answers a small JSON reply
per probe; the pending-connect dict holds one short-TTL entry per in-flight attempt. Neither is
bandwidth- or CPU-proportional to behetask API usage.
```

## API

All under `/v1`, additive to the existing CRUD (`main.py`).

- **UDP reflector** (new dedicated port, no Caddy in front): request = `{"token": "<owner_token or
  resolve_token>"}` as UTF-8 JSON. If the token hash-matches either column on any link, reply =
  `{"ip": "<observed source ip>", "port": <observed source port>}`. If the token doesn't match
  anything, **silently drop** — no reply — same "no signal to an invalid credential" posture as the
  existing 404-for-invisible-resources pattern on the HTTP side.
- **`POST /v1/links/{id}:requestConnect`** (Bearer: `resolve_token`) — body `{"port": <int>}` only
  (no `ip` field; the HTTP-observed IP is used, same `_client_ip()` helper as everywhere else).
  Stores an in-memory pending-connect record for `link_id` with a short TTL, and sets that link's
  `asyncio.Event`. Response: `{"ip": <link's current ip>, "port": <link's current port>}` (the
  link's own server-side candidate, from the existing `links` row) in the same round trip.
- **`GET /v1/links/{id}/pending-connect?wait=N`** (Bearer: `owner_token`) — `async def` handler.
  Returns immediately with the pending-connect's `{ip, port}` if one is already queued; otherwise
  awaits the link's `asyncio.Event` up to a server-clamped max (independent of the requested `N`).
- **Existing `PATCH /v1/links/{id}`**: no code change. `port` is now documented as "the caller's
  self-STUN'd candidate port" rather than "a declared listening port" for links used in hole-punch
  mode — a documentation/interpretation change only.

## Components

- **`main.py`**: two new route handlers (`:requestConnect`, `pending-connect`); a new
  `asyncio.DatagramProtocol` subclass plus a `loop.create_datagram_endpoint()` call wired into the
  app's startup (FastAPI lifespan).
- **`ratelimit.py`**: no class change — two additional `RateLimiter(...)` instances constructed in
  `create_app()`, same as the existing `app.state.rate_limiter`.
- **`db.py`**: untouched. No new table, no new columns.
- **New in-process state**: a `dict[str, PendingConnect]` (link_id → candidate + `asyncio.Event` +
  expiry), owned by `app.state`, alongside the existing `rate_limiter`.
- **`config.py`**: new `BEHELINK_` settings for the new port and the tunables listed under Open
  Implementation Flags.

## Data Flow

**Server registration/heartbeat (unchanged endpoint, widened meaning):** server self-STUNs against
the reflector using its punching socket → `PATCH /v1/links/{id} {port: <self-STUN'd port>}` →
behelink updates `port` (client-declared, as always) and re-captures `ip` (server-observed, as
always).

**Server awaits a connect signal:** server calls `GET /v1/links/{id}/pending-connect?wait=N`
continuously (Telegram-`getUpdates`-style) — returns as soon as a CLI's `:requestConnect` lands, or
after the clamped wait.

**CLI resolution + connect request:** CLI self-STUNs from its own punching socket → `POST
/v1/links/{id}:requestConnect {port}` → behelink stores the pending-connect (waking the server's
long-poll) and replies with the server's current `{ip, port}` in the same round trip → both sides
fire their punch bursts → behelink is out of the picture.

## Error Handling

- **Reflector probe with a bad/missing token:** dropped silently, no reply — a prober can't
  distinguish "wrong token" from "packet lost," matching the existing invisible-resource posture.
- **`:requestConnect` on an unknown/wrong-token link:** `404`, same `_not_found` helper already used
  everywhere else.
- **`pending-connect` long-poll on an unknown/wrong-token link:** `404`, same reasoning.
- **Long-poll timeout with nothing queued:** exact response shape is an Open Implementation Flag
  (leaning a `204` with no body, to distinguish cleanly from the 200-with-payload case).
- **behelink restarts mid-attempt:** in-memory pending-connect state is lost; the CLI's bounded
  punch-retry window (a behetask-side concern) naturally covers this — no special handling needed on
  behelink's side.

## Security Considerations

- **The reflector requires a token, unlike vanilla STUN.** This closes off use as a free public
  reflection service for arbitrary internet hosts — only holders of a live link's `owner_token` or
  `resolve_token` get a reply at all.
- **Reply is smaller than the request**, since the request carries a ~50-character token and the
  reply is a short `{ip, port}` JSON blob — this largely defeats the classic UDP amplification-abuse
  shape by construction, on top of the per-IP rate limiter.
- **`ip` trust boundary is unchanged from the shipped v1** — still always server-observed, never
  client-supplied, on every endpoint including the two new ones. No new spoofing/redirection surface
  is introduced by this feature.
- **New public, directly-exposed UDP port** — unlike every other public surface today (fronted by
  Caddy), this listener answers raw UDP straight from the OS socket. This is a materially bigger,
  statefully-interactive public surface than the current "small stateless-ish HTTP CRUD store," which
  is exactly what the original handoff's open questions flagged before this was accepted. Needs a new
  `HARNESS-DIVERGENCES.md` entry once implemented (not written in this spec, which predates any code
  change).
- **Rate limiting is best-effort**, same caveat as the existing registration limiter — a
  determined attacker spoofing UDP source addresses isn't fully stopped by IP-keyed limiting. Accepted
  at the same risk tier as the shipped registration limiter; no stronger guarantee is claimed.

## Testing

- **behelink:** pytest for the reflector (valid-token probe gets an accurate `{ip, port}` echo;
  invalid/missing token gets no reply), `:requestConnect` (stores pending-connect, wakes a waiting
  long-poll, returns the link's current candidate), `pending-connect` (immediate return when queued
  vs. clamped-timeout when not), and the two new rate limiters (reused `httpx.AsyncClient`-against-
  test-app style already used across the suite).
- **Live:** once deployed, the existing T0-style live check gains an authenticated reflector probe
  against the live box's new UDP port, plus a `:requestConnect`/`pending-connect` round trip — full
  live suite authored at implementation-plan time, not here.

## Open Implementation Flags (to verify during the plan, not resolved here)

- Exact UDP port number (a placeholder was discussed, not committed here) — needs registering in the
  umbrella's `docs/HARNESS-PLAN.md` port table alongside the existing `47150` entry.
- Exact pending-connect TTL (a few seconds, per the original handoff) — tunable via a new
  `BEHELINK_` setting, default finalized in the plan.
- Exact long-poll `wait` server-side clamp (independent of the caller's requested `N`).
- Exact rate-limit thresholds for the reflector (per-IP) and `:requestConnect` (per-token) — these
  need a UDP-appropriate cadence, not the existing hourly registration limiter's calibration.
- Exact response shape for a `pending-connect` long-poll timeout (`204` vs. `200 {}` vs. something
  else).
- Whether the reflector needs a max inbound UDP payload size cap as defense-in-depth against
  oversized/garbage probes.

## Open Follow-Ups (explicitly out of scope here)

- Writing the `HARNESS-DIVERGENCES.md` entry for the new directly-exposed UDP port — done once this
  is actually implemented and deployed, not as part of this spec.
- Any behelink-side ops tooling for inspecting in-memory pending-connect state — v1 stays
  logs-and-SQLite-file-direct for ops, same deferral the shipped design already made for a `behelink`
  CLI.
- Migration/self-heal handling for behetask-side links registered before this shipped, whose stored
  `port` predates the reinterpretation — a behetask-side concern per the original handoff's framing,
  not behelink's to design.

---

— behelink team, 2026-07-21
