from shared.contracts.payloads import IngestionRecord, EventType
from node1_ingestion.scorer import tier1_score
from node1_ingestion.rate_limiter import RateLimiter
from shared.utils.mocks import mock_record

def test_contract_fields():
    r = mock_record().to_dict()
    assert set(r) == {"event_type","user_id","display_name","content",
                      "sent_at","tier1_score","timing"}
    assert r["event_type"] == "chat"
    assert r["timing"]["t0_received"] is not None

def test_score_bounds():
    assert 0.0 <= tier1_score("cứu với") <= 1.0
    assert tier1_score("giúp mình?") > tier1_score("hi")

def test_rate_limit():
    rl = RateLimiter(window_s=10)
    assert rl.allow("u1") is True
    assert rl.allow("u1") is False   # trong window -> chặn
    assert rl.allow("u2") is True    # user khác không bị ảnh hưởng