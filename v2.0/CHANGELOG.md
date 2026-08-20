# Changelog — Mai

Mọi thay đổi product sau baseline dùng Semantic Versioning. Product version nằm tại
`config/system.yaml::app.version`; schema/component version là trục độc lập.

## [Unreleased]

### Fixed

- Feature toggle từ dashboard persist atomic vào `config/features.yaml`, giữ nguyên metadata/comment;
  restart nạp lại trạng thái đã ghi và persistence failure rollback thay vì báo success trong RAM.
- Feature config và runtime-critical config fail-closed với scalar sai kiểu, dependency/conflict sai,
  initial enabled set không hợp lệ hoặc resource budget vượt giới hạn.
- Startup composition rollback các resource đã mở theo thứ tự ngược; runtime startup failure
  chạy cleanup an toàn, giữ exception gốc và launcher luôn `stop()` trong `finally`.
- Credential/environment boundary dùng chung strict env-reference và secret-state, không trim/coerce
  secret, không lộ giá trị trong preflight/error và chỉ yêu cầu credential khi consumer bật.
- Discord live path tạo real client sau credential gate; OBS/VTS auth failure có reason/metric
  sanitized, VTS token file được Git ignore, đọc strict và ghi atomic.

### Documentation

- Hợp nhất tài liệu runtime/component/phase vào `docs/MAI_V2_SYSTEM_SPEC.md`.
- Tách `docs/V1_BASELINE.md` thành baseline lịch sử bất biến; inventory feature hiện tại chỉ thuộc
  System Spec.
- Thu gọn `docs/` còn mục lục, hai tài liệu canonical và thư mục evidence máy đọc.
- Thêm documentation guard cho version, baseline/current feature inventory, config registry, link,
  phase closure, release limit và evidence kiểm thử hiện hành.
- Loại character draft/checklist/tuning plan không được runtime tham chiếu; chuẩn hóa comment/docstring
  về invariant hiện tại thay cho nhãn công việc hoặc lời hứa triển khai đã lỗi thời.

### V2 migration

- Đóng gate kỹ thuật Phase 1–10: compatibility contracts, World/Self shadow, capability registry,
  mock action loop, Director V2 shadow/takeover, speech/avatar adapters, OBS scene action và canonical
  perception ingress.
- Giữ controlled takeover, speech/avatar action adapters, OBS scene action và OBS perception mặc định
  tắt cho tới khi có rollout plan, live canary và rollback evidence tương ứng.
- Phase 11–15 chưa đóng gate; code thành phần có sẵn không được coi là release evidence.

Những thay đổi trên chưa phải release product. Version tiếp tục là `1.4.3`; chỉ tăng version khi
change được duyệt như một release.

### Environment

- Thêm `scripts/bootstrap_environment.ps1`: cài Python `3.11.15` bằng `uv`, dựng staging, cài
  `requirements.lock.txt`, chạy `pip check` và chỉ thay `venv` sau khi toàn bộ đạt.
- Sửa `scripts/check_environment.ps1` để tự xác định project root đúng trên Windows PowerShell 5.
- Môi trường hiện tại khớp 125 dependency trong lock; `pip check` và environment checker đạt.

### Verification

- Phase 10 targeted: 29 đạt; impacted Phase 2–4/runtime: 246 đạt; documentation guard: 9 đạt.
- Feature persistence/strict config targeted: 129 đạt; impacted dashboard/Director/runtime: 173 đạt.
- Startup rollback/cleanup targeted: 29 đạt, gồm composition failure, partial start,
  cancellation, cleanup failure, TTS sink và launcher lifecycle.
- Credential/environment targeted: 116 đạt; impacted composition, external action, perception,
  animation và release preflight: 100 đạt.
- Comment/document cleanup targeted: 255 đạt; documentation guard hiện tại: 11 đạt.
- Full offline `pytest tests -q`: 2.148 đạt, 0 lỗi; còn một cảnh báo deprecation giữa
  Starlette TestClient và `httpx`.
