# Mai V2 — Roadmap

> **Vai trò:** scope, thứ tự phase và acceptance gate **tương lai**. Không chứng minh feature đã production.
> Hành vi hiện tại là `MAI_V2_SYSTEM_SPEC.md`; lịch sử đóng băng là `V1_BASELINE.md`.

## Định hướng

Hai mục tiêu, làm song song track:

1. **Một engine, không trồng lấn.** Hiện có 3 lớp trí tuệ chồng nhau (Legacy Director rule-based, Director
   V2 primary, Cognitive Brain shadow — xem SYSTEM_SPEC §4.2). Đích: chọn **Cognitive Brain làm engine
   chính**, cắt Director V2, Legacy chỉ còn safety-preempt + fallback.
2. **Nói tự nhiên hơn.** Đòn bẩy chính không phải engine mà là model + context + grounding: fine-tune,
   tầng ký ức episodic, nhận diện regular — với kỷ luật memory **dệt vào context, không quote ra output**.

Track A (naturalness) độc lập engine, làm ngay trên hệ hiện tại và dựng context giàu cho Brain dùng.
Track B (Brain-primary) chỉ bắt đầu khi A1–A4 xong. **Thứ tự:** `A1→A2→A3→A4 → B1→B2→B3→B4→B5→B6`, `A5`
(fine-tune) song song bất kỳ lúc nào.

## Guardrails — mọi phase

- **Docs-first:** cập nhật `MAI_V2_SYSTEM_SPEC.md` trước khi sửa code.
- **Một phase / task:** làm đúng một phase → targeted test + impacted regression → STOP review.
- **Flag + metric:** feature mới đăng ký `FeatureManager`, có metric.
- **Config over code:** ngưỡng/TTL/cooldown/weight ở YAML.
- **Interface-based:** service cross-subsystem implement `interfaces/`.
- **Fail-safe:** không gỡ legacy fallback trước shadow validation; lỗi → về hành vi cũ.
- **No PII:** memory/relationship lưu ý nghĩa + bí danh, không transcript/định danh thật.
- **No V3:** không thêm scope ngoài phase đang làm.

---

## Track A — Tự nhiên trên hệ hiện tại

Nguyên tắc chống máy móc: **con người nhớ rồi nói tiếp tự nhiên, không recite log.** Memory feed context,
không quote output; recall chọn lọc, không mỗi lần.

### A1 — Bật & hardening semantic memory · công thấp
Đưa `semantic_memory` từ OFF → LIVE mặc định, bounded, fail-safe.
- **Files:** `services/memory/*`, `config/features.yaml`, memory YAML, compose `stream_runtime`.
- **Interface:** `interfaces/memory.py` (không đổi contract).
- **Test:** unit + chain `memory_fallback` (semantic→working) + impacted regression.
- **Metric:** hit/miss/latency p95/fallback.
- **Gate:** bound 150ms; timeout → working-only; no PII; regression xanh.
- **Rollback:** flag off → working-only.

### A2 — Tầng episodic (session summary) · công trung
Thêm tầng nhớ giữa đang thiếu: cứ N turn LLM tóm tắt rolling → lưu, retrieve được.
- **Files:** `services/memory/episodic.py` (mới) hoặc mở rộng `extractor.py`.
- **Contract:** summary bounded, có provenance, TTL theo session; lưu **ý nghĩa** không transcript/timestamp.
- **YAML:** `summary_every_turns`, max summaries, retrieval weight (recency+salience).
- **Test:** transcript → summary bounded tất định; retrieval; replay; no PII scan.
- **Gate:** không quote PII; size bounded; feed context không vỡ budget prompt.
- **Flag:** `episodic_memory`, `depends_on: [memory_semantic]` (feature ID canonical).

### A3 — Recall gate (chống recite máy móc) · công trung
Quyết **khi nào** memory/fact được surface + đảm bảo chỉ inject dạng hint, không để quote nguyên văn.
- **Files:** `services/memory/recall_gate.py` (mới); tích hợp cả typed Brain projection trong
  `services/cognition/context_builder.py` và public DirectorLoop projection trong
  `services/cognition/compatibility_context.py`.
- **Contract:** gate trả (surface? + salience); context nhận **latent hint** không phải raw text; cooldown.
- **YAML:** recall cooldown, frequency cap, salience threshold.
- **Test:** không surface mỗi turn; cooldown; **scan output không có memory verbatim**; deterministic.
- **Gate:** recall rate dưới ngưỡng; public projection được xét trước Brain shadow; lỗi gate bỏ memory thay vì
  fallback raw; test khẳng định không leak raw memory vào prompt/câu nói.
- **Flag:** `recall_gate`.

