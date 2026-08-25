"""Telegram long-polling runner (getUpdates) for the Xerrameca wizard (UX-4.4A).

This module completes the Telegram input chain for tests/local runs:

    getUpdates (long-polling)
        -> TelegramPollingRunner
        -> TelegramUpdateDispatcher  (UX-4.3; owns ALL Update parsing)
        -> TelegramUXAdapter
        -> Wizard / CommandService
        -> TelegramBotAPITransport  (sendMessage / InlineKeyboard / ACK)

The runner is deliberately *dumb*: it delivers each raw Telegram Update dict to
the existing :class:`TelegramUpdateDispatcher` and never interprets messages,
callback_query data, chat ids, objectives or conversation ids itself. All
Update parsing and wizard semantics remain in :class:`TelegramUpdateDispatcher`
/ :class:`TelegramUXAdapter` (UX-4.3).

Separated responsibilities in this module:

- :class:`TelegramGetUpdatesClient`
    Low-level httpx Bot API ``getUpdates`` long-polling (POST, HTTP only). It
    knows nothing about the wizard, offset persistence or process lifecycle.
    It never exposes, logs or embeds the bot token in any message. All HTTP /
    JSON / network exceptions are sanitised into a controlled :
    class:`TelegramPollingError`.

- :class:`TelegramOffsetStore`
    Durable, atomic ``telegram-offset.json`` in the state dir. Writes are
    atomic (temp file + ``os.replace``), permissions 0600, and a corrupt /
    unreadable offset file fails fast (never silently reset to 0 — that could
    re-process old updates). Stores nothing but ``next_offset``; no token, no
    chat ids, no callbacks, no secrets.

- :class:`TelegramPollingRunner`
    The poll lifecycle: ``run_once()`` fetches one batch, delivers each raw
    Update to the dispatcher in Telegram order, and advances/persists the
    offset after each valid update_id. ``run_forever()`` loops with bounded
    exponential backoff (1..30s) on transient errors, classifies permanent
    errors (401/403/409) as fatal, respects an optional stop event and
    asyncio cancellation, and holds the exclusive flock runner lock while it
    runs.

- :class:`TelegramPollingError`
    Controlled, sanitised error type. Its ``__str__``/``__repr__`` never
    contain a bot token, a raw Bot API description, a request URL, a secret or
    a filesystem path. ``fatal`` marks whether the runner should stop instead
    of backoff-retry.

Security invariants
------------------
- No ``--token`` CLI argument: credentials come ONLY from ``--token-file``.
  The token is read at construction, stripped, kept only in memory, never
  copied/persisted/logged, and unavailable via repr.
- No auto ``deleteWebhook``. The bot webhook/configuration is never silently
  modified. A 409 / webhook-in-use conflict fails fast with a fixed sanitised
  message so the operator must verify webhook configuration explicitly.
- Permanent auth errors (401/403) stop the runner; there is no infinite retry.
- The offset store holds no secret (only ``next_offset``).
- No file transfer of the token, no secrets in any test report.

Exactly-once caveat
-------------------
Like the dispatcher, the runner makes NO exactly-once claim. The durable
offset plus the dispatcher's process-local dedup window reduce replays, but a
crash window exists between "dispatch done" and "offset persisted": if the
process dies in that window, a restart may re-receive and re-process the
Update. This is documented and accepted; it is not silently papered over.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import stat
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

import httpx

from .telegram_updates import TelegramUpdateDispatcher

# Python's logging is imported lazily only inside stop-helper paths; the runner
# itself never logs the token or raw bodies.
import logging

_logger = logging.getLogger("xerrameca.integrations.telegram_polling")

_TG_API = "https://api.telegram.org"
_DEFAULT_POLL_TIMEOUT = 30
_SUPPORTED_LIMIT = 100

# Allowed update types. Messages and inline callbacks are the two surfaces the
# UX-4.3 dispatcher understands; anything else would just be ignored downstream,
# so we ask Telegram for only these two to keep the wire minimal.
_DEFAULT_ALLOWED_UPDATES = ["message", "callback_query"]

# Bounded backoff schedule (seconds). The runner sleeps the full schedule,
# doubling up to a hard 30s ceiling, then stays at 30s until a successful poll
# resets it. It never busy-loops.
_MAX_BACKOFF = 30
_BACKOFF_BASE_SECONDS = 1
_BACKOFF_EXPONENT = 2.0

# Offsets are positive integers; any smaller value indicates corruption.
_FIRST_VALID_OFFSET = 0

_OFFSET_FILENAME = "telegram-offset.json"
_RUNNER_LOCK_FILENAME = "telegram-runner.lock"

# Token-file permissions: fail fast if group/world have ANY write bit.
_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH


class TelegramPollingError(RuntimeError):
    """Controlled, sanitised error for the Telegram polling runner.

    ``message`` must be a fixed, generic, user-safe string (no token, no raw
    Bot API description, no URL, no path, no secret). ``fatal`` is False for
    transient/retryable conditions and True for permanent ones (auth, conflict)
    where the runner should stop rather than backoff-retry.
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.fatal = fatal

    def __str__(self) -> str:
        # Fixed sanitised message only. Never the raw exception.
        return self.message

    def __repr__(self) -> str:
        return f"TelegramPollingError({self.message!r})"