- Chưa có live/LLM acceptance, audio/VTS/OBS canary hoặc canary takeover cho release `2.0.0`.

## [1.4.3] — 2026-08-14

Khóa freshness và durability của live-readiness artifact để báo cáo platform cũ hoặc file ghi dở không
thể được dùng như bằng chứng sẵn sàng hiện tại.

### Fixed

- Preflight thêm `generated_at_utc`; release builder kiểm tra max age và future clock skew từ
  `operations.yaml`, fail closed với timestamp sai định dạng/thiếu timezone/quá hạn.
- Khi truyền `--preflight`, release-evidence CLI chỉ exit `0` nếu software và platform đều ready; trạng
  thái blocked/invalid không còn trả success cho automation.
- Preflight và release evidence ghi qua temporary file cùng thư mục rồi atomic replace, giữ destination
  cũ nguyên vẹn nếu replace thất bại.

Rollback: revert patch freshness/atomic writer và trở về contract `1.4.2`; không có migration hay thay đổi
generation/delivery behavior.

## [1.4.2] — 2026-08-14

Siết release-readiness evidence để trạng thái sẵn sàng chỉ dựa trên báo cáo có contract hợp lệ và đúng
product version hiện tại; các artifact milestone M10 tiếp tục là lịch sử đóng băng.

### Fixed

- Preflight report khai báo marker, sanitized flag và product version; release builder kiểm tra đầy đủ
  schema, kiểu dữ liệu, tên check duy nhất và tính nhất quán của `ready` với blocking checks.
- Automated verification được tách thành artifact theo release, bắt buộc đủ targeted/offline/llm/slow/
  smoke và không có failure trước khi `software_ready=true`.
- JSON malformed, tampered, thiếu contract hoặc stale version fail closed với lỗi validation sanitized;
  không còn tin trực tiếp vào truthy `ready` hay regression result từ artifact M10 cũ.
- Output mặc định là evidence theo product version, không ghi đè `m10_release_evidence.json`.

Rollback: revert patch validator và dùng quy trình evidence `1.4.1`; không có migration hay thay đổi
runtime generation/delivery contract.

## [1.4.1] — 2026-08-14

Hợp nhất production entrypoint: bootstrap command lịch sử không còn có thể mở một dashboard-only
pseudo-runtime song song với live stack.

### Fixed

- `python -m orchestrator.main` fail-fast với exit code `2` và chỉ dẫn tới
  `scripts/start_live.ps1`; không bind port, chạy migration hoặc khởi động service.
- `scripts/start_live.ps1` và hai platform script tiếp tục là đường production duy nhất, compose
  `orchestrator.stream_runtime.StreamRuntime` có đủ LLM/TTS/Director/operations boundary.
- Giữ các state-machine/trigger module và `/legacy` dashboard cho test/rollback; không diễn giải chúng
  thành composition root production.

Rollback: revert patch này nếu cần bootstrap dashboard-only lịch sử; không có migration/config-data
change ngoài product version.

## [1.4.0] — 2026-08-13

Đưa character lore đã duyệt vào grounded material của Thought Engine để Mai có thể tự kể về mình khi
chat vắng. Minor release vì self-talk có thêm nguồn nội dung production; public contract và storage
schema giữ tương thích.

### Added

- `services/autonomy/lore_material.py` đọc bullet từ section allowlist của chính
  `config/prompts/mai_lore.txt`, cap anchor và xoay vòng có cửa sổ chống lặp bounded.
- Feature `self_talk_lore` phụ thuộc `self_talk_planner`; có metric reserve/commit/release/unavailable.
- Cấu hình `self_talk.lore_material` sở hữu section allowlist và bounds, không nhân đôi fact lore.

### Delivery semantics

- Lore material chỉ advance sau output được giao thành công; generation/filter/delivery failure release
  reservation và không consume candidate.
