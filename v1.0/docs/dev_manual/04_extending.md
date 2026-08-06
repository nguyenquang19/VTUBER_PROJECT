# 04 — Extending & Contributing

Pattern để thêm module mới. Follow N1-N8 (xem `01_architecture.md`).

---

## 1. Workflow chuẩn 1 task

Từ CLAUDE.md:

```
1. Đọc STATE.md → biết phase + task hiện tại
2. Đọc module liên quan trong 02_modules.md (persona.md nếu cần)
3. Viết code theo interface có sẵn, số liệu từ config
4. Viết test cho phần vừa code
5. Chạy test → xanh mới đi tiếp
6. Cập nhật STATE.md + dev_manual nếu đổi logic
7. Commit: "phaseN: <mô tả ngắn>"
```

**Trước khi code file bất kỳ (per CLAUDE.md):**
1. Xác nhận đã đọc module tương ứng trong `02_modules.md`
2. List file sẽ tạo/sửa
3. List test sẽ viết
4. Confirm với user rồi mới code

---

## 2. Interface-based (N8)

Tất cả module chính implement interface ở `interfaces/*.py`:

- `interfaces/base.py` — `Service` ABC + `HealthStatus`
- `interfaces/llm.py` — `LLMService`
- `interfaces/tts.py` — `TTSService`
- `interfaces/memory.py` — `MemoryService`
- `interfaces/filter.py` — `FilterService`
- `interfaces/input.py` — `InputService`
- `interfaces/animation.py` — `AnimationService` (Phase 6, chưa impl)
- `interfaces/stt.py` — `STTService` (Phase 5 deferred)
- `interfaces/trigger.py` — `Trigger`, `TriggerType`, `QueueStats`

**Pattern service:**
```python
from interfaces.base import Service, HealthStatus

class MyService(SomeInterface):
    service_id = "my_service"

    def __init__(self, param1, param2=default, injected=None):
        self._injected = injected   # cho test inject fake
        self._log = get_logger("my_service")

    @classmethod
    def from_loader(cls, loader, injected=None):
        """Build từ ConfigLoader — số từ YAML."""
        return cls(
            param1=loader.get("models", "my_service.param1"),
            param2=int(loader.get("models", "my_service.param2", 10)),
            injected=injected,
        )

    async def start(self):
        if self._injected is None:
            self._injected = await asyncio.to_thread(self._load_dependency)
        self._log.info("my_service_ready", ...)

    async def stop(self):
        # Cleanup
        self._injected = None

    async def health_check(self) -> HealthStatus:
        if self._injected is None:
            return HealthStatus.unhealthy(self.service_id, "chưa start")
        return HealthStatus.healthy(self.service_id, ...)

    def get_metrics(self) -> dict:
        return {"my_service_calls": self._calls, ...}
```

---

## 3. Config over code (N6)

Không hardcode magic numbers. Thêm section vào YAML:

```yaml
# config/models.yaml
my_service:
  param1: "value"
  param2: 42
  timeout_s: 5.0
```

Register `config_loader.py` nếu file mới:
```python
CONFIG_FILES: dict[str, str] = {
    ...existing...,
    "my_new_config": "my_new_config.yaml",
}
```

Đọc trong service qua `from_loader`.

---

## 4. Fallback + timeout (N7)

Mọi external call phải wrap fallback chain. Không để 1 module crash cả hệ.

Pattern:
```python
from orchestrator.fallback_manager import FallbackManager

fb = FallbackManager()
fb.register_chain("my_op",
    handlers=[self._primary, self._fallback],
    timeouts=[5.0, 0.1],  # per level
)
result = await fb.execute("my_op", request)
# result.value = từ handler thành công
# result.level_used = 0 primary / 1 fallback
```

Handler `async def(request) -> Any`. Timeout → next level.

**Filter fail-open exception (N7):** filter crash → cho qua + log warning. Không block output.

**Async op timeout hard:**
```python
try:
    result = await asyncio.wait_for(self._do_op(), timeout=0.15)
except asyncio.TimeoutError:
    self._log.warning("timeout")
    return []  # fail-safe empty, không raise
```

---

## 5. Test theo phase (N5)

Mỗi module có 3 tầng test:

### 5.1. Unit test — fake dependencies

