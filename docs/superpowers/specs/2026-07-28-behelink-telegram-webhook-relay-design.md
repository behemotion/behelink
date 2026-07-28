# behelink — Telegram Webhook Relay (Design)

## Overview

`2026-07-21-behelink-hosted-relay-design.md` established behelink as a **pure rendezvous**
service: it answers "where is server X reachable right now" and gets out of the way. That spec's
Non-Goals say, in as many words, "No data-plane relaying/proxying of task-server traffic."

This spec **amends that charter**, narrowly and deliberately, to add one data path: inbound
**Telegram update payloads** for links that explicitly opt in.

The motivating problem is concrete. behetask's Telegram integration runs in `polling` transport
because a webhook needs a publicly reachable HTTPS URL and a self-hosted behetask-server usually
has none. Long-polling `getUpdates` is NAT-friendly, but Telegram permits **exactly one** consumer
of `getUpdates` per bot token and answers every other caller with `409 Conflict`. Any second
consumer — a stale deployment, a dev box pointed at the live token, a leftover container — silently
steals updates from the live one. On 2026-07-28 this presented as "the bot answers nothing at all"
for ~30 hours on the `behetask.mezin.lu` deployment, with a healthy pod, a healthy poller, zero
errors in the log, and no way to tell from the server which of the two was happening.

Webhooks eliminate that failure class structurally: Telegram delivers to exactly one registered
URL, and `getWebhookInfo` reports `last_error_date` / `last_error_message` / `pending_update_count`,
so delivery failures become visible instead of silent. The only thing standing between a NAT'd
behetask-server and a webhook is a public HTTPS endpoint — which behelink already is.

Mode D hole-punching cannot serve this. A hole-punch requires a cooperating peer that speaks QUIC
and participates in the handshake; Telegram is an ordinary HTTPS client and will do neither.

## Goals

- A behetask-server behind arbitrary NAT (including CGNAT, where no port-forward exists) can
  receive Telegram updates, with no domain, DDNS account, Cloudflare account, VPS, or router
  configuration on the operator's side.
- Delivery is **at-least-once and durable**: once behelink answers Telegram `200`, the update is
  committed and survives a behelink restart.
- behelink never holds, sees, or needs the bot token.
- Opt-in per link. Existing registrations are unaffected and unchanged.
- behelink remains self-hostable, so an operator who does not want their task text transiting
  BEHEMOTION infrastructure can run their own instance — the same stance behetask itself takes.
- Follows behelink's established conventions: bare `/v1`, RFC 9457 `application/problem+json`
  errors, bearer auth, `BEHELINK_` env prefix, one module + one test file per concern.

## Non-Goals

- **No general data-plane proxying.** This covers Telegram update payloads and nothing else. The
  behetask CLI data path stays direct (Mode A/B/C) or hole-punched (Mode D). behelink does not
  become an ngrok.
- **No outbound relaying.** behetask's replies go straight to `api.telegram.org`, which is an
  outbound call that already works from behind NAT. Only the inbound direction needs help.
- **No payload encryption at rest in v1.** See Security Considerations — this is a named,
  accepted gap with a follow-up, not an oversight.
- **No bot-token custody.** behelink never calls `setWebhook`; behetask does. behelink cannot
  register, redirect, or hijack a bot.
