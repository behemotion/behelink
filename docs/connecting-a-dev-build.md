# Connecting a dev build to the production relay

How to point a **client you're developing** (a behetask-server instance, a
one-off script, or anything else that needs to register/resolve a rendezvous
link) at the **production** behelink instance instead of running your own
throwaway copy.

Production: **https://link.behemotion.com** — see [`DEPLOY.md`](../DEPLOY.md)
for how it's hosted. This doc is about being a *client* of it, not operating
it.

## Should you use production, or run behelink locally?

| | Use production (`link.behemotion.com`) | Run behelink locally (`uv run behelink`) |
|---|---|---|
| Testing your client against a real public IP / NAT | ✅ this is what production is for | ❌ resolves to `127.0.0.1`, not useful |
| Iterating on behelink itself | ❌ | ✅ |
| CI / automated tests | ❌ — don't hit prod from CI; it's rate-limited (10 registrations/IP/hour) and single-instance | ✅ spin up a local instance per test run |
| One-off manual smoke test of your integration | ✅ | either works |

Default to production whenever you're testing that *your* client correctly
registers, heartbeats, and gets resolved from somewhere else — that's the
scenario production is verified for (see `DEPLOY.md`'s Verify
section). Reach for a local instance only when working on behelink's own
code, or in automated tests where hitting a shared public service would be
wrong regardless of correctness.

## The API, from a client's perspective

No auth to register; bearer tokens for everything else. Full contract is in
the [README](../README.md#api) — summarized here for the connect flow:

```
POST   /v1/links            {id, port}   →  201 {owner_token, resolve_token}   (once, never shown again)
PATCH  /v1/links/{id}       {port?}      →  200 {ip, port, last_seen}          (heartbeat, Bearer owner_token)
GET    /v1/links/{id}                    →  200 {ip, port, last_seen}          (resolve, Bearer resolve_token)
DELETE /v1/links/{id}                    →  204                                (Bearer owner_token)
POST   /v1/links/{id}:rotateResolveToken →  200 {resolve_token}                (Bearer owner_token)
```

- `id`: 3–63 chars, lowercase letters/digits with inner hyphens (e.g.
  `myproject-dev-alex`). Pick something unlikely to collide with someone
  else's dev build — `POST` 409s if it's taken.
- `owner_token` and `resolve_token` are shown exactly once, at registration.
  Store them like secrets (env vars / local config, never committed). If you
  lose the `owner_token` you cannot recover or delete that link — register a
  new `id` instead.
- A wrong or unknown-id `Authorization: Bearer` gets `404`, not `401` — by
  design, so probers can't distinguish "not yours" from "doesn't exist".
  `401` only means the header itself was missing or malformed.
- Links go stale after `BEHELINK_HEARTBEAT_TTL_SECONDS` (180s in production)
  without a heartbeat. Heartbeat on an interval comfortably under that —
  every 60s is the reference cadence used in behelink's own tests.

## Manual walkthrough (curl)

This is the same sequence used to live-verify production; useful for
smoke-testing a dev build by hand before wiring up real client code.

```sh
ID="myproject-dev-$(whoami)"        # pick something namespaced to you
MY_PORT=9999                        # the port your dev build actually listens on

# 1. Register
curl -s -X POST https://link.behemotion.com/v1/links \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"$ID\",\"port\":$MY_PORT}"
# → {"owner_token":"blo_...","resolve_token":"blr_..."}
# Save both tokens now — this is the only time they're returned.

OWNER_TOKEN="blo_..."
RESOLVE_TOKEN="blr_..."

# 2. Resolve (this is what a remote CLI/consumer would do)
curl -s https://link.behemotion.com/v1/links/$ID \
  -H "Authorization: Bearer $RESOLVE_TOKEN"
# → {"ip":"<your real egress IP>","port":9999,"last_seen":"..."}

# 3. Heartbeat (run this on an interval < 180s while your dev build is up)
curl -s -X PATCH https://link.behemotion.com/v1/links/$ID \
  -H "Authorization: Bearer $OWNER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"port\":$MY_PORT}"
# → {"ip":"...","port":9999,"last_seen":"<refreshed>"}

# 4. Clean up when done — don't leave dev links registered indefinitely
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE https://link.behemotion.com/v1/links/$ID \
  -H "Authorization: Bearer $OWNER_TOKEN"
# → 204
```

A minimal heartbeat loop, if your dev build doesn't have one yet:

```sh
while true; do
  curl -s -X PATCH https://link.behemotion.com/v1/links/$ID \
    -H "Authorization: Bearer $OWNER_TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"port\":$MY_PORT}" > /dev/null
  sleep 60
done
```

## behetask-server / behetask-cli integration (not yet implemented)

If you're working in `behetask-server` or `behetask-cli`, note that the
register/heartbeat/resolve wiring described above is **planned but not yet
built** there — see
[`docs/superpowers/plans/handoffs/2026-07-21-behelink-service-handoff.md`](superpowers/plans/handoffs/2026-07-21-behelink-service-handoff.md)
for the exact follow-up scope:

- `behetask-server` will gain `BEHETASK_RELAY_URL` (default
  `https://link.behemotion.com`), `BEHETASK_RELAY_ID`, and
  `BEHETASK_RELAY_OWNER_TOKEN` settings plus a background task that
  registers idempotently and heartbeats.
- `behetask-cli` will gain a resolve-before-connect step using a saved
  `relay_id` + `relay_resolve_token`.

Until that lands, use the manual curl flow above to exercise the relay
against a `behetask-server` dev build you're testing.

## Verifying the relay itself is reachable

Before debugging your own client, confirm the relay is up:

```sh
curl https://link.behemotion.com/healthz
# → {"status":"ok"}
```

If this fails, the problem is the relay or your network, not your
integration — check `DEPLOY.md`'s Ops section (`journalctl --user -u
behelink`, `systemctl --user status behelink` on the host).
