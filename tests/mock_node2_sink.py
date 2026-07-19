# Điểm nhận giả thay Node2 thật: in ra record nhận được, kiểm tra định dạng.
import socket, json, sys

def serve(host="127.0.0.1", port=8802):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port)); srv.listen()
    print(f"mock-node2 listening {host}:{port}")
    while True:
        conn, _ = srv.accept()
        with conn, conn.makefile() as f:
            for line in f:
                rec = json.loads(line)
                assert {"event_type","user_id","display_name","content",
                        "sent_at","tier1_score","timing"} <= rec.keys()
                assert rec["timing"]["t0_received"] and rec["timing"]["t1_enqueued"]
                print("OK", json.dumps(rec, ensure_ascii=False))

if __name__ == "__main__": serve()