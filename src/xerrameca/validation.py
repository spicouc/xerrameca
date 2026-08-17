from __future__ import annotations

import json
import re
from typing import Any

from .domain.errors import ValidationError


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def clean_identifier(value: str, field: str) -> str:
    value = (value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValidationError(f"{field} invàlid")
    return value


def clean_scope(value: str) -> str:
    value = (value or "").strip()
    if not _SCOPE_RE.fullmatch(value):
        raise ValidationError("scope invàlid")
    return value


def clean_content(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationError("content buit")
    if len(value) > 100_000:
        raise ValidationError("content massa llarg")
    return value


def clean_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError("metadata no és serialitzable a JSON") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValidationError("metadata massa gran")
    return value
