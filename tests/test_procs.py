"""Session process trees: `elate ps`, and `stop` killing what outlives Emacs.

The contract: a session's processes are Emacs's descendants plus anything
carrying the session's environment marker, started after the session.
`stop` kills exactly those that survive Emacs and reports them; an
unrelated process -- even one with an identical command line, or one
whose arguments mention the marker -- is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

from elate import procs
from elate import session as S
from elate.semantic import elisp_string

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux")
    and shutil.which("emacsclient")
)
needs_emacs = pytest.mark.skipif(not HAVE_DEPS, reason="needs emacs + tmux")

# A daemon that double-forks away from Emacs (reparented to init) and
# ignores SIGHUP/SIGTERM, so neither Emacs's exit nor a polite signal
# takes it. Python rather than /bin tools: macOS hides the environment of
# SIP-protected system binaries, so only a non-system binary is findable
# by the marker once detached.
_DAEMON = (
    "import os, signal, sys, time\n"
    "if os.fork(): sys.exit(0)\n"
    "os.setsid()\n"
    "if os.fork(): sys.exit(0)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(600)\n"
)


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="elate-test-"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    return pid in procs.process_table()


def _wait_for(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


@needs_emacs
def test_ps_lists_children_and_stop_kills_detached_daemon(elate_home: Path):
    sess = S.start_session("pt", cols=80, rows=24)
    outsider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        sem = sess.semantic()
        sem.eval_raw(
            '(make-process :name "child" :command (list "sleep" "600"))')
        sem.eval_raw(
            f"(call-process {elisp_string(sys.executable)} nil 0 nil"
            f" \"-c\" {elisp_string(_DAEMON)})")
        assert _wait_for(lambda: len(S.session_ps("pt")["processes"]) >= 3)
        data = S.session_ps("pt")
        entries = data["processes"]
        emacs = [e for e in entries if e.get("emacs")]
        assert len(emacs) == 1 and emacs[0]["pid"] == sess.emacs_pid
        child = [e for e in entries if "sleep 600" in e["command"]]
        assert child and child[0]["ppid"] == sess.emacs_pid
        detached = [e for e in entries if e.get("detached")]
        assert len(detached) == 1 and detached[0]["ppid"] == 1
        daemon_pid = detached[0]["pid"]
        pids = {e["pid"] for e in entries}
        assert outsider.pid not in pids
        assert data["count"] == len(entries) - 1
        # info carries the same tree, Emacs excluded.
        info_pids = {p["pid"] for p in S.session_info("pt")["processes"]}
        assert info_pids == pids - {sess.emacs_pid}

        result = S.stop_session("pt")
        assert daemon_pid in {k["pid"] for k in result.get("killed", [])}
        assert _wait_for(lambda: not _pid_alive(daemon_pid))
        assert outsider.poll() is None, "an unrelated process was killed"
        # After stop nothing of the session is left.
        assert S.session_ps("pt")["processes"] == []
    finally:
        outsider.kill()
        outsider.wait()
        S.stop_session("pt")


def test_marker_in_arguments_is_not_environment(tmp_path: Path):
    marker = str(tmp_path / "scratch")
    # Mentions the marker in argv only, like the tmux server's
    # `exec env ELATE_SCRATCH=...` command line.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)",
         f"ELATE_SCRATCH={marker}"])
    marked = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"ELATE_SCRATCH": marker, "PATH": "/usr/bin:/bin"})
    try:
        assert _wait_for(lambda: marked.pid in procs.process_table())
        found = procs.marked_pids(marker, procs.process_table())
        assert marked.pid in found
        assert proc.pid not in found
    finally:
        for p in (proc, marked):
            p.kill()
            p.wait()


def test_session_tree_ignores_processes_older_than_the_session(tmp_path: Path):
    marker = str(tmp_path / "scratch")
    old = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"ELATE_SCRATCH": marker, "PATH": "/usr/bin:/bin"})
    try:
        assert _wait_for(lambda: old.pid in procs.process_table())
        # A session created after the process started (name reuse) does
        # not claim it.
        later = time.time() + 5
        assert procs.session_tree([], marker, later) == []
        earlier = time.time() - 60
        assert old.pid in {p.pid for p in procs.session_tree([], marker, earlier)}
    finally:
        old.kill()
        old.wait()


@needs_emacs
def test_creator_state_and_default_owner(elate_home: Path,
                                         monkeypatch: pytest.MonkeyPatch):
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcdef12-3456")
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        S.start_session("cs-self", cols=80, rows=24)
        monkeypatch.setenv("CLAUDE_PID", str(agent.pid))
        S.start_session("cs-other", cols=80, rows=24)
        rows = {r["name"]: r for r in S.list_sessions()}
        assert rows["cs-self"]["creator_state"] == "self"
        assert rows["cs-other"]["creator_state"] == "other"
        assert rows["cs-other"]["creator_pid"] == agent.pid
        assert rows["cs-self"]["owner"] == "claude-abcdef12"
        agent.kill()
        agent.wait()
        rows = {r["name"]: r for r in S.list_sessions()}
        assert rows["cs-other"]["creator_state"] == "gone"
        assert S.session_info("cs-other")["creator_state"] == "gone"
        # An explicit owner wins over the derived default.
        monkeypatch.setenv("ELATE_OWNER", "ci-7")
        S.start_session("cs-tag", cols=80, rows=24)
        assert S.load_session("cs-tag").owner == "ci-7"
    finally:
        agent.kill()
        agent.wait()
        for name in ("cs-self", "cs-other", "cs-tag"):
            S.stop_session(name)


def test_marked_process_does_not_pull_in_its_group(tmp_path: Path):
    # A marked child inside an unrelated, live group leader's group (an
    # agent's shell that started `ELATE_SCRATCH=... server &`): the leader
    # and an unmarked sibling must not be attributed to the session.
    marker = str(tmp_path / "scratch")
    script = (
        "import os, subprocess, sys, time\n"
        "os.setpgid(0, 0)\n"
        "env = dict(os.environ, ELATE_SCRATCH=sys.argv[1])\n"
        "a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], env=env)\n"
        "b = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(a.pid, b.pid, flush=True)\n"
        "time.sleep(60)\n")
    leader = subprocess.Popen(
        [sys.executable, "-c", script, marker], stdout=subprocess.PIPE,
        text=True, env={k: v for k, v in os.environ.items()
                        if k != "ELATE_SCRATCH"})
    try:
        marked, sibling = (int(x) for x in leader.stdout.readline().split())
        tree = {p.pid for p in procs.session_tree([], marker, time.time() - 60)}
        assert marked in tree
        assert leader.pid not in tree and sibling not in tree
    finally:
        os.killpg(leader.pid, 9)
        leader.wait()
