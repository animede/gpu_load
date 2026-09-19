#!/usr/bin/env python3
"""GPU Pulse: a dependency-free Linux GPU monitoring web server."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
NVIDIA_FIELDS = (
    "index,name,uuid,utilization.gpu,utilization.memory,memory.used,"
    "memory.total,temperature.gpu,power.draw,power.limit,"
    "clocks.current.graphics,fan.speed"
)


def _number(value: str, *, integer: bool = False) -> float | int | None:
    """Convert nvidia-smi/sysfs output to a finite number or None."""
    value = value.strip()
    if not value or value.lower() in {"n/a", "[n/a]", "not supported"}:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if integer else round(number, 2)


def _percent(part: float | int | None, total: float | int | None) -> float | None:
    if part is None or total in (None, 0):
        return None
    return round(min(100.0, max(0.0, float(part) / float(total) * 100)), 2)


def read_nvidia() -> list[dict[str, Any]]:
    """Read all NVIDIA GPUs with one nvidia-smi process."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={NVIDIA_FIELDS}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    devices: list[dict[str, Any]] = []
    for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
        if len(row) != 12:
            continue
        memory_used = _number(row[5], integer=True)
        memory_total = _number(row[6], integer=True)
        devices.append(
            {
                "id": row[2].strip(),
                "index": _number(row[0], integer=True),
                "name": row[1].strip(),
                "vendor": "NVIDIA",
                "backend": "nvidia-smi",
                "utilization": _number(row[3]),
                "memoryUtilization": _number(row[4]),
                "memoryUsedMiB": memory_used,
                "memoryTotalMiB": memory_total,
                "memoryPercent": _percent(memory_used, memory_total),
                "temperatureC": _number(row[7]),
                "powerW": _number(row[8]),
                "powerLimitW": _number(row[9]),
                "clockMHz": _number(row[10]),
                "fanPercent": _number(row[11]),
            }
        )
    return devices


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_scaled(path: Path, scale: float = 1.0) -> float | None:
    raw = _read_text(path)
    value = _number(raw or "")
    return round(float(value) / scale, 2) if value is not None else None


def _first_value(paths: list[Path], scale: float = 1.0) -> float | None:
    for path in paths:
        value = _read_scaled(path, scale)
        if value is not None:
            return value
    return None


