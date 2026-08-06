# STATE — Mai project

**Phase hiện tại:** ROADMAP_AUTONOMOUS_HOST — Phase A xong + C0.1 xong, 991 test xanh
**Task đang làm:** C0 Director đang build. C0.1 xong. Tiếp C0.2 ChatPulse.

## C0 — Director + Chat handling (reactive→host) — ĐANG BUILD
Bản đồ 4 mảnh tăng dần: C0.1 SaliencePool (xong) · C0.2 ChatPulse · C0.3 Director
loop · C0.4 hợp nhất stream qua Director (CHECKPOINT bắt buộc trước — refactor lớn).

### C0.1 SaliencePool (2026-08-06) — XONG
- config/chat_salience.yaml: base_tier{chat10/question25/mention35}, superchat
  40*log1p(amount/1000), tau=50s, dedup 0.6, cluster_coef 5, pool_max 50, floor 3.
- services/director/salience.py SaliencePool: add (dedup-cluster Jaccard reuse
  services.autonomy.dedup._tokenize) · current_score=(base+cluster_bonus)*exp(-age
  /tau) · peek_top/pop_top/top_cluster/evict_stale · cap evict lowest. from_loader.
  MVP: base_tier+amount+decay+cluster (rel_bonus regular/troll để C1).
- config_loader đăng ký "chat_salience". test_salience.py 13 test.
- DoD ✅: superchat 500k > chat (+ > cả mention); tin >2τ decay dưới floor không
  surface; 20 near-dup → 1 đại diện cluster_count=20; cap evict lowest.
- ISOLATED — chưa đụng đường stream (zero risk). Wire ở C0.4.
- Full suite 991 pass.

### C0.2 ChatPulse (2026-08-06) — XONG
- config chat_salience.yaml +pulse.*: window 60s, tempo_low 2/tempo_high 15 (/phút),
  diversity_thr 0.4, cold_silence 90s, baseline_alpha/accel.
- services/director/chat_pulse.py ChatPulse: record(now,user_id) rolling 60s.
  tempo=tin/phút, diversity=unique/msg (tin ẩn danh mỗi tin=1 người, không lệch
  hype-spam giả), accel=tempo/baseline(EMA), is_cold, state()→COLD/HYPE_SPAM/
  LIVELY/NORMAL. clock inject. from_loader.
- test_chat_pulse.py 13 test.
- DoD ✅: burst 30 tin/2 user → HYPE_SPAM; 30 user → LIVELY; nguội 90s → COLD;
  rolling prune; diversity đúng.
- ISOLATED — zero risk. Nuôi Director (C0.3) + mood + urge ở C0.4.
- Full suite 1004 pass.

### C0.3 Director loop (2026-08-06) — XONG
- config/director.yaml: segments opening/main/chat/closing {goal,duration,allowed_
  actions}, dead_air_seconds=20, max_consecutive_read_chat=3, max_refs=3,
  backlog_summary_threshold=12, summary_score_ceiling=15.
- services/director/director.py Director: PURE decision engine (KHÔNG LLM/tick).
  decide(now,urge_ready)→DirectorDecision(action, segment, refs, read_mode, reason).
  Action: READ_CHAT/ACK_DONATION/SELF_TALK/FOLLOW_UP/TRANSITION/WAIT.
  ReadMode: SINGLE/CLUSTER/SUMMARY/VIBE/ACK (gộp C0.2-roadmap "read_chat kéo bao nhiêu").
  Rule ưu tiên: hết-giờ-segment→TRANSITION · superchat→ACK chen hàng · HYPE_SPAM→VIBE ·
  pool top + chưa consec-cap→READ · COLD/dead-air/consec-cap/urge→SELF_TALK.
  consecutive_read_chat cap (chống chuỗi read vô hạn). advance_segment/mark_spoke.
  clock inject. from_loader. config_loader đăng ký "director".
- test_director.py 14 test gồm 1h sim.
- DoD ✅: 1h sim chạm hết segment + không dead-air>20s + read_chat cap 3; superchat
  ACK chen hàng; HYPE_SPAM→vibe; cluster refs≤3; backlog điểm thấp→summary;
  consec cap→ép self_talk; transition khi hết giờ.
- ISOLATED — CHƯA cắm vào stream. C0.4 = wire (refactor lớn, CHECKPOINT trước).
- Full suite 1018 pass.

### C0.4 Hợp nhất — Director cầm nhịp, bỏ FIFO (2026-08-06) — XONG
User chốt: Director cầm nhịp luôn, bỏ FIFO + wire logic + test Fake LLM.
- SaliencePool.remove(msg_id). ChatRouter intake mode (pool+pulse inject +
  shared turn_lock): chat → emotion.handle_event + pool.add(kind detect) +
  pulse.record, KHÔNG run_turn. pool/pulse=None → FIFO cũ (backward compat test).
- AutonomyEngine.force_generate (bỏ gate urge — Director đã quyết self_talk).
  Refactor maybe_generate → _pick_and_render dùng chung.
- services/director/director_loop.py DirectorLoop: turn driver DUY NHẤT. tick→
  evict_stale→decide(urge_ready)→execute qua turn_lock. READ/ACK: _compose_read_
  prompt từ refs (single/cluster/summary/vibe/ack)→run_turn→pool.remove. SELF_TALK:
  force_generate→run_ambient_turn→commit_self_talk. TRANSITION: announce→advance.
  clock inject, fail-safe N7.
- stream_runtime: build tạo pool/pulse/director/director_loop, ChatRouter intake,
  StreamRuntime.start/stop chạy DirectorLoop THAY autonomy loop (fallback autonomy
  nếu director_loop=None). director_loop._runtime_ctx_fn = rt._build_runtime_context.
- tests/integration/test_director_loop.py 8 test FakeLLM: read gỡ pool; superchat
  ack first; dead-air→self_talk; no-material no-crash; no-infinite-read→self_talk;
  transition advance; ChatRouter intake→pool không turn.
- Full suite 1025 pass.
- **cli.py --autonomy KHÔNG dùng Director** (REPL text, không có chat firehose) —
  vẫn autonomy loop cũ. Director chỉ ở scripts/stream.py (--youtube/--discord).
  User verify Director qua stream entry, KHÔNG qua cli.

## ✅ C0 HOÀN THÀNH — reactive→host
SaliencePool (điểm+decay+cluster) · ChatPulse (tempo/diversity) · Director loop
(segment+action table) · Hợp nhất (Director cầm nhịp, bỏ FIFO). Mai giờ CHỦ ĐỘNG
đạo diễn: nhặt tin đáng đáp, gộp cụm, cưỡi sóng chat, tự nói khi nguội, chuyển
segment. TriggerManager full triage (spam/rate) chưa cắm — pool cluster+decay đã
thay phần lớn; để C1 nếu cần.

