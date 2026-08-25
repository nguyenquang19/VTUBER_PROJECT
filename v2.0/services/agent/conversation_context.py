"""Compatibility imports for the canonical cognition context projection.

Production composition imports `services.cognition`; this path remains only for
rollback/import compatibility until structure wave S8.
"""
from services.cognition.compatibility_context import (
    ConversationContextComposer,
    ConversationContextConfig,
)

__all__ = ["ConversationContextComposer", "ConversationContextConfig"]
