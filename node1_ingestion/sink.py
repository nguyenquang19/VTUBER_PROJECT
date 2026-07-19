import json, socket
from typing import Protocol
from shared.contracts.payloads import IngestionRecord
from shared.utils.timing_recorder import stamp
from .config import NODE2_HOST, NODE2_PORT

# Hợp đồng gửi đi — đổi transport không đụng bot.py
class Node2Sink(Protocol):
    def send(self, rec: IngestionRecord) -> None: ...

class TcpNode2Sink:
    def __init__(self, host=NODE2_HOST, port=NODE2_PORT):
        self._host, self._port = host, port
    def send(self, rec: IngestionRecord) -> None:
        stamp(rec.timing, "t1_enqueued")   # mốc lúc đẩy vào hàng đợi Node2
        line = (json.dumps(rec.to_dict()) + "\n").encode()
        with socket.create_connection((self._host, self._port), timeout=2) as s:
            s.sendall(line)