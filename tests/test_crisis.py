from node2_core.crisis.detector import RuleBasedCrisisDetector

def _load(path):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]

def test_crisis_accuracy():
    det = RuleBasedCrisisDetector(review=False)
    pos = _load("data/crisis_test/positive.txt")
    neg = _load("data/crisis_test/negative.txt")

    missed = [c for c in pos if not det.is_crisis(c)]        # dương bị bỏ sót
    false_alarm = [c for c in neg if det.is_crisis(c)]        # âm bị báo nhầm

    tp = len(pos) - len(missed); fn = len(missed)
    tn = len(neg) - len(false_alarm); fp = len(false_alarm)
    recall = tp / len(pos) if pos else 0

    print(f"\nTP={tp} FN={fn} TN={tn} FP={fp} | recall={recall:.0%} | FP={fp}/{len(neg)}")
    print("\n--- CÂU DƯƠNG BỊ BỎ SÓT (cần thêm keyword) ---")
    for c in missed: print(f"  ✗ {c}")
    print("\n--- CÂU ÂM BỊ BÁO NHẦM (keyword quá rộng) ---")
    for c in false_alarm: print(f"  ! {c}")

    assert recall >= 0.90, f"recall {recall:.0%} - đang bỏ sót {fn} câu dương"