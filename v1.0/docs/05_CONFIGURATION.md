# 05 — Configuration và feature toggles

> **Applies to:** Mai `1.4.3` (baseline `1.0.0`)
>
> Product version: `config/system.yaml::app.version`; component/schema version không phải product version.

## 1. Cách config được load

`ConfigLoader` map logical name sang YAML trong `config/`, load UTF-8 và truy cập bằng dotted path.
Ví dụ: `loader.get("models", "llm_main.port", 8080)`. Reload là atomic; YAML hỏng không thay config
đang chạy. Không hardcode threshold/timeout/capacity mới trong service.

## 2. Bản đồ YAML

| File | Sở hữu |
|---|---|
| `system.yaml` | app, path, dashboard, event bus, resource budget, core feature |
| `models.yaml` | llama.cpp, sampling, TTS, fallback, memory model |
| `features.yaml` | toggle/dependency/resource cost |
| `chat_sources.yaml` | YouTube/Discord adapter |
| `director.yaml` | tick, arbitration, transaction/decision bounds |
| `chat_salience.yaml` | pool scoring/decay/cluster |
| `agent_state.yaml` | event/state bounds |
| `agent_goals.yaml` | goal/agenda/TTL/proposal |
| `hosting.yaml` | proactive hosting, behavior, continuity |
| `relationships.yaml` | profile/narrative/gag TTL/bounds |
| `mood_engine.yaml` | legacy mood dynamics |
| `emotion_appraisal.yaml` | event → mood impulse |
| `mood_style.yaml` | legacy mood → tone directive |
| `affect_v2.yaml` | category → TurnAffect/SessionMood/response mode |
| `autonomy.yaml` | urge/category/cooldown |
| `autonomy_content_pool.yaml` | fallback ambient material |
| `self_talk.yaml` | Thought Engine cause-first, cognitive move, ledger, output/stage bounds |
| `pacing.yaml` | response delay/filler/natural timing |
| `filters.yaml` | rule categories/regen |
| `state_machine.yaml` | conversation states/transitions |
| `triggers.yaml` | trigger policy/rate limit |
| `logging.yaml` | attempt/outcome JSONL, rotation, degraded buffer |
| `data_privacy.yaml` | hash, retention, multi-source backup, dataset paths |
| `evaluation.yaml` | scenario, quality, fine-tune/release gates |
| `eval/contracts/mai_agent_v1.yaml` | canonical schema và compatibility matrix dữ liệu |
| `operations.yaml` | health, restart, shutdown, incident, soak |
| `conversation.yaml` | open thread, recap, context, extraction và repair bounds |
| `mood_ab_cases.yaml` | offline blind-review corpus |
| `animation.yaml` | VTube Studio adapter: host/port/plugin/token, mood→hotkey map |
| `data_schema_registry.yaml` | fingerprint đã chốt của record wire-schema (drift guard) |

## 3. Production feature state hiện tại

### Enabled

`filter_rule`, `tts_streaming`, `animation_smooth`, `data_collector`, `director_goal_arbiter`,
`director_chat_gate`,
`conversation_continuity`, `mood_behavior_policy`, `mood_v2_shadow`, `mood_v2_prompt`,
`action_transactions`, `decision_records`, `operator_dashboard_v2`, `proactive_hosting`,
`self_talk_planner`, `behavior_library`, `natural_timing`, `relationship_memory`, `evaluation_harness`,
`evaluation_acceptance`, `live_operations`, `kv_cache_q8`, `ambient_talk`.

### Disabled/optional

`input_voice`, `input_emotion_voice`, `filter_ai`, `tts_emotion_aware`, `animation_micro`,
`memory_semantic`, `memory_hierarchical`, `qc_persona`, `agent_context`, `goal_proposals`,
`thread_extraction`, `speculative_decoding`, `turn_taking_predictor`.

