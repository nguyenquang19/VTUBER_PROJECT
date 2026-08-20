"""Strict YAML configuration loader with hot reload.

Load YAML từ `config/`, cho phép truy cập bằng dotted path, và tự reload
khi file thay đổi trên disk (watchdog) — không cần restart process.

Nguyên tắc:
- N6 config over code: mọi số liệu đọc từ đây, không hardcode trong .py
- Reload là atomic: parse thành công mới swap; parse lỗi → giữ config cũ + log
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import yaml
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# Các file config được quản lý. Key = tên logic, value = tên file.
CONFIG_FILES: dict[str, str] = {
    "system": "system.yaml",
    "models": "models.yaml",
    "logging": "logging.yaml",
    "data_privacy": "data_privacy.yaml",          # M0.4 privacy/retention
    "agent_state": "agent_state.yaml",            # M1 grounded working state
    "agent_goals": "agent_goals.yaml",            # M2 goal/agenda policy
    "conversation": "conversation.yaml",           # bounded thread/context/repair policy
    "hosting": "hosting.yaml",                    # M5 mood/persona/proactive hosting
    "relationships": "relationships.yaml",        # M7 privacy-safe social history
    "evaluation": "evaluation.yaml",              # M8 eval/data/fine-tune gates
    "operations": "operations.yaml",              # M9 live operations/recovery
    "capabilities": "capabilities.yaml",          # Phase 4 declarative availability
    "features": "features.yaml",
    "triggers": "triggers.yaml",
    "state_machine": "state_machine.yaml",
    "filters": "filters.yaml",
    "mood_engine": "mood_engine.yaml",         # Phase 7.5.A
    "emotion_appraisal": "emotion_appraisal.yaml",  # Phase 7.5.B
    "chat_sources": "chat_sources.yaml",             # Platform.A stream mode
    "autonomy": "autonomy.yaml",                     # Autonomy Engine v2
    "autonomy_content_pool": "autonomy_content_pool.yaml",
    "self_talk": "self_talk.yaml",                 # cause-first Thought Engine
    "pacing": "pacing.yaml",                          # A3 nhịp + filler
    "chat_salience": "chat_salience.yaml",            # C0.1 salience pool
    "director": "director.yaml",                       # C0.3 director loop
    "mood_style": "mood_style.yaml",                   # mood → chỉ dẫn giọng
    "affect_v2": "affect_v2.yaml",                     # M10.6 turn/session affect
    "mood_ab_cases": "mood_ab_cases.yaml",             # M10.6 balanced blind replay
    "animation": "animation.yaml",                     # VTube Studio animation adapter
    "data_schema_registry": "data_schema_registry.yaml",  # record wire-schema fingerprints
}

ReloadCallback = Callable[[str, dict[str, Any]], None]


class ConfigError(Exception):
    """Config không load được (file thiếu, YAML sai cú pháp)."""


class _ReloadHandler(FileSystemEventHandler):
    """Watchdog handler: debounce rồi gọi loader.reload_file()."""

    def __init__(self, loader: ConfigLoader, debounce_ms: int) -> None:
        self._loader = loader
        self._debounce_s = debounce_ms / 1000
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: Path) -> None:
        name = self._loader.name_for_path(path)
        if name is None:
            return
        with self._lock:
            existing = self._timers.pop(name, None)
            if existing:
                existing.cancel()
            timer = threading.Timer(self._debounce_s, self._loader.reload_file, args=(name,))
            timer.daemon = True
            self._timers[name] = timer
            timer.start()

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(Path(str(event.src_path)))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(Path(str(event.src_path)))


class ConfigLoader:
    """Load + hot-reload YAML config.

    Usage:
        loader = ConfigLoader(Path("config"))
        loader.load_all()
        port = loader.get("system", "dashboard.port")
        loader.on_reload(lambda name, data: print(f"{name} reloaded"))
        loader.start_watching()
        ...
        loader.stop_watching()
    """

    def __init__(self, config_dir: Path, required: tuple[str, ...] = ("system",)) -> None:
        self._dir = Path(config_dir)
        self._required = required
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._callbacks: list[ReloadCallback] = []
        self._observer: Observer | None = None
        self._handler: _ReloadHandler | None = None

    # ---------- load ----------

    def name_for_path(self, path: Path) -> str | None:
        """Map file path → config name. None nếu file không thuộc quản lý."""
        filename = path.name
        for name, fname in CONFIG_FILES.items():
            if filename == fname:
                return name
        return None

    def _path_for(self, name: str) -> Path:
        if name not in CONFIG_FILES:
            raise ConfigError(f"Unknown config name: {name}")
        return self._dir / CONFIG_FILES[name]

    def path_for(self, name: str) -> Path:
        """Return the owned path for a registered config name."""
        return self._path_for(name)

    def _read_yaml(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path.name}: top-level phải là mapping, nhận {type(data).__name__}")
        return data

    def load_all(self) -> None:
        """Load mọi file trong CONFIG_FILES tồn tại trên disk.

        File trong `required` mà thiếu → raise. File optional thiếu → skip
        (các file như triggers.yaml/state_machine.yaml tạo ở milestone sau).
        """
        for name in CONFIG_FILES:
            path = self._path_for(name)
            if not path.exists():
                if name in self._required:
                    raise ConfigError(f"Config bắt buộc không tồn tại: {path}")
                continue
            try:
                data = self._read_yaml(path)
            except yaml.YAMLError as e:
                raise ConfigError(f"{path.name}: YAML sai cú pháp: {e}") from e
            with self._lock:
                self._data[name] = data

    def reload_file(self, name: str) -> bool:
        """Reload 1 file. Atomic: parse lỗi → giữ config cũ, return False."""
        path = self._path_for(name)
        if not path.exists():
            return False
        try:
            data = self._read_yaml(path)
        except (yaml.YAMLError, ConfigError, OSError):
            # Giữ config cũ. Logger chưa chắc đã init nên không log ở đây;
            # caller (dashboard/health) quan sát qua return value + callback.
            return False
        with self._lock:
            self._data[name] = data
            callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(name, data)
            except Exception:
                # Callback lỗi không được làm chết reload của các callback khác
                continue
        return True

    # ---------- access ----------

    def get(self, name: str, path: str, default: Any = None) -> Any:
        """Đọc value bằng dotted path, vd get("system", "dashboard.port")."""
        with self._lock:
            node: Any = self._data.get(name)
        if node is None:
            return default
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, name: str, path: str) -> Any:
        """Như get() nhưng raise nếu thiếu — dùng cho value không có default hợp lý."""
        sentinel = object()
        value = self.get(name, path, sentinel)
        if value is sentinel:
            raise ConfigError(f"Thiếu config bắt buộc: {name}.{path}")
        return value

    def section(self, name: str) -> dict[str, Any]:
        """Trả về copy shallow của toàn bộ 1 config file."""
        with self._lock:
            return dict(self._data.get(name, {}))

    def loaded_names(self) -> list[str]:
        with self._lock:
            return sorted(self._data.keys())

    # ---------- hot-reload ----------

    def on_reload(self, callback: ReloadCallback) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def start_watching(self, debounce_ms: int | None = None) -> None:
        if self._observer is not None:
            return
        if debounce_ms is None:
            debounce_ms = int(self.get("system", "config_reload.debounce_ms", 500))
        self._handler = _ReloadHandler(self, debounce_ms)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(self._dir), recursive=False)
        self._observer.start()

    def stop_watching(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=5)
        self._observer = None
        if self._handler is not None:
            with self._handler._lock:
                for timer in self._handler._timers.values():
                    timer.cancel()
                self._handler._timers.clear()
            self._handler = None