- Toggle OFF hoặc lore thiếu/rỗng/không có bullet hợp lệ quay về silence self-talk hiện tại.
- Director vẫn quyết định thời điểm nói; lore không được nâng priority hoặc tạo viewer/operator event.

Rollback: tắt `features.self_talk_lore`; không có migration hay data-schema change.

## [1.3.1] — 2026-08-13

Refactor nội bộ composition runtime và Director execution để giảm coupling, không đổi public contract,
config, output, transaction hoặc delivery semantics.

### Changed

- Tách TTS startup gate, feature bindings và operations wiring khỏi `stream_runtime.py` thành module
  chuyên trách; `stream_runtime.py` vẫn là composition root và giữ lifecycle order.
- Tách pure action prompt rendering và deferred typed-delivery mechanics khỏi `director_loop.py`;
  `DirectorLoop` vẫn là scheduler/transaction owner và chỉ commit sau delivery thành công.
- Giữ compatibility import cho các helper đang được test/tool nội bộ sử dụng.

Rollback: revert patch này; không có migration/config/data change.

## [1.3.0] — 2026-08-13

Thêm lore chi tiết nhân vật cho Mai (dễ thương, hợp trẻ em) để giọng ra "người có cá tính + đời sống",
không phải template. Minor: thêm capability, tương thích ngược (không có file lore = hành vi cũ).

### Added — character lore

- `config/prompts/mai_lore.txt`: chi tiết nhân vật — sở thích (sưu tầm thú bông, hảo ngọt), bướng yêu
  (bánh quy phải chấm sữa, kem sô-cô-la vô địch), sợ/ghét (gián, bị tắt), đời sống/gag (ám ảnh mốc
  follow, main character bị "ông" dìm), tật, cách cư xử theo vai, canon. Định hướng cute-ngang, trêu lành.
- `config/models.yaml::llm_main.lore_prompt_path`: đường dẫn lore. `PromptCache.from_loader`
  (`services/llm/prompt_cache.py`) nối persona + lore thành cùng system prefix; cả hai tĩnh nên prefix
  vẫn byte-stable → KV cache reuse giữ nguyên. Thiếu file lore → chỉ dùng persona, không lỗi.
- Lore thành "fact được phép nói về chính mình" (khớp luật chống-bịa của persona): Mai được kể về thú
  bông/đồ ngọt/gag của mình, không phải bịa người khác.

### Ranh giới

- Quan hệ CỤ THỂ đang lớn dần (regular, bạn) KHÔNG vào lore — thuộc relationship memory. Lore chỉ giữ
  thái độ theo vai (ông/regular-chung/chat mới) và canon cố định.

### Chưa làm (follow-up)

- Đẩy lore vào grounded material cho `self_talk_planner` (để Mai tự lôi lore ra nói khi chat vắng). Bản
  1.3.0 mới nối lore vào prompt trả lời thường; phần self-talk idle để bản sau.

### Docs

- Nội dung lịch sử hiện được giữ trong `docs/V1_BASELINE.md` và `docs/MAI_V2_SYSTEM_SPEC.md`.

### Fixed — release stabilization

- Đồng bộ product version `1.3.0` giữa config, README, AGENTS, core docs và documentation guard.
- Đồng bộ runtime data-contract view với SFT/DPO schema `2` đã phát hành ở `1.2.0`.
- Legacy autonomy chỉ finalize history/continuity sau khi delivery trả `delivered=true`; callback thiếu,
  lỗi hoặc không delivered đều release pending outcome và không commit self-talk.
- Bổ sung regression cho persona+lore prefix, delivery failure, strict turn journal và dataset schema v2.

Rollback: xóa/để trống `lore_prompt_path` → về persona cũ.

## [1.2.1] — 2026-08-13

Tune sampling `llm_main` để Mai bớt "sặc AI", giọng tự nhiên hơn. Patch, tương thích ngược: chỉ đổi
giá trị sampling gửi per-request, KHÔNG đụng runtime/interface/schema/contract. Rollback = trả 3 giá trị cũ.

