"""Polish from the ghostel-session feedback: arg-order hint, list
filtering + prune hint, the `interrupt` signal helper, and the
screenshot lock/asleep-vs-permission diagnosis with machine-readable
reason codes. All pure-CLI/unit -- no Emacs or tmux required.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli, gui, screenshot
from elate import session as S
from elate.errors import ScreenshotError


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="elate-test-"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- global-flag ordering footgun -------------------------------------------

def test_session_flag_after_subcommand_hints_ordering(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `-s` is a global flag, so it must precede the subcommand. The wrong
    # order trips argparse; we append a pointer instead of a bare error.
    with pytest.raises(SystemExit) as exc:
        cli.main(["stop", "-s", "x"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "elate -s NAME <command>" in err


def test_correct_ordering_does_not_hint(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Right order, missing session: a normal error, not the ordering hint.
    assert cli.main(["--json", "-s", "nope", "stop"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "elate -s NAME" not in out["error"]


# -- list filtering + prune hint --------------------------------------------

def test_list_status_filter_messages(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--human", "list", "--status", "running"]) == 0
    assert "no running sessions" in capsys.readouterr().out
    assert cli.main(["--human", "list", "--status", "stopped"]) == 0
    assert "no stopped sessions" in capsys.readouterr().out


def test_list_name_filter_message(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--human", "list", "ghost"]) == 0
    assert "no session named 'ghost'" in capsys.readouterr().out


def test_list_default_still_lists_all(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Default (no name, --status all) keeps the original JSON shape.
    assert cli.main(["--json", "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["sessions"] == []


def test_list_prune_hint_when_many_inert(
        elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    many = [{"name": f"s{i}", "ui": "tty", "status": "stopped",
             "emacs_version": "30.1", "uptime": None, "idle_for": 10.0}
            for i in range(6)]
    monkeypatch.setattr(S, "list_sessions", lambda: many)
    assert cli.main(["--human", "list"]) == 0
    out = capsys.readouterr().out
    assert "6 inert session(s)" in out
    assert "elate purge --stopped-older-than" in out


# -- interrupt: the GUI signal helper ---------------------------------------

def test_signal_pid_skips_missing_pid() -> None:
    assert gui.signal_pid(None, signal.SIGINT) is False
    assert gui.signal_pid(0, signal.SIGINT) is False
    assert gui.signal_pid(-1, signal.SIGINT) is False


def test_signal_pid_delivers_null_signal_to_self() -> None:
    # signal 0 is the existence probe; with no identity guard it is
    # delivered to a live pid (ourselves) and reports success.
    assert gui.signal_pid(os.getpid(), 0) is True


def test_signal_pid_false_for_dead_pid() -> None:
    # A pid that cannot exist: os.kill raises ESRCH -> not delivered.
    assert gui.signal_pid(2_000_000_000, 0) is False


def test_interrupt_unknown_session_errors(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--json", "-s", "nope", "interrupt"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


# -- screenshot: lock/asleep vs permission, with reason codes ---------------

class _FakeQuartz:
    """Minimal stand-in for the bits of Quartz the blocker probe uses."""

    kCGSessionOnConsoleKey = "kCGSSessionOnConsoleKey"

    def __init__(self, session: dict | None, asleep: bool = False) -> None:
        self._session = session
        self._asleep = asleep

    def CGSessionCopyCurrentDictionary(self):  # noqa: N802
        return self._session

    def CGMainDisplayID(self):  # noqa: N802
        return 1

    def CGDisplayIsAsleep(self, _display):  # noqa: N802
        return self._asleep


def _with_quartz(monkeypatch: pytest.MonkeyPatch, fake: _FakeQuartz) -> None:
    monkeypatch.setitem(sys.modules, "Quartz", fake)


def test_blocker_detects_locked_screen(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _with_quartz(monkeypatch, _FakeQuartz({"CGSSessionScreenIsLocked": True}))
    assert screenshot._macos_capture_blocker() == "locked"


def test_blocker_detects_off_console_as_locked(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _with_quartz(monkeypatch,
                 _FakeQuartz({"kCGSSessionOnConsoleKey": False}))
    assert screenshot._macos_capture_blocker() == "locked"


def test_blocker_detects_asleep_display(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _with_quartz(monkeypatch,
                 _FakeQuartz({"kCGSSessionOnConsoleKey": True}, asleep=True))
    assert screenshot._macos_capture_blocker() == "display_asleep"


def test_blocker_none_when_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_quartz(monkeypatch,
                 _FakeQuartz({"kCGSSessionOnConsoleKey": True}, asleep=False))
    assert screenshot._macos_capture_blocker() is None


def test_blocker_none_when_no_gui_session(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # CGSessionCopyCurrentDictionary -> None (daemon/SSH): unknown, not a
    # blocker we can name, so the caller falls back to the permission hint.
    _with_quartz(monkeypatch, _FakeQuartz(None, asleep=False))
    assert screenshot._macos_capture_blocker() is None


def test_screenshot_error_reason_reaches_json(
        elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    # main() must spread ScreenshotError.reason into the JSON, like it does
    # for RpcError.backtrace and WaitTimeout.state.
    def boom(_name: str) -> dict:
        raise ScreenshotError("screen is locked", reason="locked")

    monkeypatch.setattr(S, "session_info", boom)
    assert cli.main(["--json", "-s", "x", "info"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["reason"] == "locked"
    assert "locked" in out["error"]
