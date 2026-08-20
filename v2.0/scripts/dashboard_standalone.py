"""Start Mai dashboard without StreamRuntime and open it in the Windows browser."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.dashboard_server import DashboardServer  # noqa: E402
from orchestrator.config_loader import ConfigLoader  # noqa: E402
from orchestrator.credential_contract import require_dashboard_control_token  # noqa: E402
from orchestrator.logger import setup_from_config  # noqa: E402
from services.operations.dashboard_data_source import DashboardDataSource  # noqa: E402


def _open_browser(url: str) -> None:
    if os.name != "nt":
        raise RuntimeError("standalone dashboard launcher supports Windows only")
    os.startfile(url)  # type: ignore[attr-defined]


async def _delayed_browser(url: str, delay_s: float) -> None:
    await asyncio.sleep(max(0.0, delay_s))
    await asyncio.to_thread(_open_browser, url)


def _dashboard_available(url: str, timeout_s: float) -> bool:
    try:
        with urlopen(f"{url}/api/snapshot", timeout=timeout_s) as response:
            return int(response.status) == 200
    except (OSError, URLError, ValueError):
        return False


async def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    loader = ConfigLoader(Path("config"))
    loader.load_all()
    setup_from_config(loader)
    host = args.host or str(loader.get(
        "operations", "dashboard_standalone.host", "127.0.0.1",
    ))
    port = args.port or int(loader.get(
        "operations", "dashboard_standalone.port", 7861,
    ))
    provider = DashboardDataSource.from_loader(loader)
    server = DashboardServer(
        snapshot_provider=provider,
        data_dir=loader.get("logging", "jsonl.dir", "logs"),
        host=host,
        port=port,
        push_interval_s=float(loader.get("system", "dashboard.push_interval_s", 1.0)),
        control_token=require_dashboard_control_token(loader),
    )
    await provider.start()
    url = f"http://{host}:{port}"
    browser_task = None
    should_open = bool(loader.get(
        "operations", "dashboard_standalone.open_browser", True,
    )) and not args.no_browser
    request_timeout_s = float(loader.get(
        "operations", "dashboard_standalone.request_timeout_s", 0.75,
    ))
    if await asyncio.to_thread(_dashboard_available, url, request_timeout_s):
        if should_open:
            await asyncio.to_thread(_open_browser, url)
        await provider.stop()
        return 0
    if should_open:
        browser_task = asyncio.create_task(_delayed_browser(
            url,
            float(loader.get(
                "operations", "dashboard_standalone.browser_delay_s", 0.8,
            )),
        ))
    try:
        await server.serve()
        return 0
    finally:
        if browser_task is not None and not browser_task.done():
            browser_task.cancel()
        await server.shutdown()
        await provider.stop()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
