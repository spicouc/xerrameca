"""Transport-independent command layer for Xerrameca federation.

XerramecaCommandService knows nothing about Telegram, Pluribus or any
transport. It wraps the local node federation API (dialogue service,
trust store) and exposes high-level operations reused by CLI, Telegram,
MCP and web adapters. Wizard session state lives only in the UX layer.
"""

from __future__ import annotations

from typing import Any

from ..node.trust import list_peers
from .dto import AgentChoice, ConversationSummary


class XerramecaCommandService:
    def __init__(self, state_dir: str, *, node_port: int = 8891) -> None:
        self.state_dir = state_dir
        self.node_port = node_port

    def list_agents(self, *, check_online: bool = False) -> list[AgentChoice]:
        peers = list_peers(self.state_dir)
        choices: list[AgentChoice] = []
        for peer in peers:
            if peer.trust_status != "trusted":
                continue
            online: bool | None = None
            if check_online:
                online = self._probe_online(peer.endpoint)
            choices.append(
                AgentChoice(
                    node_id=peer.node_id,
                    display_name=peer.display_name,
                    endpoint=peer.endpoint,
                    trusted=True,
                    online=online,
                )
            )
        return choices

    @staticmethod
    def _probe_online(endpoint: str) -> bool:
        import httpx

        try:
            r = httpx.get(f"{endpoint.rstrip('/')}/health", timeout=3.0)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False
    def list_conversations(
        self, *, status: str | None = None, peer_node_id: str | None = None, limit: int | None = None
    ) -> list[ConversationSummary]:
        from ..node.dialogue import FederatedDialogueService

        dialogue = FederatedDialogueService(self.state_dir)
        summaries: list[ConversationSummary] = []
        for cid in dialogue.list_conversations():
            view = dialogue.get(cid)
            if status is not None and view.status != status:
                continue
            if peer_node_id is not None and peer_node_id not in view.participant_node_ids:
                continue
            summaries.append(
                ConversationSummary(
                    id=view.id,
                    objective=view.objective,
                    status=view.status,
                    coordinator_id=view.coordinator_id,
                    coordinator_epoch=view.coordinator_epoch,
                    current_round=view.current_round,
                    max_rounds=view.max_rounds,
                    participants=view.participant_node_ids,
                )
            )
        if limit is not None:
            summaries = summaries[:limit]
        return summaries

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        from ..node.dialogue import FederatedDialogueService

        return FederatedDialogueService(self.state_dir).get(conversation_id).to_dict()

    def sync_conversation(self, conversation_id: str) -> dict[str, Any]:
        import httpx

        from ..node.identity import load_node_state

        state = load_node_state(self.state_dir)
        with open(state.local_api_key_path) as fh:
            api_key = fh.read().strip()
        r = httpx.post(
            f"http://127.0.0.1:{self.node_port}/v1/node/federation/conversations/{conversation_id}/sync",
            headers={"X-API-Key": api_key},
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()

    def create_conversation(
        self,
        *,
        peer_node_id: str,
        objective: str,
        max_rounds: int = 5,
        delay_seconds: int = 0,
    ) -> dict[str, Any]:
        import httpx

        from ..node.identity import load_node_state

        state = load_node_state(self.state_dir)
        with open(state.local_api_key_path) as fh:
            api_key = fh.read().strip()
        r = httpx.post(
            f"http://127.0.0.1:{self.node_port}/v1/node/federation/conversations",
            headers={"X-API-Key": api_key},
            json={
                "peer_node_id": peer_node_id,
                "objective": objective,
                "max_rounds": max_rounds,
                "delay_seconds": delay_seconds,
            },
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()
