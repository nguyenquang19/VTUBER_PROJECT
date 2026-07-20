# Đọc crisis_review + audio_out để tổng hợp nhanh. Chủ yếu: gom [TIMING] từ log.
# Cách đơn giản nhất: collector nên ghi thêm ra file. Thêm 1 dòng vào report.py:
#   mở file 'data/timing.jsonl' append mỗi lần finalize.
import json, statistics, glob, os

PATH = "data/timing.jsonl"
if not os.path.exists(PATH):
    print("Chưa có data/timing.jsonl - xem hướng dẫn thêm ghi file bên dưới")
    raise SystemExit

rows = [json.loads(l) for l in open(PATH, encoding="utf-8") if l.strip()]
e2e = [r["deltas"]["end_to_end"] for r in rows if r["deltas"].get("end_to_end")]
llm = [r["deltas"]["llm_call"] for r in rows if r["deltas"].get("llm_call")]

if e2e:
    print(f"N turns = {len(e2e)}")
    print(f"end-to-end: min={min(e2e):.0f} median={statistics.median(e2e):.0f} max={max(e2e):.0f} ms")
    print(f"LLM call:   median={statistics.median(llm):.0f} ms ({statistics.median(llm)/statistics.median(e2e)*100:.0f}% tổng)")