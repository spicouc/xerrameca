"""FederatedTurnClaimAdapter — Phase 4F.

Implements TurnClaimPort for the federated Xerrameca protocol.
Uses RemoteDialogueClient to claim turns via the federation inbox API.
Propagates conversation_id, turn_id, sequence, epoch from TaskEnvelope.

Error classification:
- transport_timeout: httpx ReadTimeout / ConnectTimeout
- claim_conflict_409: HTTP 409 from server (turn already claimed)
- stale_epoch: HTTP 409 with stale_epoch in message
- conversation_completed: HTTP 409 with completed in message
- transport_failure: other connection errors
"""
from __future__ import annotations

import httpx
from typing import TYPE_CHECKING, Optional

from xerrameca.transport.interfaces import TurnClaimPort
from xerrameca.transport.models import TaskEnvelope, ClaimDecision

if TYPE_CHECKING:
    from xerrameca.node.dialogue_transport import RemoteDialogueClient


class FederatedTurnClaimAdapter(TurnClaimPort):
    """Federated turn claim via RemoteDialogueClient.

    Classifies errors:
    - transport_timeout: network read/connect timeout
    - claim_conflict_409: turn already claimed by another node (HTTP 409)
    - stale_epoch: epoch mismatch (HTTP 409 stale)
    - conversation_completed: conversation is done (HTTP 409)
    - transport_failure: other connection/network errors
    """

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
        if self.federated_client is None:
            from xerrameca.node.dialogue_transport import RemoteDialogueClient
            self.federated_client = RemoteDialogueClient(self.state_dir)
        return self.federated_client

    def claim_turn(self, envelope: TaskEnvelope) -> ClaimDecision:
        """Claim a turn via the federated dialogue client.

        Propagates conversation_id, expected_epoch from envelope.
        Classifies errors based on exception type/message.
        """
        try:
            client = self._get_client()
            client.claim(
                conversation_id=envelope.conversation_id,
                expected_epoch=envelope.epoch,
                timeout_seconds=self.timeout_seconds,
            )
            return ClaimDecision(ok=True, reason=None)
        except httpx.ReadTimeout:
            return ClaimDecision(ok=False, reason="transport_timeout")
        except httpx.ConnectTimeout:
            return ClaimDecision(ok=False, reason="transport_timeout")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                body = exc.response.text.lower()
                if "stale" in body or "epoch" in body:
                    return ClaimDecision(ok=False, reason="stale_epoch")
                if "complet" in body or "done" in body:
                    return ClaimDecision(ok=False, reason="conversation_completed")
                return ClaimDecision(ok=False, reason="claim_conflict_409")
            return ClaimDecision(ok=False, reason="transport_failure")
        except (ConnectionError, OSError) as exc:
            return ClaimDecision(ok=False, reason="transport_failure")
        except Exception as exc:
            msg = str(exc).lower()
            if "already claimed" in msg or "409" in msg:
                return ClaimDecision(ok=False, reason="claim_conflict_409")
            if "stale" in msg or "epoch" in msg:
                return ClaimDecision(ok=False, reason="stale_epoch")
            if "complet" in msg:
                return ClaimDecision(ok=False, reason="conversation_completed")
            return ClaimDecision(ok=False, reason="transport_failure")
