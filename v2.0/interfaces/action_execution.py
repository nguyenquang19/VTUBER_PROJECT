"""S5 compatibility re-export; canonical contracts live in interfaces.execution."""
from interfaces.compatibility import ActionRequest, ActionResult
from interfaces.execution import (
    ActionExecutor,
    ActionVerifier,
    GeneralActionService,
    LocalActionBoundaryService,
    VerificationResult,
)

__all__ = [
    "ActionExecutor", "ActionRequest", "ActionResult", "ActionVerifier",
    "GeneralActionService", "LocalActionBoundaryService", "VerificationResult",
]