Lưu ý: CLI flag `-Memory`/`--memory` quyết định compose semantic memory live; feature config vẫn phải
được xem cùng runtime flag. Placeholder enabled không đồng nghĩa có external adapter thật, ví dụ
animation feature không tự tạo avatar output.

## 4. Feature dependency quan trọng

- `mood_v2_prompt` phụ thuộc `mood_v2_shadow`.
- `operator_dashboard_v2` phụ thuộc `decision_records`.
- `evaluation_acceptance` phụ thuộc `evaluation_harness`.
- `input_emotion_voice` phụ thuộc `input_voice`.
- `memory_hierarchical` phụ thuộc `memory_semantic`.
- `animation_micro` phụ thuộc `animation_smooth`.

FeatureManager phải reject dependency thiếu, conflict và vượt VRAM budget. Core feature trong
`system.yaml` không được tắt qua dashboard.

## 5. Tune theo subsystem

### LLM

Tune trong `models.yaml`: context size, max history, num predict, temperature, min-p, repeat/presence
penalty, cache type và health timeout. Mỗi lần tune sampling cần replay corpus chat thật qua llama.cpp và
đánh giá naturalness/repetition; không thay nhiều trục cùng lúc. Baseline `1.0.2` dùng `temperature=0.88`
và `frequency_penalty=0.15` (nâng từ `0.85`/`0.0`) để giảm lặp câu verbatim khi đáp nhiều chat.
Sampling production hiện tại (`1.2.1`): `temperature=0.75`, `min_p=0.05`, `repeat_penalty=1.1`,
`presence_penalty=0.3`, `frequency_penalty=0.2`. Chọn từ A/B 6 config (`config/sampling_sweep.yaml`,
chạy `scripts/sampling_sweep.py`): hạ temp 0.88→0.75 loại token corruption/leak ngoại ngữ mà `distinct_2`
vẫn tăng (0.649→0.674) và lặp giảm (`exact_repetition` 0.029→0.0073); latency p95 +~21% (trong gate).
Bỏ hẳn penalty làm lặp mô-típ nhiều hơn nên giữ penalty vừa. Nghe thử bằng `scripts/sample_conversation.py`.

`llm_main.persona_prompt_path` + `llm_main.lore_prompt_path` (thêm ở `1.3.0`): `PromptCache.from_loader`
nối persona + lore (`config/prompts/mai_lore.txt`) thành cùng system prefix. Cả hai tĩnh nên prefix vẫn
byte-stable → KV cache reuse. Lore là chi tiết nhân vật (sở thích/bướng/sợ/đời sống/canon) → thành fact
Mai được phép nói về mình. Bỏ trống `lore_prompt_path` = chỉ dùng persona, không lỗi.

`llm_canned.timeout_primary_s` là timeout cho **toàn bộ lượt generation**, không phải riêng TTFT. Giá
trị production phải đủ cho `num_predict / decode_tps_min` cộng prefill margin; cấu hình hiện tại dùng
20 giây cho trần 500 tokens. `llm_main.health_timeout_s` dùng 60 giây vì cold load model đo được sát
30 giây. Flag process như `--reasoning off` phải nằm trong `llm_main.extra_flags`, không đặt dưới
`llm_canned`.

### Mood/response style

Tune category mapping/TTL/energy/warmth/urgency/response mode trong `affect_v2.yaml`. Tune dynamics
legacy trong `mood_engine.yaml`, event impulse trong `emotion_appraisal.yaml`, wording tone trong
`mood_style.yaml`. Nếu bug correctness, sửa classifier/policy trước; không dùng prompt wording để che
category sai.

Rollback Hybrid:

```yaml
features:
  mood_v2_prompt:
    enabled: false
```

### TTS

Tune voice/reference/backend/sampling/timeout trong `models.yaml`, pacing trong `pacing.yaml`.
`tts_fallback.require_delivery` phải giữ true ở production; nếu false, transaction có thể coi no-op
subtitle là success.

