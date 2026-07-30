"""llama-server process lifecycle (ARCHITECTURE 8.2, Phase 1 task 1).

v2.3: 1 instance duy nhất (main, port 8080) — bỏ E4B.

Windows (CLAUDE.md 2): không SIGTERM POSIX. `proc.terminate()` =
TerminateProcess = hard-kill. Chấp nhận cho llama-server (không giữ state
quan trọng giữa request — 13.3).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from orchestrator.logger import get_logger


@dataclass
class LlamaServerConfig:
    binary: str
    model_path: str
    host: str = "127.0.0.1"
    port: int = 8080
    context_size: int = 4096
    gpu_layers: int = 999
    kv_cache_type_k: str = "q8_0"
    kv_cache_type_v: str = "q8_0"
    batch_size: int = 512
    num_predict: int = 300
    prompt_cache_path: str | None = None
    health_timeout_s: int = 30
    extra_flags: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def to_cli_args(self) -> list[str]:
        args = [
            self.binary,
            "-m", self.model_path,
            "--host", self.host,
            "--port", str(self.port),
            "-c", str(self.context_size),
            "-ngl", str(self.gpu_layers),
            "-ctk", self.kv_cache_type_k,
            "-ctv", self.kv_cache_type_v,
            "-b", str(self.batch_size),
        ]
        # LƯU Ý: KHÔNG dùng --prompt-cache ở đây. Đó là flag của llama-cli,
        # KHÔNG phải llama-server (server báo "invalid argument"). Spec 10.3 ghi
        # nhầm. Prompt caching của server hoạt động qua `cache_prompt: true` trong
        # request body (slot KV cache giữ prefix persona giữa các turn) — xem
        # llama_cpp_llm.py (1.B). prompt_cache_path giữ trong config chỉ để tham
        # khảo, không truyền làm CLI flag.
        args += list(self.extra_flags)
        return args

    @classmethod
    def from_loader(cls, loader) -> LlamaServerConfig:
        g = lambda k, d=None: loader.get("models", f"llm_main.{k}", d)  # noqa: E731
        return cls(
            binary=g("binary"),
            model_path=g("model_path"),
            host=g("host", "127.0.0.1"),
            port=int(g("port", 8080)),
            context_size=int(g("context_size", 4096)),
            gpu_layers=int(g("gpu_layers", 999)),
            kv_cache_type_k=g("kv_cache_type_k", "q8_0"),
            kv_cache_type_v=g("kv_cache_type_v", "q8_0"),
            batch_size=int(g("batch_size", 512)),
            num_predict=int(g("num_predict", 300)),
            prompt_cache_path=g("prompt_cache_path"),
            health_timeout_s=int(g("health_timeout_s", 30)),
            extra_flags=list(g("extra_flags", []) or []),
        )


class LlamaServerError(Exception):
    pass


class LlamaServerProcessManager:
    """Spawn + wait healthy + hard-kill 1 llama-server instance."""

    def __init__(self, config: LlamaServerConfig, poll_interval_s: float = 0.5) -> None:
        self.config = config
        self.poll_interval_s = poll_interval_s
        self.process: subprocess.Popen | None = None
        self._log = get_logger("llama_process")

    # ---------- checks ----------

    def _preflight(self) -> None:
        if not Path(self.config.binary).exists():
            raise LlamaServerError(f"llama-server không tồn tại: {self.config.binary}")
        if not Path(self.config.model_path).exists():
            raise LlamaServerError(f"model không tồn tại: {self.config.model_path}")

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{self.config.base_url}/health", timeout=3)
                return r.status_code == 200
        except Exception:
            return False

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Spawn process, chờ /health OK trong health_timeout_s. Không managed
        nếu đã có server chạy sẵn ở port đó (dev tự start tay)."""
        if await self.is_healthy():
            self._log.info("llama_server_already_up", port=self.config.port, managed=False)
            return

        self._preflight()
        args = self.config.to_cli_args()
        self._log.info("llama_server_starting", port=self.config.port, binary=self.config.binary)
        # CREATE_NO_WINDOW để không bật cửa sổ console riêng trên Windows
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        await self._wait_healthy()
        self._log.info("llama_server_started", port=self.config.port, pid=self.process.pid)

    async def _wait_healthy(self) -> None:
        import asyncio

        deadline = asyncio.get_event_loop().time() + self.config.health_timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise LlamaServerError(
                    f"llama-server thoát sớm (exit={self.process.returncode}) — "
                    "xem lại flags/model/VRAM"
                )
            if await self.is_healthy():
                return
            await asyncio.sleep(self.poll_interval_s)
        await self.stop()
        raise LlamaServerError(
            f"llama-server không healthy sau {self.config.health_timeout_s}s"
        )

    async def stop(self) -> None:
        """Hard-kill (Windows terminate = TerminateProcess). Không quản process
        do dev start tay (process is None)."""
        if self.process is None:
            return
        if self.process.poll() is None:
            self._log.info("llama_server_stopping", pid=self.process.pid)
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()