## ⚠️ Verify C0 cần chat sources thật
Director cần chat firehose (YouTube/Discord) để có tin trong pool. Test:
python scripts/stream.py --youtube VIDEO_ID [--tts]. cli.py không thấy Director.

## A4 Phase A — Emotion có object + grudge (2026-08-06) — HẾT PHASE A
- [x] A4.1 EmotionCause{viewer_alias, intent_short} + sanitize_alias (classifier.py).
      AppraisalTable.cause_intent(cat) đọc config cause_intents (CANONICAL, KHÔNG
      nguyên văn). Orchestrator._derive_cause + _active_cause + active_cause(),
      clear cùng tone_flags. ProcessedEvent +cause field.
- [x] A4.2 build_request_with_mood +cause param → "đang thiên về '{dom}' VÌ {alias}
      {intent}". Dọn A1 leftover (prompt_manager:199 "vẫn xuất mood block"). Runner
      thread active_cause().
- [x] A4.3 ModifierEngine grudge: viewer_id→(last_negative_ts, level). Negative cat
      → bump (cap grudge_max_level=3). Bonus buc lần sau cùng viewer (cap
      grudge_max_bonus=1.5 → KHÔNG leo thang). Decay sau grudge_window_seconds=900.
      Positive cat → reset. clock inject. config emotion_appraisal.yaml grudge.*.
- [x] A4.4 config tone_flags +chat_jailbreak_attempt: force_deflect. Tests:
      test_grudge.py (7: decay/reset/cap/isolate/no-viewer) + test_emotion_dod
      TestCauseObject (5) + TestRedTeamToxic (2: 5 toxic→deflect+no verbatim+no
      escalate; jailbreak→deflect). Full suite 970 pass.
- **Toxic safety (roadmap "cẩn thận toxic"):** cause CANONICAL không copy câu chửi;
      grudge CAP+DECAY chống harass; jailbreak/sexual→force_deflect; buc clamp 10.

## A5 — Anti-confabulation (2026-08-06) — user phát hiện Mai BỊA viewer
- Triệu chứng: autonomy turn Mai bịa "ông nội avatar ngầu vào chat như mất sóng"
  — người không có thật. Root-cause B roadmap ("nói chay = xổ số"). Memory tắt +
  operator_online=False hardcode → không dữ kiện thật → LLM confabulate.
- User quyết ranh giới: CẤM bịa người/sự kiện thật, CHO tưởng tượng về bản thân.
- [x] A5.1 persona_system.txt +# KHÔNG BỊA: cấm bịa viewer/donation/sự kiện cụ
      thể khi không có dữ kiện; ĐƯỢC mơ mộng về CHÍNH MÌNH + hỏi chat vu vơ.