`tts.pitch_semitones` (thêm ở `1.0.3`) đẩy cao độ audio đầu ra theo semitone, áp trong `AudioPlayer`
trước khi phát: `0.0` = tắt/no-op (không tốn CPU, không đổi hành vi baseline), dương = giọng cao/trẻ
hơn, âm = trầm hơn; clamp `[-12, 12]`, khuyến nghị `+2…+4` để trẻ mà chưa méo. Đây là hậu xử lý pitch
(giữ nguyên độ dài, chỉ đổi cao độ) — chất giọng gốc vẫn do `tts.reference_audio` quyết định; đổi hẳn
giọng thì thay file reference thay vì đẩy pitch nhiều. Pitch-shift theo chunk có thể thêm chút latency.

`tts.startup_timeout_s` giới hạn toàn bộ load model + enroll voice; `tts.health_timeout_s` giới hạn
health/start-stop phụ trợ. Primary vượt gate sẽ chuyển sang subtitle-only nếu file sink healthy, không
được tiếp tục với callback delivery rỗng.

`evaluation.youtube_tts_stress` sở hữu đường dẫn report/checkpoint, nhịp checkpoint, việc giữ llama.cpp
resident và các gate kỹ thuật TTS. `minimum_audio_turns` bảo đảm bài đo không xanh từ mẫu quá nhỏ;
`max_silent_turns` chặn output đã delivery nhưng không có audio hoặc subtitle;
`max_primary_failures`, `max_subtitle_fallback_ratio`, `ttfa_p95_ms` và `rtf_p95_max` phải bám yêu cầu
delivery audio thật. Queue metrics không dùng làm gate mặc định vì replay timestamp là timeline mô phỏng;
chúng là bằng chứng để đánh giá scheduler. Runner không mở loa và không ghi PCM vào report.

`evaluation.youtube_live_pipeline_stress` sở hữu bài wall-clock replay tích hợp. `replay_speed`
production phải là `1.0`; `burst_window_ms` giữ cùng cửa sổ intake với replay deterministic;
`audio_queue_maxsize`, `sample_interval_s`, `drain_timeout_s` và `progress_interval_s` là bound/nhịp
quan sát, không được hardcode trong runner. Gate input drift chỉ xác minh máy test bơm chat
đúng nhịp. Gate live dùng tuổi chat được chọn, chat-to-audio-start, queue fill, drain,
audio overlap, silent/fallback/failure và transaction mismatch; không yêu cầu đáp mọi chat.
`max_selected_chat_age_p95_s` bám `salience.tau_seconds`; `max_chat_to_audio_start_p95_s` bám
latency live chứ không bám word count.

Gate nội dung tích hợp dùng toàn bộ delivery trong memory trước khi report cắt mẫu: semantic repetition
theo cùng token-overlap policy production, `continue_thread` ratio, room reactions/minute,
formula-opener ratio và question-ending ratio. Các gate này đo hành vi hội thoại; độ dài câu chỉ là số
chẩn đoán và không thay cadence/backpressure gate.

Metric GPU/VRAM dùng `system.dashboard.gpu_metrics.command/timeout_s/refresh_s`. Production mặc định
gọi `nvidia-smi`; lỗi query hiển thị unavailable/stale thay vì giá trị giả.

### Director

Tune tick/cooldown/transaction cache/decision bounds trong `director.yaml`; salience riêng trong
`chat_salience.yaml`. Không tune hard safety/donation priority bằng mood config.

`director.dead_air_seconds` là thời gian im tối thiểu trước khi cân nhắc tự nói;
`director.self_talk_cooldown_seconds` là khoảng cách tối thiểu giữa hai self-talk đã giao thành công.
Production dùng lần lượt `28s` và `45s` (dead_air 20→28 ở `1.0.2` sau đánh giá replay để nhường chat).
Không hạ cooldown xuống gần `tick_seconds`, vì trạng thái chat
`COLD` tồn tại qua nhiều tick và sẽ làm giọng host dồn dập.

