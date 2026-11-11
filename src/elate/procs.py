"""Process-tree discovery for a session: what a sandboxed Emacs spawned.

A session's processes are the descendants of its Emacs (by parent pid)
plus every process carrying the session's environment marker
(``ELATE_SCRATCH=<sandbox>/scratch``, set for Emacs and inherited by
everything it starts) and their descendants. The marker is what finds a
double-forked daemon after it has been reparented to init/launchd, and a
subprocess that outlived a crashed Emacs. Only processes started at or
after the session itself count, so a recycled pid is never mistaken for
one of ours. Nothing here signals a process the scan did not attribute to
the session.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .gui import _parse_lstart

_COMMAND_MAX = 300


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    pgid: int
    started: float
    command: str
    zombie: bool = False

    def as_dict(self) -> dict:
        return {"pid": self.pid, "ppid": self.ppid, "pgid": self.pgid,
                "started": self.started,
                "command": self.command[:_COMMAND_MAX]}


def process_table() -> dict[int, Proc]:
    """Every visible process, keyed by pid. Empty on failure; never raises."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="],
            capture_output=True, text=True, timeout=5.0,
            env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table: dict[int, Proc] = {}
    for line in out.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        started = _parse_lstart(" ".join(parts[4:9]))
        if started is None:
            continue
        table[pid] = Proc(pid, ppid, pgid, started,
                          parts[9] if len(parts) > 9 else "",
                          zombie=parts[3].startswith("Z"))
    return table


def marked_pids(marker_value: str, table: dict[int, Proc],
                key: str = "ELATE_SCRATCH") -> set[int]:
    """Pids whose environment has KEY=MARKER_VALUE exactly. Never raises.

    Linux reads /proc/PID/environ. On macOS ``ps -E`` appends the
    environment to the command line; the command from TABLE is stripped
    first so an argument that merely mentions the marker (tmux's
    ``exec env ...`` line) does not count. macOS withholds the environment
    of SIP-protected system binaries (/bin, /usr/bin), so those are found
    only through their parent chain.
    """
    entry = f"{key}={marker_value}"
    found: set[int] = set()
    proc_root = Path("/proc")
    if sys.platform.startswith("linux") and proc_root.is_dir():
        needle = entry.encode()
        for child in proc_root.iterdir():
            if not child.name.isdigit():
                continue
            try:
                env = (child / "environ").read_bytes()
            except OSError:
                continue
            if needle in env.split(b"\0"):
                found.add(int(child.name))
        return found
    try:
        out = subprocess.run(
            ["ps", "-A", "-E", "-ww", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=5.0,
            env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return found
    pattern = re.compile(r"(?:^|\s)" + re.escape(entry) + r"(?:\s|$)")
    for line in out.splitlines():
        head, _, rest = line.strip().partition(" ")
        if not head.isdigit():
            continue
        proc = table.get(int(head))
        if proc is None:
            continue
        rest = rest.lstrip()
        if not rest.startswith(proc.command):
            continue  # command changed between the two ps calls
        if pattern.search(rest[len(proc.command):]):
            found.add(proc.pid)
    return found


def own_lineage(table: dict[int, Proc]) -> set[int]:
    """This process and its ancestors: never part of a session's tree.

    An ``elate`` invoked from inside the sandbox (a shell in the driven
    Emacs) inherits the marker; it must not kill itself or its parents.
    """
    mine: set[int] = set()
    pid = os.getpid()
    while pid and pid not in mine:
        mine.add(pid)
        proc = table.get(pid)
        pid = proc.ppid if proc else 0
    return mine


def session_tree(roots: Iterable[int | None], marker_value: str,
                 since: float,
                 table: dict[int, Proc] | None = None) -> list[Proc]:
    """Processes belonging to a session, ordered by start time.

    ROOTS (the Emacs pid, plus any already-known members) seed a walk
    over children and over process groups led by a member; a group led
    by anything else is never entered, so a marked process cannot pull in
    its unmarked parent or siblings. MARKER_VALUE adds
    environment-marked processes. Everything started before SINCE
    (floored to ps's 1-second granularity) is dropped, as are this
    process and its ancestors.
    """
    table = process_table() if table is None else table
    threshold = int(since)
    exclude = own_lineage(table)
    seeds = {pid for pid in roots if pid} | marked_pids(marker_value, table)
    children: dict[int, list[int]] = {}
    groups: dict[int, list[int]] = {}
    for proc in table.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
        groups.setdefault(proc.pgid, []).append(proc.pid)
    members: dict[int, Proc] = {}
    stack = list(seeds)
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = table.get(pid)
        if proc is None or proc.started < threshold or pid in exclude:
            continue
        members[pid] = proc
        stack.extend(children.get(pid, ()))
        stack.extend(groups.get(pid, ()))
    return sorted(members.values(), key=lambda p: (p.started, p.pid))


def still_alive(procs: Iterable[Proc],
                table: dict[int, Proc] | None = None) -> list[Proc]:
    """The PROCS that still run as the same process (pid and start time)."""
    table = process_table() if table is None else table
    alive = []
    for proc in procs:
        now = table.get(proc.pid)
        if now is not None and now.started == proc.started and not now.zombie:
            alive.append(now)
    return alive


def kill_all(procs: list[Proc], grace: float = 1.0) -> list[Proc]:
    """SIGTERM PROCS, wait up to GRACE, SIGKILL survivors; return the signalled.

    Identity-checked twice (pid + start time) against reuse: once before
    the first signal and again before the SIGKILL. Never raises.
    """
    targets = still_alive(procs)
    signalled: list[Proc] = []
    for proc in targets:
        try:
            os.kill(proc.pid, 15)
            signalled.append(proc)
        except OSError:
            pass
    if not signalled:
        return []
    deadline = time.monotonic() + grace
    remaining = signalled
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = still_alive(remaining)
    for proc in still_alive(remaining):
        try:
            os.kill(proc.pid, 9)
        except OSError:
            pass
    return signalled
