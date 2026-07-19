import socket, json, threading

def _listen(port, tag):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port)); srv.listen()
    print(f"mock-node3 {tag} on {port}")
    while True:
        c, _ = srv.accept()
        with c, c.makefile() as f:
            for line in f:
                if line.strip(): print(tag, json.loads(line))

if __name__ == "__main__":
    for p, t in [(8803, "SENT"), (8804, "STOP")]:
        threading.Thread(target=_listen, args=(p, t), daemon=True).start()
    threading.Event().wait()