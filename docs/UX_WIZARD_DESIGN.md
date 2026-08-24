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


## UX-2 — WIZARD STATE MACHINE + PRESETS + ROLES + SESSIONS

### XerramecaWizardService (transport-independent)
Fitxer: src/xerrameca/command/wizard.py
Cap dependència de Telegram, Pluribus o transport concret.
Reutilitza XerramecaCommandService (no duplica accés al node API).

State machine (transicions explícites, VALID_TRANSITIONS):
ROOT -> SELECT_PEER -> SELECT_DIALOGUE_TYPE -> ENTER_OBJECTIVE
 -> SELECT_ROLE_A -> SELECT_ROLE_B -> SELECT_ROUNDS
 -> SELECT_OUTPUT_MODE -> CONFIRM -> STARTED
ROOT/qualsevol -> CANCELLED (via wizard:cancel)

### Presets (src/xerrameca/command/presets.py, dades no condicionals)
conversation, brainstorm, debate, critical_review, decision, task.
Cada preset: key, label, instruction, default_role_a, default_role_b,
completion_instruction. VALID_ROLES, VALID_ROUNDS (3/5/6/10),
VALID_OUTPUT_MODES (summary/live/silent).

### Effective objective (determinista, NO toca protocol v1)
Abans de START es construeix effective_objective a partir de:
[USER OBJECTIVE] + [DIALOGUE MODE] + [LOCAL AGENT ROLE] + [PEER AGENT ROLE]
 + [COMPLETION]. Mateixes entrades -> mateix text. user_objective es
conserva separat dins la WizardSession. Cap role field nou a l'event schema.

### Sessions locals (TTL + isolation)
- session_id opac (secrets.token_hex), caller_id, created_at, updated_at,
  expires_at, state, wizard selections.
- TTL configurable (default 900s). _purge_expired al require.
- owner/caller isolation: caller B no pot llegir/modificar/executar la
  sessió del caller A (test_session_ownership_isolation).
- NO escriu res al federated event log abans de START.

### Accions (action_id transport-independent)
root:new, peer:<node_id>, mode:<key>, objective:set, role_a:<role>,
role_b:<role>, rounds:<n>, output:<mode>, nav:back, wizard:cancel,
confirm:start. Cap secret a action_id.