### Changed — sampling `config/models.yaml::llm_main`

- `temperature` 0.88 → **0.75**, `repeat_penalty` 1.08 → **1.1**, `frequency_penalty` 0.15 → **0.2**
  (`min_p` 0.05 và `presence_penalty` 0.3 giữ nguyên).
- Chọn từ A/B 6 config (`config/sampling_sweep.yaml`) replay corpus chat thật qua llama.cpp
  (`scripts/sampling_sweep.py`; ~150–220 generation/config). Winner `cool_stable`.
- Số liệu baseline → cool_stable: `distinct_2` 0.649 → **0.674** (đa dạng hơn), `exact_repetition`
  0.029 → **0.0073** (ít lặp hơn), `turn_latency_p95` 3110ms → 3759ms (+20.8%, trong gate +25%);
  `assistant_register`/`meta_leak`/`fallback` = 0 cả hai; TTFT/decode_tps ~không đổi.
- Lý do (đọc `operator_review_sample`): thủ phạm "sặc AI" là temperature cao chứ không phải penalty —
  temp 0.88 gây corrupt token/leak ngoại ngữ (baseline lộ "kalau", placeholder "[giờ hiện tại]";
  config penalty-nặng `anti_repeat` lộ "đôngáveis"/"Nhưngmà"). Bỏ penalty (`light_penalty`) làm Mai lặp
  mô-típ nhiều hơn → bác giả thuyết "penalty quá nặng". Hạ temp + giữ penalty vừa = sạch giọng, vẫn đa dạng.
- Evidence: `logs/evaluation/sweep/sweep_summary.json` + `report_*.json`, `logs/evaluation/sample_convo.txt`.

### Docs

- Cấu hình LLM và lịch sử sampling hiện được hợp nhất trong `docs/MAI_V2_SYSTEM_SPEC.md`.

## [1.2.0] — 2026-08-12

Nâng cấp cách tạo dataset để model học hội thoại tự nhiên, không rác. Đổi format SFT/DPO example (bump
`sft_schema`/`dpo_schema` → 2); raw journal, turn/delivery/canonical schema KHÔNG đổi.

### Changed — SFT multi-turn, no-directive

- `scripts/export_dataset.py::build_sft` (schema v2): mỗi example là cả mạch hội thoại. Group theo
  `session_id`, sort `turn_id`, dựng history từ các lượt đã `delivered` trong sliding window
  (`--history-window`, mặc định 8). **Bỏ `context_block`** (mood/cause/stage directive) — model học mood/nhịp
  từ context, không từ scaffolding.
- `build_dpo` (schema v2): prompt mang cùng context multi-turn (bỏ directive); cặp chosen/rejected luôn
  cùng-context (regen/correction).
- `eval/contracts/mai_agent_v1.yaml`: `sft_schema_version` 1→2, `dpo_schema_version` 1→2.

### Added — LLM-judge lọc ngữ nghĩa

- `services/evaluation/quality_judge.py`: chấm mỗi SFT candidate CÙNG context theo rubric (đúng chất
  persona, mạch lạc, không nhạt, không bịa); dưới ngưỡng thì loại. Bật bằng `export_dataset.py
  --judge-min-score`. Pluggable `llm_fn` (default dùng llama-server local `/completion`); fail-safe: judge
  lỗi → giữ example.

### Added — Cờ tắt directive ở inference (gated)

- `models.yaml::llm_main.inject_mood_directive` (mặc định `true`). `false` = bỏ mood directive ở live để
  model tự đọc mood từ context. CHỈ bật sau khi model fine-tuned validate trên holdout; train/inference
  phải cùng phía. Wiring: `services/llm/prompt_manager.py`.

### Docs

- Nội dung baseline và data/storage hiện nằm trong `docs/V1_BASELINE.md` và `docs/MAI_V2_SYSTEM_SPEC.md`.

