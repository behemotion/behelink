# Handoff reply: Telegram webhook relay — built, deployed, live

**From:** behelink
**To:** behetask
**Date:** 2026-07-28
**Status:** Closed — live on `https://link.behemotion.com`. behetask Tasks 7–9 are unblocked.
**Answers:** `behetask/docs/handoffs/to-behelink/TELEGRAM-WEBHOOK-RELAY-REQUEST.md`

## The charter amendment is accepted

behelink now has a narrow inbound data plane for Telegram update payloads, opt-in per link.
`README.md` and the 2026-07-21 spec's Non-Goals have been amended rather than quietly left false.
General task-server proxying stays out of scope — the CLI data path is still direct or hole-punched.

## The two decisions you asked us to rule on

**Payload privacy — shipping v1 unencrypted, as specced.** Rows are deleted on ack (seconds in
practice), the TTL sweep bounds the worst case, the relay is opt-in per link, and behelink is
self-hostable for anyone who won't accept the exposure. Encryption at rest keyed to the behetask
instance stays a named follow-up, not a silent gap: it is now written into `README.md`'s security
notes in as many words, so nobody discovers it by reading the schema.

**Enable idempotency — keeping hash-only plus the in-process cache.** The `path_token` stays a
write-once credential like `owner_token` and `resolve_token`. **Do not drop your re-register step.**
We verified the behaviour against the live service rather than assuming it: after a behelink
restart, `:enableTelegramRelay` returns a *new* `path_token`, the previous `webhook_url` answers
`404`, and the new one answers `200`. Treat `:enableTelegramRelay` as authoritative on every
startup and `setWebhook` whatever comes back.

## The contract is frozen exactly as you specified

All seven endpoints, unchanged from your request — including `updates` entries as
`{seq, received_at, secret_header, payload}` with `payload` as parsed JSON. `received_at` is an
ISO-8601 `Z` string. All five properties you depend on hold, and each has a test:

1. **Durable before `200`** — the row is committed before Telegram is answered. Verified beyond the
   test suite: an update was committed, the process killed, and the row still served after restart.
2. **`503` when full**, with `Retry-After: 10`. Never a `200`.
3. **At-least-once** — `updates` does not delete; `ack` does; redelivery before ack is tested both
   ways.
4. **Single consumer** — only `owner_token` reads the queue. A resolve token or another link's owner
   token gets `404`; a missing header gets `401`.
5. **behelink never holds the bot token** — `X-Telegram-Bot-Api-Secret-Token` is stored opaquely and
   round-trips verbatim.

## Three things we added beyond the spec — none change the contract

- **Rate limiting is per link *and* per source IP.** The spec called for both; the plan implemented
  only per-link. Per-link alone lets one sender flood across many links.
- **The master switch gates enable/rotate, not just the ingress.** With
  `BEHELINK_TELEGRAM_RELAY_ENABLED=false`, minting would otherwise hand back a `webhook_url` that is
  dead on arrival; it now `404`s at enable time.
- **The long-poll waiter re-peeks the durable queue after clearing its event.** As planned, a
  `notify` landing between the consumer's read and the waiter's `clear()` was erased, stranding a
  committed update for the full 25s wait. Latency, not loss — but it is closed.

## Three things you should handle on your side

1. **Re-register the webhook on every startup.** See above. This is the accepted cost of hashing the
   token at rest, and it is confirmed live, not theoretical.
2. **`payload` must be valid JSON or `GET .../telegram/updates` fails for the whole batch.** behelink
   stores the body byte-exact and does not parse it at ingress — deliberately, so a Telegram schema
   change can't break the relay — but the consumer endpoint `json.loads` it. Telegram only ever sends
   JSON, so this is an edge, not a live risk. We left it rather than invent an error shape inside a
   contract you had already frozen. If you would rather have `payload: null` or a documented
   `problem+json` for an undecodable row, say so and we will add it.
3. **Updates larger than 16 KB used to be rejected at the edge.** Fixed before you build against it —
   see below. Nothing for you to do, but worth knowing it was a real wedge and not a hypothetical.

## Deployment

Live on `https://link.behemotion.com` since 2026-07-28. Full runbook entry in `deploy.md`
("Telegram webhook relay"). Two things were more than a code push:

- **A live schema migration.** `ALTER TABLE links` for three columns plus the `telegram_updates`
  table, applied idempotently on connect. Backup taken first; all 3 pre-existing links survived.
- **A Caddy body-cap bug that would have wedged your webhook.** The edge proxy capped request bodies
  at 16 KB globally, but a legitimate 4096-character Telegram message of 4-byte glyphs (emoji, CJK)
  is ~49 KB — measured, not estimated. Telegram retries non-2xx, so those updates would have retried
  forever with `pending_update_count` climbing while the bot looked perfectly healthy: exactly the
  invisible-failure class this design exists to eliminate, re-entering through the proxy. `/v1/tg/*`
  is now capped at 2 MB and everything else stays at 16 KB, so behelink's own
  `TELEGRAM_MAX_PAYLOAD_BYTES` (1 MiB) is the binding limit with a proper RFC 9457 body.

  Worth flagging for your own e2e work: the pytest suite talks to the ASGI app directly and never
  traverses Caddy, so it was green throughout while production was broken. Only a `curl` against the
  public hostname caught it.

E2E verified against the public hostname: register → enable → ingress as Telegram (with the secret
header) → status → updates → ack → disable → deregister, plus the body-size boundaries above. Live
state is back to 3 links, 0 queued updates, 0 relays enabled.

## Still yours

The docs listed as Task 10 of your plan: `behetask/docs/network-reachability.md` and the umbrella
`README.md` / `HANDOFF.md`, all of which still describe behelink as pure rendezvous.
