"""Transport-independent federated conversation wizard.

XerramecaWizardService owns no agent runtime, no Telegram, no Pluribus.
It builds an effective objective from wizard selections and, at START,
delegates to XerramecaCommandService.create_conversation. Wizard sessions
are local, TTL-bounded and caller-owned. They never enter the signed
federated event log before START.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .dto import WizardAction, WizardButton, WizardScreen, WizardSession
from .presets import PRESETS, VALID_OUTPUT_MODES, VALID_ROLES, VALID_ROUNDS, get_preset

CUSTOM_ROLE_MARKER = "custom"
CUSTOM_INPUT_MARKER = "custom_input"

DEFAULT_TTL_SECONDS = 900

WIZARD_STATES = (
    "ROOT",
    "SELECT_PEER",
    "SELECT_DIALOGUE_TYPE",
    "ENTER_OBJECTIVE",
    "SELECT_ROLE_A",
    "SELECT_ROLE_B",
    "SELECT_ROUNDS",
    "SELECT_OUTPUT_MODE",
    "CONFIRM",
    "STARTED",
    "CANCELLED",
)

ROOT_ACTIONS = (
    WizardButton(label="Nova conversa", action_id="root:new"),
    WizardButton(label="Converses", action_id="root:conversations"),
    WizardButton(label="Agents", action_id="root:agents"),
    WizardButton(label="Ajuda", action_id="root:help"),
)

VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "ROOT": ("SELECT_PEER", "CANCELLED"),
    "SELECT_PEER": ("SELECT_DIALOGUE_TYPE", "ROOT", "CANCELLED"),
    "SELECT_DIALOGUE_TYPE": ("ENTER_OBJECTIVE", "SELECT_PEER", "CANCELLED"),
    "ENTER_OBJECTIVE": ("SELECT_ROLE_A", "SELECT_DIALOGUE_TYPE", "CANCELLED"),
    "SELECT_ROLE_A": ("SELECT_ROLE_B", "ENTER_OBJECTIVE", "CANCELLED"),
    "SELECT_ROLE_B": ("SELECT_ROUNDS", "SELECT_ROLE_A", "CANCELLED"),
    "SELECT_ROUNDS": ("SELECT_OUTPUT_MODE", "SELECT_ROLE_B", "CANCELLED"),
    "SELECT_OUTPUT_MODE": ("CONFIRM", "SELECT_ROUNDS", "CANCELLED"),
    "CONFIRM": ("STARTED", "SELECT_OUTPUT_MODE", "CANCELLED"),
    "STARTED": (),
    "CANCELLED": (),
}


@dataclass
class _SessionRecord:
    session: WizardSession
    expires_at: int


class XerramecaWizardService:
    def __init__(self, state_dir: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, node_port: int = 8891) -> None:
        self.state_dir = state_dir
        self.ttl_seconds = ttl_seconds
        self.node_port = node_port
        self._sessions: dict[str, _SessionRecord] = {}
        self._active_caller: dict[str, str] = {}

    # ----- session lifecycle ------------------------------------------
    def _now(self) -> int:
        return int(time.time())

    def _purge_expired(self) -> None:
        now = self._now()
        expired = [sid for sid, rec in self._sessions.items() if rec.expires_at <= now]
        for sid in expired:
            del self._sessions[sid]
    def create_session(self, caller_id: str) -> WizardSession:
        self._purge_expired()
        session_id = secrets.token_hex(16)
        now = self._now()
        session = WizardSession(
            session_id=session_id,
            caller_id=caller_id,
            state="ROOT",
            data={},
        )
        self._sessions[session_id] = _SessionRecord(
            session=session, expires_at=now + self.ttl_seconds
        )
        # track active session per caller (FASE 4)
        self._active_caller[caller_id] = session_id
        return session

    def _require_session(self, session_id: str, caller_id: str) -> WizardSession:
        self._purge_expired()
        rec = self._sessions.get(session_id)
        if rec is None:
            raise WizardError("sessió no trobada o expirada")
        if rec.session.caller_id != caller_id:
            raise WizardError("sessió no pertany a aquest caller")
        return rec.session

    # ----- public session API (FASE 6) -----
    def get_session(self, session_id: str, caller_id: str) -> WizardSession:
        """Public, caller-owned session lookup (TTL + ownership + stale rejection)."""
        return self._require_session(session_id, caller_id)

    def resume(self, caller_id: str) -> WizardSession | None:
        """Return the caller's active wizard session if valid (not started/cancelled/expired)."""
        self._purge_expired()
        sid = self._active_caller.get(caller_id)
        if sid is None:
            return None
        rec = self._sessions.get(sid)
        if rec is None:
            self._active_caller.pop(caller_id, None)
            return None
        st = rec.session.state
        if st in ("STARTED", "CANCELLED"):
            return None
        return rec.session

    def current_screen(self, session_id: str, caller_id: str) -> WizardScreen:
        """Rebuild the neutral screen for the session's current state."""
        session = self._require_session(session_id, caller_id)
        state = session.state
        if state == "ROOT":
            return self.root_screen()
        if state == "SELECT_PEER":
            return self._peer_screen()
        if state == "SELECT_DIALOGUE_TYPE":
            return self._rebuild_dialogue_type(session)
        if state == "ENTER_OBJECTIVE":
            return WizardScreen(state="ENTER_OBJECTIVE", text="Objectiu:",
                                buttons=[WizardButton(label="Enrere", action_id="nav:back")])
        if state == "SELECT_ROLE_A":
            return self._rebuild_roles(session, "role_a")
        if state == "SELECT_ROLE_B":
            return self._rebuild_roles(session, "role_b")
        if state == "SELECT_ROUNDS":
            return self._rebuild_rounds(session)
        if state == "SELECT_OUTPUT_MODE":
            return self._rebuild_output(session)
        if state == "CONFIRM":
            return self._confirm_screen(session)
        if state == "STARTED":
            return WizardScreen(state="STARTED",
                                text=f"Conversa ja iniciada: {session.data.get('conversation_id', '')}",
                                buttons=[])
        if state == "CANCELLED":
            return WizardScreen(state="CANCELLED", text="Wizard cancel·lat (sessió local, no federada).", buttons=[])
        return self.root_screen()

    def root_screen(self) -> WizardScreen:
        return WizardScreen(
            state="ROOT",
            text="Xerrameca — tria una acció:",
            buttons=list(ROOT_ACTIONS),
        )

    def expected_text_input(self, session: WizardSession) -> str:
        """Local metadata: what free-text the wizard currently expects.

        One of: 'objective', 'role_a_custom', 'role_b_custom', 'none'.
        Does NOT add any federated protocol field.
        """
        if session.state == "ENTER_OBJECTIVE":
            return "objective"
        if session.state == "SELECT_ROLE_A" and session.data.get("role_a_expects_custom"):
            return "role_a_custom"
        if session.state == "SELECT_ROLE_B" and session.data.get("role_b_expects_custom"):
            return "role_b_custom"
        return "none"

    def handle_action(self, session_id: str, caller_id: str, action: WizardAction) -> WizardScreen:
        session = self._require_session(session_id, caller_id)
        if session.state == "CANCELLED":
            raise WizardError("sessió cancel·lada; no es pot avançar")
        action_id = action.action_id
        if session.state == "STARTED" and action_id == "confirm:start":
            return self._start(session)  # idempotent: retorna la mateixa conversa
        if session.state == "STARTED":
            raise WizardError("sessió ja iniciada; no es pot avançar")
        # navigation
        if action_id == "nav:back":
            return self._back(session)
        if action_id == "wizard:cancel":
            return self._cancel(session)
        # root
        if session.state == "ROOT":
            if action_id == "root:new":
                return self._transition(session, "SELECT_PEER", self._peer_screen())
            if action_id in ("root:conversations", "root:agents", "root:help"):
                return self.root_screen()
            raise WizardError("acció arrel no reconeguda")
        # peer
        if session.state == "SELECT_PEER" and action_id.startswith("peer:"):
            return self._select_peer(session, action_id)
        # dialogue type
        if session.state == "SELECT_DIALOGUE_TYPE" and action_id.startswith("mode:"):
            return self._select_mode(session, action_id)
        # objective
        if session.state == "ENTER_OBJECTIVE" and action_id == "objective:set":
            return self._set_objective(session, action)
        # roles
        if session.state == "SELECT_ROLE_A" and action_id.startswith("role_a:"):
            return self._select_role(session, action_id, "role_a")
        if session.state == "SELECT_ROLE_B" and action_id.startswith("role_b:"):
            return self._select_role(session, action_id, "role_b")
        # rounds
        if session.state == "SELECT_ROUNDS" and action_id.startswith("rounds:"):
            return self._select_rounds(session, action_id)
        # output mode
        if session.state == "SELECT_OUTPUT_MODE" and action_id.startswith("output:"):
            return self._select_output(session, action_id)
        # confirm / start
        if session.state == "CONFIRM" and action_id == "confirm:start":
            return self._start(session)
        raise WizardError(f"acció {action_id} no vàlida a l'estat {session.state}")

    def _transition(self, session: WizardSession, target: str, screen: WizardScreen) -> WizardScreen:
        if target not in VALID_TRANSITIONS.get(session.state, ()):
            raise WizardError(f"transició il·legal {session.state} -> {target}")
        session.state = target
        self._sessions[session.session_id].expires_at = self._now() + self.ttl_seconds
        return self._with_cancel(screen)
    def _with_cancel(self, screen: WizardScreen) -> WizardScreen:
        if screen.state in ("ROOT", "CANCELLED", "STARTED"):
            return screen
        if not any(b.action_id == "wizard:cancel" for b in screen.buttons):
            screen.buttons.append(WizardButton(label="Cancel·lar", action_id="wizard:cancel"))
        return screen

    def _role_buttons(self, slot: str) -> list[WizardButton]:
        """Build role selection buttons for a given slot.

        The visible "custom" button uses the internal marker action
        ``{slot}:custom_input`` (NOT ``{slot}:custom``), so free text is only
        accepted after the user explicitly chooses custom. ``VALID_ROLES``
        intentionally includes "custom" as a *value* (consumed elsewhere, e.g.
        UX-2 direct handle_action); it must never be rendered as a selectable
        role button here.
        """
        buttons = [
            WizardButton(label=role, action_id=f"{slot}:{role}")
            for role in VALID_ROLES
            if role != CUSTOM_ROLE_MARKER
        ]
        buttons.append(
            WizardButton(label=CUSTOM_ROLE_MARKER, action_id=f"{slot}:{CUSTOM_INPUT_MARKER}")
        )
        return buttons

    def _peer_screen(self) -> WizardScreen:
        from .service import XerramecaCommandService

        agents = XerramecaCommandService(self.state_dir, node_port=self.node_port).list_agents()
        buttons = [
            WizardButton(label=f"{a.display_name} ({'online' if a.online else ('unknown' if a.online is None else 'offline')})", action_id=f"peer:{a.node_id}")
            for a in agents
        ]
        if not buttons:
            buttons = [WizardButton(label="Cap agent trusted disponible", action_id="root:agents")]
        buttons.append(WizardButton(label="Enrere", action_id="nav:back"))
        return WizardScreen(state="SELECT_PEER", text="Tria un agent trusted:", buttons=buttons)

    def _select_peer(self, session: WizardSession, action_id: str) -> WizardScreen:
        node_id = action_id.split(":", 1)[1]
        from .service import XerramecaCommandService

        agents = XerramecaCommandService(self.state_dir).list_agents()
        if not any(a.node_id == node_id for a in agents):
            raise WizardError("peer no trusted o no seleccionable")
        session.data["peer_node_id"] = node_id
        buttons = [WizardButton(label=p.label, action_id=f"mode:{p.key}") for p in PRESETS.values()]
        buttons.append(WizardButton(label="Enrere", action_id="nav:back"))
        return self._transition(session, "SELECT_DIALOGUE_TYPE", WizardScreen(
            state="SELECT_DIALOGUE_TYPE", text="Tria el tipus de diàleg:", buttons=buttons))

    def _select_mode(self, session: WizardSession, action_id: str) -> WizardScreen:
        key = action_id.split(":", 1)[1]
        preset = get_preset(key)
        if preset is None:
            raise WizardError("preset desconegut")
        session.data["dialogue_type"] = key
        session.data.setdefault("role_a", preset.default_role_a)
        session.data.setdefault("role_b", preset.default_role_b)
        return self._transition(session, "ENTER_OBJECTIVE", WizardScreen(
            state="ENTER_OBJECTIVE",
            text=f"Objectiu per a '{preset.label}':\n{preset.instruction}",
            buttons=[WizardButton(label="Enrere", action_id="nav:back")],
        ))

    def _set_objective(self, session: WizardSession, action: WizardAction) -> WizardScreen:
        objective = str(action.payload.get("objective", "")).strip()
        if not objective:
            raise WizardError("objectiu buit")
        session.data["user_objective"] = objective
        buttons = self._role_buttons("role_a")
        buttons.append(WizardButton(label="Enrere", action_id="nav:back"))
        return self._transition(session, "SELECT_ROLE_A", WizardScreen(
            state="SELECT_ROLE_A", text="Rol de l'agent local:", buttons=buttons))

    def _select_role(self, session: WizardSession, action_id: str, slot: str) -> WizardScreen:
        role = action_id.split(":", 1)[1].strip()
        if not role:
            raise WizardError("rol buit")
        if role == CUSTOM_INPUT_MARKER:
            # User explicitly chose custom: expect free-text input next, stay on screen.
            session.data[f"{slot}_expects_custom"] = True
            return self._transition(session, "SELECT_ROLE_" + ("A" if slot == "role_a" else "B"),
                                    self._rebuild_roles(session, slot))
        expects_custom = session.data.get(f"{slot}_expects_custom", False)
        if len(role) > 64:
            raise WizardError("rol massa llarg")
        if role in VALID_ROLES or role == CUSTOM_ROLE_MARKER:
            # explicit valid role, or the literal "custom" value (UX-2 contract)
            session.data.pop(f"{slot}_expects_custom", None)
            session.data[slot] = role
        elif expects_custom:
            # free-text custom role: store verbatim
            session.data.pop(f"{slot}_expects_custom", None)
            session.data[slot] = role
        else:
            raise WizardError("rol no reconegut (tria un rol vàlid o 'custom')")
        if slot == "role_a":
            buttons = self._role_buttons("role_b")
            buttons.append(WizardButton(label="Enrere", action_id="nav:back"))
            return self._transition(session, "SELECT_ROLE_B", WizardScreen(
                state="SELECT_ROLE_B", text="Rol de l'agent peer:", buttons=buttons))
        rounds = [WizardButton(label=str(r), action_id=f"rounds:{r}") for r in VALID_ROUNDS]
        return self._transition(session, "SELECT_ROUNDS", WizardScreen(
            state="SELECT_ROUNDS", text="Rondes:", buttons=rounds))

    def _select_rounds(self, session: WizardSession, action_id: str) -> WizardScreen:
        rounds = int(action_id.split(":", 1)[1])
        if rounds not in VALID_ROUNDS:
            raise WizardError("rondes no permeses")
        session.data["max_rounds"] = rounds
        modes = [WizardButton(label=m, action_id=f"output:{m}") for m in VALID_OUTPUT_MODES]
        return self._transition(session, "SELECT_OUTPUT_MODE", WizardScreen(
            state="SELECT_OUTPUT_MODE", text="Mode de sortida:", buttons=modes))

    def _select_output(self, session: WizardSession, action_id: str) -> WizardScreen:
        mode = action_id.split(":", 1)[1]
        if mode not in VALID_OUTPUT_MODES:
            raise WizardError("mode no vàlid")
        session.data["output_mode"] = mode
        return self._transition(session, "CONFIRM", self._confirm_screen(session))

    def _confirm_screen(self, session: WizardSession) -> WizardScreen:
        preset = get_preset(session.data.get("dialogue_type", "conversation"))
        text = (
            f"Agent A: {session.data.get('peer_node_id')}\n"
            f"Tipus: {preset.label if preset else session.data.get('dialogue_type')}\n"
            f"Objectiu: {session.data.get('user_objective', '')}\n"
            f"Rol A: {session.data.get('role_a')}\n"
            f"Rol B: {session.data.get('role_b')}\n"
            f"Rondes: {session.data.get('max_rounds')}\n"
            f"Sortida: {session.data.get('output_mode')}\n\n"
            f"Effective objective:\n{self.build_effective_objective(session)}"
        )
        buttons = [
            WizardButton(label="INICIAR", action_id="confirm:start"),
            WizardButton(label="Enrere", action_id="nav:back"),
            WizardButton(label="Cancel·lar", action_id="wizard:cancel"),
        ]
        return WizardScreen(state="CONFIRM", text=text, buttons=buttons)

    def _back(self, session: WizardSession) -> WizardScreen:
        order = WIZARD_STATES[: WIZARD_STATES.index("CONFIRM") + 1]
        idx = order.index(session.state)
        if idx <= 0:
            return self.root_screen()
        prev = order[idx - 1]
        if prev == "ROOT":
            return self._transition(session, "ROOT", self.root_screen())
        if prev == "SELECT_PEER":
            return self._transition(session, "SELECT_PEER", self._peer_screen())
        if prev == "SELECT_DIALOGUE_TYPE":
            return self._transition(session, "SELECT_DIALOGUE_TYPE", self._rebuild_dialogue_type(session))
        if prev == "ENTER_OBJECTIVE":
            return self._transition(session, "ENTER_OBJECTIVE", WizardScreen(state="ENTER_OBJECTIVE", text="Objectiu:", buttons=[WizardButton(label="Enrere", action_id="nav:back")]))
        if prev == "SELECT_ROLE_A":
            return self._transition(session, "SELECT_ROLE_A", self._rebuild_roles(session, "role_a"))
        if prev == "SELECT_ROLE_B":
            return self._transition(session, "SELECT_ROLE_B", self._rebuild_roles(session, "role_b"))
        if prev == "SELECT_ROUNDS":
            return self._transition(session, "SELECT_ROUNDS", self._rebuild_rounds(session))
        if prev == "SELECT_OUTPUT_MODE":
            return self._transition(session, "SELECT_OUTPUT_MODE", self._rebuild_output(session))
        return self.root_screen()

    def _rebuild_dialogue_type(self, session: WizardSession) -> WizardScreen:
        buttons = [WizardButton(label=p.label, action_id=f"mode:{p.key}") for p in PRESETS.values()]
        buttons.append(WizardButton(label="Enrere", action_id="nav:back"))
        return WizardScreen(state="SELECT_DIALOGUE_TYPE", text="Tria el tipus de diàleg:", buttons=buttons)

    def _rebuild_roles(self, session: WizardSession, slot: str) -> WizardScreen:
        buttons = self._role_buttons(slot)
        buttons.append(WizardButton(label="Enrere", action_id="nav:back"))
        state = "SELECT_ROLE_A" if slot == "role_a" else "SELECT_ROLE_B"
        return WizardScreen(state=state, text=f"Rol {slot}:", buttons=buttons)

    def _rebuild_rounds(self, session: WizardSession) -> WizardScreen:
        rounds = [WizardButton(label=str(r), action_id=f"rounds:{r}") for r in VALID_ROUNDS]
        return WizardScreen(state="SELECT_ROUNDS", text="Rondes:", buttons=rounds)

    def _rebuild_output(self, session: WizardSession) -> WizardScreen:
        modes = [WizardButton(label=m, action_id=f"output:{m}") for m in VALID_OUTPUT_MODES]
        return WizardScreen(state="SELECT_OUTPUT_MODE", text="Mode de sortida:", buttons=modes)

    def _cancel(self, session: WizardSession) -> WizardScreen:
        session.state = "CANCELLED"
        return WizardScreen(state="CANCELLED", text="Wizard cancel·lat (sessió local, no federada).", buttons=[])

    def build_effective_objective(self, session: WizardSession) -> str:
        preset = get_preset(session.data.get("dialogue_type", "conversation"))
        parts = [
            "[USER OBJECTIVE]",
            session.data.get("user_objective", ""),
            "",
            "[DIALOGUE MODE]",
            preset.label if preset else session.data.get("dialogue_type", ""),
            preset.instruction if preset else "",
            "",
            "[LOCAL AGENT ROLE]",
            session.data.get("role_a", ""),
            "",
            "[PEER AGENT ROLE]",
            session.data.get("role_b", ""),
            "",
            "[COMPLETION]",
            preset.completion_instruction if preset else "",
        ]
        return "\n".join(parts)

    def _start(self, session: WizardSession) -> WizardScreen:
        if "conversation_id" in session.data:
            # idempotent: torna la mateixa conversa ja creada
            return WizardScreen(state="STARTED", text=f"Conversa ja iniciada: {session.data['conversation_id']}",
                                buttons=[])
        if session.state != "CONFIRM":
            raise WizardError("només es pot iniciar des de CONFIRM")
        from .service import XerramecaCommandService

        result = XerramecaCommandService(self.state_dir, node_port=self.node_port).create_conversation(
            peer_node_id=session.data["peer_node_id"],
            objective=self.build_effective_objective(session),
            max_rounds=int(session.data["max_rounds"]),
        )
        session.data["conversation_id"] = result["id"]
        session.state = "STARTED"
        return WizardScreen(state="STARTED", text=f"Conversa creada: {result['id']}", buttons=[])
class WizardError(Exception):
    """Controlled wizard failure (stale callback, illegal transition, etc.)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


__all__ = ["WizardError"]