Rollback: `--judge-min-score 0` (tắt judge), `inject_mood_directive: true` (giữ directive). Muốn quay lại
SFT 1-lượt thì dùng bundle cũ (immutable, không bị ghi đè).

## [1.1.0] — 2026-08-12

Hai capability mới, tương thích ngược (reader cũ đọc được dữ liệu cũ; field turn/delivery/canonical
không đổi). Không phá interface/CLI/behavior boundary.

### Added — VTube Studio animation adapter

- `VTSAnimationService` (`services/animation/vts_service.py`) implement `interfaces/animation.py`:
  đọc `MoodState.dominant()` sau `DELIVERED` rồi trigger hotkey tương ứng trong VTS. Transport riêng
  ở `services/animation/vts_transport.py` (websocket, auth token-persist, config-injected). Fail-safe:
  VTS không kết nối → `degraded`, không giết turn. `sync_with_audio` là no-op có chủ đích (lip-sync do
  VTS lấy từ audio input, không qua API).
- Config `config/animation.yaml` (host/port/plugin/token, `mood_hotkeys` map, `retrigger_on_same_mood`);
  đăng ký trong `ConfigLoader.CONFIG_FILES`. Gate qua feature `animation_smooth`.
- Wiring: `orchestrator/stream_runtime.py` (compose + start + feature handlers + shutdown step),
  `services/director/director_loop.py` (gọi `express()` sau delivered). Metric `animation_*`.
- Tool `scripts/vts_smoke.py` kiểm tra kết nối VTS chạy tay.
- Avatar animation output chuyển từ "interface-only" sang "adapter production (VTS)".

### Added — Dataset trust hardening (write-time contract)

- `services/data/record_schema.py`: model Pydantic frozen `TurnRecordV3`/`DeliveryOutcomeV1`/
  `CanonicalTurnV1` (`extra="forbid"`) làm nguồn chân lý wire-format cho journal, tách khỏi object
  engine. `schema_fingerprint()` + registry `config/data_schema_registry.yaml`.
- Trust boundary dời từ export-time về **write-time**: `TurnLogger` validate record theo model trước
  khi ghi; record không khớp → `logs/quarantine.jsonl` kèm lỗi + metric, KHÔNG lọt vào journal train.
- `canonicalize_turn` projection qua `CanonicalTurnV1` field-set cố định (không copy dict), nên field
  engine mới không rò vào canonical.
- Fingerprint drift guard: startup fail-fast nếu model schema lệch fingerprint đã chốt; CI test tương ứng.
- Field turn schema v3 / delivery v1 / canonical v1 **không đổi** → dataset cũ vẫn canonicalize được.

### Docs

- Nội dung tài liệu liên quan hiện được hợp nhất trong `docs/V1_BASELINE.md` và
  `docs/MAI_V2_SYSTEM_SPEC.md`.

Rollback: đặt `animation_smooth: false` (tắt animation); dataset hardening thuần thêm lớp validate,
revert commit nếu cần.

## [1.0.3] — 2026-08-12

### Added
- `tts.pitch_semitones` (`config/models.yaml`): pitch-shift audio đầu ra TTS theo semitone, áp trong
  `AudioPlayer` trước khi phát. Mặc định `0.0` = no-op (không đổi hành vi baseline, không tốn CPU);
  dương = giọng cao/trẻ hơn, clamp `[-12, 12]`. Giữ nguyên độ dài (chỉ đổi cao độ) qua
  `librosa.effects.pitch_shift`. Metric mới `audio_pitch_semitones`.
- Code: `services/tts/pitch.py`; wiring trong `services/tts/audio_player.py`, `scripts/cli.py`,
  `orchestrator/stream_runtime.py`. Test trong `tests/unit/test_audio_player.py`.
- Docs: nội dung hiện được hợp nhất trong `docs/MAI_V2_SYSTEM_SPEC.md`.

