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

2. **New public, directly-exposed UDP port (not Caddy-fronted).** The UDP self-STUN reflector
   (`src/behelink/reflector.py`) binds `BEHELINK_REFLECTOR_HOST:BEHELINK_REFLECTOR_PORT` (default
   `0.0.0.0:47151`) directly — Caddy can't proxy raw UDP the way this needs, so unlike every other
   public surface today, this listener answers straight from the OS socket. Design and the
   behelink-owner sign-off on this trade-off:
   `docs/superpowers/specs/2026-07-21-behelink-holepunch-signaling-design.md` (Security
   Considerations). Mitigations: bearer-token-gated probes (no anonymous reflection), a
   per-source-IP rate limiter, and a request/reply size ratio that defeats classic UDP
   amplification abuse by construction.

## Conformance notes (not divergences)

- Port registry (§7): 47150 registered in the umbrella's `docs/CONVENTIONS.md`
  §7 and indexed in `docs/HARNESS-PLAN.md`'s per-repo table (fixed 2026-07-21).

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
