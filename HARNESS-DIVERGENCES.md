# HARNESS-DIVERGENCES — behelink

Audited against the umbrella's [`docs/CONVENTIONS.md`](../docs/CONVENTIONS.md)
(2026-07-03 standard) at creation, 2026-07-21.

## Divergences

1. **Network posture (§4): deliberately public-facing.** Conventions assume a
   LAN-bound harness where only Caddy vhosts and the behemcp gateway face the
   network. behelink's entire purpose — NAT rendezvous for clients' servers —
   requires its Caddy vhost to be reachable from the public internet. This is
   the design spec's explicit, documented concession
   (`docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md`,
   Security Considerations). The listener itself still binds `127.0.0.1:47150`
   (`src/behelink/config.py:9-10`); only Caddy is exposed.

2. **Port registry (§7): 47150 not yet in the umbrella registry.** The spec
   assigns behelink port 47150 as the next open slot; the umbrella's
   `docs/CONVENTIONS.md` §7 / `docs/HARNESS-PLAN.md` registry hasn't been
   updated yet — umbrella-owned files, flagged for a `/handoff BEHEMOTION`
   rather than edited from here.

## Conformance notes (not divergences)

- Bare `/v1` prefix + AIP colon method (`:rotateResolveToken`) —
  `src/behelink/main.py`.
- RFC 9457 `application/problem+json` errors with underscore `type`
  vocabulary — `src/behelink/errors.py`.
- Bearer auth, 401 + `WWW-Authenticate: Bearer` on missing credentials,
  404-for-invisible-resources on wrong tokens (allowed by §4, documented in
  README), constant-time compares — `src/behelink/main.py`,
  `src/behelink/tokens.py:26`.
- `GET /healthz` unauthenticated, DB-ping only — `src/behelink/main.py`.
- `BEHELINK_` env prefix on every setting — `src/behelink/config.py`.
- `pyproject.toml` name = slug, Python ≥ 3.12, version single-sourced.
- No CLI ships in v1 (per spec), so the beheaxi CLI profile (§2) imposes no
  obligations yet; a future ops CLI must be beheaxi-based.
