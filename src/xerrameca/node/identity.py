from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..adapters.local_identity import LocalIdentityAdapter
from ..domain.errors import ConflictError, ProviderUnavailableError


NODE_STATE_FILENAME = "node.json"
PRIVATE_KEY_FILENAME = "node-private-key.pem"
LOCAL_API_KEY_FILENAME = "local-agent-api-key"
LOCAL_IDENTITIES_FILENAME = "local-identities.json"
NODE_DB_FILENAME = "xerrameca.db"


@dataclass(frozen=True, slots=True)
class NodeState:
    node_id: str
    agent_id: str
    display_name: str
    public_key: str
    endpoint: str
    db_path: str
    state_dir: str

    @property
    def private_key_path(self) -> str:
        return str(Path(self.state_dir) / PRIVATE_KEY_FILENAME)

    @property
    def local_api_key_path(self) -> str:
        return str(Path(self.state_dir) / LOCAL_API_KEY_FILENAME)

    @property
    def local_identity_path(self) -> str:
        return str(Path(self.state_dir) / LOCAL_IDENTITIES_FILENAME)

    def public_dict(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "public_key": self.public_key,
            "endpoint": self.endpoint,
            "db_path": self.db_path,
        }


def _public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _node_id(public_key_bytes: bytes) -> str:
    digest = hashlib.sha256(public_key_bytes).hexdigest()
    return f"xn_{digest[:32]}"


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _write_text_private(path: Path, content: str) -> None:
    _write_private(path, content.encode("utf-8"))


def initialize_node(
    state_dir: str | Path,
    *,
    agent_id: str,
    display_name: str,
    endpoint: str,
) -> NodeState:
    """Create durable per-agent node identity and local storage.

    Initialization is intentionally non-destructive: if node state already
    exists the caller must load it rather than silently replacing identity.
    """

    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass

    state_path = root / NODE_STATE_FILENAME
    if state_path.exists():
        raise ConflictError(f"node already initialized: {root}")

    agent_id = agent_id.strip()
    display_name = display_name.strip()
    endpoint = endpoint.strip()
    if not agent_id or not display_name or not endpoint:
        raise ValueError("agent_id, display_name and endpoint are required")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = _public_key_bytes(private_key)
    public_b64 = base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")
    node_id = _node_id(public_raw)

    private_path = root / PRIVATE_KEY_FILENAME
    api_key_path = root / LOCAL_API_KEY_FILENAME
    identities_path = root / LOCAL_IDENTITIES_FILENAME
    db_path = root / NODE_DB_FILENAME

    local_api_key = secrets.token_urlsafe(32)
    _write_private(private_path, private_pem)
    _write_text_private(api_key_path, local_api_key + "\n")

    local_identities = {
        "agents": [
            {
                "id": agent_id,
                "name": display_name,
                "api_key_sha256": LocalIdentityAdapter.hash_api_key(local_api_key),
                "permissions": {"read": True, "write": True, "admin": True},
                "allowed_scopes": ["shared"],
                "capabilities": {"xerrameca": True, "node": True},
                "is_active": True,
            }
        ]
    }
    _write_text_private(
        identities_path,
        json.dumps(local_identities, sort_keys=True, indent=2) + "\n",
    )

    fd = os.open(db_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)

    state = NodeState(
        node_id=node_id,
        agent_id=agent_id,
        display_name=display_name,
        public_key=public_b64,
        endpoint=endpoint,
        db_path=str(db_path),
        state_dir=str(root),
    )
    _write_text_private(
        state_path,
        json.dumps(asdict(state), sort_keys=True, indent=2) + "\n",
    )
    return state


def _decode_public_key(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise ProviderUnavailableError("invalid node public key encoding") from exc
    if len(decoded) != 32:
        raise ProviderUnavailableError("invalid Ed25519 public key length")
    return decoded


def load_node_state(state_dir: str | Path) -> NodeState:
    root = Path(state_dir)
    state_path = root / NODE_STATE_FILENAME
    private_path = root / PRIVATE_KEY_FILENAME
    identities_path = root / LOCAL_IDENTITIES_FILENAME
    api_key_path = root / LOCAL_API_KEY_FILENAME

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        state = NodeState(
            node_id=str(raw["node_id"]),
            agent_id=str(raw["agent_id"]),
            display_name=str(raw["display_name"]),
            public_key=str(raw["public_key"]),
            endpoint=str(raw["endpoint"]),
            db_path=str(raw["db_path"]),
            state_dir=str(root),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderUnavailableError(f"invalid node state: {root}") from exc

    try:
        private_key = serialization.load_pem_private_key(
            private_path.read_bytes(), password=None
        )
    except Exception as exc:
        raise ProviderUnavailableError("node private key unavailable") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ProviderUnavailableError("node private key is not Ed25519")

    derived_public = _public_key_bytes(private_key)
    declared_public = _decode_public_key(state.public_key)
    if not secrets.compare_digest(derived_public, declared_public):
        raise ProviderUnavailableError("node keypair does not match state")
    if state.node_id != _node_id(derived_public):
        raise ProviderUnavailableError("node_id does not match public key")
    if not identities_path.exists() or not api_key_path.exists():
        raise ProviderUnavailableError("node local identity material unavailable")

    return state
