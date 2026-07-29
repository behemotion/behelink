# Handoff: NAT hole-punch signaling — a proposed contract addition to behelink

**From:** behetask
**To:** behelink
**Date:** 2026-07-21
**Status:** Open

## Context

behetask's Mode D (hosted relay) currently uses behelink exactly as shipped: pure
address-rendezvous via `POST /v1/links` / `PATCH /v1/links/{id}` / `GET /v1/links/{id}`. That
design deliberately never proxies traffic, but it does still require the behetask-server operator
to configure port-forwarding on their own router — behelink only ever answers "where is server X
reachable right now," not "how do I get in without opening anything."

A direct product decision on the behetask side now wants Mode D to work with **zero
port-forwarding**, while keeping behelink's two existing properties intact: it must **never carry
actual behetask API traffic**, and it must **not become resource-heavy** (no bandwidth- or
CPU-proportional cost as behetask usage grows). The only way to get "no port-forwarding" without
turning behelink into a data-plane relay is NAT hole-punching: behelink brokers a handshake so the
CLI and behetask-server connect **directly** to each other, then behelink is out of the picture —
the same shape WebRTC/ICE and Tailscale use, just for two parties with no relay-of-last-resort
fallback (that fallback was explicitly ruled out — see Open questions).

Full design writeup, including why this is scoped as hole-punching specifically (and the QUIC/
HTTP-3 choice for the resulting data plane, which is entirely a behetask-side concern):
`behetask/docs/superpowers/specs/2026-07-21-behelink-holepunch-mode-d-design.md`. This handoff is
the subset of that design that's actually behelink's to build or push back on.

This is a **design proposal, not yet an implementation plan** — nothing has been built on the
behetask side either. The point of sending this now, before behetask writes an implementation
plan, is so behelink's owner can react to the contract shape (and any operational implications on
the live, hardened `link.behemotion.com` box) before either side commits code.

## What's needed

behelink would need two new capabilities, additive to (not replacing) the existing `/v1/links`
CRUD:

1. **A UDP reflector** — a minimal STUN-alike. Any inbound UDP probe gets a reply containing the
   observed `(ip, port)` of the sender. No RFC 5389 STUN wire-format compliance needed — only
   behetask-server and behetask-cli ever speak to it, so the wire shape is behelink's choice.
   Needed because NAT mappings are per-socket: each side has to self-probe *from the exact socket*
   it will later use to punch, to learn its real externally-mapped address for that socket.

2. **`POST /v1/links/{id}:requestConnect`** (Bearer: `resolve_token`) — the CLI submits its own
   candidate `{ip, port}`; behelink stores it as a short-TTL (a few seconds — this is only relevant
   for one hole-punch attempt, not a durable record) "pending connect," and the response includes
   the link's current server-side candidate in the same round trip (so the CLI doesn't need a
   separate resolve call first).

3. **`GET /v1/links/{id}/pending-connect?wait=N`** (Bearer: `owner_token`) — long-poll: return
   immediately if a pending-connect record already exists for this link, otherwise hold the request
   open up to `wait` seconds. This is what lets behetask-server learn "someone wants to connect"
   with sub-heartbeat-interval latency instead of waiting up to ~60s. (behetask-server already has
   an identical long-poll pattern against Telegram's `getUpdates` —
   `behetask-server/src/behetask_server/integrations/telegram/polling.py` — happy to share that
   code as a reference for the shape, not the wire format.)

4. **Semantic shift, not a schema change necessarily:** the `ip`/`port` a link already stores via
   the existing register/heartbeat calls would now represent a NAT-observed *candidate* (learned via
   self-STUN against #1) rather than a bare declared port. Whether that needs new columns or just a
   documentation update to the existing ones is behelink's call.

## Affected code / surfaces

- `src/behelink/main.py` — where `register`/`heartbeat`/`resolve`/`deregister`/
  `rotate_resolve_token` currently live; the two new endpoints would sit alongside these.
- `src/behelink/db.py` — the `links` table; may need new columns depending on how #4 above is
  resolved, plus a new (or reused) table for short-TTL pending-connect records.
- `src/behelink/ratelimit.py` — the existing `RateLimiter` only covers `POST /v1/links`
  (registration). A UDP echo-on-request service is a classic amplification-abuse shape if exposed
  to arbitrary third parties without its own limiting — flagged, not designed, in the behetask spec
  (see its "Open Implementation Flags").
- `deploy.md` — the live box's `ufw` currently allows only `22/tcp` (rate-limited), `80/tcp`,
  `443/tcp+udp`. A UDP reflector needs its own allowed port, and Caddy's `reverse_proxy
  127.0.0.1:47150` fronts HTTP/TCP only — a raw UDP listener would need a separate bound port,
  not something routed through the existing Caddy vhost.

## Verification

None yet — this is a proposal, not a change to verify. Once behelink's owner has reacted to the
contract shape, the next concrete step is behetask writing an implementation plan (which will
include specific test/live-verification steps on both sides) — that plan will reference whatever
you land on here.

## Open questions

- **Port/exposure for the UDP reflector:** same port as the existing HTTP listener (47150, but UDP)
  or a distinct one? Given Caddy doesn't proxy raw UDP the way this needs, does behelink's owner
  want it directly `ufw`-exposed, and does that change anything about the current hardening
  posture worth re-reviewing?
- **Abuse/rate-limiting on the reflector:** should behelink build this in, or should behetask
  constrain probe volume from the client side instead (e.g. capping retries per connect attempt)?
- **Does behelink's owner want this role at all?** It's still never proxying real behetask traffic
  or seeing task data, but it is a bigger, statefully-interactive surface (a live long-poll, a UDP
  listener) than the current "small stateless-ish HTTP CRUD store." Worth an explicit yes/no before
  behetask plans further, not assumed from this handoff alone.
- **Additive `/v1/` vs. a new version:** the behetask spec assumed these are additive under the
  existing `/v1/links` resource, not a breaking `/v2/` — confirm that's fine, or push back if
  behelink's own versioning conventions want otherwise.
- **Hard non-goal, stated for alignment, not up for negotiation from the behetask side:** if
  hole-punching fails (symmetric NAT/CGNAT on either end), behetask wants a clear failure back to
  the operator — never a fallback where behelink starts relaying actual traffic. Flagging this
  explicitly so it's clear behelink is never being asked to become a TURN-style relay later "just
  for the hard cases."

---

— behetask team, 2026-07-21
