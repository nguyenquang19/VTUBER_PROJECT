import json
import os
import queue
import threading
import time

_q: "queue.Queue" = queue.Queue()
_started = False
_lock = threading.Lock()


def _path_for_today() -> str:
    day = time.strftime("%Y-%m-%d")
    return os.path.join("data", f"turns-{day}.jsonl")


def _pump():
    while True:
        item = _q.get()
        try:
            os.makedirs("data", exist_ok=True)
            with open(_path_for_today(), "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass  # log không được làm chết đường chính


def _ensure_started():
    global _started
    if _started:
        return
    with _lock:
        if _started:
            return
        threading.Thread(target=_pump, daemon=True).start()
        _started = True


def log_turn(
    turn_id: str,
    kind: str,            # "reply" | "auto"
    prompt_messages: list,
    raw_output: str,
    safe_output: str,
    mood: dict | None,
    user_id: str,
    display_name: str,
) -> None:
    _ensure_started()
    item = {
        "turn_id": turn_id,
        "kind": kind,
        "prompt_messages": prompt_messages,
        "raw_output": raw_output,
        "safe_output": safe_output,
        "mood": mood,
        "user_id": user_id,
        "display_name": display_name,
        "ts": time.time(),
    }
    try:
        _q.put_nowait(item)
    except Exception:
        pass