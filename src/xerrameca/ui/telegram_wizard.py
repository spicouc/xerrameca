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
        # FASE 4: resume active valid session; otherwise create new (invalidating old callbacks).
        existing = self.wizard.resume(caller_id)
        if existing is not None:
            return self._render(existing.session_id, caller_id, self.wizard.current_screen(existing.session_id, caller_id))
        old = self._active.get(caller_id)
        if old is not None:
            self.callbacks.invalidate_session(old)
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
        """Free-text input routed by the wizard's expected-text contract.

        Returns a rendered screen when the text advances the wizard, else None.
        Does NOT interpret arbitrary text as wizard input; only acts when the
        wizard explicitly expects objective or a custom role for the active slot.
        """
        session = self.wizard.get_session(session_id, caller_id)
        text = text.strip()
        if not text:
            return None
        expected = self.wizard.expected_text_input(session)
        if expected == "objective":
            screen = self.wizard.handle_action(
                session_id, caller_id, WizardAction(action_id="objective:set", payload={"objective": text})
            )
            return self._render(session_id, caller_id, screen)
        if expected == "role_a_custom":
            screen = self.wizard.handle_action(
                session_id, caller_id, WizardAction(action_id=f"role_a:{text}")
            )
            return self._render(session_id, caller_id, screen)
        if expected == "role_b_custom":
            screen = self.wizard.handle_action(
                session_id, caller_id, WizardAction(action_id=f"role_b:{text}")
            )
            return self._render(session_id, caller_id, screen)
        return None
