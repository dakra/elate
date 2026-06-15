"""Tests for `elate attach` (human handoff).

CLI-surface, argv construction, and error paths only -- the interactive
`tmux attach` exec is never run (it needs a real pty). `os.execvp` is
monkeypatched to fail loudly so any accidental exec is caught.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator

import pytest

from elate import cli, session as S
from elate.errors import ElateError

# Short prefix/names: the emacsclient socket path lives under this dir and
# macOS caps unix-socket paths at ~104 bytes.
NAME = "at"


def _fake_session(ui: str = "tty",
                  tmux_socket: str = "/tmp/elate-fake/tmux.sock") -> S.Session:
    return S.Session(name="fake", session_dir="/tmp/elate-fake", emacs="emacs",
                     emacsclient="emacsclient", config="", cols=80, rows=24,
                     created_at=0.0, ui=ui, tmux_socket=tmux_socket)


# -- unit: Session.tmux_attach_argv (no Emacs) --------------------------------

def test_attach_argv_tty() -> None:
    sess = _fake_session(tmux_socket="/tmp/s/tmux.sock")
    assert sess.tmux_attach_argv() == [
        "tmux", "-S", "/tmp/s/tmux.sock", "attach", "-t", "elate"]
    assert sess.tmux_attach_argv(read_only=True)[-1] == "-r"


def test_attach_argv_gui_errors() -> None:
    with pytest.raises(ElateError, match="GUI session"):
        _fake_session(ui="gui").tmux_attach_argv()


def test_attach_argv_no_socket_errors() -> None:
    with pytest.raises(ElateError, match="no tmux socket"):
        _fake_session(tmux_socket="").tmux_attach_argv()


def test_attach_missing_session_exit_1(capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["attach", "nope-xyzzy-attach"]) == 1
    assert "no session named" in capsys.readouterr().err


# -- integration: real TTY session --------------------------------------------

@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elat-")
    old = os.environ.get("ELATE_HOME")
    os.environ["ELATE_HOME"] = tmp
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop("ELATE_HOME", None)
        else:
            os.environ["ELATE_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def tty_sess(elate_home: str) -> Iterator[S.Session]:
    session = S.start_session(NAME, cols=90, rows=24)
    try:
        yield session
    finally:
        try:
            S.stop_session(NAME)
        except Exception:
            session.raw().kill_server()


def test_attach_print_command(tty_sess: S.Session,
                              capsys: pytest.CaptureFixture,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "execvp",
                        lambda *a: pytest.fail("attach must not exec"))
    assert cli.main(["-s", NAME, "attach", "--print-command"]) == 0
    assert capsys.readouterr().out.strip() == (
        f"tmux -S {tty_sess.tmux_socket} attach -t elate")
    # read-only variant appends -r
    assert cli.main(["-s", NAME, "attach", "-r", "--print-command"]) == 0
    assert capsys.readouterr().out.strip().endswith(" -r")


def test_attach_not_a_tty_exits_2(tty_sess: S.Session,
                                  capsys: pytest.CaptureFixture,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "execvp",
                        lambda *a: pytest.fail("attach must not exec"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert cli.main(["-s", NAME, "attach"]) == 2
    assert "real terminal" in capsys.readouterr().err


def test_attach_dead_session_exits_1(elate_home: str,
                                     capsys: pytest.CaptureFixture,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "execvp",
                        lambda *a: pytest.fail("attach must not exec"))
    dead = "atd"
    S.start_session(dead, cols=80, rows=24)
    S.stop_session(dead)
    assert cli.main(["-s", dead, "attach"]) == 1
    assert "not running" in capsys.readouterr().err
