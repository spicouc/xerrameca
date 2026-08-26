# Xerrameca v1.1.0 — Release Notes

## Overview

Release estable de la línia UX/Telegram sobre protocol federat v1.

## Certified release candidate

- Certified RC tag: v1.1.0rc1
- Certified RC source commit: `e7f8006dededb4d31b791f40decaa34a919ad69a`
- Physical RC certification: PASS
- Certified physical conversation: `xfc_13f960cee0ed47ba851fa29ce1065055`

## What's new

- `/xerrameca` universal UX entry
- wizard de creació de converses
- presets/tipus de diàleg implementats
- peer selection
- roles / rounds / output wizard flow
- Telegram Update dispatcher
- callback ACK
- bounded in-process dedup
- allowed chat filtering
- Telegram Bot API long polling
- durable monotonic offset
- singleton polling runner
- safe restart/recovery behavior
- explicit node-base-url wiring
- node-port propagation fix
- START idempotency
- federated conversation view via Telegram

## Physical certification

- Telegram real end-to-end: PASS
- exactly 1 conversation created by START
- 2 participants
- endpoint 8991
- production 8891 not used
- terminal COMPLETED
- bidirectional replication: PASS
- turn ordering: PASS
- signature failures: 0
- duplicates: 0
- sequence gaps: 0
- invalid epochs: 0
- fencing violations: 0
- A/B canonical digest: MATCH
- Telegram runner restart: PASS
- node A restart: PASS
- peer B offline/rejoin: PASS
- catch-up / convergence: PASS
- SQLite quick_check: ok A/B
- secret exposure: NONE

## Protocol

- federated protocol remains v1
- exactly 2 participants
- signed append-only events
- coordinator_epoch fencing
- idempotent sync
- Pluribus not required

## Known non-blocking findings

- RACE-1 HTTP status semantics remain observationally ambiguous: auth and some state rejections share HTTP 403, with the remote participant path able to surface 503.
- Consistency is unaffected.
- No patch required for v1.1.0.

Note: the Telegram `Mode -> summary` button is a local UI preference and does not alter the federated protocol. It does not imply persistence of the wizard `Output -> Summary` selection; they are independent concepts.

## Deployment note

Production deployment requires migration of the legacy production runtime to an immutable dedicated runtime before v1.1.0 is deployed. This migration is not yet complete.
