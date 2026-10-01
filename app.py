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
NVIDIA_PROCESS_FIELDS = "gpu_uuid,pid,process_name,used_gpu_memory"
MIN_PROCESS_MEMORY_MIB = 64
MAX_PROCESSES_PER_GPU = 5
IGNORED_GPU_PROCESSES = {
    "xorg",
    "xwayland",
    "gnome-shell",
    "kwin_wayland",
    "kwin_x11",
    "plasmashell",
    "mutter",
    "weston",
    "sway",
    "wayfire",
    "hyprland",
    "cinnamon",
    "compiz",
    "picom",
    "compton",
}


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


def _process_basename(name: str) -> str:
    """Return a compact executable name suitable for the dashboard."""
    normalized = name.strip().replace("\\", "/")
    return normalized.rsplit("/", 1)[-1] or "unknown"


def _is_ignored_gpu_process(name: str) -> bool:
    """Hide desktop compositors and display servers from the workload list."""
    return _process_basename(name).lower().removesuffix(".exe") in IGNORED_GPU_PROCESSES


def _process_display_name(pid: int, process_name: str) -> str:
    """Make Python GPU jobs distinguishable without exposing full command lines."""
    executable = _process_basename(process_name)
    if not executable.lower().startswith(("python", "pypy")):
        return executable
    try:
        raw_args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        args = [arg.decode("utf-8", errors="replace") for arg in raw_args if arg]
    except OSError:
        return executable
    if len(args) < 2:
        return executable

    detail: list[str] = []
    if args[1] == "-m" and len(args) > 2:
        detail.append(args[2])
        if len(args) > 3 and not args[3].startswith("-"):
            detail.append(_process_basename(args[3]))
    elif not args[1].startswith("-"):
        detail.append(_process_basename(args[1]))
        if (
            len(args) > 2
            and detail[0] in {"uvicorn", "gunicorn", "torchrun"}
            and not args[2].startswith("-")
        ):
            detail.append(_process_basename(args[2]))
    return f"{executable} · {' '.join(detail)}" if detail else executable


def read_nvidia_processes() -> dict[str, list[dict[str, Any]]]:
    """Read the largest NVIDIA compute processes, grouped by GPU UUID."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--query-compute-apps={NVIDIA_PROCESS_FIELDS}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}

    processes: dict[str, list[dict[str, Any]]] = {}
    for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
        if len(row) != 4:
            continue
        gpu_id = row[0].strip()
        pid = _number(row[1], integer=True)
        memory_mib = _number(row[3], integer=True)
        if (
            not gpu_id
            or pid is None
            or memory_mib is None
            or memory_mib < MIN_PROCESS_MEMORY_MIB
            or _is_ignored_gpu_process(row[2])
        ):
            continue
        processes.setdefault(gpu_id, []).append(
            {
                "pid": pid,
                "name": _process_display_name(pid, row[2]),
                "memoryUsedMiB": memory_mib,
            }
        )

    for gpu_processes in processes.values():
        gpu_processes.sort(key=lambda process: process["memoryUsedMiB"], reverse=True)
        del gpu_processes[MAX_PROCESSES_PER_GPU:]
    return processes


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
            "processesSupported": True,
            "processes": [
                {"pid": 18420, "name": "python3", "memoryUsedMiB": 6240},
                {"pid": 9187, "name": "blender", "memoryUsedMiB": 2310},
            ],
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
            "processesSupported": True,
            "processes": [
                {"pid": 22731, "name": "ollama", "memoryUsedMiB": 7920},
            ],
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
            processes_by_gpu = read_nvidia_processes() if nvidia else {}
            for device in nvidia:
                device["processesSupported"] = True
                device["processes"] = processes_by_gpu.get(device["id"], [])
            devices = nvidia + read_sysfs(skip_nvidia=bool(nvidia))
            for device in devices[len(nvidia):]:
                device["processesSupported"] = False
                device["processes"] = []
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