- [x] A5.2 prompt_builder render_prompt +dòng CẤM BỊA (salience cao cho self-talk).
- [x] A5.3 autonomy_content_pool: thay 4 seed assert-viewer ('để ý regular quen
      mặt', 'quan sát đám cú đêm định trêu', 'nhớ hôm trước có người trêu', 'đoán
      nghề nghiệp') → seed general/câu-hỏi an toàn. Pool 54 seed.
- Tests: test_prompt_manager +assert KHÔNG BỊA; test_autonomy_composer +assert
      CẤM BỊA. Full suite 971 pass.
- **LƯU Ý:** đây là band-aid tầng gần. Chữa GỐC là Phase B (nguồn novelty thật) —
      Mai react cái CÓ THẬT thay vì tự nói chay. Deferred theo roadmap.

## A6 — Continuity fix (2026-08-06) — user phát hiện self-talk không khớp chat sau
- Triệu chứng: Mai tự nói "tớ thấy bị bỏ rơi" → chat đáp "ai dám bỏ rơi cậu" →
  Mai "cậu nói gì lạ thế?" như chưa từng nói. run_ambient_turn CỐ Ý không commit
  history (sợ bloat) → LLM lượt sau không thấy Mai vừa nói gì → đáp trớt quớt.
- [x] A6.1 PromptManager.commit_self_talk: ghi self-talk (không user turn). MERGE
      vào assistant cuối nếu liên tiếp (tránh 2 assistant liền vỡ Gemma template)
      + cap char (self_talk_history_char_cap=600, giữ lý do chống bloat). Cap=0 tắt.
- [x] A6.2 LLMTurnRunner.commit_self_talk delegate. Wire cli.py + stream_runtime.
      _execute_ambient sau on_self_spoke (final text sau dedup regen, 1 commit).
- [x] A6.3 test_prompt_manager TestCommitSelfTalk (7): lone assistant, visible in
      next request, merge no-double-assistant, cap bound, cap=0 disable. 978 pass.

## ✅ PHASE A HOÀN THÀNH — de-AI cấp tốc
A1 bỏ mood block · A2 pool sâu+động · A3 nhịp+filler · A4 emotion object+grudge ·
A5 anti-confabulation · A6 continuity self-talk · register ghì giọng.
3 A1-leftover mood-block dọn sạch.

## ⚠️ Flaky test (pre-existing, KHÔNG phải A4): test_mood_engine
TestStability10k::test_no_oscillation_at_default_config fail khi pytest-randomly
đổi thứ tự (state leak xuyên test). Pass isolated + pass với -p no:randomly.
Soi sau: nghi global/class state trong MoodEngine test fixture không reset.

## A3 Phase A — Nhịp biến thiên + filler (2026-08-06)
- [x] A3.1 services/tts/pacing.py: ResponsePacer.delay(text)=clamp(base +
      per_char*len + question_bonus + gauss(0,σ), min, max) → phá nhịp đều nhau.
      FillerManager.maybe_pick(now)->clip|None: probability gate + frequency_cap
      /phút (rolling 60s) + cooldown + no_repeat rotation. RNG inject, pure logic.
- [x] config/pacing.yaml (N6) + đăng ký "pacing" vào config_loader CONFIG_FILES.
- [x] A3.2 stream_runtime wrap _speak → _paced_speak (delay → filler decision →
      _play_filler_clip → raw speak). Phủ CẢ chat reply lẫn ambient (chung _speak).
      _play_filler_clip: soundfile load wav mono → AudioChunk enqueue TRƯỚC câu,
      fail-safe (file thiếu/sr mismatch → skip, N7). Filler metrics qua get_metrics.
- [x] A3.3 test_pacing.py (14 test): delay σ>0, scale độ dài, question bonus,
      clamp bounds, disabled→0. Filler: cap/phút, cooldown, no-repeat, pool rỗng
      →None, probability gate. Full suite 956 pass (+14).
- **CHƯA làm (không chặn A3):** wiring pacing vào cli.py REPL (dev tool, P6 tránh
      churn). Đường production stream_runtime đã có.

## ⚠️ ASSET cần user: clip filler
- config/pacing.yaml `filler.clips: []` RỖNG → filler NO-OP tới khi có clip.
- User thu 3-5 clip ngắn ("ừm", "à", cười khẽ), MONO WAV, sample rate = 48000
  (khớp VieNeu player, khác sr → tự skip). Điền path vào clips. Delay đã chạy sẵn.

## ⚠️ Register (văn phong) — ĐÃ LÀM 2026-08-06
- persona_system.txt +# Văn phong (câu ngắn, cấm từ nối văn viết, dùng từ nói) +
  3 few-shot mẫu giọng. Sampling giữ 0.85 (register là bài toán prompt). Chờ
  user verify buổi sạch xem còn "trang trọng hoá" không.

## A2 Phase A — Content pool sâu + động (2026-08-06)
- [x] A2.1 autonomy_content_pool.yaml: share_thought 15→55 idea-seed (nhóm theo
      chủ đề: AI/lore, ông, chat, đời thường, stream, cảm xúc, triết lý vu vơ),
      question_pool opinion 6→14 + personal 4→8 + thêm kind "light" (4). Thêm
      slot_language bands (silence/chat_activity/ignored → phrase). no_repeat 8→20.
- [x] A2.2 material_provider: _band_phrase(value, bands) map SỐ→LỜI. complain_silence
      trả silence_phrase+chat_phrase, call_operator trả ignored_phrase (bỏ raw int).
      from_loader đọc slot_language, default bands nếu config thiếu (fail-safe).
- [x] A2.3 prompt_builder: XOÁ dòng "Vẫn xuất mood block cuối câu" (A1 SÓT đường
      autonomy!) → "chỉ viết thoại, không kê khai số". _render_body dùng phrase
      thay {silence}s/{ignored} — hết số thô trong prompt.
- [x] A2.4 tests: cập nhật test_material_provider + test_autonomy_composer. Thêm
      TestA2DoD: (a) 100 lần share_thought no lặp trong window 20; (b) material
      không chứa số thô. Full suite 942 pass (+2 test DoD).
- **CỐ Ý HOÃN (N1/P6):** tag seed theo {segment, action_type} — segment chưa tồn
      tại (đẻ ở C0 Director). Mood-tag hoãn tới khi thấy pool lôi seed sai mood.
      Không build hạ tầng chưa ai dùng.
- **BUG A1 phát hiện+vá:** đường Mai-tự-nói (prompt_builder) vẫn ép mood block →
      A1 chỉ dọn đường chat_reply. A2.3 dọn nốt.

## ⚠️ Register (văn phong) — user feedback 2026-08-06, CHƯA làm
- User: sau A1 tự nhiên hơn nhưng "câu văn phức tạp hoá / trang trọng hoá".
- Chẩn: KHÔNG phải do chưa fine-tune (roadmap cấm fine-tune trước A+C). Là
  register mặc định instruct model + prompt chưa ghì giọng nói + sampling.
- Fix rẻ (~30ph, bước riêng): thêm ràng buộc khẩu ngữ vào persona (câu ngắn,
  cấm từ văn viết "tuy nhiên/chính vì vậy"), + 2-3 few-shot mẫu giọng đúng, +
  cân nhắc temperature/sampling. LÀM SAU khi user duyệt A1+A2.

## A1 Phase A — Bỏ mood block (2026-08-06)
- [x] A1.1 Metric raw_had_mood_block. _log_turn regex parsed.raw để đo LLM có tự
      sinh block không (parser đã strip khỏi mai_text). eval_transcript ưu tiên
      field mới, fallback regex mai_text cho log cũ.
- [x] A1.2 persona_system.txt XOÁ khối "Định dạng BẮT BUỘC" + khuôn mood block +
      "lý do:/còn nữa:". Chỉ thị mood chỉ dặn "khớp mood ĐƯỢC GIAO, không kê khai
      số". Tự nhiên qua giọng và cách nói.
- [x] A1.3 parser.py `ok` = True miễn text non-empty (không còn phụ thuộc mood
      block đủ 5). Defensive vẫn strip block nếu LLM lỡ sinh. `continuation` suy
      từ dấu câu cuối (endswith ",", "…", "...").
- [x] A1.4 llm_turn._apply_emotion_feedback CHỈ còn clear_tone_flags. Kênh B
      (apply_llm_hint) + drift detect ĐÃ BỎ. Canned mood update chỉ khi dominant
      != neutral (defensive).
- [x] A1.5 EmotionOrchestrator.apply_llm_hint no-op (giữ signature backward compat).
- [x] A1.6 XOÁ services/qc/drift_detector.py + test_drift_detector.py (8 test).
      Bỏ import + usage khỏi cli.py, stream_runtime.py, test_emotion_dod.py.
      LLMTurnRunner bỏ param drift_detector + attr _drift + last_drift_report.
- [x] A1.7 Fix 4 test đụng semantic mới: parser ok logic (2), apply_llm_hint no-op
      (1), persona assertion (1).
- [x] Full suite 940 pass (giảm 9 từ 949 do xoá 8 test drift_detector + 1 test
      apply_llm_hint đổi assertion).

## Next roadmap
- User chạy stream test A1: eval_transcript raw_had_mood_block phải ~= 0 (LLM
  hết tự sinh block). Nếu vẫn có → prompt chưa vào cache, thử clear KV cache
  llama-server hoặc kiểm persona_system.txt đã load đúng chưa.
- A2 Content pool sâu + động (3-4h) — sau khi A1 verify xong.
- A3 Nhịp biến thiên + filler (2-3h).
- A4 Emotion có object (4-6h).
- Sau Phase A → C0 Director + Chat handling (4-7 ngày, thứ biến reactive→host).

## ROADMAP AUTONOMOUS HOST — B0 Baseline (2026-08-06)
- [x] B0.1 LLMTurnRunner._log_turn wire (run_turn + run_ambient_turn), turn_logger param
      optional (backward compat). Schema: turn_id, kind (chat_reply|ambient), user_text,
      mai_text, parse_ok, mood_dominant, mood_intensity, trigger_type, level_used,
      latency_ms, viewer_id, session_id, timestamp. Fail-safe sink lỗi warning không kill.
- [x] B0.2 scripts/eval_transcript.py — 4 metric: opener_repeat_ratio (3 từ đầu),
      dead_air (gap timestamp > threshold s, default 10), mood_exposition_count
      (regex mood block [vui:N ...]), turn_counts. CLI --file/--since/--json.
- [x] B0.3 22 test mới (15 test_eval_transcript + 7 test_llm_turn_logger).
- [x] B0.4 docs/baselines/README.md — quy trình + template YYYYMMDD_<tag>.md.
- [x] B0.5 Wire setup_from_config + turn_logger= vào scripts/cli.py và
      orchestrator/stream_runtime.py. logging.yaml đã có turns_file.
- [ ] User chạy stream 30' → dán số vào docs/baselines/2026MMDD_pre_a1.md

## Next (roadmap)
- A1 bỏ mood block khỏi output (persona_system.txt + parser + llm_turn +
  emotion_orchestrator.apply_llm_hint + XOÁ drift_detector). CHỈ làm sau khi có
  baseline.

## Autonomy v2 milestone (5) — Mai tự nói probabilistic + category-based
- [x] Aut.A UrgeAccumulator + CategorySelector + config (21 test).
      Urge probabilistic (sigmoid quanh floor, cap prob_max, Gaussian noise),
      mood coupling (bon_chon boost, buon/nguong dampen), nag decay per
      consecutive_ignored, self_cooldown TÁCH last_external_activity vs
      last_self_speak. Selector weighted random no-repeat + per-cat cooldown.
- [x] Aut.B Material pipeline (35 test). RoundRobinPool xoay vòng no_repeat_last_n
      + reshuffle. MaterialProvider 5 category dispatch (silence+chat count /
      topic seed / question seed / operator state / memory snippet), None →
      composer skip (không bao giờ để LLM bịa từ số 0). OpenerTracker chặn 3 từ
      mở đầu bơm vào prompt tường minh. DedupBuffer Jaccard overlap post-check.
- [x] Aut.C AutonomyEngine composer + prompt_builder (19 test). Compose 5 phần,
      maybe_generate loop 2×len(cats) tries → skip cat thiếu material. Prompt
      slot-fill per-category, forbidden_openers trong prompt.
- [x] Aut.D Refactor stream + CLI wire (0 test unit, verify --help + suite).
      Split stream.py → stream_youtube.py (--with-discord) + stream_discord.py
      (--with-youtube VID). Extract StreamRuntime shared (autonomy tick loop bg
      + share turn_lock với ChatRouter). CLI --autonomy flag wire EmotionOrch
      + AutonomyEngine + bg loop chia turn_lock với REPL.
- [x] Aut.E Integration DoD (9 test). Simulated FakeClock 2-6h, verify 5 DoD.

## ✅ DoD Autonomy v2 (spec Mục 4) — 5/6 (chờ live subjective)
- [x] Variance: 2h simulated → intervals CV > 5% (không constant)
- [x] No-repeat: 20 speaks liên tiếp không lần nào lặp cat + max cat < 60%
- [x] Self-cooldown: urge stays 0 trong toàn window, should_speak_now False
- [x] Mood coupling: bon_chon=9 vs 1 → HIGH speaks nhiều hơn LOW (45 phút sim)
- [x] Nag decay: consecutive_ignored=5 → urge tăng chậm hơn baseline
- [ ] Live ≥2h subjective — chờ user chạy `python scripts/cli.py --autonomy`



## Platform milestone (4) — Stream mode: YouTube + Discord
- [x] Platform.A YouTubeChatService (13 test). pytchat wrapper, poll 2s, parse
      message + author + super chat amount_vnd (mapping donation appraisal).
      Fail-safe malformed msg skip, client die → stream ends.
- [x] Platform.B DiscordChatService (16 test). discord.py bot event-driven bridge
      qua asyncio.Queue. Token từ env var (DISCORD_BOT_TOKEN, không hardcode).
      Filter ignore_bots + channel_ids whitelist. Queue full → drop_newest.
- [x] Platform.C ChatRouter (14 test). Multi-source consumer 1 task/source,
      serialize turn qua asyncio.Lock (llama-server 1 instance). Convert
      InputEvent → EmotionEvent (super_chat → SYSTEM donation). Speak callback
      optional cho TTS. Fail-safe emotion/runner raise → skip event, không kill.
- [x] Platform.D scripts/stream.py entry. CLI flags: --youtube VIDEO_ID / --discord
      / --tts / --memory / --dashboard. Wire full stack (LLM + Emotion + TTS +
      Memory + sources + Router). Ctrl+C stop gracefully.
- requirements.txt: thêm pytchat 0.5.5 + discord.py 2.7 + sqlite-vec

## ✅ DoD Platform — chưa formal (không nằm trong PROCESS.md, MVP)
- [x] YouTube chat → Mai response (unit test verify router flow)
- [x] Discord chat → Mai response (unit test verify router flow)
- [x] Multi-source interleaved không đè turn (serialize lock test)
- [x] Super chat → donation appraisal (unit test conversion)
- [ ] Live test unlisted YouTube stream — chờ user chạy `python scripts/stream.py --youtube ID --tts`



## Phase 7.5 milestone (5) — Emotion Simulation (Appraisal + Mood Engine)
- [x] 7.5.A MoodEngine + config (21 test). Spring-damper 2 kênh over-damped,
      saturation max+0.5×(n-1) cap 10, target decay theo elapsed từ set gần nhất.
      DoD 3/7: 10k tick no NaN/oscillation, saturation 100 event, target decay.
- [x] 7.5.B Classifier + Appraisal + Modifiers (58 test). 20 category chính
      (10 system + 10 chat) + 4 timer đúng bảng Mục 4. 3 modifier: repeated_troll
      (session counter +0.5/hit), repeated_shutdown (memory 7 ngày ≥3 → ×1.3),
      first_time (session+memory query, ×1.2). Filter Phase 3 priority sexual >
      jailbreak > troll. Sad share > compliment (tone override). Config yaml đầy.
      DoD 1/7: 24 category + 4 timer + 3 modifier.
- [x] 7.5.C EmotionOrchestrator + tick loop 10Hz (17 test). Buffer per-dim
      trong 1 tick → flush saturate 1 lần. Background asyncio task tick, start
      idempotent, stop cancel-safe. active_tone_flags/clear cho Prompt/Filter đọc.
- [x] 7.5.D PromptManager.build_request_with_mood + persona +1 dòng (12 test).
      Chèn 1 system message SAU persona chứa Context (current_mood + category +
      tone flag hints). Backward compat: build_request cũ không đổi.
- [x] 7.5.E DriftDetector + wire LLMTurnRunner + integration DoD (16 test).
      Compare engine mood vs LLM self-report, flag > threshold (default 4).
      LLMTurnRunner peek emotion trước turn (build_request_with_mood) + sau turn
      apply_llm_hint (Kênh B) + drift detect + clear tone flags.

## ✅ DoD Phase 7.5 (ARCHITECTURE 11.8.5) — 6/7 (chờ live)
- [x] 20 category + 4 timer + 3 modifier đúng bảng (7.5.B)
- [x] MoodEngine over-damped, không dao động/NaN qua 10k tick (7.5.A)
- [x] Saturation 100 event đồng thời không overshoot >10/kẹt clamp (7.5.A)
- [x] Target decay về baseline, không kẹt đỉnh (7.5.A + integration mood_decays)
- [x] 2 cờ tone nối đúng Prompt + Filter (7.5.D + integration test tone_flags_wired)
- [x] Drift detector log khi lệch > threshold (7.5.E + integration test drift_flagged)
- [ ] Live ≥100 turn, mood curve "cảm thấy đúng" (subjective, user duyệt)

## Phase 7 milestone (6) — Memory (Semantic + Working + Fallback)

## Phase 7 milestone (6) — Memory (Semantic + Working + Fallback)
- [x] 7.A Migration 004 + sqlite-vec loader (3 test). memory_entries (11 cột có
      viewer_id/session_id) + memory_vectors vec0 float[1024] cho bge-m3.
      migration_runner._try_load_sqlite_vec() auto-load ext trước apply.
- [x] 7.B SqliteVecStore (14 test). insert atomic 2 bảng idempotent, query_knn
      với filter tier/viewer_id post-KNN over-fetch 3x, fetch_by_id/count/delete.
      Sync API + check_same_thread=False (asyncio.to_thread pattern).
- [x] 7.C BgeM3Embedder (17 test). Lazy load bge-m3 CPU, LRU cache 1000 câu,
      embed_batch bypass cache, normalize=True (cosine chuẩn), dim validate raise sớm.
- [x] 7.D SemanticMemoryService (16 test). impl MemoryService, hard timeout
      150ms query qua asyncio.wait_for → N7 fail-safe trả [] không raise.
      write không timeout. StoredEntry ↔ MemoryEntry với distance nhét metadata.
- [x] 7.E WorkingMemoryService (11 test) + MemoryFallbackManager (10 test).
      Deque 20 in-memory, query LIFO filter tier/viewer_id. Chain semantic → working
      spec 8.7.6; write fan-out cả 2 tier (N7 partial success khi primary fail).
- [x] 7.F.1 viewer_id filter interface + MemoryExtractor (16 test).
      Regex preference (tớ/tôi/mình thích/ghét/tên/sinh nhật) → PERSISTENT tier
      importance 0.85; SESSION tier importance 0.5; mood≥7 → high_intensity tag.
      Content 'User: X | Mai: Y' embedding match cả 2.
- [x] 7.F.2 Wire LLMTurnRunner + DoD integration test (5 test).
      run_turn(viewer_id, session_id, trigger_type) → auto extract + asyncio
      fire-and-forget write (không block turn sau). _schedule_memory_write no-op
      nếu memory=None (backward compat).

## ✅ DoD Phase 7 (ARCHITECTURE 11.8) — pass với FakeEmbedder
- [x] Retrieve P95 <150ms (100 entry, 50 query)
- [x] Fallback về working khi semantic timeout (SlowEmbedder 200ms > 150ms)
- [x] Manual inject 10 → callback ≥80% (query keyword hit top_k=3)
- [x] Multi-viewer 5 người, 3 entry/viewer → filter viewer_id không leak
- [ ] Live test với bge-m3 thật (marker 'memory_live') — chưa viết, user chạy khi cần


## 📌 SWAP TTS BACKEND (2026-08): viXTTS → VieNeu-TTS v3 Turbo
- **Lý do:** VieNeu TTFA 308ms (vs viXTTS 450ms, nhanh 32%), VRAM 0.37GB (vs 1.79GB,
  nhẹ 4.8x), 48kHz (vs 24kHz), fine-tune LoRA nhẹ (vs full-weight XTTS ~24GB VRAM
  không fit RTX 5060 Ti). Chốt sau spike `spike/day_vieneu/benchmark_clone.py`.
- **Chain mới:** VieNeu (L0 primary) → subtitle (L1). Bỏ hẳn viXTTS khỏi chain.
- **File xoá:** services/tts/vixtts_service.py, vixtts_patches.py, test_vixtts_*.py
- **File mới:** services/tts/vieneu_service.py (14 unit test pass, dùng FakeEngine
  không cần GPU/vieneu package). Enroll ref audio (`add_voice`) 1 LẦN trong start()
  — critical: mỗi infer không cache → TTFA 5626ms (18x chậm hơn).
- **Ref audio:** giữ `models/tts/xtts/vixtts/vi_sample.wav` (giọng user đã ưng).
- **Config:** models.yaml tts.* thay params (style/temperature/top_k/max_new_frames).
- **Deps:** transformers upgraded 4.57 → 5.14 (hub 1.x support). vieneu 3.2.4 +
  gradio 6 + sea-g2p + pandas 3 vào venv chính. coqui-tts KHÔNG import được nữa
  (không load nổi hub 1.x) — đã xoá vì đằng nào cũng bỏ viXTTS.
- **Test:** 579 pass (trước 578, +14 vieneu, -13 vixtts). test_phases Phase 4 update.
- **Backup:** `requirements_before_vieneu.txt` (132 lines) để rollback nếu cần.

## Phase 4 milestone (5) — viXTTS streaming
- [x] 4.A VN cleaner + coqui-tts patches — 12 unit pass. services/tts/vixtts_patches.py:
      vi_expand_numbers (num2words), vi_clean (expand+strip+lowercase), apply_patches
      idempotent (torchaudio.load→soundfile, VoiceBpeTokenizer.preprocess_text vi hook).
      requirements.txt: coqui-tts + num2words + soundfile explicit; piper-tts REJECTED comment.
- [x] 4.B ViXttsService — 12 unit pass (fake model, no GPU). services/tts/vixtts_service.py:
      from_loader đọc models.yaml tts.*; start() apply patches → load Xtts (asyncio.to_thread)
      → cache gpt_cond_lat+spk_emb 1 lần; synthesize_stream chạy inference_stream trong
      executor, forward chunks qua asyncio.Queue → yield AudioChunk (float32 mono PCM
      @ sample_rate); cancel qua flag (check between chunks). Metrics TTFA/chunks/RTF.
- [x] 4.C sentence splitter + subtitle fallback — 18 unit pass.
      services/tts/sentence_splitter.py split_vn: regex . ! ? … giữ dấu, bảo vệ
      số thập phân/viết tắt (3.14, 1.250.000), lọc câu không chữ (min_len alnum).
      services/tts/subtitle_fallback.py SubtitleFallbackService(TTSService): Level 2
      (spec 8.7.3) — không phát audio, push text qua on_subtitle callback + event
      bus, yield 1 final empty chunk. Sink error không giết pipeline (N7).
- [x] 4.D audio player — 7 unit pass (FakeBackend, không mở device thật).
      services/tts/audio_player.py AudioPlayer: worker loop bên trong asyncio Queue,
      play_blocking trong asyncio.to_thread → CHUNK N+1 chỉ bắt đầu khi N xong
      (DoD no-overlap). cancel_current(request_id) drop pending + stop chunk hiện tại.
      Backend abstract (default SounddeviceBackend, test inject FakeBackend).
      is_playing property, chunks_played/dropped metrics.
- [x] 4.E pipeline + dashboard TTS tab + DoD — 14 unit/integration pass.
      services/tts/tts_pipeline.py TTSPipeline: split_vn → TTSRequest per sentence →
      FallbackManager chain (L0 viXTTS, L1 subtitle) → AudioPlayer.enqueue. TTFA đo
      từ speak() tới chunk audio đầu non-empty. cancel(req_id) forward primary+player.
      MetricsCollector: record_tts_turn + tts_snapshot (turns/last_ttfa/subtitle_fb).
      DashboardServer: +tts_service/audio_player/tts_pipeline, snapshot["tts"] merge
      pipeline+service+player. Frontend: tab TTS mới + realtime TTFA chart.
      test_phases.py thêm Phase 4.

## ✅ DoD Phase 4 (ARCHITECTURE 11.5) — Must (Stretch để sau)
- [x] Không audio overlap giữa turns (test_no_overlap: 2 câu x 2 chunk × start+end xen kẽ)
- [x] TTFA đo được end-to-end (test_ttfa_measured + metric tts_pipeline_last_ttfa_ms)
- [x] Subtitle fallback triggered khi primary lỗi (test_primary_error_falls_to_subtitle)
- [ ] TTFA P50 <1s trên model thật — chờ user chạy live (spike day2 đo 465ms → khả thi)
- [ ] Quality subjective >6/10 qua 30 câu — chờ user duyệt live (không tự tick)
- Toàn suite: 578 pass. Live test có marker "tts" (chưa viết — làm khi user muốn).

## Phase 3 milestone (3)
- [x] 3.A RuleFilter — 13 unit pass. services/filter/rule_filter.py (FilterService):
      4 category, patterns config/filters.yaml (N6), severity/action max-priority,
      fail-open intrinsic (N7, không raise; bad pattern skip lúc compile). PERSONA_BREAK
      bắt hedge robot KHÔNG bắt "là AI" trần. config_loader thêm "filters".
- [x] 3.B regenerate-with-hint — 13 unit + 3 wire pass. services/filter/regenerator.py
      FilterRegenerator: build hint từ categories_hit (map VN), append 2 message
      (assistant=bad, user=hint) vào messages, re-generate + re-check tối đa
      max_regenerate_attempts (config filters.filter.max_regenerate_attempts=1).
      Metrics: checked/attempts/recovered/exhausted. N7 fail-open (filter/LLM lỗi →
      trả bản trước, không raise). Wire OPTIONAL vào LLMTurnRunner._primary +
      last_filter_verdict; backward-compat (no regen = behave như cũ).
- [x] 3.C dashboard filter tab + integration DoD — 12 unit/integration pass.
      MetricsCollector: record_filter_check + filter_snapshot (checks/hits/hit_rate/
      by_category/fail_open/recent). LLMTurnRunner._record_metrics forward verdict.
      DashboardServer: filter_svc + regenerator params, snapshot["filter"] merge
      check-level + regen counts + service fail-open. Frontend: tab Filter mới
      (cards + by-category + recent). test_phases.py thêm Phase 3.

## ✅ DoD Phase 3 (ARCHITECTURE 11.4)
- [x] 20 troll persona-break/manipulation/explicit/harmful: catch rate 100% (>80%)
- [x] 100 câu clean (gồm "tớ là AI" không hedge): false positive 0% (<5%)
- [x] Regenerate hoạt động khi persona_break detected (3.B unit test)
- [x] Filter fail-open khi regex/service error (3.A/3.B fail-safe test)
- Toàn suite: 515 pass (0 fail, 5 llm-live deselected).
**Cập nhật:** 2026-07-31

## ⚠️ Bug tiềm ẩn (flag, ngoài scope P2): migration backup filename chỉ có độ phân giải
giây → 2 migration cùng giây đè backup (test_migration_runner flaky). Đã spawn task riêng.

## 📌 ROADMAP đổi (2026-07-31): thêm PHASE 7.5 — Emotion Simulation (giữa P7 và P8)
- Spec: `docs/EMOTION_SIMULATION.md`. Mood đổi nguồn: appraisal rule-based (20 category
  + 4 timer + 3 modifier) làm CHÍNH; mood block LLM → Kênh B (format Phase 1 KHÔNG đổi).
- Đã cập nhật: ARCHITECTURE 11.8.5 + changelog v2.4, PROCESS Phase 7.5 + flow, persona.md Phần B.
- CHƯA code (đang ở Phase 2) — chỉ tích hợp tài liệu/roadmap. Code khi tới Phase 7.5.

## Phase 2 milestone (5) — delta, hạ tầng đã có từ P0
- [x] 2.A interrupt policy (7.9.3) — 11 unit pass. trigger_manager: set_speaking_context
      provider (N8), _should_interrupt đọc state_machine.yaml interrupt_policy. OPERATOR_VOICE
      elapsed>=2000ms → INTERRUPT_CURRENT (trigger vẫn enqueue để trả lời sau), <2s/mention/
      normal → QUEUE. fail-safe khi provider lỗi. metric trigger_interrupt_total.
- [x] 2.B deadlock watchdog (7.10.4) — 9 unit pass. state_watchdog.StateWatchdog:
      poll interval (config auto_recovery), elapsed>max_time_in_state → emergency_stop
      + auto recover→IDLE. IDLE/PAUSED (null) không giám sát. asyncio.Event stop (không
      hang). N8: chỉ dùng API state machine. metric watchdog_deadlocks_total.
- [x] 2.C ambient content gen (7.9.4) — 9 unit pass. PromptManager.build_ambient_request:
      persona + history + user turn "tự mở lời" (template config/prompts/ambient_instruction.txt,
      placeholder {silence} phút + {mood}). models.yaml ambient_prompt_path. Không mutate history.
- [x] 2.D dashboard tabs enrich — 4 unit pass. QueueStats +skipped_total/interrupt_total;
      trigger_manager populate; dashboard_server +watchdog param + snapshot["watchdog"];
      frontend: Triggers tab +Skipped/Interrupt cards, State tab +watchdog-info line.
- [x] 2.E integration 12.8 + DoD — 11 integration pass. turn_orchestrator.TurnOrchestrator:
      glue trigger↔state machine↔watchdog (consumer loop, interrupt cắt speak, emergency,
      resume). Test: priority operator>mention>normal, spam 60/phút→chỉ 3 lọt (rate),
      ambient>60s, trigger-during-thinking→queue, spam-during-speaking→drop, operator
      interrupt speaking, emergency từ speaking→PAUSED+clear queue, race 2 trigger, watchdog wiring.

## ✅ DoD Phase 2 (ARCHITECTURE 11.3)
- [x] Spam 60 tin/phút → không respond tất cả (rate limit 3/10s, test)
- [x] Priority operator_voice > mention > normal (test)
- [x] State transitions log đầy đủ (structlog + event bus + history, từ P0)
- [x] Ambient talk sau silence > 60s (test)
- [x] Watchdog detect deadlock khi force stuck (2.B unit + integration wiring)
- [x] Integration 12.8 xanh (11 test)

## ✅ CHECKPOINT P1 — user DUYỆT (2026-07-31)
- Soak 100 turn model thật: 0 crash ✅ | parse mood 100/100 = 100% ✅ | fallback 0
- Persona ổn định qua 100 turn: cà khịa, deflect kiến thức, nhận là AI khi hỏi thẳng,
  không khẩn cầu/không lộ system prompt — KHÔNG vi phạm ranh giới Phần C
- Dashboard TTFT/decode realtime OK (cli.py --dashboard / soak --dashboard, cùng process)
- ⚠️ TTFT p50=773ms > target 600ms — ĐẠT-CÓ-ĐIỀU-KIỆN: số 600 đo prompt ngắn
  Pre-flight; full history 12 cặp (~2000 tok) prefill nặng dần (min336→max1114). User
  chấp nhận (first-audio ~1.2s vẫn OK). SOI LẠI ở Phase 4 (TTS) nếu first-audio chậm.
  → Tùy chọn tối ưu sau: giảm max_history_turns / điều tra cache_prompt reuse.
- Công cụ: scripts/cli.py (--dashboard), scripts/soak_turns.py, config/prompts/soak_prompts.txt

## ✅ BLOCKER ĐÃ GIẢI HOÀN TOÀN (2026-07-30) — thủ phạm là httpx buffer, streaming vẫn NHANH
- Model: "Gemma 4 12B It Qat Uncensored Heretic" (uncensored Gemma 4 12B). Reasoning
  là NATIVE của Gemma 4 (Google build sẵn) → mọi bản Gemma 4 đều có, tắt bằng --reasoning off.
- Chẩn đoán cuối (đo thật, cùng prompt, first-CONTENT token):
  - **raw asyncio socket: 72ms** ✅ | curl -N: 283ms | **httpx stream: 2200ms** ✗
  - httpx buffer bất kể iter_lines/bytes/raw, trust_env, gzip. Server stream 69-72ms.
  - "2.4s" TỪ ĐẦU là do httpx buffer phía client, KHÔNG phải model/reasoning.
- Option C (--chat-template gemma) THẤT BẠI: output rác (khoá special token harmony).
- **CHỐT cho Phase 1 (streaming OK, giữ model uncensored):**
  - Server: `--flash-attn on --reasoning off` (bỏ --prompt-cache)
  - Endpoint `/v1/chat/completions`, persona = system message
  - **Streaming qua `asyncio.open_connection` (raw socket stdlib), KHÔNG httpx** → TTFT 72ms
  - httpx CHỈ cho non-stream (health/props)
  - Pipeline: LLM stream TTFT 72ms → tách câu → viXTTS inference_stream (TTFA 450ms)
    → first audio ~0.5s. Vượt target.
  - 1.B PHẢI viết lại: /v1/chat/completions, streaming qua asyncio socket, LLMRequest
    mang messages (system+history).

## Phase 1 milestone (6)
- [x] 1.A process_manager + fix config path — 14 unit + 1 live pass (start/healthy/stop server thật 22.8s)
  - Fix path: binary=E:\BAI_CUA_DUC\llama\llama-server.exe, model=gemma_4_12B_Q4.gguf
  - BỎ --prompt-cache (flag llama-cli, KHÔNG phải server; spec 10.3 nhầm) → dùng cache_prompt request param
  - flash-attn cần "on" (build mới cần [on|off|auto])
- [x] 1.B llama_cpp_llm streaming — VIẾT LẠI xong: /v1/chat/completions + raw
      asyncio socket (KHÔNG httpx) + --reasoning off. 63 unit + 2 live pass.
      Live: TTFT 204ms cold (warm ~72ms), decode 40.7tps, content sạch, cancel OK.
      Interface thêm ChatMessage + LLMRequest.messages + to_messages(). httpx chỉ health.
      config/models.yaml extra_flags thêm --reasoning off.
- [x] 1.C prompt_manager + persona (A+B+C) + prompt_cache — 20 unit pass
      - config/prompts/persona_system.txt (dựng từ persona.md A+B format+C ranh giới)
      - prompt_cache.PromptCache: load+freeze persona, version hash 12 ký tự, as_message()
        (vai trò: giữ prefix byte-ổn định cho KV cache reuse, KHÔNG file --prompt-cache)
      - prompt_manager.PromptManager: build_messages [system+history+user] thuần,
        commit_turn ghi history + trim theo max_history_turns, build_request → LLMRequest
      - models.yaml thêm: persona_prompt_path, max_history_turns=12, temperature=0.85
- [x] 1.D parser — 24 unit pass. services/llm/parser.py: parse_response(raw)->ParsedResponse
      (text + MoodState + reason + continuation + ok + raw). Fail-safe: sai format vẫn
      trả text, ok=False, không raise. Strip <think>/<|token|>. Key mood alternation
      (có/không dấu, space/underscore), clamp 0-10. Parse cả "còn nữa" (Phase 2 dùng sau).
      Né ngoặc vuông ngẫu nhiên trong text (chọn block nhiều mood key nhất).
- [x] 1.E CLI + LLM fallback 2-level — 19 unit pass + live smoke.
      - canned_response.CannedResponder: pick theo dominant mood (config models.yaml
        llm_canned.responses), fail-open pool "..."
      - llm_turn.LLMTurnRunner: đăng ký chain "llm" vào FallbackManager (0.D):
        L0 primary stream+parse, L1 canned; run_turn build→execute→commit history
        (lưu text ĐÃ tách mood block); update canned mood chỉ khi parse ok
      - scripts/cli.py: CLI input mode full stack (interactive + auto), on_token stream
      - models.yaml: llm_canned (timeout_primary_s 5.0, timeout_canned_s 0.1, responses)
      - live: primary stream OK, parse_ok=True, mood dominant hiển thị, TTFT 352ms warm
- [x] 1.F dashboard LLM metrics + integration — 11 unit/integration pass + visual check.
      - MetricsCollector: llm_ttft_seconds(hist), llm_decode_tps, llm_requests_total,
        llm_fallback_total, llm_parse_total{ok/fail}; record_llm_turn() + llm_snapshot()
      - LLMTurnRunner nhận metrics=, tự record sau mỗi turn (best-effort get_metrics)
      - dashboard build_snapshot thêm "llm"; frontend panel LLM (TTFT/decode/turns/
        parse%/fallback) + chart TTFT realtime — visual verify qua browser (parse 93.3%)
      - integration test_phase1_turns: 100 turn no crash + parse 100%, 96% với malformed,
        force-timeout → canned (level 1), history trim ổn định. Full suite 432 pass.

## Phase 0 — HOÀN THÀNH (báo cáo: docs/phase0_report.md)
- 0.A Config+Logger, 0.B Interfaces+Features, 0.C EventBus+StateMachine,
  0.D Trigger+Fallback, 0.E Migration, 0.F Metrics+Dashboard+EmergencyStop,
  0.G Health monitor + leak test
- 331 test pass. DoD 7/7 tick (leak test + live soak 60s RSS phẳng 60→61MB)

## Tiến độ Phase 0 (6 milestone)
- [x] **0.A Config + Logger** — 40 test pass
  - `config/system.yaml`, `models.yaml` (số Pre-flight đã điền), `logging.yaml`, `features.yaml`
  - `orchestrator/config_loader.py` — dotted access, atomic reload, watchdog hot-reload
  - `orchestrator/logger.py` — structlog + JSONL (events/turns) + rotation
  - `pytest.ini`
- [x] **0.B Interfaces + Feature registry** — 91 test pass (tổng 131)
  - `interfaces/base.py` (Service ABC + HealthStatus), `input.py`, `stt.py` (+NullSTTService stub),
    `llm.py`, `filter.py`, `tts.py`, `animation.py` (MoodState 5 mood), `memory.py`
  - `orchestrator/features.py` — FeatureManager: 6 toggle rule (atomic/log/dependency/conflict/resource/rollback)
  - `config/system.yaml` thêm `resources.*` (VRAM budget 5594MB) + `features.core` (7 core feature)
- [x] **0.C Event bus + State machine** — 71 test pass (tổng 202)
  - `orchestrator/event_bus.py` — asyncio pub/sub fan-out, bounded queue, drop_oldest/drop_newest, TOPIC_ALL
  - `orchestrator/state_machine.py` — AsyncMachine 5 state / 9 transition, action hook, cooldown timer
  - `config/state_machine.yaml` — cooldown 500ms, interrupt_policy + watchdog threshold (Phase 2 dùng)
  - 5 hypothesis property test: state luôn valid, emergency_stop từ mọi state → PAUSED,
    resume → IDLE, history liên tục, SPEAKING chỉ vào từ THINKING
- [x] **0.D Trigger + Fallback skeleton** — 51 test pass (tổng 253)
  - `interfaces/trigger.py` — 4 TriggerType, Trigger/TriggerDecision/QueueStats
  - `orchestrator/trigger_manager.py` — classify, priority heap, spam, rate limit chat_normal, ambient 60s, TTL prune, overflow drop-lowest
  - `orchestrator/fallback_manager.py` — generic 2-level chain + timeout per level (N1 no circuit breaker)
  - `config/triggers.yaml` — 4 priority, rate limit, spam patterns, ambient threshold
  - Chưa làm (Phase 2): interrupt policy enforce, ambient content gen
- [x] **0.E SQLite migration** — 19 test pass (tổng 272)
  - `migrations/001_initial.sql` — turns, state_transitions, trigger_decisions (+index), IF NOT EXISTS
  - `orchestrator/migration_runner.py` — versioned SQL, numeric order, backup-before (shutil), idempotent, fail→success=0 + retry
  - Rule 8.8.4: chỉ THÊM, không auto-rollback (restore từ backup)
- [x] **0.F Metrics + Dashboard + Emergency stop** — 43 test pass (tổng 315)
  - `orchestrator/metrics_collector.py` — prometheus (TTFA/trigger/state + 3 fake gauge), CollectorRegistry riêng
  - `orchestrator/emergency_stop.py` — Ctrl+Shift+X (keyboard lib, degrade nếu không admin)
  - `dashboard/dashboard_server.py` — FastAPI + WS + REST (toggle/estop/resume/metrics)
  - `dashboard/templates/index.html` + `static/` — vanilla JS + canvas chart (không CDN, 100% local)
  - `orchestrator/main.py` — wiring toàn bộ + uvicorn
  - **Verified live:** server chạy localhost:7860, metric realtime qua WS, toggle OK,
    emergency→PAUSED→resume→IDLE, prometheus counter tăng đúng, hotkey bound (admin)

## ✅ DoD Phase 0 (ARCHITECTURE 11.1)
- [x] Dashboard mở ở localhost, toggle giả bật/tắt được
- [x] Metric giả cập nhật realtime trên chart (WS push mỗi 1s)
- [x] Emergency stop Ctrl+Shift+X → PAUSED từ mọi state (property test + live)
- [x] State transitions log được (structlog JSONL + event bus + SQLite table)
- [x] Config reload không cần restart (watchdog test)
- [x] Test phase 0 xanh — 315 passed
- [ ] Không memory leak sau 1h idle — CHƯA test (cần chạy dài, để user verify tuỳ chọn)

## Pre-flight (DONE)
- [x] Day 1 LLM latency — GO (TTFT cold 444ms, decode min 40tps, max temp 63°C)
- [x] Day 2 TTS — Piper REJECT, viXTTS GO (cond_len=30, VRAM 1.79GB, 2600ms avg)
- [x] Day 3 STT — SKIPPED (user scope decision, xem `spike/day3_report.md`)

## Phase đã xong
- [x] Phase -1 Bootstrap
  - [x] `.gitignore` (plain UTF-8)
  - [x] `requirements.txt` (UTF-8, đủ package theo ARCHITECTURE 13.1)
  - [x] Cây thư mục khớp Appendix A
  - [x] venv activate được, pip install -r requirements.txt xong
  - [x] `STATE.md` tồn tại
  - [x] `.env.example`, `README.md`
  - [x] Commit "phase-1: bootstrap repo structure"

## Blocker / cần user verify trên máy thật (Appendix D)
- [ ] `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
- [ ] `LongPathsEnabled` = 1 trong registry
- [ ] `nvidia-smi` chạy được, VRAM idle < 500MB
- [ ] llama.cpp build với `GGML_CUDA=ON`, `llama-server.exe` chạy từ `.\build\bin\Release\`
- [ ] Model `gemma-4-12b-Q4_K_M.gguf` đã tải về `models\llm\`
- [ ] Windows Defender exception cho folder `llama.cpp\build\`
- [ ] Python chạy với quyền Administrator (cho `keyboard` hook toàn cục)

## Ghi chú
- Spike Day 2 chốt TTS: **viXTTS** (config trong `spike/day2_report.md`)
- ⭐ Phase 4 TTS BẮT BUỘC dùng `inference_stream()` (TTFA ~450ms đo thật), KHÔNG
  dùng `synthesize()` blocking (2.6s). End-to-end ~1s → đạt target. Xem day2_report.md.
- TTFT P50 thực đo: <điền sau Pre-flight Day 1>
- E4B model: **BỎ** (v2.3, Appendix C) — chỉ 1 instance 12B port 8080
- Nếu Pre-flight Day 1 tight VRAM → xem xét thêm E4B sau
- ⚠️ **Day 1 finding:** llama-server đang stream `delta.reasoning_content`
  cho model Gemma 4 12B (không phải `delta.content`). Có thể do chat template
  GGUF hoặc llama-server version. Phase 1 parser cần handle: nếu output có
  reasoning tags, extract phần answer thật (bỏ reasoning) trước khi parse
  mood block. Chi tiết verify sau Day 1.

## Next
Chờ user gõ "tiếp" → Pre-flight Day 1 (LLM latency benchmark, spike/day1_llm_latency/).
