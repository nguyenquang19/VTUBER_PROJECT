"""Poll nvidia-smi để phát hiện GPU throttle trong khi benchmark.

Dùng `throttle_reasons.sw_thermal_slowdown` / `hw_thermal_slowdown` của nvidia-smi
để phân biệt thermal throttle thật vs power-save idle (clock giảm khi rảnh).
"""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class GpuSample:
    t_sec: float
    temp_c: int
    clock_mhz: int
    max_clock_mhz: int
    gpu_idle: bool
    sw_thermal: bool
    hw_thermal: bool
    hw_slowdown: bool


class GpuMonitor:
    """Poll nvidia-smi periodically. Tính:
    - max_temp / avg_temp
    - thermal_throttle_ratio (chỉ tính thermal throttle thật)
    - clock_throttle_active_ratio (throttle clock lúc GPU đang active, loại idle)
    """

    QUERY = (
        "temperature.gpu,clocks.gr,clocks.max.gr,"
        "clocks_throttle_reasons.gpu_idle,"
        "clocks_throttle_reasons.sw_thermal_slowdown,"
        "clocks_throttle_reasons.hw_thermal_slowdown,"
        "clocks_throttle_reasons.hw_slowdown"
    )

    def __init__(self, poll_interval: float = 10.0, throttle_threshold: float = 0.9) -> None:
        self.poll_interval = poll_interval
        self.throttle_threshold = throttle_threshold
        self._samples: list[GpuSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0

    def _query(self) -> GpuSample | None:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                text=True,
                timeout=5,
            )
            parts = [p.strip() for p in out.strip().split(",")]
            if len(parts) < 7:
                return None

            def _flag(v: str) -> bool:
                return v.lower() in {"active", "1"}

            return GpuSample(
                t_sec=time.perf_counter() - self._t0,
                temp_c=int(parts[0]),
                clock_mhz=int(parts[1]),
                max_clock_mhz=int(parts[2]),
                gpu_idle=_flag(parts[3]),
                sw_thermal=_flag(parts[4]),
                hw_thermal=_flag(parts[5]),
                hw_slowdown=_flag(parts[6]),
            )
        except Exception:
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            s = self._query()
            if s:
                self._samples.append(s)
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
        active = [s for s in self._samples if not s.gpu_idle]
        thermal = [s for s in self._samples if s.sw_thermal or s.hw_thermal]
        hw_slow = [s for s in self._samples if s.hw_slowdown]
        clock_throttle_active = [
            s for s in active
            if s.clock_mhz < s.max_clock_mhz * self.throttle_threshold
        ]

        return {
            "num_samples": len(self._samples),
            "num_active_samples": len(active),
            "max_temp": max(temps),
            "avg_temp": sum(temps) / len(temps),
            # Chính xác nhất: thermal throttle từ nvidia-smi
            "thermal_throttle_ratio": len(thermal) / len(self._samples),
            "hw_slowdown_ratio": len(hw_slow) / len(self._samples),
            # Clock throttle nhưng chỉ tính lúc GPU active (loại idle)
            "clock_throttle_active_ratio": (
                len(clock_throttle_active) / len(active) if active else 0.0
            ),
        }
