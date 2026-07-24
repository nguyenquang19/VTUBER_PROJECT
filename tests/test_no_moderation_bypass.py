import ast

from shared.contracts.payloads import IngestionRecord, EventType
from shared.contracts.timing import TimingTrace

# he-thong-logic §11 + DAC-TA 4.7: đường tự nói RẤT dễ vô tình bỏ qua kiểm
# duyệt — với model uncensored đây là chỗ nguy hiểm nhất. Mấy test này để nếu
# ai đó (kể cả sau này) thêm đường tắt thì đỏ ngay.

NODE6_FILES = [
    "node6_idle/config.py", "node6_idle/urge.py",
    "node6_idle/facts.py", "node6_idle/node6.py",
]


def _import_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    return mods


def test_node6_does_not_import_llm():
    for f in NODE6_FILES:
        joined = " ".join(_import_names(f)).lower()
        assert "llamacpp" not in joined and "llm" not in joined, f


def test_node6_does_not_import_node3_egress():
    for f in NODE6_FILES:
        src = open(f, encoding="utf-8").read()
        assert "Node3Egress" not in src, f
        assert "egress" not in " ".join(_import_names(f)).lower(), f


def test_node6_has_no_direct_speech_path():
    # Kiểm IMPORT + LỆNH thật, không kiểm văn bản comment (comment có nhắc
    # "không đụng node3" là chuyện bình thường).
    for f in NODE6_FILES:
        joined = " ".join(_import_names(f)).lower()
        assert "node3" not in joined, f          # không import gì từ node3
    src = open("node6_idle/node6.py", encoding="utf-8").read()
    assert "send_sentence" not in src            # node6 không tự phát câu
    assert "send_stop" not in src                # node6 không tự ngắt lời


def test_auto_blocked_content_never_reaches_node3():
    """Lượt AUTO có nội dung nằm trong danh sách chặn -> node2 chặn, node3
    không nhận câu nào. Kiểm CƠ CHẾ (chèn cụm test vào _BLOCK), không phụ
    thuộc nội dung chặn thật (Phần 7, đội ngũ tự điền)."""
    from node2_core.orchestrator import Orchestrator
    from node2_core import moderation

    class FakeLLM:
        def __init__(self, t): self.t = t
        def stream(self, p, cc):
            for c in self.t:
                if cc(): return
                yield c

    class RecEgress:
        def __init__(self): self.sents = []
        def send_sentence(self, s, trace=None): self.sents.append(s)
        def send_stop(self, t): pass

    class HealthyStub:
        def is_healthy(self): return True

    orig = moderation._BLOCK
    moderation._BLOCK = ("cấm_test",)
    try:
        eg = RecEgress()
        o = Orchestrator(egress=eg, llm=FakeLLM("một câu có cấm_test bên trong"),
                         crisis=type("C", (), {"is_crisis": lambda s, c: False})(),
                         health=HealthyStub(),
                         node5=type("N", (), {"get_profile": lambda s, x: None})())
        rec = IngestionRecord(EventType.AUTO, "__auto__", "Mai", "", 0.0, 0.0,
                              TimingTrace(turn_id="a1"), {"chat_silent_sec": 300})
        o.submit(rec)
        o.run_once()
        assert eg.sents == []   # bị chặn -> KHÔNG câu nào tới node3
    finally:
        moderation._BLOCK = orig
