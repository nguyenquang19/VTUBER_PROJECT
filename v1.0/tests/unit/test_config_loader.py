"""Test ConfigLoader: load, dotted access, hot-reload (ARCHITECTURE Section 12).

Phase 0 DoD: "Config reload không cần restart".
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import yaml

from orchestrator.config_loader import ConfigError, ConfigLoader

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    (tmp_path / "system.yaml").write_text(
        yaml.safe_dump(
            {
                "app": {"name": "mai", "version": "1.0.0"},
                "dashboard": {"host": "127.0.0.1", "port": 7860},
                "config_reload": {"enabled": True, "debounce_ms": 50},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "models.yaml").write_text(
        yaml.safe_dump({"llm_main": {"port": 8080, "context_size": 4096}}),
        encoding="utf-8",
    )
    return tmp_path


class TestLoad:
    def test_load_all_reads_existing_files(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.loaded_names() == ["models", "system"]

    def test_missing_optional_file_is_skipped(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        # triggers.yaml chưa tạo (milestone 0.D) — không được raise
        assert "triggers" not in loader.loaded_names()

    def test_missing_required_file_raises(self, tmp_path: Path) -> None:
        loader = ConfigLoader(tmp_path, required=("system",))
        with pytest.raises(ConfigError, match="bắt buộc không tồn tại"):
            loader.load_all()

    def test_bad_yaml_raises(self, tmp_path: Path) -> None:
        (tmp_path / "system.yaml").write_text("key: [unclosed", encoding="utf-8")
        loader = ConfigLoader(tmp_path)
        with pytest.raises(ConfigError, match="YAML sai cú pháp"):
            loader.load_all()

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        (tmp_path / "system.yaml").write_text("- a\n- b\n", encoding="utf-8")
        loader = ConfigLoader(tmp_path)
        with pytest.raises(ConfigError, match="top-level phải là mapping"):
            loader.load_all()

    def test_empty_file_becomes_empty_dict(self, tmp_path: Path) -> None:
        (tmp_path / "system.yaml").write_text("", encoding="utf-8")
        loader = ConfigLoader(tmp_path)
        loader.load_all()
        assert loader.section("system") == {}


class TestAccess:
    def test_dotted_path(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.get("system", "dashboard.port") == 7860
        assert loader.get("system", "app.name") == "mai"
        assert loader.get("models", "llm_main.context_size") == 4096

    def test_missing_key_returns_default(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.get("system", "nope.nope", "fallback") == "fallback"
        assert loader.get("system", "dashboard.nope") is None

    def test_missing_config_name_returns_default(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.get("triggers", "anything", 42) == 42

    def test_descend_into_non_dict_returns_default(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        # app.name là str, không descend được tiếp
        assert loader.get("system", "app.name.deeper", "d") == "d"

    def test_require_raises_when_missing(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.require("system", "dashboard.port") == 7860
        with pytest.raises(ConfigError, match="Thiếu config bắt buộc"):
            loader.require("system", "dashboard.missing")

    def test_section_returns_copy(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        sec = loader.section("system")
        sec["app"] = "mutated"
        assert loader.get("system", "app.name") == "mai"

    def test_unknown_config_name_in_reload_raises(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        with pytest.raises(ConfigError, match="Unknown config name"):
            loader.reload_file("not_a_config")


class TestReload:
    def test_reload_file_picks_up_change(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.get("system", "dashboard.port") == 7860

        (tmp_config_dir / "system.yaml").write_text(
            yaml.safe_dump({"dashboard": {"port": 9999}}), encoding="utf-8"
        )
        assert loader.reload_file("system") is True
        assert loader.get("system", "dashboard.port") == 9999

    def test_reload_keeps_old_config_on_bad_yaml(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()

        (tmp_config_dir / "system.yaml").write_text("bad: [unclosed", encoding="utf-8")
        assert loader.reload_file("system") is False
        # config cũ vẫn nguyên — atomic
        assert loader.get("system", "dashboard.port") == 7860

    def test_reload_missing_file_returns_false(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        assert loader.reload_file("triggers") is False

    def test_callback_fires_on_reload(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        seen: list[tuple[str, int]] = []
        loader.on_reload(lambda name, data: seen.append((name, data["dashboard"]["port"])))

        (tmp_config_dir / "system.yaml").write_text(
            yaml.safe_dump({"dashboard": {"port": 1234}}), encoding="utf-8"
        )
        loader.reload_file("system")
        assert seen == [("system", 1234)]

    def test_failing_callback_does_not_block_others(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        calls: list[str] = []

        def boom(name, data):
            raise RuntimeError("callback exploded")

        loader.on_reload(boom)
        loader.on_reload(lambda name, data: calls.append(name))

        (tmp_config_dir / "system.yaml").write_text(
            yaml.safe_dump({"dashboard": {"port": 5}}), encoding="utf-8"
        )
        assert loader.reload_file("system") is True
        assert calls == ["system"]

    def test_name_for_path_maps_known_files(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        assert loader.name_for_path(Path("whatever/system.yaml")) == "system"
        assert loader.name_for_path(Path("whatever/state_machine.yaml")) == "state_machine"
        assert loader.name_for_path(Path("whatever/random.yaml")) is None


class TestWatching:
    def test_watchdog_triggers_reload(self, tmp_config_dir: Path) -> None:
        """DoD: config reload không cần restart — file change trên disk tự apply."""
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()

        reloaded = threading.Event()
        loader.on_reload(lambda name, data: reloaded.set())
        loader.start_watching(debounce_ms=50)
        try:
            time.sleep(0.2)  # để observer sẵn sàng
            (tmp_config_dir / "system.yaml").write_text(
                yaml.safe_dump({"dashboard": {"port": 4321}}), encoding="utf-8"
            )
            assert reloaded.wait(timeout=10), "watchdog không trigger reload trong 10s"
            assert loader.get("system", "dashboard.port") == 4321
        finally:
            loader.stop_watching()

    def test_start_watching_twice_is_noop(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.load_all()
        loader.start_watching(debounce_ms=50)
        try:
            loader.start_watching(debounce_ms=50)  # không được raise
        finally:
            loader.stop_watching()

    def test_stop_watching_without_start_is_noop(self, tmp_config_dir: Path) -> None:
        loader = ConfigLoader(tmp_config_dir)
        loader.stop_watching()


class TestRealConfigFiles:
    """Config thật trong repo phải load được và có các key Phase 0 cần."""

    def test_real_config_loads(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        assert "system" in loader.loaded_names()
        assert "models" in loader.loaded_names()
        assert "logging" in loader.loaded_names()
        assert "data_privacy" in loader.loaded_names()
        assert "features" in loader.loaded_names()
        assert "agent_goals" in loader.loaded_names()

    def test_real_config_has_phase0_keys(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        assert loader.require("system", "dashboard.port") == 7860
        assert loader.require("system", "emergency_stop.hotkey") == "ctrl+shift+x"
        assert loader.require("system", "conversation.cooldown_ms") == 500
        # ambient talk: threshold cứng 60s, KHÔNG probability (N1 / spec 7.9.2)
        assert loader.require("system", "ambient_talk.min_silence_seconds") == 60

    def test_models_config_matches_preflight(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        # Stack chốt: llama.cpp, context 4096 (CLAUDE.md Section 3)
        assert loader.require("models", "llm_main.provider") == "llama_cpp"
        assert loader.require("models", "llm_main.context_size") == 4096
        assert loader.require("models", "llm_main.port") == 8080
        # TTS chuyển sang VieNeu-TTS v3 Turbo (spike day_vieneu, 2026-08)
        assert loader.require("models", "tts.provider") == "vieneu"
        assert loader.require("models", "tts.params.style") == "tu_nhien"
        # STT deferred theo scope decision
        assert loader.require("models", "stt.enabled") is False
