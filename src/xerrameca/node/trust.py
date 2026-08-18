from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..domain.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .identity import PRIVATE_KEY_FILENAME, NodeState, load_node_state

PEERS_FILENAME = "peers.json"
INVITES_FILENAME = "invites.json"
INVITE_VERSION = 1
DEFAULT_INVITE_TTL_SECONDS = 600
MAX_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class PeerRecord:
    node_id: str
    agent_id: str
    display_name: str
    public_key: str
    endpoint: str
    trust_status: str
    created_at: int
    updated_at: int
    capabilities: dict[str, object]

    def public_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ValidationError("invalid base64url value") from exc


def _public_key_bytes(value: str) -> bytes:
    raw = _unb64(value)
    if len(raw) != 32:
        raise ValidationError("invalid Ed25519 public key length")
    return raw


def _derived_node_id(public_key: str) -> str:
    return f"xn_{hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:32]}"


def _load_private_key(state_dir: str | Path) -> Ed25519PrivateKey:
    path = Path(state_dir) / PRIVATE_KEY_FILENAME
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        raise ValidationError("node private key unavailable") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValidationError("node private key is not Ed25519")
    return key


def _write_private_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _load_json(path: Path, *, default: object) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid local trust store: {path.name}") from exc


def _peers_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / PEERS_FILENAME


def _invites_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / INVITES_FILENAME


def _load_peer_rows(state_dir: str | Path) -> list[dict[str, Any]]:
    raw = _load_json(_peers_path(state_dir), default={"peers": []})
    rows = raw.get("peers") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValidationError("invalid peers store")
    return [row for row in rows if isinstance(row, dict)]


