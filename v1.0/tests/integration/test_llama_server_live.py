"""Live test: start/stop llama-server thật (ARCHITECTURE 8.2, 1.A DoD).

Marker `llm`: cần binary + model thật (~10s load 7.5GB). Skip bằng:
    pytest -m "not llm"
Chạy riêng:
    pytest -m llm
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.llm.process_manager import LlamaServerConfig, LlamaServerProcessManager

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.llm


@pytest.fixture
def config() -> LlamaServerConfig:
    loader = ConfigLoader(REPO_ROOT / "config")
    loader.load_all()
    return LlamaServerConfig.from_loader(loader)


async def test_start_wait_healthy_stop(config: LlamaServerConfig) -> None:
    mgr = LlamaServerProcessManager(config)
    try:
        await mgr.start()
        assert mgr.is_running() or await mgr.is_healthy()  # managed hoặc đã có sẵn
        assert await mgr.is_healthy() is True
    finally:
        await mgr.stop()
    # sau stop: nếu ta quản process thì nó phải tắt
    assert mgr.process is None
