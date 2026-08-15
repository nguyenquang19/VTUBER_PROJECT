"""Test EmergencyStop (Phase 0 task 12).

DoD: Emergency stop → PAUSED từ mọi state. Hotkey thật (keyboard lib) không
test được trong CI/không admin — test qua trigger programmatic (nguồn dashboard).
"""
from __future__ import annotations

import pytest

from orchestrator.config_loader import ConfigLoader
from orchestrator.emergency_stop import EmergencyStop
from orchestrator.state_machine import ConversationState, ConversationStateMachine
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


async def fire(sm, trigger) -> None:
    from transitions.core import MachineError
    try:
        await getattr(sm, trigger)()
    except (MachineError, AttributeError):
        pass


class TestTrigger:
    async def test_trigger_calls_callback(self) -> None:
        calls: list[int] = []

        async def cb() -> None:
            calls.append(1)

        es = EmergencyStop(callback=cb)
        await es.trigger()
        assert calls == [1]
        assert es.trigger_count == 1

    async def test_trigger_moves_state_machine_to_paused(self) -> None:
        sm = ConversationStateMachine(auto_cooldown=False)
        es = EmergencyStop(callback=sm.emergency_stop)
        await fire(sm, "trigger_received")  # THINKING
        await es.trigger()
        assert sm.current_state is ConversationState.PAUSED

    @pytest.mark.parametrize(
        "path",
        [[], ["trigger_received"], ["trigger_received", "first_token"],
         ["trigger_received", "llm_fail"]],
    )
    async def test_paused_from_every_state(self, path) -> None:
        sm = ConversationStateMachine(auto_cooldown=False)
        es = EmergencyStop(callback=sm.emergency_stop)
        for t in path:
            await fire(sm, t)
        await es.trigger()
        assert sm.current_state is ConversationState.PAUSED


class TestBind:
    def test_bind_failure_is_graceful(self, monkeypatch) -> None:
        """Không admin / lib lỗi → bind False, không raise (degrade)."""
        async def cb() -> None:
            pass

        es = EmergencyStop(callback=cb)

        # ép import keyboard fail
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "keyboard":
                raise RuntimeError("no admin / no display")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert es.bind() is False
        assert es.is_bound is False

    def test_bind_success_path(self, monkeypatch) -> None:
        async def cb() -> None:
            pass

        es = EmergencyStop(callback=cb, hotkey="ctrl+shift+x")

        class FakeKeyboard:
            def __init__(self):
                self.hotkeys = []
            def add_hotkey(self, hk, fn):
                self.hotkeys.append(hk)
            def remove_hotkey(self, hk):
                self.hotkeys.remove(hk)

        fake = FakeKeyboard()
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "keyboard":
                return fake
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert es.bind() is True
        assert es.is_bound is True
        assert "ctrl+shift+x" in fake.hotkeys
        es.unbind()
        assert es.is_bound is False

    def test_bind_twice_idempotent(self, monkeypatch) -> None:
        async def cb() -> None:
            pass
        es = EmergencyStop(callback=cb)

        class FakeKeyboard:
            def add_hotkey(self, hk, fn): pass
            def remove_hotkey(self, hk): pass

        import builtins
        real_import = builtins.__import__
        monkeypatch.setattr(
            builtins, "__import__",
            lambda name, *a, **k: FakeKeyboard() if name == "keyboard" else real_import(name, *a, **k),
        )
        assert es.bind() is True
        assert es.bind() is True  # không lỗi


class TestFromConfig:
    def test_reads_hotkey_from_config(self) -> None:
        async def cb() -> None:
            pass
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        es = EmergencyStop.from_config(loader, callback=cb)
        assert es._hotkey == "ctrl+shift+x"
