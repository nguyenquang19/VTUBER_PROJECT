# 00 — Mai v1.0.0 baseline và version policy

> **Product release:** Mai `1.0.0` (gọi ngắn là **v1.0**)
>
> **Baseline status:** frozen technical baseline
>
> **Platform:** Windows 11, Python 3.11+, local-first
>
> **Product version source:** `config/system.yaml::app.version`
>
> **Current product version:** `1.4.0` — thêm grounded character-lore material có transaction cho
> self-talk khi chat vắng. `1.3.1` refactor nội bộ runtime/Director thành module chuyên trách,
> không đổi public contract hay behavior. `1.3.0` thêm lore chi tiết nhân vật (`config/prompts/mai_lore.txt`) nối
> vào persona prefix để Mai có cá tính + đời sống, hợp trẻ em. `1.2.1` — tune sampling `llm_main` (temp 0.88→0.75).
> `1.2.0` — SFT multi-turn không directive (`sft_schema` 2), DPO full-context
> (`dpo_schema` 2), LLM-judge lọc ngữ nghĩa, và cờ `inject_mood_directive` cho inference (mặc định giữ
> nguyên, gated cho post-fine-tune). `1.1.0` thêm VTS animation adapter + dataset trust hardening. Lịch sử
> `1.0.1`→`1.4.0` xem `CHANGELOG.md`.

Tài liệu này là điểm vào bắt buộc cho AI hoặc người bảo trì mới. Nó mô tả hệ thống đang tồn tại ở
release `1.0.0`, không phải roadmap. Các nhãn như Mood v2, schema v3, M8 hoặc M10 là version/mốc nội bộ
của component; chúng **không** thay đổi product version Mai v1.0.0.

## 1. Mai v1.0.0 là gì

Mai là runtime AI VTuber local nhận YouTube/Discord chat, chọn hành động bằng Director, sinh tiếng Việt
bằng llama.cpp, lọc output, phát VieNeu-TTS hoặc subtitle fallback, rồi chỉ commit state sau delivery
thành công. Runtime có mood Hybrid, Thought Engine cho self-talk, Conversation Thread Engine, memory,
relationship state, dashboard operator, evaluation/replay và data pipeline phục vụ fine-tune sau này.

Luồng quyền sở hữu cốt lõi:

```text
platform adapter
  -> ChatRouter / grounded state / salience
  -> Director (quyết định)
  -> DirectorLoop (transaction owner)
  -> LLMTurnRunner (generation + parse + filter)
  -> TTSPipeline (audio/subtitle delivery)
  -> DirectorLoop (commit hoặc release)
```

`orchestrator/stream_runtime.py` là composition root của live stack. Không subsystem nào được bỏ qua
delivery boundary để tự commit history, memory, goal, thread hoặc chat removal.

## 2. Capability baseline

| Vùng | Trạng thái v1.0.0 | Implementation chính |
|---|---|---|
| YouTube/Discord text input | production path | `services/input/` |
| llama.cpp conversation | production path | `services/llm/` |
| Director + salience/chat gate | production path | `services/director/` |
| Mood Hybrid + response plan | production path | `services/emotion/`, `orchestrator/mood_engine.py` |
| Self-talk Thought Engine | production path | `services/autonomy/self_talk_planner.py` |
| Conversation threads | production path | `services/agent/` + DirectorLoop |
| VieNeu-TTS + subtitle fallback | production path | `services/tts/` |
| Working/relationship memory | production path | `services/memory/`, `services/relationship/` |
| Semantic memory | optional, launcher `-Memory` | BGE-M3 + SQLite |
| Operator/legacy dashboard | production path | `dashboard/` |
| Evaluation/replay/stress | offline/live tooling | `services/evaluation/`, `scripts/` |
| Training data export | offline tooling | `scripts/export_dataset.py` |
| STT/voice input | interface only, disabled | `interfaces/stt.py` |
| Real avatar animation output | adapter production (VTube Studio), từ `1.1.0` | `services/animation/` |
| Real game/environment action | chưa nối executor production | context/interface only |
| Fine-tuned model cutover | chưa thực hiện | llama.cpp production model hiện tại |

Enabled toggle ở baseline:

