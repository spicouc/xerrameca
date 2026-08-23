"""Neutral button/screen model + opaque callback token store.

The neutral model is transport-independent. Telegram (or any transport)
renders NeutralScreen into its own UI primitives. Callback tokens are
short, opaque, caller+session+action bound, and TTL-limited.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NeutralButton:
    label: str
    callback_token: str


@dataclass
class NeutralScreen:
    text: str
    buttons: list[NeutralButton] = field(default_factory=list)
    state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "state": self.state,
            "buttons": [{"label": b.label, "callback_token": b.callback_token} for b in self.buttons],
        }


@dataclass
class _CallbackRecord:
    caller_id: str
    session_id: str
    action_id: str
    expires_at: int


class CallbackStore:
    """Maps short opaque tokens to (caller, session, action).

    Tokens never embed API keys, private keys, tokens, objectives or
    other secrets. They are random and bound to a caller + session +
    action, and expire after ttl_seconds.
    """

    def __init__(self, *, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, _CallbackRecord] = {}
        self._by_session: dict[str, set[str]] = {}

    def _now(self) -> int:
        return int(time.time())

    def _purge(self) -> None:
        now = self._now()
        expired = [t for t, r in self._tokens.items() if r.expires_at <= now]
        for t in expired:
            del self._tokens[t]

    def mint(self, *, caller_id: str, session_id: str, action_id: str) -> str:
        self._purge()
        token = base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")
        self._tokens[token] = _CallbackRecord(
            caller_id=caller_id,
            session_id=session_id,
            action_id=action_id,
            expires_at=self._now() + self.ttl_seconds,
        )
        self._by_session.setdefault(session_id, set()).add(token)
        return token

    def resolve(self, token: str, *, caller_id: str) -> tuple[str, str]:
        """Return (action_id, session_id) for token, or raise CallbackError.

        Rejects unknown, expired or caller-mismatched tokens.
        """
        self._purge()
        rec = self._tokens.get(token)
        if rec is None:
            raise CallbackError("callback desconegut o expirat")
        if rec.caller_id != caller_id:
            raise CallbackError("callback no pertany a aquest caller")
        return rec.action_id, rec.session_id

    def invalidate_session(self, session_id: str) -> None:
        """Drop all live callback tokens bound to a session (no secrets exposed)."""
        self._purge()
        tokens = self._by_session.pop(session_id, set())
        for t in tokens:
            self._tokens.pop(t, None)

    def size_ok(self, token: str) -> bool:
        return len(token) <= 64


class CallbackError(Exception):
    pass
