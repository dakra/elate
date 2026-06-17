"""Crash / death / orphan-reaping lifecycle (the fuzz-testing feedback).

Two flavours of live test here:

* Death detection + enrichment: a real TTY Emacs is crashed mid-eval
  (SIGABRT to itself) and we assert `wait dead`, `info`, and `eval` report
  the death with its fatal signal (grepped from the redirected stderr log).
* Process-group reaping: a real `sh`/`sleep` process group exercises
  gui.reap_group / count_group against the OS (`ps -g` + kill), no GUI
  Emacs needed -- the mechanism a GUI session's teardown relies on.

Plus the live start/stop lifecycle additions (--replace over a live
session, auto-name, stop --all).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli, gui
from elate import session as S
from elate.errors import SessionExists

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux")
    and shutil.which("emacsclient")
)


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # tempfile.mkdtemp (not tmp_path): macOS caps unix-socket paths at
    # ~104 bytes, which pytest's nested tmp dirs blow for the tmux socket.
    tmp = Path(tempfile.mkdtemp(prefix="elate-test-"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _stop_quietly(name: str) -> None:
    try:
        S.stop_session(name)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process-group reaping (no Emacs required: real sh/sleep + ps + kill)

def test_reap_group_kills_a_real_grandchild() -> None:
    since = time.time() - 1
    # sh (group leader, pgid == pid via start_new_session) backgrounds a
    # sleep child in the same group -- the grandchild a single-pid kill
    # misses. ("& wait" also dodges sh's single-command exec optimization,
    # which would otherwise replace sh with sleep and leave no grandchild.)
    proc = subprocess.Popen(["sh", "-c", "sleep 600 & wait"],
                            start_new_session=True)
    pgid = proc.pid
    try:
        deadline = time.time() + 5
        while (gui.count_group(pgid, since, exclude={pgid}) < 1
               and time.time() < deadline):
            time.sleep(0.05)
        assert gui.count_group(pgid, since, exclude={pgid}) >= 1, (
            "the sleep grandchild should be visible in the group")

        killed = gui.reap_group(pgid, since)
        assert pgid in killed              # the leader
        assert len(killed) >= 2            # leader + grandchild

        proc.wait(timeout=5)               # reap the leader zombie we own
        # The reparented grandchild was SIGKILLed; wait for init to reap it.
        deadline = time.time() + 5
        while gui.count_group(pgid, since) > 0 and time.time() < deadline:
            time.sleep(0.05)
        assert gui.count_group(pgid, since) == 0
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def test_reap_group_start_time_filter_spares_old_pgid() -> None:
    # A group whose only members predate `since` (a recycled pgid) is left
    # untouched -- the safety property reaping relies on.
    proc = subprocess.Popen(["sh", "-c", "sleep 600"], start_new_session=True)
    pgid = proc.pid
    try:
        future = time.time() + 100_000  # everything is "older than" this
        assert gui.count_group(pgid, future) == 0
        assert gui.reap_group(pgid, future) == []
        # The process is genuinely still alive (we did not kill it).
        assert proc.poll() is None
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Live death detection + crash enrichment (TTY)

pytestmark_live = pytest.mark.skipif(
    not HAVE_DEPS, reason="emacs, emacsclient, and tmux are required")


def _crash_self(sess: S.Session) -> None:
    """Make the session's Emacs SIGABRT itself; swallow the transport death."""
    try:
        sess.semantic().eval_form("(signal-process (emacs-pid) 6)", timeout=5.0)
    except Exception:
        pass


@pytestmark_live
def test_wait_dead_and_info_report_the_signal(elate_home: Path) -> None:
    name = f"crash{os.getpid()}"
    sess = S.start_session(name, cols=80, rows=24)
    try:
        _crash_self(sess)
        data = S.wait_dead(sess, timeout=10.0)
        assert data["died"] is True
        # Emacs prints "Fatal error 6: Aborted" to the (redirected) stderr.
        assert data.get("signal") == "SIGABRT"

        info = S.session_info(name)
        assert info["status"] == "dead"
        assert info.get("signal") == "SIGABRT"
    finally:
        _stop_quietly(name)


