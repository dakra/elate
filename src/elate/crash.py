"""Best-effort crash-report + fatal-signal detection for dead sessions.

When an Emacs session dies unexpectedly, two cheap facts make the death
actionable without a manual dig: the *signal* it died from (grepped from
its own stderr/GUI log, which carries Emacs's ``Fatal error N: ...``
line) and, on macOS, the path to the OS *crash report* (``.ips``)
attributed to it.

Deliberately lean (per the plan): we locate the report and read only
enough to confirm its pid and name the signal -- never the faulting-frame
backtrace. Everything here is best-effort and never raises.
"""

from __future__ import annotations

import glob
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Emacs's fatal-signal handler prints "Fatal error N: <strsignal>" to
# stderr before dying; N is the signal number.
_FATAL_RE = re.compile(r"Fatal error (\d+):")

# Signal number -> name (the handful that actually kill Emacs).
_SIGNALS = {
    2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP", 6: "SIGABRT",
    7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGBUS", 11: "SIGSEGV",
    13: "SIGPIPE", 15: "SIGTERM", 24: "SIGXCPU", 25: "SIGXFSZ",
}


def signal_name(num: int) -> str:
    return _SIGNALS.get(num, f"signal {num}")


def signal_from_log(log_path: Path | None) -> str | None:
    """Fatal-signal name grepped from an Emacs stderr / GUI process log.

    Reads the log tail-first for the LAST ``Fatal error N:`` line (the one
    that actually killed it). Returns e.g. ``"SIGABRT"``, or None when the
    log is missing or carries no such line.
    """
    if not log_path:
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        m = _FATAL_RE.search(line)
        if m:
            return signal_name(int(m.group(1)))
    return None


def find_crash_report(emacs_pid: int | None, comm: str | None,
                      since: float) -> dict[str, Any] | None:
    """Locate the OS crash report attributed to EMACS_PID, if any.

    Returns ``{"path": str, "signal": str | None}`` or None. SINCE (the
    session's ``created_at``) bounds the search to fresh reports; the pid
    recorded inside each report is matched to EMACS_PID so attribution is
    correct even when several sandboxed Emacsen crash in parallel. COMM is
    an optional process-name hint that narrows the macOS glob.
    """
    if not emacs_pid or emacs_pid <= 0:
        return None
    if sys.platform == "darwin":
        return _find_macos(emacs_pid, comm, since)
    return _find_linux(emacs_pid)


# -- macOS: ~/Library/Logs/DiagnosticReports/<Proc>-<time>.ips --------------

def _read_ips(path: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(header, body) JSON of a macOS .ips report; None if unreadable.

    An .ips file is a one-line JSON header followed by a JSON body (which
    spans many lines). The pid and termination live in the body.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head, _, rest = text.partition("\n")
    try:
        header = json.loads(head)
    except ValueError:
        return None
    body: dict[str, Any] = {}
    if rest.strip():
        try:
            parsed = json.loads(rest)
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            body = {}
    if not isinstance(header, dict):
        header = {}
    return header, body


def _ips_signal(header: dict[str, Any], body: dict[str, Any]) -> str | None:
    """Signal name from a report's termination block (no frame parsing)."""
    term = body.get("termination")
    if isinstance(term, dict):
        code = term.get("code")
        if isinstance(code, int) and code in _SIGNALS:
            return _SIGNALS[code]
        indicator = term.get("indicator")
        if isinstance(indicator, str):
            # e.g. "Abort trap: 6", "Segmentation fault: 11"
            m = re.search(r":\s*(\d+)\b", indicator)
            if m and int(m.group(1)) in _SIGNALS:
                return _SIGNALS[int(m.group(1))]
            return indicator
    return None


def _find_macos(emacs_pid: int, comm: str | None,
                since: float) -> dict[str, Any] | None:
    reports = Path("~/Library/Logs/DiagnosticReports").expanduser()
    if not reports.is_dir():
        return None
    # A clean process-name hint narrows the glob; fall back to all reports
    # (pid matching below is what actually attributes the crash).
    patterns = ["*.ips"]
    if comm and re.fullmatch(r"[A-Za-z0-9._-]+", comm):
        patterns = [f"{comm}-*.ips", "*.ips"]
    candidates: list[tuple[float, Path]] = []
    seen: set[str] = set()
    for pattern in patterns:
        for path_str in glob.glob(str(reports / pattern)):
            if path_str in seen:
                continue
            seen.add(path_str)
            path = Path(path_str)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            # int() floors to whole seconds: .ips mtimes and ps/lstart-style
            # timing have ~1s granularity, so a report written in the same
            # second the session started must still count as fresh.
            if mtime < int(since):
                continue
            candidates.append((mtime, path))
    # Newest first: the crash we want is the most recent one whose pid matches.
    for _, path in sorted(candidates, reverse=True):
        parsed = _read_ips(path)
        if parsed is None:
            continue
        header, body = parsed
        pid = body.get("pid")
        if pid is None:
            pid = header.get("pid")
        if pid != emacs_pid:
            continue
        return {"path": str(path), "signal": _ips_signal(header, body)}
    return None


# -- Linux: coredumpctl, when present ---------------------------------------

def _find_linux(emacs_pid: int) -> dict[str, Any] | None:
    if not shutil.which("coredumpctl"):
        return None
    try:
        proc = subprocess.run(["coredumpctl", "info", str(emacs_pid)],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    signal = None
    m = re.search(r"Signal:\s*\d+\s*\(([A-Z0-9]+)\)", proc.stdout)
    if m:
        sig = m.group(1)
        signal = sig if sig.startswith("SIG") else f"SIG{sig}"
    return {"path": f"coredumpctl info {emacs_pid}", "signal": signal}