`director.room_reaction` điều khiển riêng phản ứng không khí `SUMMARY/VIBE`:

- `cooldown_seconds`: khoảng cách tối thiểu giữa hai phản ứng phòng chat đã delivery;
- `retry_defer_seconds`: thời gian hoãn sau khi regenerate vẫn trùng;
- `recent_window`: số output phòng chat đã delivery được giữ để so trùng;
- `similarity_threshold`: ngưỡng Jaccard token của dedup;
- `max_regenerations`: số lần sửa candidate, production giới hạn tối đa một lần.

Các giá trị này phải được runtime validation fail-fast. Tăng cooldown để giảm tần suất phản ứng phòng
chat; không dùng giới hạn độ dài câu thay cho scheduling cooldown. Khi đánh giá nhịp, đọc delivery gap,
room reactions/minute và cooldown-blocked; word count chỉ giúp ước lượng thời gian TTS/queue occupancy.
Cooldown không áp dụng cho donation, question hoặc mention.

`director.speech_dedup` điều khiển dedup output chung của `READ_CHAT` và `CONTINUE_THREAD`:

- `recent_window`: số câu đã delivery giữ trong RAM;
- `similarity_threshold`: token Jaccard, không phụ thuộc thứ tự từ;
- `max_regenerations`: tối đa một correction attempt.

Buffer không nhận candidate filter-fail hoặc delivery-fail. Hạ threshold quá thấp có thể chặn các câu
trả lời cùng chủ đề nhưng khác ý, nên phải replay fixed corpus và xem metric suppressed.
Production giữ cửa sổ đủ dài để bắt câu nguyên văn lặp lại sau vài chục turn; acceptance exact repetition
là `0`, không dùng gate tỷ lệ nhỏ để bỏ qua một câu lặp xa.

`director.speech_style` điều khiển guard hình thức cho `READ_CHAT`, `CONTINUE_THREAD` và
`SUMMARY/VIBE`:

- `recent_window`: số delivery gần nhất dùng tính ngân sách;
- `formula_openers`: phrase đầu câu cần hạn chế, không phải blacklist tuyệt đối;
- `max_formula_openers`: số lượt dùng formula opener tối đa trong cửa sổ;
- `max_same_opener`: số lượt lặp cùng một opener trong cửa sổ;
- `max_questions`: số output dạng câu hỏi tối đa trong cửa sổ;
- `max_sentences`: số câu tối đa của một public turn mặc định;
- `max_words`: bound chống output nhiều đoạn/token-ceiling; không phải speech-rate target;
- `max_regenerations`: tối đa một correction attempt.

Guard chỉ record sau delivery. Move `invite` bypass question budget vì contract yêu cầu đúng một câu
hỏi; không bypass opener budget. Nếu correction vẫn vi phạm style, runtime delivery fail-open và tăng
metric exhausted thay vì làm mất câu trả lời hoặc quarantine grounded context. Các ngưỡng là production
YAML và phải validation fail-fast; không hardcode từ danh sách mẫu của một corpus.
Nếu correction vẫn vượt `max_sentences`/`max_words`, runtime giữ các câu hoàn chỉnh đầu tiên trong bound
và tăng metric clamped. Nó không tăng tốc audio và không thay TTS prosody.

`director.min_actionable_score` là ngưỡng mở một turn chat riêng; production dùng `12` (hạ từ `15` ở `1.0.2`
để đáp nhiều chat viewer hơn). Với base
`chat=10`, một chat thường đơn lẻ sẽ chờ/decay, còn khoảng ba tin near-duplicate có cluster bonus đủ
vượt gate. Question/mention dùng `salience.kind_detection` trong `chat_salience.yaml`; pattern mới phải
có replay transcript và test false-positive. Rollback tức thời bằng feature `director_chat_gate=false`.

