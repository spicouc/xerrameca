# Xerrameca UX / Wizard / Telegram — Disseny i Contracte (Fase 0 + UX-1)

Baseline: v1.0.0 (a1c1c4e94acde657d1ce044778de11883e2)
Worktree desenvolupament: /opt/xerrameca-ux (branch feature/ux-wizard, separat dels nodes físics).

## Restriccions (no negociables)
- NO modificar el protocol federat v1.
- NO modificar la invariant de dos participants.
- NO introduir dependència de Telegram al core.
- NO introduir dependència de Pluribus.
- NO tocar failover / event log / signatures llevat que sigui estrictament necessari.
- NO inventar operacions que el core no suporta (ex: stop/cancel sense suport core).

## FASE 0 — AUDITORIA (GATE GREEN)
- Worktree creat a /opt/xerrameca-ux (HEAD = a1c1c4e...).
- Suite v1.0.0: 63 passed, 0 failed (GREEN).
- Contracte existent documentat (veure historial del Fase 0).

## UX-1 — SHARED COMMAND LAYER + LISTING + CLI

### XerramecaCommandService (transport-independent)
Fitxer: src/xerrameca/command/service.py
Cap dependència de Telegram, Pluribus o transport. Només coneix
l'API local del node (dialogue service + trust store).

Mètodes:
- list_agents(*, check_online=False) -> list[AgentChoice]
    Només peers trust_status == "trusted".
    Exposa: node_id, display_name, endpoint, trusted.
    online: bool | None (None si no es demana; si check_online=True,
    probe bounded a /health amb timeout 3.0s).
    Untrusted mai seleccionable.
- list_conversations(*, status=None, peer_node_id=None, limit=None)
    -> list[ConversationSummary]
    Reconstruït des de l'estat federat autoritatiu (events).
    NO manté segona font d'estat.
- get_conversation(conversation_id) -> dict
- sync_conversation(conversation_id) -> dict
    Llegeix local-agent-api-key del state-dir (mai per argv).
- create_conversation(*, peer_node_id, objective, max_rounds=5, delay_seconds=0) -> dict

### DTOs (src/xerrameca/command/dto.py)
AgentChoice, ConversationSummary, ConversationPreset, WizardButton,
WizardScreen, WizardAction, WizardSession. Cap camp de secret.

### Endpoint federat READ-ONLY
GET /v1/node/federation/conversations
- Autenticació local-agent igual que la resta d'API federada.
- Reconstruït des de federated_events (cap segona font).
- Només converses federades.
- Filtres: status, peer_node_id, limit.
- No toca protocol/event schema.

Implementació:
- EventStore.list_conversation_ids() (events.py) — SELECT DISTINCT
  conversation_id FROM federated_events.
- FederatedDialogueService.list_conversations() (dialogue.py) — wrapper.
- list_federated_conversations() (app.py) — endpoint GET.

### CLI federada (cli.py)
xerrameca conversation create  --state-dir --peer --objective --rounds --delay [--json]
xerrameca conversation status  --state-dir <id> [--json]
xerrameca conversation list    --state-dir [--status --peer --limit] [--json]
xerrameca conversation sync    --state-dir <id> [--json]
- Llegeix local-agent-api-key del state-dir (mai per argv).
- Output humà per defecte, --json opcional.
- NO implementa stop/cancel (el core no té operació segura).

### Discovery
- list_agents() només retorna trust_status == "trusted".
- Exposa node_id, display_name, endpoint, trusted.
- online/offline només si es determina amb probe bounded.
- Untrusted mai seleccionable.

## TESTS UX-1 (tests/test_ux1.py, 7 tests) — 7 passed
- test_list_agents_only_trusted
- test_list_agents_untrusted_not_selectable
- test_command_service_list_conversations_empty
- test_dto_no_secrets
- test_local_api_key_not_in_arguments
- test_create_status_list_sync
- test_list_endpoint_requires_auth

## REGRESSIÓ COMPLETA: 70 passed (63 baseline + 7 UX-1), 0 failed, 0 errors.

## GATES UX-1
- baseline regression: 63/63 PASS
- new UX-1 tests: 7/7 PASS
- protocol v1: UNCHANGED
- event schema: UNCHANGED
- Pluribus dependency: NONE
- Telegram: NOT modified
- physical nodes: NOT touched
- secret exposure: NONE

## NOTA STOP/CANCEL
No s'implementa /xerrameca stop ni cap cancel·lació perquè el core
federat no exposa una operació segura de stop/cancel. La UX no
inventa estat (principi heretat de TelegramUXAdapter).
