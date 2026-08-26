"""Compatibility import for the canonical Operations metrics owner.

Live composition imports :mod:`services.operations.metrics`. This exact re-export
remains until the S8 compatibility-removal wave.
"""
from services.operations.metrics import MetricsCollector

__all__ = ["MetricsCollector"]
