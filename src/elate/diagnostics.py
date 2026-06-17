"""Best-effort thread-backtrace sampling of a wedged process.

Used by ``eval --on-timeout sample`` to capture *where* an Emacs that
stopped answering the semantic channel is stuck, sparing the most
expensive manual round-trip when a form hangs. Sampling needs no special
privileges for a process the caller owns. Every entry point is
best-effort and never raises: a missing sampler or a failed run comes
back as ``{"available": False, "reason": ...}``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any


def sample_process(pid: int | None, secs: int = 2) -> dict[str, Any]:
    """Sample PID's threads for SECS seconds; return a backtrace dict.

    macOS uses ``sample`` (always present); Linux uses ``eu-stack`` or
    ``gdb`` when one is on PATH. Returns
    ``{"available": True, "tool": ..., "backtrace": ...}`` on success, or
    ``{"available": False, "reason": ...}`` otherwise. Never raises.
    """
    if not pid or pid <= 0:
        return {"available": False, "reason": "no pid to sample"}
    if sys.platform == "darwin":
        return _sample_macos(pid, secs)
    return _sample_linux(pid, secs)


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str] | None:
    """Run CMD; return (returncode, stdout, stderr) or None on failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode, proc.stdout, proc.stderr


def _sample_macos(pid: int, secs: int) -> dict[str, Any]:
    if not shutil.which("sample"):
        return {"available": False, "reason": "sample not found"}
    # -mayDie: tolerate the target exiting mid-sample (it is, after all,
    # a process we suspect is wedged or dying).
    result = _run(["sample", str(pid), str(secs), "-mayDie"],
                  timeout=secs + 15.0)
    if result is None:
        return {"available": False, "reason": "sample failed or timed out"}
    rc, out, err = result
    out = out.strip()
    if rc != 0 and not out:
        return {"available": False,
                "reason": (err.strip() or f"sample exited {rc}")}
    return {"available": True, "tool": "sample", "backtrace": out}


def _sample_linux(pid: int, secs: int) -> dict[str, Any]:
    if shutil.which("eu-stack"):
        cmd, tool = ["eu-stack", "-p", str(pid)], "eu-stack"
    elif shutil.which("gdb"):
        cmd = ["gdb", "-batch", "-p", str(pid),
               "-ex", "thread apply all bt"]
        tool = "gdb"
    else:
        return {"available": False,
                "reason": "no sampler available (install elfutils' "
                          "eu-stack, or gdb)"}
    result = _run(cmd, timeout=secs + 15.0)
    if result is None:
        return {"available": False, "reason": f"{tool} failed or timed out"}
    rc, out, err = result
    out = out.strip()
    if rc != 0 and not out:
        return {"available": False, "reason": (err.strip() or f"{tool} exited {rc}")}
    return {"available": True, "tool": tool, "backtrace": out}
