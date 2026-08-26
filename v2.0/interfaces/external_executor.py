"""S5 compatibility re-export; canonical contracts live in interfaces.execution."""
from interfaces.execution import (
    ExternalActionExecutor,
    ExternalExecutorBinding,
    ExternalExecutorRegistryService,
    OBSCommandAck,
    OBSSceneState,
    OBSSceneTransportService,
    RollbackResult,
    RollbackStatus,
)

__all__ = [
    "ExternalActionExecutor", "ExternalExecutorBinding", "ExternalExecutorRegistryService",
    "OBSCommandAck", "OBSSceneState", "OBSSceneTransportService", "RollbackResult", "RollbackStatus",
]
