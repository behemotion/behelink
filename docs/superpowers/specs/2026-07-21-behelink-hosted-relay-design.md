# behelink — Hosted NAT Rendezvous Service (Design)

## Overview

`docs/network-reachability.md` already gives an *operator* three ways to make their own
behetask-server reachable off-LAN (Mode A: direct exposure + DDNS, Mode B: Cloudflare Tunnel,
Mode C: self-hosted `frp`). All three assume the operator sets the exposure up themselves.
`2026-07-21-client-deployment-package-design.md` (same day, separate spec) covers the other half
of the story — a client's own technical person self-deploying behetask on their own Linux box via
a versioned tarball, with `install.sh` asking one question (a domain, blank = LAN mode).

This spec adds a fourth reachability option that neither of those covers: BEHEMOTION runs a small,
shared, public rendezvous service — **behelink** — that any client's behetask-server can register
with by default. A client still configures port-forwarding on their own router (see NAT Assumption
below), but no longer needs their own domain, DDNS account, Cloudflare account, or VPS. behelink is
**not** a data-plane relay — it never proxies task-server traffic. It answers exactly one question,
"where is server X reachable right now," and gets out of the way.

This is a new, standalone repo (`behelink`, BEHEMOTION org), not a mode bolted onto
behetask-server — behelink is inherently multi-tenant (one deployment serves every client), while
behetask-server is deliberately single-tenant/self-hosted per client. Conflating the two would mean
whoever runs the default relay is operating a materially different service using the same codebase
as single-tenant deployments.

## Goals

- A client's behetask-server can become reachable from anywhere with zero DNS/domain/Cloudflare/VPS
  setup on the client's side — only port-forwarding on their own router.
- behelink is pure rendezvous: it stores and returns `{ip, port}`, nothing else. Actual API traffic
  never flows through it.
- BEHEMOTION operates one public instance by default; a client (or anyone) can self-host their own
  `behelink` instance instead and point their deployment at it — same open-source/self-hostable
  philosophy as behetask itself.
- A client's own CLI users experience this transparently — `behetask login` / the CLI install flow
  resolves the server's current address without the end user ever handling a "relay ID" by hand.
- Follows the umbrella's `docs/CONVENTIONS.md` where behelink's own nature allows it (bare `/v1`,
  RFC 9457 errors, bearer auth, `BEHELINK_` env prefix, `/healthz`) — and explicitly documents the
  one place it cannot (network posture: behelink itself must be genuinely public, not
  localhost-only — see Security Considerations).

## Non-Goals

- No data-plane relaying/proxying of task-server traffic. If a client's network can't support
  direct reachability once resolved (e.g. no port-forwarding possible, real NAT hole-punching
  needed), that's Mode B/C territory (`docs/network-reachability.md`), not behelink.
- No NAT hole-punching / STUN-style negotiation. Explicitly out of scope — see NAT Assumption.
- No change to behetask-server's application-level auth (API keys, org-admin token, claim flow).
  Reachability and authentication remain orthogonal, same principle as the existing
  network-reachability spec.
- No automatic integration into `packaging/client/install.sh`'s first-run prompt in this spec —
  that script belongs to the already-approved client-deployment-package design; wiring behelink in
  as a third option there is called out as a coordination follow-up, not designed here.
- No multiple/revocable resolve tokens per registration in v1 (single shared resolve_token,
  rotatable). Per-CLI-user revocable tokens are a possible follow-up, not required for launch.

## NAT Assumption