@pytestmark_live
def test_eval_that_crashes_reports_session_died(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    name = f"crashev{os.getpid()}"
    S.start_session(name, cols=80, rows=24)
    try:
        # The form crashes Emacs before it can reply: the eval must turn the
        # resulting transport error into a death verdict (not "connection
        # refused"), exit 1.
        rc = cli.main(["--json", "-s", name, "eval",
                       "(progn (signal-process (emacs-pid) 6) (sleep-for 5))"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["ok"] is False
        assert out.get("session_died") is True
        assert out.get("signal") == "SIGABRT"
        assert "session died" in out["error"]
    finally:
        _stop_quietly(name)


@pytestmark_live
def test_eval_on_timeout_sample_captures_a_backtrace(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    name = f"hang{os.getpid()}"
    S.start_session(name, cols=80, rows=24)
    try:
        # A synchronous call-process ignores the in-Emacs timeout, so the
        # eval hard-times-out with Emacs genuinely wedged -- the case
        # --on-timeout sample exists for.
        rc = cli.main(["--json", "-s", name, "eval",
                       '(call-process "sleep" nil nil nil "30")',
                       "--timeout", "2", "--on-timeout", "sample"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["ok"] is False
        assert "sample" in out
        assert out["sample"]["available"] is True
        assert out["sample"]["backtrace"]
    finally:
        # The session is wedged on sleep; stop falls through to a hard kill.
        _stop_quietly(name)


@pytestmark_live
def test_wait_dead_choice_via_cli(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    name = f"waitd{os.getpid()}"
    sess = S.start_session(name, cols=80, rows=24)
    try:
        _crash_self(sess)
        rc = cli.main(["--json", "-s", name, "wait", "dead", "--timeout", "10"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["ok"] is True and out["died"] is True
    finally:
        _stop_quietly(name)


# ---------------------------------------------------------------------------
# Live stderr logs + redirect

@pytestmark_live
def test_logs_tails_tty_stderr(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    name = f"logs{os.getpid()}"
    sess = S.start_session(name, cols=80, rows=24)
    try:
        # external-debugging-output writes (unbuffered) to fd 2, which the
        # TTY boot redirects into emacs-stderr.log.
        sess.semantic().eval_form(
            "(princ \"elate-logs-probe\\n\" 'external-debugging-output)")
        # Give the write a beat to hit the file, then tail it.
        time.sleep(0.2)
        rc = cli.main(["--json", "-s", name, "logs"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["ok"] is True
        assert "elate-logs-probe" in out["log"]
        # The stderr alias resolves to the same command.
        assert cli.main(["--json", "-s", name, "stderr"]) == 0
    finally:
        _stop_quietly(name)


# ---------------------------------------------------------------------------
# Live start/stop lifecycle additions

@pytestmark_live
def test_start_auto_name(elate_home: Path) -> None:
    sess = S.start_session(cols=80, rows=24)  # no name given
    try:
        assert sess.name.startswith("elate-")
        assert S.session_info(sess.name)["alive"] is True
    finally:
        _stop_quietly(sess.name)


@pytestmark_live
def test_start_replace_recreates_a_live_session(elate_home: Path) -> None:
    name = f"rep{os.getpid()}"
    first = S.start_session(name, cols=80, rows=24)
    pid1 = first.emacs_pid
    try:
        # Without --replace, starting over a live session is an error.
        with pytest.raises(SessionExists):
            S.start_session(name, cols=80, rows=24)
        # With --replace, the old Emacs is stopped and a fresh one boots.
        second = S.start_session(name, cols=80, rows=24, replace=True)
        assert second.is_alive()
        assert second.emacs_pid != pid1
    finally:
        _stop_quietly(name)


@pytestmark_live
def test_stop_all_clears_running_sessions(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a, b = f"sa{os.getpid()}", f"sb{os.getpid()}"
    S.start_session(a, cols=80, rows=24)
    S.start_session(b, cols=80, rows=24)
    try:
        rc = cli.main(["--json", "stop", "--all"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["ok"] is True
        assert set(out["stopped"]) >= {a, b}
        running = [s["name"] for s in S.list_sessions()
                   if s["status"] == "running"]
        assert a not in running and b not in running
    finally:
        _stop_quietly(a)
        _stop_quietly(b)
