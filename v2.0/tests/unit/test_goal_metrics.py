from prometheus_client import CollectorRegistry

from services.operations.metrics import MetricsCollector


def test_goal_lifecycle_metrics_and_active_age_are_exported() -> None:
    metrics = MetricsCollector(registry=CollectorRegistry())
    metrics.record_goal_event("created", "ack_donation")
    metrics.record_goal_event("operator_override", "pin")
    metrics.set_goal_active_age(12.5)
    metrics.record_intention_event("advanced", "verified_delivery")
    metrics.set_intention_active(5.5, 2)
    text = metrics.prometheus_text().decode("utf-8")
    assert 'mai_agent_goals_total{outcome="created",reason="ack_donation"} 1.0' in text
    assert 'mai_agent_goals_total{outcome="operator_override",reason="pin"} 1.0' in text
    assert "mai_agent_goal_active_age_seconds 12.5" in text
    assert 'mai_agent_intentions_total{outcome="advanced",reason="verified_delivery"} 1.0' in text
    assert "mai_agent_intention_active_age_seconds 5.5" in text
    assert "mai_agent_intention_current_step 2.0" in text
    assert metrics.goal_snapshot()["events"]["created:ack_donation"] == 1
    assert metrics.intention_snapshot()["events"]["advanced:verified_delivery"] == 1
