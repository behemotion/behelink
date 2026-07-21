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

## Status

Design phase — implementation not started.
