"""Platform helpers: console setup, WSL detection, pid liveness.

These are cross-cutting utilities used by the CLI and the engine. They are
deliberately dependency-free and side-effect-light so they can be imported
early (before optional deps are checked).
"""

from __future__ import annotations

import contextlib
import os
import platform
import re


def platform_kind() -> str:
    """Return one of ``WSL`` / ``Linux`` / ``Darwin`` / ``Windows`` / ``unknown``."""
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/version", encoding="utf-8", errors="ignore") as fh:
                if "microsoft" in fh.read().lower():
                    return "WSL"
        except OSError:
            pass
        return "Linux"
    if system in ("Darwin", "Windows"):
        return system
    return "unknown"


def init_console() -> None:
    """Enable ANSI/VT processing on Windows consoles (best effort)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # stdout, stderr
            handle = k32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if handle and k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # pragma: no cover - platform specific
        pass


def safe_io() -> None:
    """Reconfigure stdio to UTF-8 so redirected output never crashes on encoding."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            enc = (stream.encoding or "").lower().replace("_", "-")
        except Exception:  # pragma: no cover - exotic streams
            enc = ""
        if enc and enc not in ("utf-8", "utf8"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def stdout_is_tty() -> bool:
    import sys

    try:
        return bool(sys.stdout.isatty()) if sys.stdout is not None else False
    except Exception:
        return False


def stdin_is_tty() -> bool:
    import sys

    try:
        return bool(sys.stdin.isatty()) if sys.stdin is not None else False
    except Exception:
        return False


def pid_alive(pid: int) -> bool:
    """Check whether a process is alive (never sends a real signal on Windows)."""
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        except Exception:  # pragma: no cover
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def win_to_wsl_path(path: str) -> str:
    """Translate ``D:\\models`` to ``/mnt/d/models`` (meaningful only under WSL)."""
    if not path:
        return path
    match = re.match(r"^([A-Za-z]):[\\/]", path)
    if match:
        rest = path[2:].lstrip("\\/")
        path = f"/mnt/{match.group(1).lower()}/{rest}"
    return path.replace("\\", "/")


def detect_wsl_proxy(proxy: str, port: int = 7897) -> str:
    """Under WSL, auto-fill the proxy from the Windows host IP when unset."""
    if proxy or platform_kind() != "WSL":
        return proxy
    host_ip = os.environ.get("WSL_HOST_IP", "")
    if not host_ip:
        try:
            with open("/etc/resolv.conf", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    match = re.match(r"^\s*nameserver\s+(\S+)", line)
                    if match:
                        host_ip = match.group(1)
                        break
        except OSError:
            return proxy
    if not host_ip or host_ip == "127.0.0.1" or ":" in host_ip:
        return proxy
    return f"http://{host_ip}:{port}"


def parse_rate(rate: str) -> float | None:
    """Parse ``5M`` / ``1m`` / ``512k`` into bytes per second."""
    if not rate:
        return None
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*$", rate)
    if not match:
        raise ValueError(f"cannot parse rate: {rate!r} (expected e.g. 5M / 1m / 512k)")
    value = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {"": 1.0, "k": 1024.0, "m": 1048576.0, "g": 1073741824.0}[unit]
    return value * multiplier


def human_size(num_bytes: float) -> str:
    """Format a byte count for humans (``0 B`` / ``1.5 MB`` / ``2.00 GB``)."""
    if num_bytes <= 0:
        return "0 B"
    if num_bytes >= 1 << 30:
        return f"{num_bytes / (1 << 30):.2f} GB"
    if num_bytes >= 1 << 20:
        return f"{num_bytes / (1 << 20):.1f} MB"
    if num_bytes >= 1 << 10:
        return f"{num_bytes / (1 << 10):.1f} KB"
    return f"{int(num_bytes)} B"


def fmt_num(value: int) -> str:
    """Format an integer with thousands separators."""
    return f"{int(value):,}"


def human_age(seconds: float) -> str:
    """Format an age in seconds for humans (``just now`` / ``5m ago`` / ``2d ago``)."""
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"