### Fixed
- Cập nhật 2 test mã hóa mốc `dead_air_seconds` cũ (20s) cho khớp giá trị production `28s` đã đổi ở
  `1.0.2` (`tests/unit/test_director.py`, `tests/integration/test_youtube_replay_simulation.py`) —
  regression lọt qua ở `1.0.2` do chỉ chạy guard-subset, nay full offline regression xanh trở lại.

Không đổi interface, CLI, storage hay data contract. Rollback: đặt `pitch_semitones: 0.0` hoặc revert commit.

## [1.0.2] — 2026-08-12

### Changed
- Tinh chỉnh nhịp hội thoại sau đánh giá replay (`2nMxpqpGtMI`, 554 chat thật, llama.cpp Gemma 12B):
  - `config/director.yaml`: `dead_air_seconds` 20→28, `min_actionable_score` 15.0→12.0 (nhường & mở turn
    cho chat viewer → chat share of speech 39.6%→~47%).
  - `config/models.yaml`: `temperature` 0.85→0.88, `frequency_penalty` 0.0→0.15 (giảm lặp câu verbatim
    1.83%→0.5% khi đáp nhiều chat).
- Giá trị tune hiện được mô tả trong `docs/MAI_V2_SYSTEM_SPEC.md`.

Không đổi runtime, interface, CLI, storage hay data contract; chỉ threshold/sampling. Rollback: revert
commit này. Bằng chứng đánh giá: `logs/evaluation/conversation_eval_2026-08-12.md`.

## [1.0.1] — 2026-08-12

### Changed
- Chuẩn hóa tên file test: bỏ hậu tố phase/milestone (`_m3`…`_m10`, `phase1`, `phase2`, `m8_...`), đặt tên
  theo component/hành vi. Không đổi nội dung/logic test; regression offline vẫn xanh.
- Ghi rõ nguyên tắc **docs-first** và quy ước đặt tên/runner test trong `AGENTS.md` và `CLAUDE.md`
  (thư mục cha); nội dung tài liệu runtime hiện được hợp nhất trong `docs/MAI_V2_SYSTEM_SPEC.md`.

### Removed
- Bỏ `scripts/test_phases.py` (runner ad-hoc gom theo phase); dùng `pytest` + marker và các lệnh theo
  vùng trong `docs/07` §10.

### Fixed
- Viết lại `CLAUDE.md` ở thư mục cha thành tài liệu markdown trỏ đúng vào `v1.0/` (trước đây là lệnh
  `Out-File` bị lưu nhầm và trỏ tới `QUICKSTART.md`/`ARCHITECTURE.md`/`PHASE.md` không tồn tại).

Không thay đổi runtime, interface, CLI, storage hay data contract. Rollback: revert commit patch này.

## [1.0.0] — 2026-08-12

Baseline đầu tiên được đóng dấu cho toàn bộ runtime Mai:

- YouTube/Discord text input và llama.cpp generation;
- Director, salience/chat gate, transaction/idempotency và delivery-aware commit;
- Mood Hybrid, Thought Engine self-talk và Conversation Thread Engine;
- VieNeu-TTS, subtitle-only degraded mode và OBS subtitle sink;
- working/semantic/relationship memory theo feature/runtime flag;
- operator dashboard, health/recovery/emergency/shutdown;
- deterministic replay, real llama.cpp stress, evaluation/release tooling;
- versioned raw/canonical/dataset pipeline, backup/restore và privacy boundary.

Contract baseline: architecture `mai-agent-v1`, turn schema `3`, delivery outcome schema `1`, canonical
schema `1`, SFT/DPO schema `1`.

Chi tiết baseline nằm trong `docs/V1_BASELINE.md`; giới hạn implementation hiện tại nằm trong
`docs/MAI_V2_SYSTEM_SPEC.md`.

## Quy tắc entry tiếp theo

- Không thêm mục vào `1.0.0` để hợp thức hóa feature phát sinh sau baseline.
- Tạo heading version mới, nêu Added/Changed/Fixed/Removed và migration/rollback nếu có.
- Breaking contract phải ghi rõ dữ liệu/version nào còn tương thích.
