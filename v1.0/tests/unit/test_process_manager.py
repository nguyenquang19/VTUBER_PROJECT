"""Test LlamaServerProcessManager config/args (ARCHITECTURE 8.2).

Test build args + preflight (không cần server thật). Live start/stop ở
tests/integration/test_llama_server_live.py (marker llm)."""
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_loader import ConfigLoader
from services.llm.process_manager import (
    LlamaServerConfig,
    LlamaServerError,
    LlamaServerProcessManager,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_config(**kw) -> LlamaServerConfig:
    defaults = dict(binary="fake.exe", model_path="fake.gguf")
    defaults.update(kw)
    return LlamaServerConfig(**defaults)


class TestCliArgs:
    def test_core_flags_present(self) -> None:
        args = make_config(port=8080, context_size=4096, gpu_layers=999).to_cli_args()
        assert args[0] == "fake.exe"
        assert "-m" in args and "fake.gguf" in args
        assert "--port" in args and "8080" in args
        assert "-c" in args and "4096" in args
        assert "-ngl" in args and "999" in args

    def test_kv_cache_flags(self) -> None:
        args = make_config(kv_cache_type_k="q8_0", kv_cache_type_v="q8_0").to_cli_args()
        assert "-ctk" in args
        assert "-ctv" in args
        i = args.index("-ctk")
        assert args[i + 1] == "q8_0"

    def test_prompt_cache_never_a_cli_flag(self) -> None:
        # --prompt-cache là flag llama-cli, KHÔNG phải llama-server (spec 10.3 nhầm).
        # Prompt caching qua cache_prompt request param, không qua CLI.
        args = make_config(prompt_cache_path="cache/p.bin").to_cli_args()
        assert "--prompt-cache" not in args

    def test_extra_flags_appended(self) -> None:
        args = make_config(extra_flags=["--flash-attn", "on"]).to_cli_args()
        assert args[-2:] == ["--flash-attn", "on"]

    def test_base_url(self) -> None:
        assert make_config(host="127.0.0.1", port=8080).base_url == "http://127.0.0.1:8080"


class TestFromLoader:
    def test_reads_real_config(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        cfg = LlamaServerConfig.from_loader(loader)
        assert cfg.port == 8080
        assert cfg.context_size == 4096
        assert cfg.kv_cache_type_k == "q8_0"
        assert cfg.model_path.endswith(".gguf")

    def test_real_config_binary_and_model_exist(self) -> None:
        """Path trong config phải trỏ tới file thật trên máy (1.A DoD)."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        cfg = LlamaServerConfig.from_loader(loader)
        assert Path(cfg.binary).exists(), f"llama-server không tồn tại: {cfg.binary}"
        assert Path(cfg.model_path).exists(), f"model không tồn tại: {cfg.model_path}"


class TestPreflight:
    def test_missing_binary_raises(self) -> None:
        mgr = LlamaServerProcessManager(make_config(binary="nope.exe", model_path=__file__))
        with pytest.raises(LlamaServerError, match="llama-server không tồn tại"):
            mgr._preflight()

    def test_missing_model_raises(self) -> None:
        mgr = LlamaServerProcessManager(make_config(binary=__file__, model_path="nope.gguf"))
        with pytest.raises(LlamaServerError, match="model không tồn tại"):
            mgr._preflight()

    def test_preflight_passes_when_files_exist(self) -> None:
        mgr = LlamaServerProcessManager(make_config(binary=__file__, model_path=__file__))
        mgr._preflight()  # không raise


class TestState:
    def test_not_running_initially(self) -> None:
        mgr = LlamaServerProcessManager(make_config())
        assert mgr.is_running() is False

    async def test_is_healthy_false_when_no_server(self) -> None:
        mgr = LlamaServerProcessManager(make_config(port=59999))
        assert await mgr.is_healthy() is False

    async def test_stop_when_not_started_is_safe(self) -> None:
        mgr = LlamaServerProcessManager(make_config())
        await mgr.stop()  # không raise