```python
# tests/unit/test_my_service.py
from services.my.my_service import MyService

class FakeDep:
    def __init__(self, ...): ...
    def method(self): ...  # simulate response

@pytest.fixture
def svc() -> MyService:
    return MyService(param1="test", injected=FakeDep())

class TestLifecycle:
    async def test_start_health_ok(self, svc):
        await svc.start()
        h = await svc.health_check()
        assert h.is_ok is True

class TestBehavior:
    async def test_does_thing(self, svc):
        await svc.start()
        result = await svc.do_thing("input")
        assert result == expected

class TestFailSafe:
    async def test_error_returns_default_not_raise(self):
        class Broken:
            def method(self): raise RuntimeError()
        svc = MyService(param1="x", injected=Broken())
        await svc.start()
        result = await svc.do_thing("x")
        assert result == default_value  # fail-safe

class TestFromLoader:
    def test_reads_config(self):
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        svc = MyService.from_loader(loader, injected=FakeDep())
        assert svc.param1 == "value"  # from yaml
```

**Fake dependencies — không tải real model:**
- LLM: `FakeLLMService` yield preset tokens
- TTS: `FakeBackend` không mở audio device
- Embedder: `FakeModel` với hash-based deterministic vec
- Memory store: `FakeStore` in-memory dict

### 5.2. Integration test — end-to-end mini flow

```python
# tests/integration/test_my_flow.py
async def test_end_to_end():
    # Setup real components (nếu có db), fake external
    loader = ConfigLoader(...)
    svc = MyService.from_loader(loader)
    await svc.start()
    
    # Chạy flow thật
    result = await svc.do_full_flow(input)
    assert result matches expected
    
    await svc.stop()
```

### 5.3. Live test — cần external process

Marker `pytest.mark.<name>`:
```python
@pytest.mark.llm_live
async def test_real_llm_stream():
    # Cần llama-server chạy
    ...
```

Chạy: `pytest tests/integration/test_llama_server_live.py -v`.
Skip default: `pytest --deselect tests/integration/test_llama_server_live.py`.

---

## 6. Thêm module mới — step-by-step

Ví dụ: thêm `TwitchChatService` (nếu tương lai cần Twitch).

### 6.1. Kiểm tra interface

`interfaces/input.py` đã có `InputService` — không cần thêm interface, chỉ implement.

### 6.2. Tạo file service

```python
# services/input/twitch_chat.py
from interfaces.input import InputEvent, InputService, EventSource
from interfaces.base import HealthStatus

class TwitchChatService(InputService):
    service_id = "input_twitch"

    def __init__(self, channel: str, client: Any = None):
        self.channel = channel
        self._client = client
        self._running = False

    @classmethod
    def from_loader(cls, loader, client=None):
        return cls(
            channel=loader.get("chat_sources", "twitch.channel", ""),
            client=client,
        )

    async def start(self):
        if self._client is None:
            # Real IRC connect ở đây (khi impl)
            ...
        self._running = True

    async def event_stream(self):
        if not self._running:
            raise RuntimeError("chưa start")
        # Loop nhận message → yield InputEvent(source=CHAT_TWITCH, ...)
        ...
```

### 6.3. Thêm EventSource nếu cần

`interfaces/input.py` — thêm `CHAT_TWITCH` (đã có sẵn).

### 6.4. Thêm config section

```yaml
# config/chat_sources.yaml
twitch:
  enabled: false
  channel: ""
  # OAuth token env var, etc.
```

### 6.5. Viết test

```python
# tests/unit/test_twitch_chat.py
class FakeIRCClient: ...

async def test_yields_events():
    svc = TwitchChatService(channel="test", client=FakeIRCClient(...))
    await svc.start()
    async for event in svc.event_stream():
        assert event.source == EventSource.CHAT_TWITCH
        ...
```

### 6.6. Wire vào script

Option A: thêm `--twitch CHANNEL` vào `stream_youtube.py` như `--with-discord`:
```python
if args.twitch:
    from services.input.twitch_chat import TwitchChatService
    sources.append(TwitchChatService.from_loader(loader))
```

Option B: tạo `scripts/stream_twitch.py` dedicated.

### 6.7. Update docs

- STATE.md: milestone note
- 02_modules.md: thêm section 9.4 Twitch

### 6.8. Commit

```
platform.E: TwitchChatService (IRC bot)

- services/input/twitch_chat.py: TwitchChatService impl InputService
- config/chat_sources.yaml: twitch section
- 10 unit test với FakeIRCClient
- scripts/stream_youtube.py: --with-twitch flag
```

---

## 7. Thêm phase / tính năng lớn mới

### 7.1. Design spec trước

Viết 1 file spec tạm `docs/PHASE_X_<TÊN>.md` (như FIX_PLAN/ROADMAP) với:
- Vấn đề gốc (why)
- Kiến trúc (component + data flow)
- Config schema
- DoD (5-7 items measurable)
- Rollback plan

### 7.2. Update dev_manual + STATE

- `01_architecture.md`: thêm layer/data-flow nếu đổi kiến trúc
- `02_modules.md`: thêm section module mới
- `STATE.md`: phase/task tracking
- Sau khi ổn định, gộp spec tạm vào dev_manual rồi xoá spec tạm (như đã làm với C0/A1).

