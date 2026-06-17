"""GUI session process management.

GUI sessions have no tmux: Emacs is spawned directly (windowed, no -nw)
and tracked by pid. Liveness = pid exists and is not a zombie; the
semantic channel works unchanged. On Linux, an optional Xvfb wrapper
provides a headless display for CI.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence

from .errors import ElateError

XVFB_STARTUP_TIMEOUT = 10.0

# Popen handles for the GUI processes this controller spawned, so an
# Emacs/Xvfb that exits while the controller lives (pytest, a long-running
# MCP server) is reaped promptly instead of lingering as a zombie until
# CPython's gc-driven subprocess._active cleanup happens to run.
_SPAWNED: dict[int, subprocess.Popen[bytes]] = {}


def _reap_spawned(pid: int) -> None:
    """poll() the Popen we hold for PID (if any); drop it once exited."""
    proc = _SPAWNED.get(pid)
    if proc is not None and proc.poll() is not None:
        del _SPAWNED[pid]


def _ps_line(pid: int, fields: Sequence[str]) -> str | None:
    """Output line of `ps -o f1= -o f2= ... -p PID`.

    "" when the pid does not exist; None when ps itself failed (cannot
    tell either way).
    """
    cmd = ["ps"]
    for f in fields:
        cmd += ["-o", f"{f}="]
    cmd += ["-p", str(pid)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip()


def proc_identity(pid: int | None) -> str | None:
    """Stable identity string ("<start time>|<command>") for PID, or None.

    Recorded at spawn and compared before signalling a registry pid:
    after a controller restart, a GUI Emacs that died and was reaped
    frees its pid for reuse, and a later stop/restart must not signal
    whatever unrelated process now holds it.
    """
    if not pid or pid <= 0:
        return None
    line = _ps_line(pid, ["lstart", "comm"])
    if not line:
        return None
    # lstart is 5 space-separated tokens ("Wed Jun 11 21:00:00 2026");
    # the remainder is the command (which may itself contain spaces).
    parts = line.split(None, 5)
    if len(parts) < 6:
        return None
    return " ".join(parts[:5]) + "|" + parts[5]


def pid_alive(pid: int | None, identity: str | None = None,
              comm_hint: str | None = None) -> bool:
    """True if PID exists, is not a zombie, and still looks like ours.

    A GUI Emacs started by a still-running controller (e.g. the test
    suite) becomes a zombie child when it exits; plain kill(pid, 0)
    would keep reporting it alive, so the process state is checked too.
    With IDENTITY (as recorded by `proc_identity` at spawn) the current
    start time must match exactly; the command must match too, except
    that a changed command with the SAME start time and a COMM_HINT
    match is accepted -- some Emacs launchers exec a differently-named
    child (e.g. the emacsformacosx.com binary execs a per-arch
    `emacs-arm64-NN`), which renames the command mid-startup while
    keeping pid and start time. Pid-reuse protection is near-intact
    rather than absolute: a recycled pid virtually always has a
    different start time (lstart has 1-second granularity), but the
    exec-chain tolerance does admit the theoretical case of a
    same-second recycle into a process whose command contains
    COMM_HINT (e.g. "emacs" also matches `emacsclient`). Without
    IDENTITY, COMM_HINT alone requires the command name to contain it.
    When ps cannot be consulted the verdict falls back to "exists".
    """
    if not pid or pid <= 0:
        return False
    _reap_spawned(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    line = _ps_line(pid, ["state", "lstart", "comm"])
    if line is None:
        return True  # cannot tell; kill(0) said it exists
    if not line:
        return False  # gone between kill(0) and ps
    parts = line.split(None, 6)
    if parts[0].startswith("Z"):
        # The process died between the _reap_spawned() above and the ps
        # call (ps itself forks, so the window is several ms wide): the
        # earlier poll() came up empty and the zombie is still ours to
        # reap. Re-poll so that "returned False" implies "reaped if we
        # spawned it" -- otherwise the zombie lingers until a gc-driven
        # subprocess._active sweep.
        _reap_spawned(pid)
        return False
    if len(parts) >= 7:
        comm = parts[6]
        if identity is not None:
            lstart, _, recorded_comm = identity.partition("|")
            if " ".join(parts[1:6]) != lstart:
                return False  # different start time: the pid was recycled
            if comm == recorded_comm:
                return True
            # Same pid AND same start time but a renamed command: an
            # exec chain (see docstring), not pid reuse. Accept it when
            # the command still looks like ours.
            return comm_hint is not None and comm_hint.lower() in comm.lower()
        if comm_hint is not None:
            return comm_hint.lower() in comm.lower()
    return True


def terminate_pid(pid: int | None, grace: float = 3.0,
                  identity: str | None = None,
                  comm_hint: str | None = None) -> None:
    """SIGTERM, wait up to GRACE seconds, then SIGKILL. Never raises.

    With IDENTITY/COMM_HINT, a pid that no longer looks like the process
    recorded at spawn (pid reuse after a controller restart) is treated
    as already dead and never signalled.
    """
    if not pid or pid <= 0:
        return
    if (identity or comm_hint) and not pid_alive(pid, identity, comm_hint):
        return  # already dead, or the pid was recycled by another process
    try:
        os.kill(pid, 15)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    _reap_spawned(pid)


def signal_pid(pid: int | None, sig: int, identity: str | None = None,
               comm_hint: str | None = None) -> bool:
    """Send SIG to PID once. Returns True if delivered, False if skipped.

    Like `terminate_pid`, an IDENTITY/COMM_HINT mismatch (a recycled pid
    after a controller restart) is treated as already dead and never
    signalled. Unlike it, this neither waits nor escalates: it is the GUI
    analogue of poking C-g at a busy Emacs, not killing it.
    """
    if not pid or pid <= 0:
        return False
    if (identity or comm_hint) and not pid_alive(pid, identity, comm_hint):
        return False
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


# Month abbreviations as `ps` emits them under LC_ALL=C (forced below).
_PS_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


def _parse_lstart(text: str) -> float | None:
    """Parse a `ps -o lstart` string ("Wed Jun 4 21:00:00 2026") to epoch.

    Locale-independent on purpose: `time.strptime`'s %a/%b are
    locale-sensitive in CPython, so a non-C process locale could make every
    parse fail and silently turn reaping into a no-op (orphans survive, the
    orphan count always reads 0). We force LC_ALL=C on the `ps` call and
    split the fixed "DOW MON DD HH:MM:SS YYYY" layout ourselves (ps
    space-pads the day-of-month, so plain split() collapses it). None on
    any parse failure.
    """
    parts = text.split()
    if len(parts) != 5:
        return None
    try:
        month = _PS_MONTHS[parts[1]]
        day = int(parts[2])
        hour, minute, second = (int(x) for x in parts[3].split(":"))
        year = int(parts[4])
        # tm_isdst=-1: let mktime resolve DST for the local zone.
        return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))
    except (KeyError, ValueError, OverflowError):
        return None


def _group_members(pgid: int | None, since: float) -> list[tuple[int, float]]:
    """(pid, start-epoch) of processes in group PGID started at/after SINCE.

    The start-time filter is what makes group reaping safe against pgid
    reuse: a recycled group's members predate our session, so they fall
    below SINCE and are never touched. `ps` start times have ~1s
    granularity, so SINCE is floored to the whole second (a member spawned
    in the same second the session started must still count). Never raises.
    """
    if not pgid or pgid <= 0:
        return []
    try:
        proc = subprocess.run(
            ["ps", "-o", "pid=", "-o", "lstart=", "-g", str(pgid)],
            capture_output=True, text=True, timeout=5.0,
            # Force C locale so lstart's month/day names are English, which
            # _parse_lstart expects -- independent of the user's locale.
            env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return []
    threshold = int(since)
    members: list[tuple[int, float]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        started = _parse_lstart(parts[1])
        if started is None or started < threshold:
            continue
        members.append((pid, started))
    return members


def reap_group(pgid: int | None, since: float) -> list[int]:
    """SIGKILL processes in group PGID started at/after SINCE; return pids.

    For GUI sessions, whose Emacs leads its own process group: a single
    os.kill takes only Emacs, leaving any backgrounded grandchildren
    behind. This kills the whole (start-time-filtered) group. Never raises.
    """
    killed: list[int] = []
    for pid, _ in _group_members(pgid, since):
        try:
            os.kill(pid, 9)
            killed.append(pid)
        except OSError:
            pass
    return killed


def count_group(pgid: int | None, since: float,
                exclude: set[int] | None = None) -> int:
    """Count members of group PGID started at/after SINCE, minus EXCLUDE.

    Used to surface leaked descendants (orphans) of a live GUI session:
    pass the Emacs pid/pgid as EXCLUDE so the count is grandchildren only.
    """
    exclude = exclude or set()
    return sum(1 for pid, _ in _group_members(pgid, since)
               if pid not in exclude)


def spawn_emacs(
    emacs_path: str,
    emacs_args: Sequence[str],
    env_overrides: Mapping[str, str],
    log_path: Path,
    display: str | None = None,
) -> subprocess.Popen[bytes]:
    """Start a windowed Emacs detached from our terminal.

    stdout/stderr go to LOG_PATH (the GUI analogue of the tmux
    post-mortem pane: startup errors and crash output land there).
    """
    env = {**os.environ, **env_overrides}
    if display is not None:
        env["DISPLAY"] = display
    try:
        log = open(log_path, "ab")
    except OSError as exc:
        raise ElateError(f"cannot open GUI log file {log_path}: {exc}") from exc
    try:
        proc = subprocess.Popen(
            [emacs_path, *emacs_args],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,  # survive the controller's exit
        )
    except OSError as exc:
        raise ElateError(f"cannot start GUI Emacs {emacs_path}: {exc}") from exc
    finally:
        log.close()
    _SPAWNED[proc.pid] = proc
    return proc


def log_tail(log_path: Path, lines: int = 12) -> str:
    """Last LINES of a process log (GUI log / TTY stderr), for crash reports."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no process log available)"
    tail = text.rstrip("\n").splitlines()[-lines:]
    return "\n".join(tail) if tail else "(process log is empty)"


