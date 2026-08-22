"""Integration between XerramecaWizardService and TelegramUXAdapter.

This module owns NO state machine, NO presets, NO transitions, NO TTL,
NO idempotency logic. All of that lives in XerramecaWizardService. This
layer only translates between the wizard's neutral domain actions and the
Telegram transport's button/callback surface.
"""

from __future__ import annotations

from typing import Any

from ..command.dto import WizardAction, WizardScreen
from ..command.wizard import XerramecaWizardService
from .neutral import CallbackError, CallbackStore, NeutralButton, NeutralScreen

CUSTOM_ROLE_MARKER = "custom"


class TelegramWizardBridge:
    def __init__(self, wizard: XerramecaWizardService, callbacks: CallbackStore) -> None:
        self.wizard = wizard
        self.callbacks = callbacks
        self._active: dict[str, str] = {}

    def start(self, caller_id: str) -> NeutralScreen:
        session = self.wizard.create_session(caller_id)
        return self._render(session.session_id, caller_id, self.wizard.root_screen())

    def _render(self, session_id: str, caller_id: str, screen: WizardScreen) -> NeutralScreen:
        self._active[caller_id] = session_id
        buttons: list[NeutralButton] = []
        for b in screen.buttons:
            token = self.callbacks.mint(
                caller_id=caller_id, session_id=session_id, action_id=b.action_id
            )
            buttons.append(NeutralButton(label=b.label, callback_token=token))
        return NeutralScreen(text=screen.text, buttons=buttons, state=screen.state)

    def active_session_id(self, caller_id: str) -> str | None:
        return self._active.get(caller_id)

    def handle_callback(self, caller_id: str, token: str) -> NeutralScreen:
        action_id, session_id = self.callbacks.resolve(token, caller_id=caller_id)
        screen = self.wizard.handle_action(session_id, caller_id, WizardAction(action_id=action_id))
        return self._render(session_id, caller_id, screen)

    def handle_text(self, caller_id: str, session_id: str, text: str) -> NeutralScreen | None:
        """Free-text input for objective and custom roles.

        Returns a rendered screen when the text advances the wizard, else None.
        """
        session = self.wizard._require_session(session_id, caller_id)
        state = session.state
        text = text.strip()
        if not text:
            return None
        if state == "ENTER_OBJECTIVE":
            screen = self.wizard.handle_action(
                session_id, caller_id, WizardAction(action_id="objective:set", payload={"objective": text})
            )
            return self._render(session_id, caller_id, screen)
        if state in ("SELECT_ROLE_A", "SELECT_ROLE_B"):
            slot = "role_a" if state == "SELECT_ROLE_A" else "role_b"
            screen = self.wizard.handle_action(
                session_id, caller_id, WizardAction(action_id=f"{slot}:{text}")
            )
            return self._render(session_id, caller_id, screen)
        return None