def _pci_name(device: Path, fallback: str) -> str:
    """Get a human-readable PCI device name without making it a dependency."""
    slot = device.resolve().name
    try:
        result = subprocess.run(
            ["lspci", "-s", slot],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        if result.returncode == 0 and ": " in result.stdout:
            return result.stdout.strip().split(": ", 1)[1]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return fallback


def read_sysfs(*, skip_nvidia: bool = False) -> list[dict[str, Any]]:
    """Read DRM sysfs counters used by AMD, Intel and some other drivers."""
    vendor_names = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
    devices: list[dict[str, Any]] = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        vendor_id = (_read_text(device / "vendor") or "").lower()
        if not vendor_id:
            continue
        vendor = vendor_names.get(vendor_id, vendor_id)
        if skip_nvidia and vendor == "NVIDIA":
            continue

        total_bytes = _read_scaled(device / "mem_info_vram_total")
        used_bytes = _read_scaled(device / "mem_info_vram_used")
        hwmon_dirs = list((device / "hwmon").glob("hwmon*"))
        temp = _first_value([p / "temp1_input" for p in hwmon_dirs], 1000)
        power = _first_value(
            [p / "power1_average" for p in hwmon_dirs]
            + [p / "power1_input" for p in hwmon_dirs],
            1_000_000,
        )
        fan_raw = _first_value([p / "fan1_input" for p in hwmon_dirs])
        fallback_name = f"{vendor} GPU ({card.name})"
        devices.append(
            {
                "id": f"sysfs-{card.name}",
                "index": int(card.name[4:]),
                "name": _pci_name(device, fallback_name),
                "vendor": vendor,
                "backend": "sysfs",
                "utilization": _read_scaled(device / "gpu_busy_percent"),
                "memoryUtilization": _read_scaled(device / "mem_busy_percent"),
                "memoryUsedMiB": round(used_bytes / 1_048_576) if used_bytes is not None else None,
                "memoryTotalMiB": round(total_bytes / 1_048_576) if total_bytes is not None else None,
                "memoryPercent": _percent(used_bytes, total_bytes),
                "temperatureC": temp,
                "powerW": power,
                "powerLimitW": None,
                "clockMHz": _read_scaled(device / "gt_cur_freq_mhz"),
                "fanPercent": None,
                "fanRpm": fan_raw,
            }
        )
    return devices


def demo_devices(now: float | None = None) -> list[dict[str, Any]]:
    """Generate stable-looking demo metrics for UI development."""
    now = now if now is not None else time.time()
    wave = (math.sin(now / 4) + 1) / 2
    utilization = round(18 + wave * 68 + random.random() * 5, 1)
    memory_used = round(4820 + wave * 4950)
    second_wave = (math.sin(now / 5 + 2.2) + 1) / 2
    second_utilization = round(8 + second_wave * 78 + random.random() * 4, 1)
    second_memory_used = round(3160 + second_wave * 7100)
    return [
        {
            "id": "demo-gpu-0",
            "index": 0,
            "name": "Demo Graphics 9000",
            "vendor": "Demo",
            "backend": "demo",
            "utilization": min(100, utilization),
            "memoryUtilization": round(12 + wave * 54, 1),
            "memoryUsedMiB": memory_used,
            "memoryTotalMiB": 16384,
            "memoryPercent": _percent(memory_used, 16384),
            "temperatureC": round(41 + wave * 25, 1),
            "powerW": round(52 + wave * 170, 1),
            "powerLimitW": 250,
            "clockMHz": round(510 + wave * 1960),
            "fanPercent": round(24 + wave * 48),
        },
        {
            "id": "demo-gpu-1",
            "index": 1,
            "name": "Demo Compute 8000",
            "vendor": "Demo",
            "backend": "demo",
            "utilization": min(100, second_utilization),
            "memoryUtilization": round(9 + second_wave * 61, 1),
            "memoryUsedMiB": second_memory_used,
            "memoryTotalMiB": 12288,
            "memoryPercent": _percent(second_memory_used, 12288),
            "temperatureC": round(38 + second_wave * 31, 1),
            "powerW": round(35 + second_wave * 185, 1),
            "powerLimitW": 230,
            "clockMHz": round(420 + second_wave * 2110),
            "fanPercent": round(20 + second_wave * 57),
        },
    ]


class GPUMonitor:
    def __init__(self, demo: bool = False) -> None:
        self.demo = demo

    def snapshot(self) -> dict[str, Any]:
        if self.demo:
            devices = demo_devices()
        else:
            nvidia = read_nvidia()
            devices = nvidia + read_sysfs(skip_nvidia=bool(nvidia))
        return {
            "timestamp": int(time.time() * 1000),
            "hostname": os.uname().nodename,
            "platform": "Linux",
            "demo": self.demo,
            "gpus": devices,
        }


class MonitorHandler(SimpleHTTPRequestHandler):
    monitor: GPUMonitor

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        path = urlparse(self.path).path
        if path == "/api/gpu":
            try:
                payload = self.monitor.snapshot()
                self._json(payload)
            except Exception as exc:  # keep the dashboard alive on driver errors
                self._json({"error": str(exc), "gpus": []}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if path == "/api/health":
            self._json({"status": "ok"})
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def end_headers(self) -> None:
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api/gpu"):
            return
        super().log_message(fmt, *args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Linux GPU real-time dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    parser.add_argument("--demo", action="store_true", help="use generated metrics")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    MonitorHandler.monitor = GPUMonitor(demo=args.demo or os.getenv("GPU_PULSE_DEMO") == "1")
    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    print(f"GPU Pulse: http://{args.host}:{args.port}")
    print("終了するには Ctrl+C を押してください。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