### 7.3. Chia milestone

VD Phase 10 chia 5 milestone: 10.A → 10.E. Mỗi milestone 1 commit, có unit test + DoD tick.

### 7.4. Update STATE.md

Sau mỗi milestone: task done + full suite pass count + milestone summary.

---

## 8. Coding conventions

### 8.1. Python style

- Type hints đầy đủ (PEP 604 pipe union `str | None`, `list[...]`)
- Async/await cho I/O (không `time.sleep` trong async context)
- `from __future__ import annotations` ở top mọi file
- Docstring 1-3 dòng đầu module, không mô tả file dài dòng
- KHÔNG comment explain WHAT (well-named identifiers đã đủ) — chỉ comment WHY (constraint, workaround, subtle behavior)

### 8.2. File organization

```
v1.0/
├── config/                  # YAML + prompts
├── data/                    # SQLite (gitignored)
├── logs/                    # JSONL (gitignored)
├── backups/                 # DB backups (gitignored)
├── interfaces/              # ABC contracts
├── orchestrator/            # cross-cutting managers
├── services/
│   ├── autonomy/
│   ├── emotion/
│   ├── filter/
│   ├── input/
│   ├── llm/
│   ├── memory/
│   ├── qc/
│   └── tts/
├── dashboard/               # FastAPI + Vanilla JS
├── migrations/              # NNN_*.sql
├── models/                  # Model files (gitignored, *.gguf/*.pth/*.wav)
├── scripts/                 # Entry points
├── tests/
│   ├── unit/
│   └── integration/
├── spike/                   # Historical spike reports
├── docs/                    # Design + operational docs
└── requirements.txt
```

### 8.3. Git commit convention

Format: `phase{N}.{letter}: <verb ngắn>` hoặc `<type>: <verb ngắn>`.

Types:
- `phase{N}.{letter}:` — milestone của phase (VD `phase7.5.B:`)
- `platform.{letter}:` — platform integration (VD `platform.A:`)
- `autonomy.{letter}:` — autonomy engine
- `fix:` — bug fix
- `tune:` — config tune, không đổi behavior lớn
- `chore:` — cleanup, no functional change
- `docs:` — docs only

Body: liệt kê file thay đổi + reason. Ví dụ:
```
phase7.D: SemanticMemoryService (async wrapper + hard timeout 150ms)

- services/memory/semantic_memory.py: impl MemoryService interface
  - Compose SqliteVecStore + BgeM3Embedder
  - write(): embed → insert atomic
  - query(): hard timeout 150ms qua asyncio.wait_for, N7 fail-safe → trả []
  ...
- 16 unit test cover: lifecycle, write, query timeout, forget
- Full suite 629 pass.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

### 8.4. Gitignore

Đã có ở `.gitignore` root + `v1.0/.gitignore`. Đảm bảo KHÔNG commit:
- `*.gguf`, `*.pth`, `*.safetensors`, `*.onnx` (model files)
- `data/*.db`, `data/*.db-journal`
- `logs/`, `backups/`
- `__pycache__/`, `.pytest_cache/`
- `.env`, `venv/`
- `spike/**/*.wav`, `spike/**/*.mp3`
- Model config JSON của viXTTS/etc. (đã xoá viXTTS)

---

## 9. Common patterns đã dùng

### 9.1. Async lifecycle + inject cho test

```python
class Service:
    def __init__(self, real_dep_path, injected: Any = None):
        self._real_path = real_dep_path
        self._dep = injected  # test inject fake

    async def start(self):
        if self._dep is None:
            self._dep = await asyncio.to_thread(self._load_real)
```

### 9.2. Sync store + async wrapper

```python
class SyncStore:
    def query(self, ...): ...  # sync SQLite

class AsyncService(SomeInterface):
    async def query(self, ...):
        return await asyncio.wait_for(
            asyncio.to_thread(self._store.query, ...),
            timeout=self._timeout_s,
        )
```

### 9.3. Fire-and-forget background write

```python
async def run_turn(self, ...):
    ... # main flow
    # Fire-and-forget, không block turn tiếp theo:
    asyncio.get_running_loop().create_task(
        self._memory.write(entry),
        name=f"memory_write_{entry.entry_id[:8]}",
    )
```

### 9.4. Config dataclass

```python
@dataclass
class MyServiceConfig:
    param1: str = "default"
    param2: int = 10

    @classmethod
    def from_loader(cls, loader):
        raw = loader.get("models", "my_service", {})
        return cls(
            param1=str(raw.get("param1", "default")),
            param2=int(raw.get("param2", 10)),
        )
