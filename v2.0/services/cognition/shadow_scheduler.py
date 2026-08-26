"""Compatibility import for the canonical Brain scheduler; remove in S8."""
from services.cognition.scheduler import CognitiveOpportunityScheduler

__all__ = ["CognitiveOpportunityScheduler"]
