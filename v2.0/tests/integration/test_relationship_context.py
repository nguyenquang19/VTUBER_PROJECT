from __future__ import annotations

from services.agent.conversation_context import ConversationContextComposer, ConversationContextConfig
from interfaces.state import AgentStateSnapshot


class _RelationshipContext:
    def __init__(self) -> None:
        self.viewer_ids: list[str | None] = []

    def render_context(self, viewer_id=None) -> str:
        self.viewer_ids.append(viewer_id)
        return "[Grounded relationship]\nApproved note [evidence=e1]: likes cats"


def test_conversation_composer_injects_relationship_as_system_context() -> None:
    relationship = _RelationshipContext()
    composer = ConversationContextComposer(
        ConversationContextConfig(max_chars=1400, evidence_items=3, item_max_chars=220),
        relationship_context=relationship,
    )
    rendered = composer.render(
        AgentStateSnapshot(),
        "hello", viewer_id="raw-viewer-id",
    )
    assert relationship.viewer_ids == ["raw-viewer-id"]
    assert "likes cats" in rendered
    assert "evidence=e1" in rendered
    assert "raw-viewer-id" not in rendered