`filter_rule`, `tts_streaming`, `animation_smooth`, `data_collector`, `director_goal_arbiter`,
`director_chat_gate`, `conversation_continuity`, `mood_behavior_policy`, `mood_v2_shadow`,
`mood_v2_prompt`, `action_transactions`, `decision_records`, `operator_dashboard_v2`,
`proactive_hosting`, `self_talk_planner`, `behavior_library`, `natural_timing`,
`self_talk_lore`, `relationship_memory`, `evaluation_harness`, `evaluation_acceptance`, `live_operations`,
`kv_cache_q8`, `ambient_talk`.

Disabled/optional toggle ở baseline:

`input_voice`, `input_emotion_voice`, `filter_ai`, `tts_emotion_aware`, `animation_micro`,
`memory_semantic`, `memory_hierarchical`, `qc_persona`, `agent_context`, `goal_proposals`,
`thread_extraction`, `speculative_decoding`, `turn_taking_predictor`.

Từ `1.1.0`, `animation_smooth=true` gate `VTSAnimationService` thật (`services/animation/`); nếu VTube
Studio không mở/không nối được thì service chạy `degraded` fail-safe, không giết turn.

## 3. Repository và entrypoint map

| Path | Vai trò v1.0.0 |
|---|---|
| `interfaces/` | typed/frozen subsystem boundaries |
| `orchestrator/` | composition, config, lifecycle, shared runtime infrastructure |
| `services/` | business/domain implementations theo subsystem |
| `dashboard/` | operator/legacy UI và snapshot API |
| `config/` | production YAML; threshold/capacity/toggle |
| `migrations/` | ordered SQLite migrations, không sửa migration đã phát hành |
| `eval/` | frozen scenario và data contracts |
| `scripts/` | live/CLI/replay/stress/eval/export/backup tools |
| `tests/` | unit, integration, acceptance, live-marker và slow tests |
| `docs/` | tài liệu core + versioned baseline evidence |
| `logs/` | active rotated journals/snapshots/output; không phải source code |
| `data/` | SQLite, privacy salt và immutable dataset bundles |
| `backups/` | checksum manifests và recovery copies |

Entrypoint chính:

| Command/file | Mục đích |
|---|---|
| `scripts/start_live.ps1` | launcher live YouTube/Discord chuẩn |
| `scripts/stream_youtube.py`, `stream_discord.py` | platform stream entrypoint |
| `scripts/cli.py` | chạy tương tác CLI để kiểm tra local |
| `scripts/live_preflight.py` | validate environment/config trước live |
| `scripts/simulate_youtube_replay.py` | deterministic replay không LLM/TTS thật |
| `scripts/stress_youtube_llm.py` | replay qua llama.cpp/filter thật |
| `scripts/dashboard_standalone.py` | xem final snapshot khi runtime offline |
| `scripts/export_dataset.py` | tạo canonical/SFT/DPO dataset bundle |
| `scripts/backup_data.py`, `restore_data.py` | backup/verify/restore dữ liệu |

## 4. Invariant không được phá

1. Không commit business state trước `TTSDeliveryResult.delivered=true`.
2. Director không được commit nếu callback speak thiếu, trả `None` hoặc outcome không delivered.
3. Một idempotency key không deliver/commit hai lần.
4. Safety/operator control thắng donation, mood, goal và proactive behavior.
5. Mood chỉ đổi style/pacing; không tạo fact, cause hoặc hard priority.
6. Chat/LLM output là dữ liệu không tin cậy; filter và PII boundary luôn áp dụng.
7. Queue/cache/state có bound hoặc TTL; logging/memory/dashboard failure không làm chết core turn.
8. Raw generation attempt không phải bằng chứng đã nói; dataset chỉ nhận explicit delivery success.
9. llama.cpp là LLM backend duy nhất của v1.0.0; không dùng Ollama/transformers/vLLM.
10. Runtime Windows dùng graceful shutdown và process ownership; không mass-kill process.

## 5. Version matrix của v1.0.0

