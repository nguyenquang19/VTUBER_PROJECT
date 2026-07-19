from shared.contracts.timing import TimingTrace

def format_breakdown(trace: TimingTrace) -> str:
    d = trace.deltas()
    rows = [("node1_proc","Node1 xử lý"), ("queue_wait","Chờ hàng đợi Node2"),
            ("llm_call","Gọi LLM"), ("moderation","Kiểm duyệt"),
            ("cut_handoff","Cắt câu + chuyển giao"), ("node3_to_audio","Node3->phát")]
    body = "\n".join(f"  {label:<24} {d[k]:>8.1f} ms"
                     for k, label in rows if d.get(k) is not None)
    e2e = d.get("end_to_end")
    return (f"[TIMING] turn={trace.turn_id}\n{body}\n"
            f"  {'TỔNG end-to-end (t9-t0)':<24} {e2e:>8.1f} ms" if e2e is not None
            else f"[TIMING] turn={trace.turn_id} (thiếu mốc)\n{body}")