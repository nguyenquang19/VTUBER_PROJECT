#!/usr/bin/env bash
set -e
export PYTHONPATH=.
echo "== dọn tiến trình cũ =="
pkill -f node1_ingestion || true; pkill -f node2_core || true
pkill -f node3_audio || true; pkill -f node4_timing || true
sleep 1
echo "== unit + wiring (in-process) =="
pytest tests/test_node1_ingestion.py tests/test_node2_core.py \
       tests/test_node3_audio.py tests/test_timing_merge.py \
       tests/test_e2e_wiring.py -q
echo "== TCP chain thật (Node3 + collector, edge-tts) =="
pytest tests/test_e2e_tcp.py -q -s
echo "== XONG: Giai đoạn 4 phần tự động PASS. Chạy Bước 3 để test với LLM+Discord thật. =="