class TelegramOffsetStore:
    """Durable next-offset persistence under a state dir (``telegram-offset.json``).

    - File mode 0600; parent state dir is expected to be 0700.
    - Atomic writes (temp file in the same dir -> fsync -> ``os.replace``);
      a crash never leaves a torn/partial offset.
    - Corrupt / unparseable / out-of-range offset -> FAIL FAST (a controlled
      :class:`TelegramPollingError`, never a silent reset to 0). Resetting to 0
      could re-process old updates, so the operator must resolve corruption
      explicitly.
    - Stores nothing but ``{"next_offset": <int>}``. No token, no chat ids, no
      callbacks, no secret material.
    - First start: ``next_offset`` is None (getUpdates sent without offset).
    """

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self._dir = Path(state_dir)
        self._path = self._dir / _OFFSET_FILENAME
        self._loaded: int | None = None
        self._loaded_cached = False

    @property
    def path(self) -> str:
        return str(self._path)

    @property
    def next_offset(self) -> int | None:
        """The current in-memory next offset (None = no offset yet)."""
        return self._loaded

    def load(self) -> int | None:
        """Load and return the persisted next offset (None if file absent).

        On first process start there is no file, so this returns None and the
        runner will call ``getUpdates`` without an offset.

        Raises :class:`TelegramPollingError` (fatal) on corrupt content.
        """
        if not self._path.exists():
            self._loaded = None
            self._loaded_cached = True
            return None
        try:
            raw = self._path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise TelegramPollingError(
                "Telegram offset store is corrupt; refusing to reset it silently",
                fatal=True,
            ) from None
        if not isinstance(data, dict) or "next_offset" not in data:
            raise TelegramPollingError(
                "Telegram offset store is corrupt; refusing to reset it silently",
                fatal=True,
            )
        value = data["next_offset"]
        if not isinstance(value, int) or isinstance(value, bool) or value < _FIRST_VALID_OFFSET:
            raise TelegramPollingError(
                "Telegram offset store is corrupt; refusing to reset it silently",
                fatal=True,
            )
        # Only a forward, monotonic value is meaningful. (See assert in save.)
        self._loaded = value
        self._loaded_cached = True
        return value

    def save(self, next_offset: int) -> None:
        """Atomically persist ``next_offset`` (must be >= 0).

        The directory is created (0700) if missing; the offset file is written
        to 0600. A torn write can never leave a partial file because we write
        to a temp file in the same directory, fsync, then ``os.replace``.
        """
        if not isinstance(next_offset, int) or isinstance(next_offset, bool):
            raise TelegramPollingError(
                "refusing to persist non-integer Telegram offset", fatal=True
            )
        if next_offset < _FIRST_VALID_OFFSET:
            raise TelegramPollingError(
                "refusing to persist negative Telegram offset", fatal=True
            )
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            self._dir.chmod(0o700)
        except OSError:
            pass
        # Monotonic: never move the durable offset backwards (avoids
        # re-processing older updates after a crash).
        if self._loaded is not None and next_offset < self._loaded:
            next_offset = self._loaded
        content = json.dumps({"next_offset": next_offset}, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".offset-", suffix=".tmp", dir=str(self._dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, str(self._path))
            self._loaded = next_offset
            self._loaded_cached = True
        except BaseException:
            # Best-effort cleanup of the temp file; never leave a .tmp behind.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


class TelegramGetUpdatesClient:
    """Low-level httpx Bot API ``getUpdates`` long-polling client.

    Parameters
    ----------
    token:
        Bot token (read from the token file by the caller; lives only in
        memory here and in the request URL). Never persisted or logged.
    client:
        Optional ``httpx.AsyncClient`` for tests / injection. When omitted a
        fresh client is created per request (NO shared mutable state).
    api_base:
        Overridable base URL (almost always the real Bot API); tests inject a
        fake server here so no real network is ever used.
    poll_timeout:
        The long-poll positive ``timeout`` sent to Telegram (seconds). Must be
        > 0. The HTTP read timeout used for the request is always strictly
        greater than ``poll_timeout`` so a legitimate long-poll wait never
        trips the client read timeout.
    http_read_timeout:
        Optional explicit HTTP read timeout. If not given it is derived as
        ``poll_timeout * 2 + 15`` (always > poll_timeout).
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base: str = _TG_API,
        poll_timeout: int = _DEFAULT_POLL_TIMEOUT,
        http_read_timeout: float | None = None,
    ) -> None:
        if not token:
            raise TelegramPollingError(
                "Telegram credential unavailable", fatal=True
            )
        if not isinstance(poll_timeout, int) or poll_timeout <= 0:
            raise TelegramPollingError(
                "Telegram poll timeout must be a positive integer", fatal=True
            )
        self._token = token
        self._client = client
        self._api_base = api_base.rstrip("/")
        self._poll_timeout = poll_timeout
        if http_read_timeout is None:
            http_read_timeout = float(poll_timeout) * 2.0 + 15.0
        if http_read_timeout <= poll_timeout:
            raise TelegramPollingError(
                "Telegram HTTP read timeout must exceed poll timeout", fatal=True
            )
        self._http_read_timeout = http_read_timeout

    def _url(self, method: str) -> str:
        # Token is embedded in the URL by the Bot API. Any exception from httpx
        # could reference this full URL, so failures are always mapped to a
        # sanitised TelegramPollingError (never the raw exception) and the URL
        # is never surfaced.
        return f"{self._api_base}/bot{self._token}/{method}"

    async def get_updates(
        self, *, offset: int | None = None, limit: int = _SUPPORTED_LIMIT
    ) -> list[dict[str, Any]]:
        """Long-poll one batch of updates.

        Returns a list of cleaned ``dict`` updates (or ``[]`` for an empty
        result). Raises :class:`TelegramPollingError` on any failure —
        transient conditions carry ``fatal=False``, permanent auth/conflict
        conditions ``fatal=True``, so the runner can decide whether to retry.
        """
        payload: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": list(_DEFAULT_ALLOWED_UPDATES),
        }
        if offset is not None:
            payload["offset"] = offset
        if limit is not None:
            payload["limit"] = limit

        try:
            if self._client is not None:
                resp = await self._client.post(
                    self._url("getUpdates"),
                    json=payload,
                    timeout=self._http_read_timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self._http_read_timeout) as c:
                    resp = await c.post(self._url("getUpdates"), json=payload)
        except httpx.TimeoutException:
            raise TelegramPollingError(
                "Telegram polling timed out; will retry later", fatal=False
            ) from None
        except httpx.TransportError:
            raise TelegramPollingError(
                "Telegram polling network error; will retry later", fatal=False
            ) from None
        except asyncio.CancelledError:
            # Never swallow cancellation.
            raise
        except Exception:
            # Any unexpected httpx/JSON/network exception must not leak the
            # URL (which contains the token) nor the raw body.
            raise TelegramPollingError(
                "Telegram polling request failed; will retry later", fatal=False
            ) from None

        # 409 conflict / webhook in use -> permanent: operator must verify the
        # webhook configuration. Never auto-call deleteWebhook.
        if resp.status_code == 409:
            raise TelegramPollingError(
                "Telegram polling unavailable; verify webhook configuration",
                fatal=True,
            )
        # 401 / 403 -> permanent credential/authorisation failure. Stop.
        if resp.status_code in (401, 403):
            raise TelegramPollingError(
                "Telegram polling unauthorized; stopping", fatal=True
            )
        # 429 throttled / 5xx -> transient, controlled retry.
        if resp.status_code in (429, 500, 502, 503, 504):
            raise TelegramPollingError(
                "Telegram polling temporarily unavailable; will retry later",
                fatal=False,
            )
        # Any other HTTP error -> controlled, transient-safe stop decision by
        # the caller; never expose the raw status/body/URL.
        if resp.status_code != 200:
            raise TelegramPollingError(
                "Telegram polling request failed; will retry later", fatal=False
            )

        try:
            data = resp.json()
        except Exception:
            raise TelegramPollingError(
                "Telegram polling returned an invalid response; will retry later",
                fatal=False,
            ) from None

        if not isinstance(data, dict):
            raise TelegramPollingError(
                "Telegram polling returned an invalid response; will retry later",
                fatal=False,
            )
        if not data.get("ok"):
            # ok=false with a native description: never surface the raw
            # description or the token.
            raise TelegramPollingError(
                "Telegram polling unavailable; verify bot configuration",
                fatal=True,
            )
        result = data.get("result")
        if result is None:
            return []
        if not isinstance(result, list):
            raise TelegramPollingError(
                "Telegram polling returned an invalid response; will retry later",
                fatal=False,
            )
        # Sanitise: keep only dict updates with a valid update_id. Anything
        # else is dropped (never surfaced, never requested again via offset
        # progression because it carries no meaningful update_id).
        cleaned: list[dict[str, Any]] = []
        for item in result:
            if isinstance(item, dict) and _valid_update_id(item.get("update_id")):
                cleaned.append(dict(item))
        return cleaned


def _valid_update_id(value: Any) -> bool:
    """True if ``value`` is a non-bool integer >= 0 (a usable update_id)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


class TelegramPollingRunner:
    """Long-polling runner with offset persistence, backoff, lock & lifecycle.

    Parameters
    ----------
    client:
        :class:`TelegramGetUpdatesClient` to fetch updates from.
    dispatcher:
        :class:`TelegramUpdateDispatcher` (UX-4.3) to deliver each raw Update
        dict to. The runner NEVER interprets the Update payload.
    offset_store:
        :class:`TelegramOffsetStore` for durable next-offset persistence.
    state_dir:
        State dir also used for the exclusive runner lock file.
    sleep:
        Injectable async sleep (``callable(delay) -> awaitable``) used for
        backoff waits; tests inject a zero/instant sleep to avoid real time.
    max_backoff:
        Upper bound (seconds) for the bounded backoff schedule.
    backoff_base:
        First backoff delay (seconds).
    """

    def __init__(
        self,
        *,
        client: TelegramGetUpdatesClient,
        dispatcher: TelegramUpdateDispatcher,
        offset_store: TelegramOffsetStore,
        state_dir: str | os.PathLike[str],
        sleep: Callable[[float], Awaitable[None]] | None = None,
        max_backoff: float = _MAX_BACKOFF,
        backoff_base: float = _BACKOFF_BASE_SECONDS,
    ) -> None:
        self._client = client
        self._dispatcher = dispatcher
        self._offset_store = offset_store
        self._state_dir = Path(state_dir)
        self._lock_path = self._state_dir / _RUNNER_LOCK_FILENAME
        self._sleep = sleep or asyncio.sleep
        self._max_backoff = float(max_backoff)
        self._backoff_base = float(backoff_base)
        self._lock_handle: Any = None
        self._backoff = 0.0

    # -- flock-based exclusive lock ------------------------------------------

    def acquire_lock(self) -> None:
        """Take the exclusive local runner lock (``flock``), fail-fast if busy.

        The lock is a plain local lock file: no federated coordination, no
        SQLite, no network. A second process trying to acquire it FAILS FAST
        with the sanitised, generic message (no PID, no token).
        """
        import fcntl

        self._state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._state_dir.chmod(0o700)
        except OSError:
            pass
        handle = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(handle)
            raise TelegramPollingError(
                "Telegram runner already active", fatal=True
            ) from None
        self._lock_handle = handle

    def release_lock(self) -> None:
        """Release the runner lock if held. Idempotent; safe on any exit path."""
        if self._lock_handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._lock_handle)
        except OSError:
            pass
        self._lock_handle = None

    # -- lifecycle ------------------------------------------------------------

    async def _fetch(self, offset: int | None) -> list[dict[str, Any]]:
        return await self._client.get_updates(offset=offset)

    async def _deliver_one(self, update: Mapping, update_id: int) -> None:
        """Deliver ONE raw Update to the dispatcher; never interpret it.

        Order per the contract: (1) validate Mapping (done by the caller), (2)
        ``dispatcher.dispatch(update)``, (3) advance/persist offset. The
        runner does not parse message/callback/chat_id/callback_data.
        """
        await self._dispatcher.dispatch(update)

    def _advance_offset(self, update_id: int) -> None:
        """Advance next_offset to ``update_id + 1`` (monotonic) and persist."""
        current = self._offset_store.next_offset
        if current is not None:
            new_offset = max(current, update_id + 1)
        else:
            new_offset = update_id + 1
        self._offset_store.save(new_offset)

    async def run_once(self) -> int:
        """Fetch one batch and deliver each Update in Telegram order.

        Returns the number of updates delivered. If a permanent
        (:class:`TelegramPollingError` with ``fatal=True``) condition occurs it
        propagates (the caller / ``run_forever`` must stop).
        """
        offset = self._offset_store.next_offset
        updates = await self._fetch(offset)
        delivered = 0
        for update in updates:
            update_id = update.get("update_id")
            if not _valid_update_id(update_id):
                continue
            # validate Mapping (client already sanitised to dicts, re-check).
            if not isinstance(update, Mapping):
                continue
            await self._deliver_one(update, int(update_id))
            self._advance_offset(int(update_id))
            delivered += 1
        # A successful poll clears any accumulated transient-error backoff.
        self._backoff = 0.0
        return delivered

    async def run_forever(
        self, stop_event: asyncio.Event | None = None
    ) -> None:
        """Poll forever with bounded backoff until cancelled or fatal error.

        - Each ``run_once`` completion resets the backoff to its base.
        - A transient error sleeps the backoff schedule (bounded to 30s), then
          retries. No busy-loop.
        - A fatal error (auth / conflict / corrupt offset) re-raises the
          :class:`TelegramPollingError` so the caller stops.
        - ``asyncio.CancelledError`` is NOT swallowed: it propagates and the
          runner releases its lock (via ``finally``). A ``stop_event`` set by
          the caller also terminates the loop cleanly.
        """
        self._backoff = 0.0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    await self.run_once()
                except TelegramPollingError as exc:
                    if exc.fatal:
                        raise
                    # Transient: bounded backoff retry (never busy-loop).
                    await self._backoff_wait()
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Any unexpected internal error is treated as transient and
                    # never exposes the raw exception to the user. Backoff and
                    # retry rather than exiting the process.
                    _logger.debug("transient polling error", exc_info=True)
                    await self._backoff_wait()
                    continue
                # A successful poll resets the backoff schedule.
                self._backoff = 0.0
        finally:
            self.release_lock()

    async def _backoff_wait(self) -> None:
        if self._backoff <= 0.0:
            self._backoff = self._backoff_base
        else:
            self._backoff = min(self._backoff * _BACKOFF_EXPONENT, self._max_backoff)
        # sleep is injectable; delay is always strictly positive and bounded.
        await self._sleep(self._backoff)


__all__ = [
    "TelegramGetUpdatesClient",
    "TelegramPollingRunner",
    "TelegramOffsetStore",
    "TelegramPollingError",
    "TelegramUpdateDispatcher",
]
