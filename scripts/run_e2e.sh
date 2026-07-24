#!/usr/bin/env bash
set -e
export PYTHONPATH=.
[ -f .env ] && { set -a; source .env; set +a; }
: "${LLM_HOST:=http://localhost:8080}"

PY="./env/bin/python"
[ -x "$PY" ] || PY="python3"

pids=()
cleanup() { echo "stopping..."; kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT INT TERM
pkill -9 -f "node1_ingestion|node2_core|node3_audio|node4_timing|node6_idle" 2>/dev/null || true
sleep 1

"$PY" -m node4_timing.collector & pids+=($!)
sleep 0.5
"$PY" -m node5_memory.server & pids+=($!)
sleep 0.5
"$PY" -m node3_audio.server & pids+=($!)
sleep 0.5
"$PY" -c "from node2_core.orchestrator import Orchestrator; from node2_core.ingress import serve; \
import threading; from node2_core.health.llm_health import LLMHealthMonitor; \
h=LLMHealthMonitor(); h.start(); o=Orchestrator(health=h); \
threading.Thread(target=o.run_forever, daemon=True).start(); serve(o)" & pids+=($!)
sleep 0.5
"$PY" -m node1_ingestion.bot & pids+=($!)
sleep 0.5
# node6 (tự nói): đọc mood.db + room_state.json, đẩy lượt AUTO vào node2.
# Tắt bằng NODE6_ENABLED=0 (mặc định bật). LƯU Ý: Phần 7 (kiểm duyệt) còn
# trống + model uncensored -> cân nhắc tắt tới khi điền xong danh sách chặn.
if [ "${NODE6_ENABLED:-1}" = "1" ]; then
  "$PY" -m node6_idle.node6 & pids+=($!)
fi

wait