Confirmed with the user: clients are expected to configure port-forwarding on their own router (or
already have a directly-facing address) — the port a client's server listens on is forwarded
through to it. behelink's job under this assumption is address *discovery* (the client's public IP
changes; the client no longer wants to run DDNS), not NAT *traversal*. This is a materially simpler
problem than blind hole-punching on arbitrary/symmetric NATs, which is unreliable even with a
rendezvous server in the loop — see the existing network-reachability doc's own discussion of this
exact trade-off for Mode A.

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Placement | New standalone repo `behelink`, own GitHub repo under BEHEMOTION org, created immediately | Multi-tenant by nature; conflating with behetask-server's single-tenant model would change what that codebase is. User's explicit direction. |
| Registration auth | Claim-and-own: first `POST /v1/links` mints an `owner_token`, required on every subsequent update | Avoids a circular dependency — the alternative (behelink calling back into the client's server to verify a behetask credential) can't work, since that server is exactly the NAT'd thing that's hard to reach directly. |
| Resolve auth | Requires a `resolve_token` (separate from `owner_token`), minted alongside it at registration | An open resolve endpoint would let anyone who learns/guesses an ID map out a client's home public IP. A credential closes that off. |
| Data plane | Never touches behelink. Resolve returns `{ip, port}`; the CLI connects directly afterward | Explicit user requirement — "pure rendezvous, nothing else." Keeps behelink's footprint (and blast radius if compromised) minimal. |
| Storage | SQLite, one table | `docs/CONVENTIONS.md` explicitly leaves internal storage "free per layer." A single-table registry with no relational queries gets no benefit from Postgres, and skipping it avoids a second container for a tiny service. |
| API shape | `/v1` bare prefix, RFC 9457 errors, bearer auth — per `docs/CONVENTIONS.md` | behelink is a new surface, not grandfathered like behetask's `/api/v1` — no reason to diverge from the umbrella standard from day one. |
| Self-hostability | Client can point their deployment at their own `behelink` instance via config override | Matches behetask's own "open-source & distributable" design stance — BEHEMOTION's instance is the convenient default, not a lock-in. |

## Architecture

```
client's behetask-server (NAT, port-forwarded)
  ──(outbound heartbeat, ~every 60s)──► behelink (public domain, real TLS via Caddy)
        stores per link: id, port (declared), ip (observed from the connection),
                          owner_token_hash, resolve_token_hash, last_seen

behetask CLI, anywhere
  ──GET /v1/links/{id}  (Bearer: resolve_token)──► behelink ──► {ip, port, last_seen}
  ──then connects directly──► client's behetask-server:port
        (behelink is out of the path for this and every subsequent request)
```

behelink itself deploys the same way behetask does today (rootless Podman + systemd-user + Caddy,
per `deploy.md`'s pattern) — the difference is *where*: on infrastructure BEHEMOTION controls with
a real public IP and domain, not dev-4 (which stays LAN-only). Internally, behelink's own listener
still binds `127.0.0.1` with Caddy fronting it — the "public-facing" property lives entirely in
Caddy's vhost being reachable from the internet rather than only the LAN, exactly like Mode A/B/C
already require for *some* component in every reachability story.

## API

All under `/v1`, RFC 9457 `application/problem+json` errors, per `docs/CONVENTIONS.md` §3.

- **`POST /v1/links`** `{id, port}` → `201 {owner_token, resolve_token}`. Tokens are returned
  exactly once (never retrievable again — same "shown once" pattern as a behetask API key). `409`
  if `id` is already taken.
- **`PATCH /v1/links/{id}`** `{port?}` (Bearer: `owner_token`) → heartbeat; refreshes `last_seen`
  and re-captures the observed source IP on every call, since a client's public IP can change
  between heartbeats. `200 {ip, port, last_seen}`.
- **`GET /v1/links/{id}`** (Bearer: `resolve_token`) → `200 {ip, port, last_seen}`, or `404` if the
  link doesn't exist or is stale (see Staleness below) — a stale link and a nonexistent link are
  indistinguishable to a resolver, deliberately (no signal about *why* resolution failed beyond
  "not currently reachable via behelink").
- **`DELETE /v1/links/{id}`** (Bearer: `owner_token`) → deregister, `204`.
- **`POST /v1/links/{id}:rotateResolveToken`** (Bearer: `owner_token`) → issues a new
  `resolve_token`, invalidating the old one. AIP colon-method form per `docs/CONVENTIONS.md` §3,
  since this is a non-CRUD action on an existing resource.
- **`GET /healthz`** → `200 {"status": "ok"}`, unauthenticated, DB ping only.

## Staleness

A link not refreshed (via `PATCH`) within **3× the heartbeat interval** (~3 minutes, given the
~60s heartbeat) is treated as offline: `GET` returns `404` rather than a possibly-dead address. The
client's behetask-server re-heartbeats on its own reconnect/backoff loop, mirroring the existing
Telegram long-poller's pattern (`behetask-server/src/behetask_server/integrations/telegram/polling.py`)
— no new backoff logic to design, the same shape already exists in this codebase.

## Components

### behelink (new repo)

- FastAPI app (consistent with the harness's existing Python/FastAPI stack), SQLite via stdlib
  `sqlite3` or a minimal ORM — single `links` table.
- Token hashing: stdlib `hashlib`, same unsalted-SHA-256-of-a-CSPRNG-secret pattern behetask's own
  API keys already use (safe because the input is already high-entropy, generated server-side).
- `BEHELINK_` env prefix (`BEHELINK_HEARTBEAT_TTL_SECONDS`, `BEHELINK_DATABASE_PATH`, etc.), port
  **47150** (next open slot in the umbrella's port registry, `docs/HARNESS-PLAN.md`; no DB port
  needed since SQLite is in-process).
- `beheaxi`-based CLI is out of scope for v1 — behelink is consumed by behetask-server/behetask-cli
  programmatically, not operated interactively day-to-day. (Flagged as an Open Follow-Up if a
  standalone `behelink` CLI later proves useful for ops/debugging.)

### behetask-server (new client of behelink)

- New settings: `BEHETASK_RELAY_URL` (default: BEHEMOTION's hosted behelink), `BEHETASK_RELAY_ID`,
  `BEHETASK_RELAY_OWNER_TOKEN` — same `.env` pattern as the existing `BEHETASK_PUBLIC_URL`.
- New background task alongside the existing Telegram poller: registers (idempotently — `PATCH`
  after a `POST 409` means "already registered, just heartbeat") and heartbeats on an interval,
  using the same reconnect/backoff shape as the Telegram integration.
- `behetask network setup` wizard (from `2026-07-11-network-setup-wizard-design.md`) gains a fourth
  mode alongside 0/A/B/C: register with behelink, defaulting to BEHEMOTION's instance, with a
  `--relay-url` override for a self-hosted behelink.

### behetask-cli (new resolve step)

- `--server` resolution (documented in `CLAUDE.md`'s connection precedence: flag > env > saved
  config > default) gains a new source: a saved `relay_id` + `relay_resolve_token` in the config
  written by `behetask login`, resolved through behelink immediately before connecting. Resolution
  result is used for that invocation, not cached indefinitely — a client's IP can change between
  CLI invocations, and re-resolving is cheap (one extra `GET`).
- `GET /cli/install.sh` bakes in the relay ID + resolve_token alongside the server URL it already
  bakes in (per the sub-project #1 pattern), so a client's CLI users get working resolution with
  zero manual config.

## Data Flow

**Registration (client operator, once):** `behetask network setup` → picks "hosted relay" →
generates a relay ID (or accepts one) → `POST /v1/links {id, port}` → behelink stores
`{id, port, ip: <observed>, owner_token_hash, resolve_token_hash}` → wizard writes
`BEHETASK_RELAY_*` into `.env` → operator redeploys (existing pattern, printed not run).

**Ongoing (client's server, automatic):** background task `PATCH /v1/links/{id} {port}` every
~60s → behelink refreshes `ip` (from the connection) and `last_seen`.

**Resolution (a CLI user elsewhere):** `behetask board` → CLI reads saved `relay_id` +
`relay_resolve_token` from config → `GET /v1/links/{id}` → `{ip, port}` → CLI connects directly to
`http://{ip}:{port}` (or `https://`, if the client's own port-forwarded service terminates TLS —
see Open Implementation Flags) → ordinary behetask API-key auth applies exactly as today.

## Error Handling

- **behelink unreachable** (network blip, behelink down): CLI resolution fails with a clear error
  distinguishing "couldn't reach the relay" from "relay says the server is offline" (`404` vs. a
  connection error) — a client shouldn't be told their server is down when it's actually behelink
  itself that's unreachable.
- **Link gone stale mid-session:** a CLI already holding a resolved `{ip, port}` from earlier in
  the process just gets a normal connection failure to that address if the client's IP changed
  since — same failure mode as any address going stale, no special handling needed beyond
  re-resolving on the next invocation.
- **`id` collision on first `POST`:** `409`, same as any other harness surface; the wizard/CLI
  prompts for a different ID or offers to reuse if the operator owns it (proven only by having
  the `owner_token`, which the wizard would have saved from the original registration).
- **Heartbeat failures:** background task retries with the same backoff shape as the Telegram
  poller; does not crash or block the rest of behetask-server if behelink is unreachable — relay
  registration is best-effort, not a hard dependency for local/LAN operation.

## Security Considerations

- **behelink is the one new public-facing exception in the harness.** `docs/CONVENTIONS.md` §4
  ("only Caddy vhosts and the gateway face the network, everything else binds localhost") already
  assumes a LAN-bound harness; behelink's *purpose* requires its Caddy vhost to be reachable from
  the public internet, the same concession Modes A/B/C already made explicit for behetask itself.
  Everything downstream of behelink (behetask-server, its DB) stays exactly as private as before —
  behelink learns only an IP, a port, and two opaque tokens per client, nothing about task data.
- **Resolve requires a credential** (locked in during brainstorming) — closes off ID enumeration
  mapping clients' home IPs to relay IDs.
- **Owner token compromise** lets an attacker redirect a relay ID to an address they control,
  which a CLI would then connect to — but behetask's own API-key auth still gates every request at
  that point, so this is a redirection/DoS risk (a CLI talks to the wrong box and gets `401`s or
  garbage), not a credential-theft risk. Documented, not silently ignored.
- **Rate limiting on `POST /v1/links`** (id-squatting, registration churn) is needed before public
  launch — flagged as an Open Implementation Flag, not resolved at design time.
- Token hashing matches the existing stdlib-only precedent (`CLAUDE.md`'s dependency ban — no
  `bcrypt`/`pyjwt` here either).

## Testing

- **behelink:** pytest against the FastAPI test app — register → heartbeat (IP re-capture) →
  resolve → stale-after-TTL → 404 → delete → id-collision 409 — same `httpx.AsyncClient`-against-
  test-app style already used across the harness (e.g. behetask's `test_protocol_version_header.py`).
- **behetask-server:** unit tests for the new registration/heartbeat background task (mocked
  behelink via `httpx.MockTransport`), covering the retry/backoff path and the "relay unreachable
  doesn't block startup" requirement.
- **behetask-cli:** `httpx.MockTransport` for the new resolve-before-connect step; covers both the
  "resolves and connects" happy path and the "relay says offline" / "relay itself unreachable"
  error-message distinction.
- **Live:** once behelink is deployed publicly, an equivalent live-service check to
  `docs/tests/PI-AGENT-TEST-SUITE.md`'s T0 — register a throwaway link from a real NAT'd box,
  resolve it from an outside network, confirm the CLI connects. Full live test suite authored in
  the implementation plan, not this design.

## Open Implementation Flags (to verify during the plan, not resolved here)

- Whether the client's server terminates plain HTTP or TLS on the port-forwarded port — if TLS,
  what cert (self-signed, accepted via a CA-bundle flag like the existing `BEHE<LAYER>_CA_BUNDLE`
  pattern) vs. plain HTTP by IP. Mirrors the still-open ACME-mode question already flagged in
  `2026-07-10-network-reachability-design.md` for Mode A.
- Exact heartbeat interval and staleness multiplier (60s / 3× proposed above) — tunable via
  `BEHELINK_HEARTBEAT_TTL_SECONDS`, default to be finalized in the plan.
- Rate-limiting mechanics for `POST /v1/links` (per-IP? per-ID-namespace?) — needed before public
  launch, mechanism not decided here.
- Whether `behelink` needs its own minimal CLI for ops (list/inspect/revoke links) or admin access
  is SQLite-file-direct for v1 — leaning toward deferring a CLI until proven necessary.

## Open Follow-Ups (explicitly out of scope here)

- Wiring behelink as a third option into `packaging/client/install.sh`'s first-run prompt
  (currently domain-or-LAN only, per `2026-07-21-client-deployment-package-design.md`) — a
  coordination point between the two specs, not designed here.
- Per-CLI-user revocable resolve tokens (v1 ships one shared resolve_token per link).
- A standalone `behelink` ops CLI, if SQLite-file-direct admin proves insufficient.
- Multi-region / HA behelink deployment — v1 is a single instance, matching the simplicity bar set
  by every other harness service today.