def list_peers(
    state_dir: str | Path, *, include_revoked: bool = False
) -> list[PeerRecord]:
    records: list[PeerRecord] = []
    for row in _load_peer_rows(state_dir):
        try:
            record = PeerRecord(
                node_id=str(row["node_id"]),
                agent_id=str(row["agent_id"]),
                display_name=str(row["display_name"]),
                public_key=str(row["public_key"]),
                endpoint=str(row["endpoint"]),
                trust_status=str(row["trust_status"]),
                created_at=int(row["created_at"]),
                updated_at=int(row["updated_at"]),
                capabilities=dict(row.get("capabilities") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid peer record") from exc
        if include_revoked or record.trust_status == "trusted":
            records.append(record)
    return sorted(records, key=lambda item: (item.display_name.lower(), item.node_id))


def get_peer(state_dir: str | Path, node_id: str) -> PeerRecord:
    for peer in list_peers(state_dir, include_revoked=True):
        if peer.node_id == node_id:
            return peer
    raise NotFoundError("peer no trobat")


def _upsert_peer(
    state_dir: str | Path,
    identity: dict[str, Any],
    *,
    status: str = "trusted",
) -> PeerRecord:
    local = load_node_state(state_dir)
    node_id = str(identity.get("node_id") or "")
    public_key = str(identity.get("public_key") or "")
    if not node_id or not public_key or _derived_node_id(public_key) != node_id:
        raise ValidationError("peer node_id/public_key mismatch")
    if node_id == local.node_id:
        raise ConflictError("cannot trust local node as peer")

    now = int(time.time())
    rows = _load_peer_rows(state_dir)
    existing = next((row for row in rows if row.get("node_id") == node_id), None)
    created_at = int(existing.get("created_at", now)) if existing else now
    record = PeerRecord(
        node_id=node_id,
        agent_id=str(identity.get("agent_id") or ""),
        display_name=str(identity.get("display_name") or identity.get("agent_id") or node_id),
        public_key=public_key,
        endpoint=str(identity.get("endpoint") or ""),
        trust_status=status,
        created_at=created_at,
        updated_at=now,
        capabilities=dict(identity.get("capabilities") or {"xerrameca": True, "node": True}),
    )
    if not record.agent_id or not record.endpoint:
        raise ValidationError("peer agent_id and endpoint are required")

    updated = [row for row in rows if row.get("node_id") != node_id]
    updated.append(record.public_dict())
    _write_private_json(_peers_path(state_dir), {"peers": updated})
    return record


def revoke_peer(state_dir: str | Path, node_id: str) -> PeerRecord:
    peer = get_peer(state_dir, node_id)
    return _upsert_peer(state_dir, peer.public_dict(), status="revoked")


def _load_invite_rows(state_dir: str | Path) -> list[dict[str, Any]]:
    raw = _load_json(_invites_path(state_dir), default={"invites": []})
    rows = raw.get("invites") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValidationError("invalid invites store")
    return [row for row in rows if isinstance(row, dict)]


def _save_invite_rows(state_dir: str | Path, rows: list[dict[str, Any]]) -> None:
    _write_private_json(_invites_path(state_dir), {"invites": rows})


def create_invite(
    state_dir: str | Path, *, ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS
) -> str:
    if ttl_seconds < 30 or ttl_seconds > 86400:
        raise ValidationError("invite ttl must be between 30 and 86400 seconds")

    state = load_node_state(state_dir)
    now = int(time.time())
    invite_id = uuid.uuid4().hex
    payload = {
        "v": INVITE_VERSION,
        "invite_id": invite_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "issuer": state.public_dict(),
        "nonce": _b64(os.urandom(18)),
    }
    payload_bytes = _canonical(payload)
    signature = _load_private_key(state_dir).sign(payload_bytes)

    rows = _load_invite_rows(state_dir)
    rows.append(
        {
            "invite_id": invite_id,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "status": "pending",
            "used_at": None,
            "used_by_node_id": None,
        }
    )
    _save_invite_rows(state_dir, rows)
    return f"{_b64(payload_bytes)}.{_b64(signature)}"


def verify_invite(token: str, *, now: int | None = None) -> dict[str, Any]:
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError as exc:
        raise ValidationError("invalid invite token") from exc
    payload_bytes = _unb64(payload_part)
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid invite payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != INVITE_VERSION:
        raise ValidationError("unsupported invite version")

    issuer = payload.get("issuer")
    if not isinstance(issuer, dict):
        raise ValidationError("invite issuer missing")
    public_key = str(issuer.get("public_key") or "")
    node_id = str(issuer.get("node_id") or "")
    if not public_key or _derived_node_id(public_key) != node_id:
        raise ValidationError("invite issuer identity mismatch")

    try:
        Ed25519PublicKey.from_public_bytes(_public_key_bytes(public_key)).verify(
            _unb64(signature_part), payload_bytes
        )
    except InvalidSignature as exc:
        raise ForbiddenError("invalid invite signature") from exc

    current = int(time.time()) if now is None else now
    try:
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("invalid invite timestamps") from exc
    if issued_at > current + MAX_CLOCK_SKEW_SECONDS:
        raise ValidationError("invite issued in the future")
    if expires_at < current:
        raise ForbiddenError("invite expired")
    if not str(payload.get("invite_id") or ""):
        raise ValidationError("invite id missing")
    return payload


def _acceptance_message(payload: dict[str, Any]) -> bytes:
    return _canonical(payload)


def build_acceptance(state_dir: str | Path, invite_token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    invite = verify_invite(invite_token)
    local = load_node_state(state_dir)
    issuer = invite["issuer"]
    if issuer["node_id"] == local.node_id:
        raise ConflictError("cannot accept own invite")

    payload = {
        "v": INVITE_VERSION,
        "invite_id": invite["invite_id"],
        "timestamp": int(time.time()),
        "acceptor": local.public_dict(),
    }
    signature = _load_private_key(state_dir).sign(_acceptance_message(payload))
    return {"payload": payload, "signature": _b64(signature)}, invite


def _verify_acceptance(acceptance: dict[str, Any]) -> dict[str, Any]:
    payload = acceptance.get("payload")
    signature = acceptance.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise ValidationError("invalid invite acceptance")
    if payload.get("v") != INVITE_VERSION:
        raise ValidationError("unsupported acceptance version")
    acceptor = payload.get("acceptor")
    if not isinstance(acceptor, dict):
        raise ValidationError("acceptor identity missing")
    public_key = str(acceptor.get("public_key") or "")
    node_id = str(acceptor.get("node_id") or "")
    if not public_key or _derived_node_id(public_key) != node_id:
        raise ValidationError("acceptor identity mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(_public_key_bytes(public_key)).verify(
            _unb64(signature), _acceptance_message(payload)
        )
    except InvalidSignature as exc:
        raise ForbiddenError("invalid acceptance signature") from exc
    timestamp = int(payload.get("timestamp") or 0)
    if abs(int(time.time()) - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise ForbiddenError("stale invite acceptance")
    return payload


def _signed_confirmation(
    state_dir: str | Path, *, invite_id: str, accepted_node_id: str
) -> dict[str, Any]:
    local = load_node_state(state_dir)
    payload = {
        "v": INVITE_VERSION,
        "invite_id": invite_id,
        "issuer_node_id": local.node_id,
        "accepted_node_id": accepted_node_id,
        "timestamp": int(time.time()),
    }
    return {
        "payload": payload,
        "signature": _b64(_load_private_key(state_dir).sign(_canonical(payload))),
    }


def accept_incoming(
    state_dir: str | Path, acceptance: dict[str, Any]
) -> dict[str, Any]:
    payload = _verify_acceptance(acceptance)
    invite_id = str(payload.get("invite_id") or "")
    acceptor = payload["acceptor"]
    acceptor_node_id = str(acceptor["node_id"])

    rows = _load_invite_rows(state_dir)
    row = next((item for item in rows if item.get("invite_id") == invite_id), None)
    if row is None:
        raise NotFoundError("invite not found")
    if int(row.get("expires_at") or 0) < int(time.time()):
        raise ForbiddenError("invite expired")

    if row.get("status") == "used":
        if row.get("used_by_node_id") != acceptor_node_id:
            raise ConflictError("invite already used by another node")
        return _signed_confirmation(
            state_dir, invite_id=invite_id, accepted_node_id=acceptor_node_id
        )

    _upsert_peer(state_dir, acceptor, status="trusted")
    now = int(time.time())
    for item in rows:
        if item.get("invite_id") == invite_id:
            item["status"] = "used"
            item["used_at"] = now
            item["used_by_node_id"] = acceptor_node_id
    _save_invite_rows(state_dir, rows)
    return _signed_confirmation(
        state_dir, invite_id=invite_id, accepted_node_id=acceptor_node_id
    )


def complete_acceptance(
    state_dir: str | Path,
    invite_token: str,
    confirmation: dict[str, Any],
) -> PeerRecord:
    invite = verify_invite(invite_token)
    issuer = invite["issuer"]
    payload = confirmation.get("payload")
    signature = confirmation.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise ValidationError("invalid invite confirmation")
    if payload.get("invite_id") != invite.get("invite_id"):
        raise ValidationError("confirmation invite mismatch")
    local = load_node_state(state_dir)
    if payload.get("accepted_node_id") != local.node_id:
        raise ValidationError("confirmation accepted node mismatch")
    if payload.get("issuer_node_id") != issuer.get("node_id"):
        raise ValidationError("confirmation issuer mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(
            _public_key_bytes(str(issuer["public_key"]))
        ).verify(_unb64(signature), _canonical(payload))
    except InvalidSignature as exc:
        raise ForbiddenError("invalid confirmation signature") from exc
    timestamp = int(payload.get("timestamp") or 0)
    if abs(int(time.time()) - timestamp) > MAX_CLOCK_SKEW_SECONDS:
        raise ForbiddenError("stale invite confirmation")
    return _upsert_peer(state_dir, issuer, status="trusted")


async def accept_invite_over_http(
    state_dir: str | Path,
    invite_token: str,
    *,
    timeout_seconds: float = 10.0,
) -> PeerRecord:
    acceptance, invite = build_acceptance(state_dir, invite_token)
    issuer = invite["issuer"]
    endpoint = str(issuer.get("endpoint") or "").rstrip("/")
    if not endpoint:
        raise ValidationError("invite issuer endpoint missing")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            f"{endpoint}/v1/node/invites/accept", json=acceptance
        )
    if response.status_code >= 400:
        raise ForbiddenError(f"invite acceptance rejected: HTTP {response.status_code}")
    try:
        confirmation = response.json()
    except ValueError as exc:
        raise ValidationError("invalid invite confirmation response") from exc
    return complete_acceptance(state_dir, invite_token, confirmation)


def _peer_request_message(
    method: str, path: str, timestamp: str, body: bytes
) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode("utf-8")


def sign_peer_request(
    state_dir: str | Path,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int | None = None,
) -> dict[str, str]:
    state = load_node_state(state_dir)
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    signature = _load_private_key(state_dir).sign(
        _peer_request_message(method, path, ts, body)
    )
    return {
        "X-Xerrameca-Node": state.node_id,
        "X-Xerrameca-Timestamp": ts,
        "X-Xerrameca-Signature": _b64(signature),
    }


def verify_peer_request(
    state_dir: str | Path,
    *,
    method: str,
    path: str,
    body: bytes,
    node_id: str,
    timestamp: str,
    signature: str,
) -> PeerRecord:
    peer = get_peer(state_dir, node_id)
    if peer.trust_status != "trusted":
        raise ForbiddenError("peer revoked or not trusted")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise ForbiddenError("invalid peer timestamp") from exc
    if abs(int(time.time()) - ts) > MAX_CLOCK_SKEW_SECONDS:
        raise ForbiddenError("stale peer request")
    try:
        Ed25519PublicKey.from_public_bytes(_public_key_bytes(peer.public_key)).verify(
            _unb64(signature), _peer_request_message(method, path, timestamp, body)
        )
    except InvalidSignature as exc:
        raise ForbiddenError("invalid peer signature") from exc
    return peer
