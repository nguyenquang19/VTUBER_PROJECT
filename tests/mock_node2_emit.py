# Bơm câu + stop thẳng vào Node3 (bỏ qua Node2 thật). Test 2 turn gối nhau.
import json, socket, time, sys

def send(port, obj):
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.sendall((json.dumps(obj) + "\n").encode()); s.close()

def sent(turn, seq, text, last, mood="neutral", timing=None):
    o = {"turn_id": turn, "seq": seq, "text": text, "is_last": last, "mood": mood}
    if timing: o["timing"] = timing
    return o

if __name__ == "__main__":
    tA, tB = "turnA", "turnB"
    # turn A câu 0 (kèm timing), rồi turn B chen vào, rồi stop A -> A seq1 KHÔNG phát.
    send(8803, sent(tA, 0, "Câu A một.", False,
                    timing={"turn_id": tA, "t0_received": time.time()*1000,
                            "t7_handoff_node3": time.time()*1000}))
    send(8803, sent(tB, 0, "Câu B một.", True))   # turn khác, không bị ảnh hưởng
    send(8804, {"turn_id": tA})                   # stop A
    send(8803, sent(tA, 1, "Câu A hai.", True))   # phải bị bỏ (A đã cancel)
    print("sent A0, B0, STOP A, A1 -> kỳ vọng: A0.wav, B0.wav có; A1 KHÔNG")