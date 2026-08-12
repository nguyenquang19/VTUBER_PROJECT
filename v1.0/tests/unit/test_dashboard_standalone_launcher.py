from __future__ import annotations

import pytest

from scripts import dashboard_standalone


@pytest.mark.asyncio
async def test_delayed_browser_uses_windows_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(dashboard_standalone, "_open_browser", opened.append)

    await dashboard_standalone._delayed_browser("http://127.0.0.1:7860", 0.0)

    assert opened == ["http://127.0.0.1:7860"]
