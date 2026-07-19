from enum import Enum, auto
class State(Enum):
    BOOT = auto(); OFFLINE = auto(); IDLE = auto()
    PROCESSING = auto(); SPEAKING = auto()
    CRISIS_PRIORITY = auto(); RECOVERY = auto()