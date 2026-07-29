"""Poll nvidia-smi để phát hiện GPU throttle trong khi benchmark.

Throttle = clock hiện tại < 90% max clock. Report tỉ lệ mẫu bị throttle.
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field


@dataclass
class GpuSample:
    t_sec: float
    temp_c: int
    clock_mhz: int
    max_clock_mhz: int


class GpuMonitor:
    def __init__(self, poll_interval: float = 10.0, throttle_threshold: float = 0.9) -> None:
        self.poll_interval = poll_interval
        self.throttle_threshold = throttle_threshold
        self._samples: list[GpuSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def _query(self) -> tuple[int, int, int] | None:
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=temperature.gpu,clocks.gr,clocks.max.gr",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                timeout=5,
            )
            parts = out.strip().split(",")
            return int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
        except Exception:
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            sample = self._query()
            if sample:
                temp, clock, max_clock = sample
                self._samples.append(
                    GpuSample(time.perf_counter() - self._t0, temp, clock, max_clock)
                )
            self._stop.wait(self.poll_interval)

    def start(self) -> None:
        self._t0 = time.perf_counter()
        self._samples.clear()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if not self._samples:
            return {"num_samples": 0}
        temps = [s.temp_c for s in self._samples]
        throttled = sum(
            1 for s in self._samples
            if s.clock_mhz < s.max_clock_mhz * self.throttle_threshold
        )
        return {
            "num_samples": len(self._samples),
            "max_temp": max(temps),
            "avg_temp": sum(temps) / len(temps),
            "throttle_ratio": throttled / len(self._samples),
        }