- **No multi-consumer fan-out.** Exactly one consumer per link, authenticated by `owner_token`.
  Fan-out would reintroduce the very duplicate-consumer problem this design exists to remove.

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Charter | Amend the "never proxy" Non-Goal, narrowly, for Telegram payloads only | The alternative — a second service — would duplicate behelink's registration, auth, rate limiting, and deploy for one endpoint pair. Amending honestly and documenting it beats a technically-pure workaround. |
| Delivery model | Store-and-forward with explicit ack, **not** synchronous proxy | Telegram needs a fast `200` and retries only on non-2xx. A synchronous proxy would drop every update that arrives while behetask restarts. |
| Durability | SQLite, same DB as `links` | Once behelink answers `200`, Telegram never redelivers. An in-memory queue would silently lose a user's command on restart. This deliberately **diverges** from `PendingConnectStore`, which is in-memory and disposable by design. |
| Ack semantics | At-least-once; `updates` does not delete, `ack` deletes | Exactly-once is unavailable across a network boundary. At-least-once with an idempotent-enough consumer is the honest, achievable contract. |
| Queue full | `503` to Telegram | Telegram retries on non-2xx. Backpressure preserves updates; silently dropping them would recreate the invisible-failure mode this design exists to kill. |
| Ingress credential | Unguessable path segment (`path_token`), not a bearer header | Telegram's webhook sender does not send arbitrary auth headers. The URL itself must carry the credential. |
| Bot-token custody | behelink stores `X-Telegram-Bot-Api-Secret-Token` opaquely; behetask validates it | Keeps end-to-end authenticity: a behelink compromise cannot forge updates that behetask will accept. |
| Opt-in | Lazy, via `:enableTelegramRelay` | Leaves `POST /v1/links`'s existing response contract untouched, so shipped behetask versions keep working unchanged. |
| Long-poll mechanics | Reuse `PendingConnectStore.wait()`'s clear-then-re-peek pattern | That code documents the exact lost-wakeup race it closes. Reimplementing it differently would reintroduce the bug. |

## Architecture

```
  Telegram
     │  POST https://link.behemotion.com/v1/tg/{link_id}/{path_token}
     ▼
  behelink (public, TLS 443)
     │  validate path_token → commit row → 200
     ▼
  telegram_updates (SQLite, FIFO per link, delete-on-ack, TTL swept)
     ▲                                   ▲
     │ GET  /v1/links/{id}/telegram/updates?wait=N   (long-poll, Bearer owner_token)
     │ POST /v1/links/{id}/telegram/ack {through_seq}
     │
  behetask-server (behind NAT — all connections outbound)
     │  validate X-Telegram-Bot-Api-Secret-Token, dispatch handle_update
     └─ replies go direct to api.telegram.org (outbound, unchanged)
```

The inbound leg is still a long-poll, but to behelink rather than to Telegram. The difference that
matters: **behelink enforces a single consumer** (`owner_token`-scoped) and owns the queue, so the
duplicate-consumer class is structurally impossible rather than silently tolerated. A second
consumer gets `401`, not a share of the updates.

## API

All endpoints under bare `/v1`; errors are RFC 9457 `application/problem+json`.

### Opt-in and token management — Bearer `owner_token`

| Endpoint | Result |
|---|---|
| `POST /v1/links/{id}:enableTelegramRelay` | `200 {webhook_url, path_token}` — idempotent; returns the existing token if already enabled |
| `POST /v1/links/{id}:rotateWebhookPathToken` | `200 {webhook_url, path_token}` — old token invalidated immediately |
| `POST /v1/links/{id}:disableTelegramRelay` | `204` — invalidates the token and purges any queued rows |

`webhook_url` is the fully-formed absolute URL for behetask to hand to `setWebhook`, derived from
`BEHELINK_PUBLIC_BASE_URL`, so a self-hosted behelink returns its own hostname without behetask
needing to know how to build it.

### Public ingress — Telegram-facing, no auth header

| Endpoint | Result |
|---|---|
| `POST /v1/tg/{link_id}/{path_token}` | `200 {"ok": true}` once committed |

- `404` — unknown link, relay not enabled, or bad `path_token`. **No distinction between these**,
  matching `GET /v1/links/{id}`'s existing design principle, so the endpoint cannot be used to
  enumerate link IDs.
- `413` — body exceeds `BEHELINK_TELEGRAM_MAX_PAYLOAD_BYTES`.
- `429` — over `BEHELINK_TELEGRAM_WEBHOOK_RATE_PER_MINUTE` (per link and per source IP).
- `503` — queue at `BEHELINK_TELEGRAM_QUEUE_MAX_DEPTH`. Telegram retries.

