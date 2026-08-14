from __future__ import annotations

import pytest

from scripts import dashboard_standalone


@pytest.mark.asyncio
async def test_delayed_browser_uses_windows_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(dashboard_standalone, "_open_browser", opened.append)

    await dashboard_standalone._delayed_browser("http://127.0.0.1:7860", 0.0)

    assert opened == ["http://127.0.0.1:7860"]


def test_dashboard_available_checks_snapshot_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    seen: list[tuple[str, float]] = []
    monkeypatch.setattr(dashboard_standalone, "urlopen", lambda url, timeout: (
        seen.append((url, timeout)) or Response()
    ))

    assert dashboard_standalone._dashboard_available("http://127.0.0.1:7861", 0.5)
    assert seen == [("http://127.0.0.1:7861/api/snapshot", 0.5)]
