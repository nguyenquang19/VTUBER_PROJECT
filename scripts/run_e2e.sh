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
pkill -9 -f "node1_ingestion|node2_core|node3_audio|node4_timing" 2>/dev/null || true
sleep 1

"$PY" -m node4_timing.collector & pids+=($!)
sleep 0.5
"$PY" -m node5_memory.server & pids+=($!)
sleep 0.5
"$PY" -m node3_audio.server & pids+=($!)
sleep 0.5
"$PY" -c "from node2_core.orchestrator import Orchestrator; from node2_core.ingress import serve; \
import threading; o=Orchestrator(); \
threading.Thread(target=o.run_forever, daemon=True).start(); serve(o)" & pids+=($!)
sleep 0.5
"$PY" -m node1_ingestion.bot & pids+=($!)

wait