`path_token` is compared in constant time and never logged. The body is stored verbatim; behelink
does not parse, validate, or interpret Telegram's schema, so a Telegram API change cannot break
the relay.

### Consumer — Bearer `owner_token`

| Endpoint | Result |
|---|---|
| `GET /v1/links/{id}/telegram/updates?wait=N&limit=M` | `200 {updates:[{seq, received_at, secret_header, payload}]}`, or `204` when nothing arrives within `min(N, BEHELINK_TELEGRAM_UPDATES_WAIT_MAX_SECONDS)` |
| `POST /v1/links/{id}/telegram/ack` `{through_seq}` | `204` — deletes all rows for this link with `seq <= through_seq` |
| `GET /v1/links/{id}/telegram` | `200 {enabled, depth, oldest_seq, last_enqueue_at, dropped_total}` |

`updates` returns rows in `seq` order without deleting them; redelivery before ack is expected and
correct. `limit` is clamped to `BEHELINK_TELEGRAM_UPDATES_MAX_BATCH`.

`GET /v1/links/{id}/telegram` exists for operator diagnosis — it is what turns "the bot is quiet"
into a one-request answer: a non-zero `depth` with a stale `last_enqueue_at` means behetask is not
consuming; `depth` at zero with a recent `last_enqueue_at` means it is.

## Data Model

One new table, in behelink's existing SQLite database:

```sql
CREATE TABLE telegram_updates (
  seq           INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id       TEXT    NOT NULL,
  payload       BLOB    NOT NULL,
  secret_header TEXT,
  received_at   REAL    NOT NULL
);
CREATE INDEX telegram_updates_link_seq   ON telegram_updates (link_id, seq);
CREATE INDEX telegram_updates_received_at ON telegram_updates (received_at);
```

`seq` is a **globally** monotonic `AUTOINCREMENT` rowid, not a per-link counter. Per-link ordering
is the natural subsequence of a globally increasing sequence, which is all the consumer needs, and
`AUTOINCREMENT` guarantees a value is never reused even after an ack empties the table.

The rejected alternative was a per-link `COALESCE(MAX(seq), 0) + 1`. It reads nicer (each link
starts at 1) but allocates outside any lock, so two concurrent ingress requests for the same link
compute the same `seq` and one dies on the primary key. Letting SQLite allocate removes the race
rather than papering over it with a retry loop.

Three columns are added to `links`:

| Column | Purpose |
|---|---|
| `telegram_path_token_hash` | Ingress credential, stored hashed |
| `telegram_relay_enabled_at` | Opt-in timestamp; `NULL` means relay disabled |
| `telegram_dropped_total` | Cumulative count of rows expired unacked |

The token is stored **hashed**, not in cleartext — it is a credential, and it is shown once at
mint time exactly like `owner_token` and `resolve_token`.

A periodic sweep deletes rows older than `BEHELINK_TELEGRAM_QUEUE_TTL_SECONDS` and adds the number
deleted to `links.telegram_dropped_total`, so silent expiry is still reportable through
`GET /v1/links/{id}/telegram`. A `503` from a full queue is **not** counted as dropped — Telegram
retries it, so nothing was lost.

## Security Considerations

- **Bot token custody: none.** behelink never sees the bot token and never calls `setWebhook`.
  behetask registers its own webhook. A fully compromised behelink cannot take over a bot.
- **End-to-end authenticity.** behetask sets a `secret_token` on `setWebhook`; Telegram sends it
  back in `X-Telegram-Bot-Api-Secret-Token` on every delivery. behelink stores it opaquely and
  behetask validates it. Forged updates injected at behelink fail that check.
- **Enumeration.** The ingress returns an indistinguishable `404` for unknown link, disabled
  relay, and bad token, and is rate-limited per IP.
- **Credential handling.** `path_token` is high-entropy, hashed at rest, constant-time compared,
  rotatable, and excluded from logs and error bodies.
- **Denial of service.** Per-link and per-IP rate limits, a hard payload cap, and a bounded queue
  depth that sheds load with `503` rather than growing without limit.