def start_xvfb(session_dir: Path, screen: str = "1280x800x24") -> tuple[int, str]:
    """Start an Xvfb on a free display; return (pid, ":N").

    Linux-only headless wrapper for `elate start --ui gui --headless`.
    Xvfb picks the display itself (-displayfd) so there is no race over
    display numbers. The caller owns the pid and must kill it at stop.
    """
    if sys.platform == "darwin":
        raise ElateError(
            "--headless (Xvfb) is Linux-only; on macOS GUI sessions always "
            "use the real display"
        )
    read_fd, write_fd = os.pipe()
    log = session_dir / "log" / "xvfb.log"
    try:
        with open(log, "ab") as log_fh:
            proc = subprocess.Popen(
                ["Xvfb", "-displayfd", str(write_fd),
                 "-screen", "0", screen, "-nolisten", "tcp"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
    except FileNotFoundError:
        os.close(read_fd)
        os.close(write_fd)
        raise ElateError(
            "Xvfb not found; install it (e.g. apt install xvfb) for "
            "headless GUI sessions"
        ) from None
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        raise ElateError(f"cannot start Xvfb: {exc}") from exc
    os.close(write_fd)
    # Xvfb writes the chosen display number (+ newline) to -displayfd.
    buf = b""
    deadline = time.monotonic() + XVFB_STARTUP_TIMEOUT
    try:
        while time.monotonic() < deadline and b"\n" not in buf:
            remaining = max(deadline - time.monotonic(), 0.01)
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(read_fd, 64)
            if not chunk:  # Xvfb died before reporting a display
                break
            buf += chunk
    finally:
        os.close(read_fd)
    number = buf.decode("ascii", errors="replace").strip()
    if not number.isdigit():
        terminate_pid(proc.pid)
        raise ElateError(
            f"Xvfb did not report a display within {XVFB_STARTUP_TIMEOUT:g}s "
            f"(see {log})"
        )
    _SPAWNED[proc.pid] = proc
    # Most Xvfb builds write to -displayfd only once the socket is ready,
    # but very old ones write early: give the X socket a moment to appear
    # (best effort -- its absence is not proof of a problem).
    socket = Path(f"/tmp/.X11-unix/X{number}")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not socket.exists():
        time.sleep(0.05)
    return proc.pid, f":{number}"