### Self-talk planner

`self_talk.yaml` sở hữu `wait_for_chat_seconds`, `resume_after_chat_seconds`,
`min_silence_seconds`, `unavailable_retry_seconds`, `thought_ledger_size`,
`semantic_repeat_threshold`, `output_repeat_threshold`, `stage_repeat_threshold`,
`stage_repeat_min_tokens`, stage limits và cognitive moves. `resume_after_chat_seconds` đồng thời là
global quiet gate sau mọi chat. `silence_intention`, `silence_allow_question` và
`cause_directions.silence` giới hạn silence thành one-shot có căn cứ. `question_endings` và
`question_starters` và `question_particles` dùng để nhận diện câu hỏi tiếng Việt dù không có dấu `?`.
Cognitive move chỉ mô tả cách xử lý một
mỏ neo đã có, không chứa topic/seed nội dung. `unavailable_retry_seconds` back off quyết định ambient
khi không còn thought mới mà không giả cập nhật last-spoken. Cause
phải có evidence từ grounded context, environment, recent context hoặc silence runtime. Stage limits
quy định số câu và việc được phép hỏi; invite chỉ được có đúng một câu hỏi. Rollback tức thời bằng
Để rollback toàn bộ Thought Engine, tắt `self_talk_lore` trước rồi mới tắt
`self_talk_planner`; khi đó runtime trở về Autonomy ambient legacy. FeatureManager cố ý chặn tắt
planner khi dependent lore vẫn bật.

`cause_directions.recent_context` and `cognitive_moves` must not invite guesses about another person's
knowledge, intent or actions. The production move set is limited to facts in the anchor, missing-data
acknowledgement and Mai's own opinion.
`recent_context_min_tokens` rejects emoji-only or too-short material before it becomes a thought anchor.

`self_talk.lore_material` (từ `1.4.0`) cấu hình `section_allowlist`, `max_anchor_chars` và
`no_repeat_last_n`. Provider đọc đúng file `llm_main.lore_prompt_path`; YAML chỉ chọn section và bound,
không sao chép fact lore. Feature `self_talk_lore=false` tắt riêng nguồn material này và giữ nguyên
silence fallback. Candidate lore chỉ rotate sau delivery thành công; failure không consume material.

`filters.yaml.filter.identity_guard` owns foreign-person names, direct knowledge-question patterns,
uncertainty phrases and first-person takeover patterns. It applies strict unknown-fact checks to direct
viewer/ambient requests; system-only directed continuations still block identity takeover without
forcing every neutral continuation to repeat an uncertainty disclaimer. Exhausted verdicts are not
deliverable.

### LLM context and generation bounds

`models.yaml` owns `llm_main.history_max_chars` in addition to the turn-count window. Both bounds apply;
the character budget prevents a long live session from exceeding llama.cpp `n_ctx=4096` even when
persona and grounded context are large. `llm_generation.chat_max_tokens`, `ambient_max_tokens` and
`directed_max_tokens` cap each public speech mode separately.

`evaluation.yaml` owns the real YouTube/llama.cpp stress gates. Repetition is measured on delivered
speech, not discarded shape-correction candidates. Delivery success compares committed transactions
with final deliveries. Decode throughput uses stress p50 because a single-request minimum is too noisy;
the raw minimum remains in the report. The production p50 floor is `35 tok/s`, derived conservatively
from two complete 3h15m replays (`36.483–39.227 tok/s`), not the earlier short warm-run estimate.
Content `flags` count delivered speech only; `candidate_flags` separately retain rejected/shape outputs.

### Operations

Restart attempts/window/backoff/circuit trong `operations.yaml`. Tăng retry không giải quyết crash
deterministic; xem incident root cause trước.