### The privacy trade-off, stated plainly

Store-and-forward means **clients' Telegram message text physically transits and is briefly stored
on BEHEMOTION infrastructure**. This is the one materially new exposure in this design, and it is a
real change from behelink's original minimal-blast-radius posture.

Mitigations: TLS in transit; rows deleted on ack (typically within seconds); TTL sweep bounds worst
case; opt-in per link; and behelink is self-hostable, so an operator who finds this unacceptable
runs their own instance and BEHEMOTION sees nothing.

**Not mitigated in v1:** payloads are stored unencrypted, so a behelink operator or anyone with
database access can read queued task text. Encrypting payloads to a behetask-held key is the
obvious follow-up and is deliberately deferred rather than half-done here.

## Configuration

Env vars, `BEHELINK_` prefix, added to `config.py`:

| Var | Default | Meaning |
|---|---|---|
| `TELEGRAM_RELAY_ENABLED` | `True` | Master switch for the whole feature |
| `PUBLIC_BASE_URL` | `https://link.behemotion.com` | Base for the returned `webhook_url` |
| `TELEGRAM_QUEUE_MAX_DEPTH` | `1000` | Per-link depth before `503` |
| `TELEGRAM_QUEUE_TTL_SECONDS` | `3600` | Unacked row lifetime |
| `TELEGRAM_UPDATES_WAIT_MAX_SECONDS` | `25.0` | Long-poll ceiling |
| `TELEGRAM_UPDATES_MAX_BATCH` | `100` | `limit` clamp |
| `TELEGRAM_WEBHOOK_RATE_PER_MINUTE` | `120` | Ingress rate limit |
| `TELEGRAM_MAX_PAYLOAD_BYTES` | `1048576` | Ingress body cap |

## Testing

One test file per module, matching behelink's existing layout:

- `test_telegram_queue_store.py` — FIFO ordering; `seq` monotonicity; ack deletes through `seq`
  and no further; TTL sweep increments `dropped_total`; **durability across a store restart**
  (the property that distinguishes this from `PendingConnectStore`); max-depth rejection.
- `test_telegram_ingress_endpoint.py` — happy path commits and returns `200`; unknown link,
  disabled relay, and bad token are byte-identical `404`s; oversized body `413`; rate limit `429`;
  full queue `503`; `secret_header` round-trips verbatim; body stored byte-exact including
  payloads behelink cannot parse.
- `test_telegram_updates_endpoint.py` — long-poll returns immediately when rows are queued;
  `204` after the wait ceiling; `limit` clamping; wrong/missing `owner_token` → `401`;
  redelivery before ack; no redelivery after ack; the clear-then-re-peek race (enqueue landing
  between peek and wait is not lost).
- `test_telegram_enable.py` — mint is idempotent; rotation invalidates the old token immediately;
  disable purges queued rows; token is never returned in cleartext after mint.

## Documentation Obligations

These say something that this design makes false, and must change in the same delivery:

1. `docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md` — Non-Goals: "No data-plane
   relaying/proxying". Amend with a pointer to this spec.
2. `behelink/README.md` — "it never proxies task-server traffic (no data-plane relaying...)".
3. behetask's `docs/network-reachability.md` — Mode D: "behelink itself never proxies your actual
   task-server traffic, only the tiny signaling exchange".
4. The umbrella `README.md` catalogue / `HANDOFF.md` one-liner describing behelink as pure
   rendezvous.

## Follow-ups (explicitly out of scope here)

- Payload encryption at rest, keyed to the behetask instance.
- behetask-side consumer transport (`BEHETASK_TELEGRAM_TRANSPORT=relay`), webhook-preferred /
  polling-fallback selection, and drift detection against `getWebhookInfo` — a separate spec in
  the behetask repo, written against this contract once frozen.
- behetask's poller observability and self-heal hardening, which remains necessary because polling
  stays the fallback for deployments that cannot reach a relay at all.
