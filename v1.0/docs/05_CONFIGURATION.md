# 05 — Configuration và feature toggles

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
| `logging.yaml` | JSONL/rotation/degraded buffer |
| `data_privacy.yaml` | hash, raw log notice, retention, backup |
| `evaluation.yaml` | scenario, quality, fine-tune/release gates |
| `operations.yaml` | health, restart, shutdown, incident, soak |
| `conversation.yaml` | open thread, recap, context, extraction và repair bounds |
| `mood_ab_cases.yaml` | offline blind-review corpus |

## 3. Production feature state hiện tại

### Enabled

`filter_rule`, `tts_streaming`, `animation_smooth`, `data_collector`, `director_goal_arbiter`,
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
penalty, cache type và health timeout. Mỗi lần tune sampling cần replay cùng seed/context và đánh giá
naturalness/repetition; không thay nhiều trục cùng lúc.

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

`tts.startup_timeout_s` giới hạn toàn bộ load model + enroll voice; `tts.health_timeout_s` giới hạn
health/start-stop phụ trợ. Primary vượt gate sẽ chuyển sang subtitle-only nếu file sink healthy, không
được tiếp tục với callback delivery rỗng.

Metric GPU/VRAM dùng `system.dashboard.gpu_metrics.command/timeout_s/refresh_s`. Production mặc định
gọi `nvidia-smi`; lỗi query hiển thị unavailable/stale thay vì giá trị giả.

### Director

Tune tick/cooldown/transaction cache/decision bounds trong `director.yaml`; salience riêng trong
`chat_salience.yaml`. Không tune hard safety/donation priority bằng mood config.

`director.dead_air_seconds` là thời gian im tối thiểu trước khi cân nhắc tự nói;
`director.self_talk_cooldown_seconds` là khoảng cách tối thiểu giữa hai self-talk đã giao thành công.
Production dùng lần lượt `20s` và `45s`. Không hạ cooldown xuống gần `tick_seconds`, vì trạng thái chat
`COLD` tồn tại qua nhiều tick và sẽ làm giọng host dồn dập.

`director.min_actionable_score` là ngưỡng mở một turn chat riêng; production khởi điểm `15`. Với base
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
feature `self_talk_planner=false`; khi đó
runtime trở về Autonomy ambient legacy.

`cause_directions.recent_context` and `cognitive_moves` must not invite guesses about another person's
knowledge, intent or actions. The production move set is limited to facts in the anchor, missing-data
acknowledgement and Mai's own opinion.
`recent_context_min_tokens` rejects emoji-only or too-short material before it becomes a thought anchor.

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

### Conversation threads

`conversation.yaml` owns thread lifecycle and matching policy. `open_threads` bounds open/terminal state,
evidence, contributions, questions, park time and TTL. `topic_matcher` owns the reject threshold and
topic/summary weights. `move_planner` owns the move-count thresholds for compare/invite/summarize.

Keep `park_after_seconds < ttl_seconds`; matcher weights must sum to `1.0`. Raising `min_score` reduces
false cross-topic matches but creates more separate threads. Tune only with fixed replay evidence.

`director.arbiter.continue_thread_chat_grace_s` lets a newly actionable chat outrank the soft
`CONTINUE_THREAD` goal briefly. After that bound, the goal wins so a busy room cannot starve a thread.