| Contract | Version hiện tại | Source of truth |
|---|---|---|
| Product | `1.0.0` | `config/system.yaml::app.version` |
| Architecture | `mai-agent-v1` | `eval/contracts/mai_agent_v1.yaml` |
| Data contract document | schema `2` | `eval/contracts/mai_agent_v1.yaml` |
| Generation attempt | turn schema `3` | contract + `services/llm/llm_turn.py` |
| Delivery outcome | schema `1` | contract + `services/llm/llm_turn.py` |
| Canonical turn | schema `1` | contract + canonical adapter (projection `CanonicalTurnV1`) |
| Record wire-schema fingerprint | registry (từ `1.1.0`) | `config/data_schema_registry.yaml` + `services/data/record_schema.py` |
| SFT / DPO | schema `2` / `2` (multi-turn, no-directive, từ `1.2.0`) | data contract |
| Context | `mai-context-v1` | data contract |
| Agenda | `mai-agenda-v1` | data contract |
| Evaluation scenarios | `mai-agent-v1` | `eval/scenarios/mai_agent_v1.yaml` |

Product, contract, schema và feature version là các trục độc lập. Ví dụ turn schema v3 vẫn thuộc product
Mai v1.0.0. Không suy ra product v3 từ schema v3.

## 6. Data/storage baseline

Data train đi qua ba lớp:

1. raw segmented journals trong `logs/`;
2. canonical records tạo lại được bằng versioned adapter;
3. immutable dataset bundle trong `data/datasets/<dataset_id>/`.

`turns.jsonl` và `delivery_outcomes.jsonl` join bằng `session_id + request_id + turn_id`. Raw journal là
append-only trong từng segment nhưng active log vẫn rotate theo `logging.yaml`; lưu dài hạn cần backup
có manifest/SHA-256. `data/mai.db` và `data/privacy_salt.bin` không phải training corpus và không được
xóa trong thao tác cleanup dataset.

## 7. Thứ tự đọc cho AI mới

1. Tài liệu này để khóa đúng product version và invariant.
2. `README.md` để biết entrypoint/chạy live và source-of-truth order.
3. `docs/01_SYSTEM_OVERVIEW.md` và `docs/02_DATA_PIPELINE.md` để hiểu lifecycle.
4. `docs/03_COMPONENT_REFERENCE.md` để tìm owner/file.
5. Chọn `docs/04`–`docs/08` theo loại task.
6. Đọc interface, composition root, implementation và YAML liên quan trước khi sửa.

AI không được tìm `PHASE.md`, `docs/QUICKSTART.md` hoặc `docs/ARCHITECTURE.md`; nội dung hợp lệ đã được
gộp vào bộ tài liệu v1.0.0 này. Comment cũ có từ “phase/milestone/v2” chỉ là lịch sử hoặc nhãn component,
không phải yêu cầu hiện hành.

## 8. Chính sách nâng version sau baseline

Dùng Semantic Versioning cho product:

- `1.0.x` — bug fix, tài liệu hoặc config correction tương thích, không đổi public contract;
- `1.x.0` — feature/capability mới tương thích ngược, field/schema reader-compatible;
- `x.0.0` — breaking interface, CLI, storage/data contract hoặc behavior boundary.

Sau khi baseline này được chấp nhận, **mọi thay đổi được merge phải tăng ít nhất patch version**. Một
release change phải đồng thời:

1. tăng `config/system.yaml::app.version`;
2. thêm entry vào `CHANGELOG.md`;
3. cập nhật nhãn `Applies to` trong tài liệu bị ảnh hưởng;
4. thêm compatibility adapter/migration nếu schema/storage đổi;
5. chạy targeted regression và lưu evidence phù hợp;
6. không sửa lịch sử v1.0.0 để làm như feature mới đã tồn tại từ baseline.

Không tăng product version chỉ vì counter/schema nội bộ tự tăng trong một work-in-progress chưa phát
hành; version được tăng khi thay đổi được chấp nhận như một release mới.

## 9. Tiêu chí coi tài liệu v1.0.0 còn đồng bộ

- Version trong docs khớp `config/system.yaml`.
- Feature inventory khớp `config/features.yaml`.
- YAML inventory khớp `ConfigLoader.CONFIG_FILES`.
- Data schema trong config/docs khớp frozen data contract.
- Mọi path/link trong bộ tài liệu tồn tại.
- Capability “production/optional/interface-only” không bị diễn giải lẫn nhau.
- Test documentation guard và full offline regression đều xanh.