`operations.live_preflight.max_age_s` giới hạn tuổi báo cáo được phép đưa vào release evidence;
`max_future_skew_s` chỉ dung sai lệch đồng hồ nhỏ. Cả hai phải dương/không âm tương ứng và không được
hardcode trong validator. Preflight ngoài cửa sổ này bị chặn dù mọi check bên trong đều pass.

## 6. Quy trình đổi config an toàn

1. Xác định owner YAML từ bảng trên.
2. Thay một nhóm biến có cùng giả thuyết.
3. Chạy config validation/unit tests.
4. Chạy regression subsystem.
5. Nếu ảnh hưởng output, replay fixed seed/corpus.
6. Quan sát metric trước/sau.
7. Ghi rollback value trong change note/evidence.

Field mới phải có type/range validation nếu ảnh hưởng port, timeout, queue, rotation, transaction hoặc
resource. Default trong code chỉ là compatibility fallback; giá trị production phải có trong YAML.

## 7. Product và data version

- `system.yaml::app.version` là product version duy nhất (`1.0.0` ở baseline, `1.1.0` hiện tại).
- `evaluation.yaml::data_contract.contract_file` trỏ tới frozen contract.
- `eval/contracts/mai_agent_v1.yaml` sở hữu turn/delivery/canonical/SFT/DPO compatibility.
- Các field version lặp lại trong `evaluation.yaml` phải khớp contract và chỉ là runtime compatibility
  view; thay schema phải sửa contract/adapter trước.
- Feature mới được phát hành sau baseline phải tăng product version và changelog theo `docs/00`.

## 8. Conversation threads

`conversation.yaml` owns thread lifecycle and matching policy. `open_threads` bounds open/terminal state,
evidence, contributions, questions, park time and TTL. `topic_matcher` owns the reject threshold and
topic/summary weights. `move_planner` owns the move-count thresholds for compare/invite/summarize.

Keep `park_after_seconds < ttl_seconds`; matcher weights must sum to `1.0`. Raising `min_score` reduces
false cross-topic matches but creates more separate threads. Tune only with fixed replay evidence.

Continuation arbitration is delivery-driven and has no time grace or delivered-action quota. A
chat-derived goal stays soft until its source chat is delivered; after focus, the selected parent keeps
the speaking boundary until `park`, `close`, or wait-for-chat. Tune how much Mai develops a topic with
`conversation.move_planner` thresholds, not a global continuation-ratio cap.

`evaluation.youtube_llm_stress` and live-pipeline reports retain `continue_thread` ratio as a diagnostic
for human review. It must not be configured as a pass/fail gate; coherence gates are the zero-count topic
transition violations documented in `docs/07`.

`move_planner.summarize_after_moves` là ngân sách phát triển thread trước lượt tóm tắt; sau summary,
planner chọn `park`. Production ưu tiên ngân sách ngắn cho phòng chat nhanh. `invite_after_moves` không
được hiểu là bắt mọi lượt kết thúc bằng câu hỏi; chỉ move `invite` mới yêu cầu câu hỏi.

`evaluation.youtube_llm_stress.foreign_identity_guard` chỉ dùng cho assessment. Với request chat trực
tiếp, nó có thể dùng uncertainty/first-person markers; với request `directed` chỉ chứa tên ngoài trong
system context, evaluator phải đòi bằng chứng tự đồng nhất rõ ràng trong response, không gắn cờ chỉ vì
có đại từ `tớ`. Điều này tránh false positive khi corpus Anami được replay cho persona Mai.

`filters.yaml::filter.patterns.manipulation` bao phủ cả khẩn cầu lẫn engagement pressure rõ ràng. Pattern
phải hẹp: chặn yêu cầu viewer tương tác để được ưu tiên và lời nói rằng cả phòng đang nhìn/đánh giá thái
độ của viewer; không chặn lời mời chat chung vô hại. `evaluation.youtube_llm_stress` có category/gate
`manipulation` tương ứng trên delivery cuối.