### BACK
Navegació enrere coherent (reconstrueix la pantalla de l'estat anterior).
Només modifica estat local del wizard. No recrea conversa federada.

### CANCEL (UX-2)
Cancela LA SESSIÓ DEL WIZARD ABANS de START. NO cancel·la cap conversa
federada. Un cop STARTED, wizard:cancel no pot fingir aturada de runtime.

### IDEMPOTÈNCIA
confirm:start és idempotent: sessió STARTED recorda conversation_id i
tota crida posterior retorna la mateixa conversa (test_start_calls_
command_service_once: exactament 1 crida a create_conversation).

### STALE CALLBACK
Sessió expirada/cancel·lada/estat incompatible -> WizardError controlat.
Mai exception crua ni avanç silenciat.

### START
Només a CONFIRM: crida XerramecaCommandService.create_conversation(
peer_node_id, effective_objective, max_rounds). Estat=STARTED,
conversation_id=result.id. NO fa claim/reply (UX-2 només crea/configura).

### Tests UX-2 (tests/test_ux2.py, 22 tests) — 22 passed
root screen, new conversation, trusted/untrusted peer, preset, custom
objective, default/custom roles, round/output selection, confirm screen,
effective objective deterministic, back navigation, wizard cancel, TTL
expiry, stale callback, session isolation, double callback idempotency,
confirm:start idempotency, no duplicate conversations, no secrets in DTO/
action/session, command service reuse.

### Architecture gate UX-2
protocol v1 unchanged: YES
event schema unchanged: YES
signed events unchanged: YES
failover unchanged: YES
replication unchanged: YES
2-participant invariant: YES
Pluribus dependency: NONE
Telegram unchanged: YES
production nodes untouched: YES

### NO implementat encara (altres PRs)
Telegram buttons, python-telegram-bot, callback_query handlers, active
conversation screen, polling, watch, federated stop/cancel, multiparty,
metrics, selfcheck.


## UX-3 — TRANSPORT-NEUTRAL BUTTONS + TELEGRAM BRIDGE

### Model neutral de pantalles i botons (src/xerrameca/ui/neutral.py)
- `NeutralButton(label: str, callback_token: str)`: botó sense lligam de
  transport. El transport (Telegram) el renderitza com a inline keyboard.
- `NeutralScreen(text: str, buttons: list[NeutralButton], state: str)`:
  pantalla neutral. `to_dict()` exposa text/state/buttons.
- El model és transport-independent: cap import de telegram/aiogram/telebot.

### CallbackStore (tokens curts, opacs i segurs)
- `mint(*, caller_id, session_id, action_id) -> str`: genera token opac
  (`base64.urlsafe_b64encode(os.urandom(9))`, sense padding) — curt i dins
  del límit de 64 bytes de callback_data de Telegram.
- Token vinculat a `(caller_id, session_id, action_id)` + `expires_at`.
- `resolve(token, *, caller_id) -> (action_id, session_id)`:
  rebutja tokens desconeguts, expirats o d'un altre caller (`CallbackError`).
- `size_ok(token) -> bool`: `len(token) <= 64`.
- **Cap secret dins callback_data**: mai conté API key, private key, token,
  objective ni node_id. Només un handle opac a la taula interna.
- TTL configurable (default 900s). Purga automàtica en mint/resolve.

### TelegramWizardBridge (src/xerrameca/ui/telegram_wizard.py)
- Cap state machine, presets, transicions, TTL ni idempotència pròpies:
  tot resideix a `XerramecaWizardService`.
- `start(caller_id) -> NeutralScreen`: crea sessió i rendeix pantalla ROOT.
- `_render(session_id, caller_id, screen)`: per cada `WizardButton` del
  wizard emet un `NeutralButton` amb un token opac (mint).
- `handle_callback(caller_id, token) -> NeutralScreen`: resolveix token ->
  (action_id, session_id) -> `wizard.handle_action` -> renderitzat.
- `handle_text(caller_id, session_id, text) -> NeutralScreen | None`:
  input de text lliure per `ENTER_OBJECTIVE` (`objective:set`) i
  `SELECT_ROLE_A/B` (rol per slot, inclosos rols custom).
- `active_session_id(caller_id)`: sessió activa del caller (per text input).
- `CUSTOM_ROLE_MARKER = "custom"`.

### TelegramUXAdapter (src/xerrameca/integrations/telegram.py)
- **NO conté la state machine**: delega 100% a `XerramecaWizardService`
  via `TelegramWizardBridge`.
- `start_wizard(chat_id)`: renderitza la pantalla ROOT per botons.
- `handle_callback(chat_id, token)`: resol i avança el wizard; captura errors
  i envia un missatge d'error (no avança silenciosament).
- `handle_wizard_text(chat_id, text)`: si el caller té sessió activa en estat
  `ENTER_OBJECTIVE`/`SELECT_ROLE_A/B`, envia el text al bridge; altrament
  ignora (deixa passar les comandes legacy).
- Accepta `wizard=` opcional al constructor; sense ell, l'adapter només fa
  les comandes legacy (Telegram continua sent OPCIONAL).

### /xerrameca root
- `/xerrameca` sense arguments obre el wizard interactiu per botons
  (pantalla ROOT: Nova conversa / Converses / Agents / Ajuda).

### Flux complet wizard per botons
ROOT -> [Nova conversa] -> SELECT_PEER (peer trusted) ->
SELECT_DIALOGUE_TYPE (preset) -> ENTER_OBJECTIVE (text) ->
SELECT_ROLE_A (rol) -> SELECT_ROLE_B (rol) -> SELECT_ROUNDS ->
SELECT_OUTPUT_MODE -> CONFIRM -> [INICIAR] -> STARTED.
Cada botó porta un callback token opac; el bridge el resol i avança.

### Text input
- **Objective**: a `ENTER_OBJECTIVE` l'usuari envia text lliure ->
  `objective:set`.
- **Custom role**: a `SELECT_ROLE_A/B` l'usuari envia el nom del rol ->
  `role_a:<text>` / `role_b:<text>` (el wizard accepta rols custom).

### BACK semantics
- Botó "Enrere" (`nav:back`): reconstrueix la pantalla de l'estat anterior.
  Només modifica l'estat local del wizard. No recrea conversa federada.

### wizard CANCEL semantics
- Botó "Cancel·lar" (`wizard:cancel`) present a TOTES les pantalles pre-START.
- Cancel·la LA SESSIÓ DEL WIZARD abans de START. NO cancel·la cap conversa
  federada (el core no exposa aturada de runtime segura).

### confirm:start idempotent
- `confirm:start` a CONFIRM crida `XerramecaCommandService.create_conversation`
  exactament 1 vegada; la sessió recorda `conversation_id` i tota crida
  posterior (doble click amb el mateix token) retorna la mateixa conversa.

### Compatibilitat legacy (sense canvis de comportament)
- `/xerrameca start <peer> <objective> [rounds]`: `adapter.start(...)`.
- `/xerrameca status <id>`: `adapter.status(...)`.
- `/xerrameca sync <id>`: `adapter.sync(...)`.
- `/xerrameca mode <id> <mode>`: `adapter.set_mode(...)`.
- Les comandes textuals continuen funcionant; el wizard és addicional.

### Telegram és opcional i no és font d'estat federat
- L'adapter pot funcionar sense wizard (només legacy).
- Cap estat federat s'origina a Telegram: el wizard només crea/configura
  via `XerramecaCommandService` (la mateixa font que les comandes CLI).

### Tests UX-3 (tests/test_ux3.py, 22 tests) — 22 passed
Neutral button model, Telegram transport compatibility, /xerrameca root,
wizard via buttons, text input (objective + custom role), trusted peer
rendering, presets, roles, rounds, output mode, BACK, wizard CANCEL,
confirm:start, duplicate START protection (idempotència + 1 crida a
create_conversation), callback ownership, callback expiry, callback size,
secret leakage, legacy Telegram commands, Telegram external dependency NONE.

### Architecture gate UX-3
protocol v1 unchanged: YES
event schema unchanged: YES
node federation unchanged: YES
replication unchanged: YES
failover unchanged: YES
signed events unchanged: YES
2-participant invariant unchanged: YES
Pluribus dependency: NONE
Telegram external library dependency: NONE
production nodes touched: NO

### NO inclòs en aquest PR (UX-4+)
Active conversation screen (runtime status per botons), polling/watch,
python-telegram-bot/aiogram wiring real, federated stop/cancel, multiparty,
metrics, selfcheck.

## UX-3.1 — HARDENING DE LA SUPERFÍCIE PÚBLICA

Objectiu: tancar les mancances entre els tests i el flux públic real de
Telegram abans del merge d'UX-3. Cap canvi de protocol, schema d'events,
replicació, failover ni invariant de 2 participants.

### /xerrameca entra realment per handle_text()
`TelegramUXAdapter.handle_text()` rep `"/xerrameca"` (sense arguments) i
obra/reprend el wizard via la superfície pública (no es crida `start_wizard`
directament als tests d'integració). Les comandes legacy (`/xerrameca start …`,
`status`, `sync`, `mode`) continuen funcionant.

### objective entra per handle_text()
El text lliure que NO és una comanda `/xerrameca …` es delega automàticament
al wizard **només si el caller té una sessió activa i l'estat espera text**
(`expected_text_input` ∈ `{objective, role_a_custom, role_b_custom}`). En cas
contrari el missatge NO es consumeix.

### Rol custom = selecció explícita + text següent
El botó visible `custom` té com a action_id **`{slot}:custom_input`** (marker
UX intern), mai `role_a:custom` / `role_b:custom`. En prémer-lo, el wizard
marca `expected_text_input = {slot}_custom` i roman a la pantalla esperant
text. El text següent es guarda com a rol (`role_a` / `role_b`) i avança.
`VALID_ROLES` continua incloent `"custom"` com a *valor* (consumit per UX-2 via
`handle_action` directe), però **no es renderitza mai com a botó seleccionable**.

La construcció dels botons de rol està centralitzada a `XerramecaWizardService._role_buttons(slot)`,
usada a SELECT_ROLE_A, SELECT_ROLE_B i `_rebuild_roles()`. Això garanteix
exactament UN botó `custom` per pantalla (action `role_{a,b}:custom_input`).

### Text lliure sense haver premut custom: REBUTJAT
A SELECT_ROLE_A/B, si l'usuari envia text sense haver triat `custom`, el wizard
NO el consumeix, NO modifica el rol i NO avança l'estat (retorna `handled=False`).

### Una sessió activa per caller
`TelegramWizardBridge.start(caller_id)` implementa la política:
- sessió activa i vàlida (estat ≠ STARTED/CANCELLED i no expirada) → reprendre i
  renderitzar la sessió existent (`wizard.resume(caller_id)`).
- altrament → crear nova sessió i **invalidar els callbacks de la sessió
  anterior del mateix caller** (`CallbackStore.invalidate_caller` /
  `invalidate_session`).

### API pública de consulta de sessió
El bridge NO accedeix a membres privats del wizard. `XerramecaWizardService`
exposa `get_session(session_id, caller_id)`, `resume(caller_id)`,
`current_screen(session_id, caller_id)` i `expected_text_input(session)`, tots
amb TTL/ownership/stale-rejection en un únic lloc.

### Peer status: online / offline / unknown
`online is None` (no comprovat) es renderitza com **`unknown`**, mai `offline`.
`list_agents()` continua sent l'autoritat.

### Errors controlats
`TelegramUXAdapter.handle_callback` / `handle_wizard_text` capturen
explícitament `WizardError` i `CallbackError` i responen amb un missatge
genèric (`"Acció no vàlida o expirada. Torna a obrir /xerrameca."`) **sense**
exposar paths, tracebacks, nodes interns ni raw exceptions. Errors inesperats
es registren internament sense silenciar-los com a funcionals.

### Telegram Inline Keyboard real: ENCARA NO
UX-3/UX-3.1 mantenen el model neutral (`NeutralButton`/`NeutralScreen`/
`CallbackStore`) + bridge Telegram. **No** hi ha `InlineKeyboardMarkup` real ni
dependència de `python-telegram-bot`/`aiogram`. El wiring físic queda per UX-4.

### Tests UX-3.1 (tests/test_ux31.py, 14 tests) — 14 passed
Cobertura end-to-end sobre la superfície pública real:
`/xerrameca` via `handle_text`, objective via `handle_text`, custom role A/B via
`handle_text`, reject de text lliure sense custom, resume de sessió activa,
invalidació de callbacks antics, caller diferent reject, callback expirat
reject, peer `unknown`, error controlat sense leak intern, legacy commands,
unicitat del botó `custom` (action `role_*:custom_input`, absència de
`role_*:custom`) i BACK/rebuild mantenen la unicitat.

### Architecture gate UX-3.1
protocol v1 unchanged: YES
event schema unchanged: YES
replication unchanged: YES
failover unchanged: YES
2-participant invariant unchanged: YES
Telegram external library dependency: NONE
Pluribus dependency: NONE
production nodes touched: NO


## UX-4.1 — Active conversation UX (local UI, no federated changes)

Implements a read-mostly "Converses" surface on top of the existing wizard
stack. Telegram owns NO federated state; everything routes through
XerramecaWizardService / XerramecaCommandService / node API.

### Conversation list

- Root → "Converses" opens a real list (no longer a placeholder), sourced from
  XramaCommandService.list_conversations().
- Each row shows a short human label: status, round (current/max), truncated id
  (e.g. `RUNNING · ronda 3/6 · xfc_a72d…`).
- Empty list renders "No hi ha converses." plus Enrere.

### Conversation detail

On selecting a conversation, shows only safe, user-facing fields:
truncated id, status, current_round/max_rounds, peer (first participant),
optional completion_pending and a summarized last_ message (<=60 chars).
Never shows API keys, private keys, raw signatures, internal payloads, full
event log, or local paths.

### Refresh

"Actualitzar" is read-only: re-fetches get_conversation() and rebuilds the
screen. Creates no federated event, does not mutate status, no polling, no
background task.

### Sync

"Sincronitzar" uses XerramecaCommandService.sync_conversation(). Errors from
an unavailable peer are controlled and user-facing ("No s'ha pogut
sincronitzar amb el peer. Torna-ho a provar.") with no raw exception leak.
After sync the screen is rebuilt from the current state.

### Local output mode

Mode (summary / live / silent) is a local UI preference only, stored on the
wizard session data (`_conv_mode`). It does NOT change the federated
conversation or create any event. The dialog-type presets (mode:*) remain the
federated dialogue type selection from the creation flow and are not confused
with this local render mode.

### BACK navigation

- CONVERSATION_MODE → CONVERSATION_DETAIL
- CONVERSATION_DETAIL → CONVERSATION_LIST
- CONVERSATION_LIST → ROOT
These are split off from the creation flow; BACK never rebuilds a creation
session by accident.

### Callback security

Conversation callbacks use short bound indexes (conv:N) resolved via
session.data["_conv_idx"]; the opaque token never contains conversation_id,
node_id, objective, or secrets. Callbacks remain opaque, <=64 bytes,
caller-bound, session-bound, TTL, and stored via CallbackStore.

### Stale data handling

Controlled, no traceback: conversation deleted/not-found, RUNNING->COMPLETED
changes between screens, stale callback, callback from another caller, expired
session, and sync with unavailable peer all resolve to a generic user-facing
message via the existing WizardError / CallbackError boundary.

### Not implemented here

- No real Telegram InlineKeyboardMarkup / python-telegram-bot / aiogram / telebot
  (that is UX-4.2).
- No Stop / Pause / Continue / Cancel runtime / Retry / Force claim / Force
  reply / manual takeover — the core does not expose a safe federated semantic
  for those.
- No polling and no background refresh tasks.

### Test note

tests/test_ux41.py injects a FakeCommandService into the wizard (dependency
injection, wizard's optional `command_service=`) so the conversation surface is
exercised without mutating the real XerramecaCommandService class or
contaminating other test modules. 29 tests cover listing, detail, refresh,
sync, mode, BACK, caller isolation, stale callbacks, and the public
`adapter.handle_text("/xerrameca")` end-to-end path.


## UX-4.2 — Telegram Bot API renderer (real inline keyboard)

Adds an optional, real Telegram Bot API transport so a NeutralScreen is
delivered as an actual `reply_markup.inline_keyboard` (1 button per row).

### Render path

    NeutralScreen / NeutralButton
            ↓
    TelegramUXAdapter._send_screen(chat_id, screen)
            ↓
    TelegramBotAPITransport.send_buttons(chat_id, text, buttons)
            ↓
    Telegram Bot API sendMessage
            ↓
    reply_markup.inline_keyboard (real)

- No Telegram SDK. It uses the `httpx` dependency Xerrameca already ships
  (no python-telegram-bot / aiogram / telebot; pyproject.toml is unchanged).
- The transport is optional and injectable. If it is never instantiated the
  federated core behaves exactly as before.

### Capability detection / legacy fallback

`TelegramUXAdapter._send_screen` detects whether the transport exposes
`send_buttons`:

- Transport with `send_buttons` → ONE sendMessage with inline_keyboard.
- Send-only transport (older fakes / tests) → legacy text + `[label] ::token`
  pseudo-button lines. This preserves UX-3, UX-3.1 and UX-4.1 tests unchanged
  (UX-4.2 is additive).

All three rendering paths (start_wizard, handle_callback, handle_wizard_text)
route through the single `_send_screen` helper, so no path accidentally emits
pseudo-buttons when a real keyboard is available.

### Callback security & size

- `callback_data` = the exact opaque `NeutralButton.callback_token` from
  CallbackStore (the only source). No conversation_id, node_id, objective,
  session JSON, API key, or bot token are ever placed inside it.
- Telegram requires callback_data <= 64 bytes; the transport validates
  `len(token.encode()) <= 64` before sending. Oversized tokens are rejected
  with a controlled failure (never truncated, never replaced).

### sendMessage payload

    { "chat_id": ..., "text": screen_text,
      "reply_markup": { "inline_keyboard": [ [ {text, callback_data} ] ] } }

- One button per row; NeutralScreen button order is preserved.

### bot token hygiene

- The bot token is accepted at construction/runtime only.
- It is never persisted, never written to SQLite, never stored in a wizard
  session or callback, never logged, never included in a user-facing
  exception, and avoided in object reprs.
- The Bot API embeds the token in the request URL, so every httpx
  HTTP/network failure is mapped to a generic `TelegramTransportError`
  ("Telegram API request failed") raised `from None`, so the raw URL (with the
  token) is never chained or surfaced.

### Client injection & no real network

- `TelegramBotAPITransport` accepts an optional `httpx.AsyncClient` and an
  `api_base` override. Tests inject `httpx.MockTransport` + a fake base URL so
  no test ever contacts `api.telegram.org`; there is zero real network in CI.

### Callback ACK

- `TelegramUXAdapter.handle_callback(chat_id, token, callback_query_id=None)`
  is backward compatible. When `callback_query_id` is present and the transport
  supports `answer_callback_query`, an ACK is sent (so Telegram stops the
  inline spinner). ACK errors are swallowed (`from _TelegramTransportError`):
  they never alter federated state and never introduce retry/background logic.

### Error semantics

- WizardError / CallbackError → existing generic message.
- Telegram transport errors → generic message if still possible.
- Never `f"{exc}"` to the user; never show the Bot API URL.

### Explicitly NOT implemented here (UX-4.3 / UX-4.4)

- polling / getUpdates loop
- webhook server / update dispatcher
- physical Telegram smoke
- python-telegram-bot / aiogram / telebot
- background workers

## UX-4.3 — Telegram Update ingestion + dispatcher + simulated E2E

Adds the Telegram **input** layer: a raw Telegram Update JSON is parsed only in
`src/xerrameca/integrations/telegram_updates.py` and routed to the existing
`TelegramUXAdapter` (message texts and `callback_query`s), which drives the
wizard / command layer and renders through the existing
`TelegramBotAPITransport` inline keyboard. No Telegram SDK, no polling, no
webhook, no `pyproject.toml` change.

### Layering
- **`integrations/telegram_updates.py`** owns ALL Update parsing. It never
  interprets wizard semantics (the callback_data stays an opaque token; free
  text is forwarded verbatim). It contains no `while True`, no `getUpdates` /
  `setWebhook`, no aiohttp / FastAPI server, no background task.
- **`integrations/telegram.py`** (+ACK hardening) owns the wizard interaction
  and rendering. Nothing in `command/`, `node/` or `ui/` parses Telegram JSON.
- The federated core is untouched: no protocol v1 / event schema / replication
  / failover / signed-events / 2-participant invariant change.

### Dispatcher contract
- `dispatch(update: Mapping) -> DispatchResult` where `DispatchResult` carries
  `kind` (`handled | ignored | duplicate | rejected`), `update_id` and a
  controlled `reason` (never a raw Update, token, objective, API key, bot token,
  private key or filesystem path).
- Message updates → `adapter.handle_text(chat_id, text)` (chat id coerced to
  `str`). Callback updates → `adapter.handle_callback(chat_id, data,
  callback_query_id=...)` (data is forwarded verbatim, never reinterpreted).
- Controlled ignores: no `update_id`; message without text (incl. polls);
  callback without data / chat; `edited_message`, `inline_query`,
  `chosen_inline_result`; any unsupported type.
- **At-most-once per `update_id`** within a live dispatcher instance (bounded
  in-memory dedup window, default `max_seen_updates = 1024`, `deque`+`set`).
  Ids are claimed **before** execution under a per-instance `asyncio.Lock`, so
  `asyncio.gather(dispatch(u), dispatch(u))` executes only once. There is NO
  global exactly-once claim and NO persistence: after a restart the cache is
  gone (documented).
- Duplicate callback_query → no wizard action; the callback transport is
  safe-ACKed only (no federated mutation) so Telegram dismisses its spinner.
- Optional `allowed_chat_ids: set[str] | None`. `None` = no filter. A denied
  message is rejected with NO wizard mutation; a denied callback is rejected
  with NO wizard mutation but a safe ACK.
- **ACK hardening** in `TelegramUXAdapter`: a single `_ack_safely(...)` helper
  (public `safe_ack(...)` for the dispatcher). Valid → ACK; expired/invalid →
  safe ACK; rejected by allowlist → safe ACK; Telegram render error → safe ACK;
  ACK failure → always silent & non-authoritative. ACK never changes wizard
  state, never reverts, never creates an event, never retries.
- Error boundary: unexpected exceptions are captured at the Telegram boundary
  and returned as a controlled result; `f"{exc}"` is never surfaced to the user.

### Test coverage (tests/test_ux43.py, >=30)
Routing / parsing (message & callback, chat-id coercion, opaque data forwarding),
ACK success/failure isolation, update_id dedup (message & callback, concurrent,
bounded, per-instance), allowlist (allowed/denied message/callback, safe ACK),
caller isolation, secret-free results, no Telegram SDK / no real network (always
`httpx.MockTransport`), and a full simulated E2E driven ONLY by Telegram Update
dicts through `dispatch()`: `/xerrameca → Nova conversa → peer → preset →
objectiu (text) → role A → role B → rondes → sortida → INICIAR → conversa
creada (exactament una vegada)`, then `/xerrameca → Converses → detall →
Actualitzar → Mode → Enrere`. Plus duplicate-START and concurrent-START safety
(`create_conversation` called exactly once).