```

### 9.5. Metric snapshot pattern

```python
def get_metrics(self) -> dict[str, Any]:
    return {
        "svc_calls_total": self._calls,
        "svc_errors_total": self._errors,
        "svc_last_latency_ms": self._last_latency,
        **self._sub_component.get_metrics(),  # forward child metrics
    }
```

### 9.6. Wrap external for hook

Ví dụ StreamRuntime wrap `ChatRouter._process`:
```python
_orig = router._process

async def _hook(event):
    autonomy.on_external_activity()
    rt.note_chat_activity()
    await _orig(event)

router._process = _hook
```

---

## 9b. Hai đường điều phối (QUAN TRỌNG khi sửa)

Repo hiện có 2 đường sinh turn — biết mình đang sửa đường nào:

| | Đường STREAM (dùng thật) | Đường LEGACY (main.py/cli.py không-director) |
|---|---|---|
| Entry | `scripts/stream_youtube.py` → `build_stream_runtime` | `orchestrator/main.py`, `cli.py` |
| Driver | `services/director/director_loop.py` | `orchestrator/turn_orchestrator.py` + `trigger_manager.py` |
| Chat | ChatRouter **intake** → SaliencePool → Director nhặt | ChatRouter **FIFO** hoặc TriggerManager |
| State machine | Segment (Director) | 5-state `state_machine.py` |
| Mood output | A1: không mood block, no drift | (cũ) có thể còn giả định mood block |

**Khi thêm/sửa hành vi turn cho stream → sửa `services/director/`, KHÔNG sửa TriggerManager/
TurnOrchestrator** (chúng chỉ phục vụ đường legacy/test). Lâu dài nên hợp nhất về 1 đường
(ghi trong FIX_PLAN "Dọn ngay").

### Thêm 1 DirectorAction mới
1. Thêm enum vào `DirectorAction` (`director.py`).
2. Thêm nhánh quyết trong `Director.decide()` (giữ thứ tự ưu tiên rõ).
3. Thêm `_exec_<action>` trong `director_loop.py` + case trong `_execute`.
4. Test `test_director.py` (quyết đúng) + `test_director_loop.py` (execute đúng).

### Thêm 1 nguồn novelty (SourceProvider cho self_talk)
Đổ dữ kiện vào `RuntimeContext` (material_provider) → thêm category + `MaterialProvider.get`
case. Nguồn tắt → `get()` trả `None` → category tự loại. Xem roadmap §Phase B.

---

## 10. Historical context — why some choices

**Tại sao raw asyncio socket cho LLM, không httpx?**
Spike day 1 đo: httpx buffer 2200ms → TTFT 2.4s. Raw socket → 72ms warm. Xem `STATE.md` § blocker 2026-07-30.

**Tại sao VieNeu thay viXTTS?**
Spike day_vieneu (2026-08): TTFA 308ms vs viXTTS 450ms (nhanh 32%), VRAM 0.37GB vs 1.79GB (nhẹ 4.8x), 48kHz, fine-tune LoRA nhẹ (viXTTS full-weight XTTS không fit 5060 Ti 16GB).

**Tại sao mood engine spring-damper thay LLM tự report? (A1)**
LLM self-report mood không có ground truth + tốn attention bookkeeping → thoại máy móc.
Appraisal rule-based có ground truth. A1 (2026-08) bỏ hẳn mood block khỏi output. Xem `02_modules.md §7`.

**Tại sao Director (C0) thay FIFO chat?**
FIFO đáp mọi tin → reactive, không ưu tiên superchat, tụt hậu khi chat đông. Director +
SaliencePool (điểm+decay) → chọn tin đáng, tự điều hành. Xem `02_modules.md §12`.

**Tại sao Autonomy v2 rewrite Phase 2 ambient?**
`silence > 60s` step function tạo pattern máy móc. Autonomy v2: urge probabilistic +
category no-repeat. Ở stream, Director gọi nó qua `force_generate`.

**Tại sao 4 trigger type, không thêm?**
N1 YAGNI. Chưa cần 12 type "phòng khi có". Autonomy v2 mở rộng THÔNG MINH HƠN 1 type có sẵn, không thêm type mới.

**Tại sao viXTTS bị xoá luôn, không giữ backup?**
transformers 5.x cần cho VieNeu incompat huggingface_hub<1.0 mà coqui-tts require. Không co-exist 1 venv. User chọn hẳn VieNeu → xoá viXTTS.

---

## 11. Contact / help

- Đọc `01_architecture.md` cho tổng quan
- `02_modules.md` cho chi tiết module
- `03_operations.md` cho chạy/config/debug
- Log ở `logs/events.jsonl`
- SQLite ở `data/mai.db`
- Test failures: `python -m pytest -v --tb=long <path>` để debug
