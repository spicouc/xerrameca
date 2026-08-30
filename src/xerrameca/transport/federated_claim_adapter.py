
from __future__ import annotations

from typing import Optional

from xerrameca.transport.interfaces import TurnClaimPort
from xerrameca.transport.models import TaskEnvelope, ClaimDecision

try:
    from xerrameca.node.dialogue_transport import RemoteDialogueClient
except ImportError:
    RemoteDialogueClient = None

class FederatedTurnClaimAdapter(TurnClaimPort):
    def __init__(
        self,
        state_dir: str,
        *,
        timeout_seconds: float = 10.0,
        federated_client: Optional["RemoteDialogueClient"] = None,
    ) -> None:
        self.state_dir = state_dir
        self.timeout_seconds = timeout_seconds
        self.federated_client: Optional[RemoteDialogueClient] = federated_client

    def _get_client(self) -> "RemoteDialogueClient":
        if self.federated_client is None and RemoteDialogueClient is not None:
            self.federated_client = RemoteDialogueClient(self.state_dir)
        if self.federated_client is None:
            raise ConnectionError("federated client unavailable")
        return self.federated_client

    def claim_turn(self, envelope: TaskEnvelope) -> ClaimDecision:
        try:
            client = self._get_client()
            view = client.claim(
                conversation_id=envelope.conversation_id,
                expected_epoch=envelope.epoch,
                timeout_seconds=self.timeout_seconds,
            )
            return ClaimDecision(ok=True, reason=None)
        except Exception as exc:
            return ClaimDecision(ok=False, reason="transport_failure")
