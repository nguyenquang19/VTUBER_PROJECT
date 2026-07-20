import curses, socket, json, time
from .state import DashState
from .receiver import serve_receiver
from .config import CONTROL_HOST, CONTROL_PORT

def _send_skip():
    # Gửi Skip qua CỔNG CONTROL RIÊNG (không qua đường event). Lỗi -> im lặng.
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=1) as s:
            s.sendall((json.dumps({"action": "skip"}) + "\n").encode("utf-8"))
    except Exception: pass

def draw(stdscr, st: DashState):
    curses.curs_set(0); stdscr.nodelay(True); stdscr.timeout(200)
    while True:
        snap = st.snapshot()
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addstr(0, 0, "═══ AI VTuber Dashboard ═══  [s]=Skip  [q]=Quit", curses.A_BOLD)
        stdscr.addstr(2, 0, f"State : {snap['state']}")
        stdscr.addstr(3, 0, f"Queue : {snap['queue']}")
        stdscr.addstr(4, 0, f"Reply : {snap['reply'][:w-8]}")
        stdscr.addstr(6, 0, "── Chat gần đây ──")
        for i, c in enumerate(snap["chats"][-6:]):
            stdscr.addstr(7 + i, 2, c[:w-3])
        row = 14
        if snap["crises"]:
            stdscr.addstr(row, 0, "── ⚠ CRISIS ──", curses.A_BOLD); row += 1
            for c in snap["crises"][-3:]:
                stdscr.addstr(row, 2, f"⚠ {c[:w-5]}"); row += 1
        if snap["timings"]:
            last = snap["timings"][-1]
            row += 1
            parts = []
            for key, label in [("llm_call","llm"),("moderation","mod"),
                               ("node2_total","node2"),("end_to_end","e2e")]:
                v = last.get(key)
                if v is not None: parts.append(f"{label}={v:.0f}ms")
            stdscr.addstr(row, 0, "── Timing: " + " ".join(parts) + " ──")
        stdscr.refresh()
        try: ch = stdscr.getch()
        except Exception: ch = -1
        if ch == ord("q"): break
        elif ch == ord("s"): _send_skip()
        time.sleep(0.05)

def main():
    st = DashState()
    serve_receiver(st)          # nhận event nền
    curses.wrapper(draw, st)

if __name__ == "__main__":
    main()