### A4 — Relationship → context · công trung
Regular được nhận ra, **tông ấm hơn**, callback chọn lọc qua recall gate. Không recite hồ sơ.
- **Files:** `services/relationship/*`; wire `context_builder`; dùng `recall_gate` (A3).
- **Contract:** fact per-pseudonym bounded, no PII; inject CognitiveContext dạng hint; callback qua recall gate.
- **YAML:** fact slots max, visit/last-seen, callback frequency cap.
- **Test:** regular quen → context có hint; lạ → không; no PII; callback selective.
- **Gate:** giữ pseudonymous; no verbatim recall; ưu tiên tông > fact.
- **Flag:** `relationship_context`, `depends_on: [recall_gate]`.

### A5 — Fine-tune LoRA (track model, song song) · công trung
Đòn bẩy tự nhiên lớn nhất. **Không phải code-phase runtime** — data/model artifact, pipeline có sẵn.
- **Pipeline:** `export_dataset` → `quality_judge` → `data_quality` → LoRA train (offline) → `readiness`
  gate → shadow A/B (`mood_ab`/`harness`) → swap.
- **Gate:** blind review > baseline; latency budget giữ; VRAM còn chỗ cho VieNeu.
- **Rollback:** giữ model cũ, swap ngược.

---

## Track B — Chuyển sang Brain-primary

Chỉ bắt đầu khi A1–A4 xong (context đã giàu để Brain dệt). Giữ legacy fallback tới B5. Mỗi bước
shadow → canary → primary, không nhảy cóc.

### B1 — Grounding gate contract · công trung
Gate `uncertainty/evidence → WAIT`, chạy **shadow** trước.
- **Files:** mở rộng `interfaces/cognition.py`; gate trong đường Brain.
- **Contract:** `mode==WAIT` | `uncertainty > ngưỡng` | `evidence_refs` rỗng/không tồn tại → suppress.
- **YAML:** uncertainty threshold, evidence policy.
- **Test:** deterministic; reject empty/bịa evidence; ngưỡng từ YAML.
- **Gate:** chạy shadow, log, chưa ảnh hưởng output thật.

### B2 — Brain live-context + timeout/fallback (vẫn shadow) · công cao
Nối Opportunity → Brain lúc **live** (không post-exec), bọc `wait_for(budget≈2s)` → fallback. Vẫn shadow
để **đo latency thật**.
- **Files:** `services/kernel/turn_kernel.py`, `services/cognition/scheduler · brain`.
- **Metric:** Brain latency live p50/p95, timeout rate, would-fallback/would-select rate.
- **Gate:** p95 live đạt budget ~2s + timeout rate thấp **trước** khi mở B3.

### B3 — Turn Kernel route public → Brain · công cao
Thay đổi lớn nhất. Kernel thêm mode `PUBLIC_BRAIN`; sau flag; canary operator-only. Legacy fallback +
safety preempt giữ nguyên.
- **Files:** `turn_kernel.py` (route + method interface), `config/kernel.yaml` (`rollout_mode: brain`).
- **Test:** route contract; fallback path; safety hard-preempt chạy **trước** Brain; grounding gate (B1)
  active; deterministic replay.
- **Gate:** canary live có giám sát + rollback rehearsal; regression đầy đủ.
- **Rollback:** `rollout_mode` về `shadow` → exact hành vi cũ (V2 primary).

### B4 — Cắt Director V2 · công trung
Sau khi Brain primary ổn qua canary, retire V2 khỏi runtime.
- **Files:** bỏ nhánh V2 trong `director_loop._select_director_decision`; gỡ compose `v2_shadow ·
  v2_takeover · v2_primary`; park code (không xóa lịch sử).
- **Test:** full regression; không còn V2 path; replay.
- **Gate:** Brain primary ổn định nhiều phiên canary; `director_v2_*` metric = 0 trước khi cắt.
- **Rollback:** revert commit (giữ trong git).

### B5 — Giảm Brain-fail → 0, gỡ legacy fallback · công cao · endgame
Đích một-engine-sạch. Đo `selected/fallback`; khi fallback ≈ 0 bền vững thì gỡ nhánh legacy decision.
- **Metric:** fallback rate theo thời gian (≈ 0 nhiều phiên liên tục).
- **Gate:** fallback dưới ngưỡng qua N phiên; **giữ** safety preempt + canned tối thiểu làm lưới cuối.
- **Rollback:** bật lại legacy fallback nếu fail rate tăng.

### B6 — Refactor god files · công trung
Sau cắt V2, `director_loop` nhỏ lại. Tách `stream_runtime` composition root. **Behavior-preserving.**
- **Files:** `director_loop.py`, `stream_runtime.py` (tách wiring/lifecycle ra module).
- **Gate:** 0 thay đổi hành vi; regression identical; chỉ cấu trúc.

---

## Đích cuối (Definition of Done)

Sau **B5**: còn đúng một engine `Context → Brain → gate → execute`; legacy chỉ còn safety preempt + canned
làm lưới cuối. Cộng A1–A5 → nói tự nhiên hơn thật sự (mượt + không bịa + nhớ + nhận ra regular).

Ngoài phạm vi này (chương sau, không phải Brain-primary): OBS perception thật, external action trong vòng
tự chủ, world-aware agent đầy đủ. Đó là điều kiện cho release `2.0.0`, chưa mở trong roadmap này.
