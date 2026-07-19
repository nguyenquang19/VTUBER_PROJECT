from node4_timing.collector import TimingCollector
import logging

def test_merge_partials_no_overwrite():
    c = TimingCollector(); tid = "turn-x"
    c.ingest({"turn_id": tid, "t0_received": 1000.0, "t1_enqueued": 1010.0})  # node1
    c.ingest({"turn_id": tid, "t2_llm_start": 1020.0, "t3_llm_end": 1200.0,
              "t4_moderation_end": 1210.0, "t7_handoff_node3": 1215.0})       # node2
    # node2 gửi lại lần nữa với t0 khác (nhiễu) -> KHÔNG được ghi đè t0 của node1
    c.ingest({"turn_id": tid, "t0_received": 9999.0})
    merged = c._acc.get(tid) or {}
    # sau khi node3 tới sẽ finalize & pop; kiểm trước finalize:
    assert merged.get("t0_received") == 1000.0     # giữ giá trị node1, không bị 9999 đè

def test_full_chain_breakdown(caplog):
    c = TimingCollector(); tid = "turn-y"
    with caplog.at_level(logging.INFO, logger="timing-collector"):
        c.ingest({"turn_id": tid, "t0_received": 0.0, "t1_enqueued": 10.0})
        c.ingest({"turn_id": tid, "t2_llm_start": 20.0, "t3_llm_end": 400.0,
                  "t4_moderation_end": 410.0, "t7_handoff_node3": 415.0})
        c.ingest({"turn_id": tid, "t8_node3_received": 420.0, "t9_audio_start": 500.0})
    out = caplog.text
    assert "end-to-end" in out and "500.0 ms" in out
    assert tid not in c._acc