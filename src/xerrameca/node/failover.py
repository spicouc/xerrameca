from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..domain.errors import ConflictError, ForbiddenError, NotFoundError
from .dialogue import FederatedDialogueService
from .events import EventEnvelope, EventStore
from .identity import load_node_state
from .replication import ReplicationService

LEASE_EVENT = "coordinator.lease_granted"
DEFAULT_LEASE_SECONDS = 120
DEFAULT_RENEW_BEFORE_SECONDS = 40
DEFAULT_TAKEOVER_GRACE_SECONDS = 5


@dataclass(frozen=True, slots=True)
class CoordinatorLeaseStatus:
    conversation_id: str
    coordinator_id: str
    coordinator_epoch: int
    enabled: bool
    effective_lease_until: int | None
    effective_event_sequence: int | None
    latest_lease_until: int | None
    latest_event_sequence: int | None
    latest_acknowledged: bool
    takeover_grant: bool
    expired: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FailoverManager:
    """Safety-first coordinator leases and deterministic two-node failover.

    Normal lease renewals are effective for the coordinator only after the peer
    has ACKed the lease event through X7's replication cursor. A coordinator
    therefore self-fences at the last peer-acknowledged lease expiry during a
    partition. A participant may advance the epoch only after the lease it has
    actually observed is expired.
    """

    def __init__(self, state_dir: str | Path):
        self.state = load_node_state(state_dir)
        self.store = EventStore(state_dir)
        self.dialogue = FederatedDialogueService(state_dir)

    def conversation_ids(self) -> list[str]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id FROM federated_heads ORDER BY updated_at DESC"
            ).fetchall()
        return [str(row["conversation_id"]) for row in rows]

    def _lease_events(self, conversation_id: str, epoch: int) -> list[EventEnvelope]:
        return [
            event
            for event in self.store.list_events(
                conversation_id, epoch=epoch, from_sequence=1
            )
            if event.event_type == LEASE_EVENT
        ]

    def _other_participant(self, conversation_id: str, coordinator_id: str) -> str:
        view = self.dialogue.get(conversation_id)
        ids = view.participant_node_ids
        if len(ids) != 2 or coordinator_id not in ids:
            raise ConflictError("failover requires exactly two conversation participants")
        return ids[1] if ids[0] == coordinator_id else ids[0]

    def status(
        self, conversation_id: str, *, now: int | None = None
    ) -> CoordinatorLeaseStatus:
        current_time = int(time.time()) if now is None else int(now)
        head = self.store.get_head(conversation_id)
        events = self._lease_events(conversation_id, head.coordinator_epoch)
        if not events:
            return CoordinatorLeaseStatus(
                conversation_id=conversation_id,
                coordinator_id=head.coordinator_id,
                coordinator_epoch=head.coordinator_epoch,
                enabled=False,
                effective_lease_until=None,
                effective_event_sequence=None,
                latest_lease_until=None,
                latest_event_sequence=None,
                latest_acknowledged=False,
                takeover_grant=False,
                expired=False,
            )

        latest = events[-1]
        latest_until = int(latest.payload["lease_until"])
        latest_takeover = bool(latest.payload.get("takeover_grant", False))

        if head.coordinator_id == self.state.node_id:
            peer_id = self._other_participant(conversation_id, head.coordinator_id)
            cursor = self.store.cursor(peer_id, conversation_id, head.coordinator_epoch)
            effective_event: EventEnvelope | None = None
            for event in reversed(events):
                # A takeover grant is safe for its initial bounded term because
                # the previous coordinator's acknowledged lease already expired.
                if bool(event.payload.get("takeover_grant", False)) or cursor >= event.sequence:
                    effective_event = event
                    break
            effective_until = (
                int(effective_event.payload["lease_until"])
                if effective_event is not None
                else None
            )
            latest_acknowledged = latest_takeover or cursor >= latest.sequence
        else:
            # A participant only knows lease events it actually received. Its
            # local receipt is enough to delay takeover until that lease expires.
            effective_event = latest
            effective_until = latest_until
            latest_acknowledged = True

        expired = effective_until is not None and current_time >= effective_until
        return CoordinatorLeaseStatus(
            conversation_id=conversation_id,
            coordinator_id=head.coordinator_id,
            coordinator_epoch=head.coordinator_epoch,
            enabled=True,
            effective_lease_until=effective_until,
            effective_event_sequence=(
                effective_event.sequence if effective_event is not None else None
            ),
            latest_lease_until=latest_until,
            latest_event_sequence=latest.sequence,
            latest_acknowledged=latest_acknowledged,
            takeover_grant=latest_takeover,
            expired=expired,
        )

    def require_local_write_lease(
        self, conversation_id: str, *, now: int | None = None
    ) -> CoordinatorLeaseStatus:
        status = self.status(conversation_id, now=now)
        if status.coordinator_id != self.state.node_id:
            raise ForbiddenError("local node is not conversation coordinator")
        if not status.enabled:
            # Compatibility path for X8/legacy federated conversations created
            # before coordinator leases were enabled.
            return status
        if status.effective_lease_until is None:
            raise ConflictError("coordinator lease is not peer-acknowledged")
        if status.expired:
            raise ConflictError("coordinator lease expired; local writer is fenced")
        return status

    def grant_initial_lease(
        self,
        conversation_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: int | None = None,
    ) -> CoordinatorLeaseStatus:
        current_time = int(time.time()) if now is None else int(now)
        if not 15 <= lease_seconds <= 3600:
            raise ConflictError("coordinator lease must be between 15 and 3600 seconds")
        head = self.store.get_head(conversation_id)
        if head.coordinator_id != self.state.node_id:
            raise ForbiddenError("only local coordinator may grant a lease")
        if self._lease_events(conversation_id, head.coordinator_epoch):
            raise ConflictError("coordinator lease already initialized")
        self.store.append_local(
            conversation_id,
            author_id=self.state.node_id,
            event_type=LEASE_EVENT,
            payload={
                "lease_until": current_time + lease_seconds,
                "lease_seconds": lease_seconds,
                "takeover_grant": False,
            },
            expected_epoch=head.coordinator_epoch,
        )
        return self.status(conversation_id, now=current_time)

    def renew_lease(
        self,
        conversation_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: int | None = None,
    ) -> CoordinatorLeaseStatus:
        current_time = int(time.time()) if now is None else int(now)
        current = self.require_local_write_lease(conversation_id, now=current_time)
        if not current.enabled:
            raise ConflictError("coordinator lease is not enabled for this conversation")
        self.store.append_local(
            conversation_id,
            author_id=self.state.node_id,
            event_type=LEASE_EVENT,
            payload={
                "lease_until": current_time + lease_seconds,
                "lease_seconds": lease_seconds,
                "takeover_grant": False,
            },
            expected_epoch=current.coordinator_epoch,
        )
        # Until the new event is ACKed, status continues to use the previous
        # acknowledged lease as the effective fencing deadline.
        return self.status(conversation_id, now=current_time)

    def takeover(
        self,
        conversation_id: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: int | None = None,
        grace_seconds: int = DEFAULT_TAKEOVER_GRACE_SECONDS,
    ) -> CoordinatorLeaseStatus:
        current_time = int(time.time()) if now is None else int(now)
        before = self.status(conversation_id, now=current_time)
        if not before.enabled:
            raise ConflictError("automatic failover requires coordinator leases")
        if before.coordinator_id == self.state.node_id:
            raise ConflictError("current coordinator cannot take over its own epoch")
        if self.state.node_id not in self.dialogue.get(conversation_id).participant_node_ids:
            raise ForbiddenError("local node is not a conversation participant")
        if before.effective_lease_until is None:
            raise ConflictError("participant has not observed a coordinator lease")
        if current_time < before.effective_lease_until + grace_seconds:
            raise ConflictError("coordinator lease/grace period has not expired")

        new_epoch_event = self.store.advance_epoch(
            conversation_id,
            previous_epoch=before.coordinator_epoch,
            previous_coordinator_id=before.coordinator_id,
            reason="coordinator-lease-expired",
        )
        self.store.append_local(
            conversation_id,
            author_id=self.state.node_id,
            event_type=LEASE_EVENT,
            payload={
                "lease_until": current_time + lease_seconds,
                "lease_seconds": lease_seconds,
                "takeover_grant": True,
                "previous_epoch": before.coordinator_epoch,
                "previous_coordinator_id": before.coordinator_id,
                "epoch_event_id": new_epoch_event.event_id,
            },
            expected_epoch=before.coordinator_epoch + 1,
        )
        return self.status(conversation_id, now=current_time)

    async def tick(
        self,
        replication: ReplicationService | None = None,
        *,
        now: int | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        renew_before_seconds: int = DEFAULT_RENEW_BEFORE_SECONDS,
        grace_seconds: int = DEFAULT_TAKEOVER_GRACE_SECONDS,
    ) -> list[dict[str, Any]]:
        current_time = int(time.time()) if now is None else int(now)
        actions: list[dict[str, Any]] = []
        for conversation_id in self.conversation_ids():
            try:
                view = self.dialogue.get(conversation_id)
                if view.status != "active":
                    continue
                status = self.status(conversation_id, now=current_time)
                if not status.enabled:
                    continue

                if status.coordinator_id == self.state.node_id:
                    if status.effective_lease_until is None or status.expired:
                        actions.append(
                            {
                                "conversation_id": conversation_id,
                                "action": "self_fenced",
                                "epoch": status.coordinator_epoch,
                            }
                        )
                        continue
                    if (
                        status.effective_lease_until - current_time
                        <= renew_before_seconds
                    ):
                        self.renew_lease(
                            conversation_id,
                            lease_seconds=lease_seconds,
                            now=current_time,
                        )
                        replication_result = "not-attempted"
                        if replication is not None:
                            other = self._other_participant(
                                conversation_id, self.state.node_id
                            )
                            try:
                                await replication.push_missing(
                                    other,
                                    conversation_id,
                                    epoch=status.coordinator_epoch,
                                )
                                replication_result = "acked"
                            except Exception:
                                replication_result = "pending"
                        actions.append(
                            {
                                "conversation_id": conversation_id,
                                "action": "lease_renewal",
                                "epoch": status.coordinator_epoch,
                                "replication": replication_result,
                            }
                        )
                elif (
                    status.effective_lease_until is not None
                    and current_time
                    >= status.effective_lease_until + grace_seconds
                ):
                    takeover = self.takeover(
                        conversation_id,
                        lease_seconds=lease_seconds,
                        now=current_time,
                        grace_seconds=grace_seconds,
                    )
                    replication_result = "not-attempted"
                    if replication is not None:
                        try:
                            await replication.push_missing(
                                status.coordinator_id,
                                conversation_id,
                                epoch=takeover.coordinator_epoch,
                            )
                            replication_result = "acked"
                        except Exception:
                            replication_result = "pending"
                    actions.append(
                        {
                            "conversation_id": conversation_id,
                            "action": "takeover",
                            "epoch": takeover.coordinator_epoch,
                            "replication": replication_result,
                        }
                    )
            except (ConflictError, ForbiddenError, NotFoundError) as exc:
                actions.append(
                    {
                        "conversation_id": conversation_id,
                        "action": "skip",
                        "reason": exc.detail,
                    }
                )
        return actions

    async def loop(
        self,
        replication: ReplicationService,
        *,
        interval_seconds: float = 5.0,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        renew_before_seconds: int = DEFAULT_RENEW_BEFORE_SECONDS,
        grace_seconds: int = DEFAULT_TAKEOVER_GRACE_SECONDS,
    ) -> None:
        while True:
            try:
                await self.tick(
                    replication,
                    lease_seconds=lease_seconds,
                    renew_before_seconds=renew_before_seconds,
                    grace_seconds=grace_seconds,
                )
            except Exception:
                # A supervisor/failover loop is never allowed to terminate the
                # node runtime. Individual actions are retried on the next tick.
                pass
            await asyncio.sleep(interval_seconds)


class LeasedDialogueService(FederatedDialogueService):
    """Runtime dialogue service that enforces X12 coordinator fencing."""

    def __init__(self, state_dir: str | Path):
        super().__init__(state_dir)
        self.failover = FailoverManager(state_dir)

    def claim(
        self,
        conversation_id: str,
        *,
        claimant_node_id: str,
        expected_epoch: int,
        now: int | None = None,
    ):
        view = self.get(conversation_id)
        if view.coordinator_id == self.state.node_id:
            self.failover.require_local_write_lease(conversation_id, now=now)
        return super().claim(
            conversation_id,
            claimant_node_id=claimant_node_id,
            expected_epoch=expected_epoch,
            now=now,
        )

    def reply(
        self,
        conversation_id: str,
        *,
        author_node_id: str,
        content: str,
        result: str,
        expected_epoch: int,
        now: int | None = None,
    ):
        view = self.get(conversation_id)
        if view.coordinator_id == self.state.node_id:
            self.failover.require_local_write_lease(conversation_id, now=now)
        return super().reply(
            conversation_id,
            author_node_id=author_node_id,
            content=content,
            result=result,
            expected_epoch=expected_epoch,
            now=now,
        )
