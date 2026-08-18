"""Optional user-facing integrations.

Nothing in this package is required by the federated runtime.
"""

from .telegram import TelegramMode, TelegramUXAdapter

__all__ = ["TelegramMode", "TelegramUXAdapter"]
