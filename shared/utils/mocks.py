import uuid
from shared.contracts.payloads import IngestionRecord, EventType
from shared.contracts.timing import TimingTrace
from shared.utils.clock import now_ms

def mock_record(content="hello", user_id="u1", name="Tester", score=0.0) -> IngestionRecord:
    tid = str(uuid.uuid4())
    return IngestionRecord(EventType.CHAT, user_id, name, content,
                           now_ms(), score, TimingTrace(turn_id=tid, t0_received=now_ms()))