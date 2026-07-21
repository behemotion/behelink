# behelink Service Handoff — 2026-07-21

**Plan:** `docs/superpowers/plans/2026-07-21-behelink-service.md`
**Spec:** `docs/superpowers/specs/2026-07-21-behelink-hosted-relay-design.md`

## State

- Repo/branch: `behelink`, `main` (no worktree — plan was executed directly on main per the plan's Global Constraints)
- Last commit: `d7a3d99` "docs: link.behemotion.com live with auto-TLS; record HTTPS verification"
- Uncommitted changes: none (`git status --short` clean)

## Progress

**The behelink v1 plan is fully done and deployed — not mid-flight.** This
handoff exists because the *next* work (behetask-server/CLI integration,
umbrella registry) is unplanned follow-up in sibling repos, not a resume
point in this plan.

Note: the plan file's own checkboxes (`docs/superpowers/plans/2026-07-21-behelink-service.md`)
were never checked off during execution — don't trust them. Verified-actual
state below.

- [x] Task 1–9 of the plan (scaffolding through entrypoint/docs) — all
  committed individually, commits `f5e966e` through `14a6e6a`
- [x] All 34 pytest tests pass (`uv run pytest` from the repo root, or on
  the server: `cd ~/apps/behelink && ~/.local/bin/uv run pytest`)
- [x] Deployed to production: `https://link.behemotion.com` →
  `46.17.103.230` (Hostkey `hostkey82312`), systemd user unit
  `behelink.service`, Caddy vhost with Let's Encrypt auto-TLS (obtained
  2026-07-21, auto-renews)
- [x] Live end-to-end verification, 14/14 checks, run twice (plain HTTP
  before the domain landed, then again over HTTPS): register → duplicate
  409 → heartbeat with correct observed public IP → resolve → 401/404 auth
  semantics → token rotation → delete → port 47150 confirmed closed
  externally → `X-Forwarded-For` spoofing confirmed ignored
- [x] `deploy.md`, `README.md`, `HARNESS-DIVERGENCES.md` all reflect the
  real deployed state (host, URL, TLS, ops commands)

## Decisions Made (not in the plan)

- Server had no `rsync` or Caddy preinstalled (clean Ubuntu 26.04 box per
  the access package's "clean slate" note) — installed both via `apt-get`
  before the plan's Task 9 smoke test could apply to production.
- Deploy mechanism: `rsync` + `uv sync` + systemd **user** unit (linger
  enabled) rather than the Podman route the spec sketches as "the same way
  behetask does today" — simpler for a single-service box, no container
  runtime was preinstalled and the spec's Open Implementation Flags didn't
  pin this down. Revisit if the harness later wants podman parity.
- Caddy went through two states: plain `:80` first (server had only an IP,
  no domain), then switched to the `link.behemotion.com` vhost once DNS
  landed this session, which triggered ACME auto-TLS. Verified the links
  table was empty before the switch, so no token rotation was needed.

## Gotchas Discovered

- SSH access lives at `~/Srv/hostkey/behelink-access/` (README, keypair,
  known_hosts, ssh_config) — **not** in the behelink repo, never commit it.
  `ssh -i build_ed25519 -o UserKnownHostsFile=known_hosts build@46.17.103.230`,
  or install `ssh_config`'s `Host behelink` block.
- `build` user has passwordless sudo; `sudo -n true` confirms without a
  password prompt.
- uvicorn's `forwarded_allow_ips="127.0.0.1"` (set in
  `src/behelink/__main__.py`) is what makes the observed-IP capture work
  correctly behind Caddy — verified live that a spoofed
  `X-Forwarded-For` from an external client is ignored, only Caddy's own
  header is trusted.

## Resume Instructions

The behelink service itself needs nothing further. Three follow-ups remain,
all explicitly flagged as out-of-scope-for-this-spec in the design doc's
"Open Follow-Ups" section — none has a plan yet, so **the next session's
first move for whichever it picks up should be `superpowers:writing-plans`
(or `superpowers:brainstorming` first if scope is unclear), not jumping
straight to code**:

1. **behetask-server: registration/heartbeat background task.** Repo:
   `/Users/alexandr/Repo/BEHEMOTION/behetask/behetask-server`. New settings
   `BEHETASK_RELAY_URL` (default `https://link.behemotion.com`),
   `BEHETASK_RELAY_ID`, `BEHETASK_RELAY_OWNER_TOKEN` — same `.env` pattern
   as existing `BEHETASK_PUBLIC_URL`. New background task alongside the
   Telegram poller (`src/behetask_server/integrations/telegram/polling.py`
   and `integrations/base.py` are the pattern to mirror for retry/backoff
   shape) — registers idempotently (`POST` then `PATCH` on `409`) and
   heartbeats on an interval against behelink's `POST/PATCH /v1/links`.
   Must not block startup if behelink is unreachable (spec's Error Handling
   section). Also: `behetask network setup` wizard gets a fourth mode
   (design doc references `2026-07-11-network-setup-wizard-design.md`).
2. **behetask-cli: resolve-before-connect step.** Same repo,
   `behetask-cli/`. `--server` resolution gains a source: saved `relay_id`
   + `relay_resolve_token` from `behetask login`'s config, resolved via
   `GET /v1/links/{id}` immediately before connecting (not cached). Also
   `GET /cli/install.sh` should bake in relay ID + resolve_token.
3. **Umbrella: port registry entry.** `/Users/alexandr/Repo/BEHEMOTION` is
   its own repo (root `CLAUDE.md`, `docs/CONVENTIONS.md`). §7's port table
   doesn't list behelink/47150 yet — confirmed still missing as of this
   session. This is a `/handoff BEHEMOTION`-routed edit to
   `docs/CONVENTIONS.md` §7 (and `docs/HARNESS-PLAN.md` if phase tracking
   needs it), not something to fork locally into behelink's
   `HARNESS-DIVERGENCES.md` (which already documents it as an open,
   umbrella-owned gap).

No single one of these blocks the others. behetask-server's task (#1) is
the natural first pick since #2 (CLI resolve) has nothing to resolve
against until a server is actually registering with behelink — but #3 is
by far the smallest and could be knocked out first if you want an easy win
before the bigger cross-repo work.
