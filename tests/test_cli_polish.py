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
import time
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli, crash, diagnostics, gui, sandbox, screenshot
from elate import session as S
from elate.errors import EvalTimeout, ScreenshotError


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
    # (`info` still errors on a missing session; `stop` is now idempotent.)
    assert cli.main(["--json", "-s", "nope", "info"]) == 1
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


# -- window-manager resize detection ----------------------------------------

def test_wm_resize_warning_within_tolerance_is_silent() -> None:
    assert S._wm_resize_warning(90, 28, 90, 28) is None
    assert S._wm_resize_warning(90, 28, 91, 29) is None  # cell rounding


def test_wm_resize_warning_flags_a_tiling_wm() -> None:
    w = S._wm_resize_warning(90, 28, 250, 78)
    assert w is not None
    assert "90x28" in w and "250x78" in w
    assert "tiling window manager" in w


def test_gui_frame_is_titled_for_wm_matching() -> None:
    forms = sandbox._frame_geometry_forms(100, 35, "elate:demo")
    assert any('frame-title-format' in f and 'elate:demo' in f for f in forms)
    # No title arg -> no title form (keeps TTY/other callers untouched).
    assert not any('frame-title-format' in f
                   for f in sandbox._frame_geometry_forms(100, 35))


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


# -- Tier 1: idempotent stop / stop --all / auto-name / older-than / prune ---

def test_stop_missing_session_is_noop_success(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Stopping a session that does not exist is a success no-op, so stop
    # (and stop --all / --replace / cleanup loops) is idempotent.
    assert cli.main(["--json", "stop", "nope"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["stopped"] is False
    assert out["reason"] == "no such session"


def test_stop_all_with_no_running_is_clean(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--human", "stop", "--all"]) == 0
    assert "no running sessions to stop" in capsys.readouterr().out


def test_stop_all_with_name_is_an_error(elate_home: Path) -> None:
    assert cli.main(["--json", "stop", "x", "--all"]) == 1


def test_free_name_is_well_formed(elate_home: Path) -> None:
    name = S._free_name()
    assert name.startswith("elate-")
    assert S._NAME_RE.match(name)


def test_free_name_skips_taken_names(
        elate_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (elate_home / "sessions" / "elate-aaaaaa").mkdir(parents=True)
    tokens = iter(["aaaaaa", "bbbbbb"])
    monkeypatch.setattr(S.secrets, "token_hex", lambda n: next(tokens))
    assert S._free_name() == "elate-bbbbbb"


def test_list_older_than_filters(
        elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"name": "old", "ui": "tty", "status": "stopped",
         "emacs_version": "30.1", "uptime": None, "idle_for": 7200.0},
        {"name": "fresh", "ui": "tty", "status": "stopped",
         "emacs_version": "30.1", "uptime": None, "idle_for": 60.0},
        {"name": "live", "ui": "tty", "status": "running",
         "emacs_version": "30.1", "uptime": 5.0, "idle_for": None},
    ]
    monkeypatch.setattr(S, "list_sessions", lambda: rows)
    assert cli.main(["--json", "list", "--older-than", "1h"]) == 0
    out = json.loads(capsys.readouterr().out)
    # running (idle_for=None) and the fresh stopped one are excluded.
    assert [s["name"] for s in out["sessions"]] == ["old"]


def test_list_renders_dead_signal(
        elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"name": "d", "ui": "tty", "status": "dead",
             "emacs_version": "30.1", "uptime": None, "idle_for": 3.0,
             "signal": "SIGABRT"}]
    monkeypatch.setattr(S, "list_sessions", lambda: rows)
    assert cli.main(["--human", "list"]) == 0
    assert "dead (SIGABRT)" in capsys.readouterr().out


def test_prune_is_a_purge_alias(
        elate_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No names / no --all: the same loud error purge gives.
    assert cli.main(["--json", "prune"]) == 1
    err = json.loads(capsys.readouterr().out)
    assert err["ok"] is False and "--all" in err["error"]
    # prune --all on an empty root is a clean no-op, exactly like purge.
    assert cli.main(["--json", "prune", "--all"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# -- Tier 2: eval --on-timeout sample ---------------------------------------

def test_sample_process_no_pid() -> None:
    assert diagnostics.sample_process(None)["available"] is False
    assert diagnostics.sample_process(0)["available"] is False


def test_sample_process_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _n: None)
    out = diagnostics.sample_process(1234)
    assert out["available"] is False


def test_sample_process_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda n: "/usr/bin/" + n)

    class _Proc:
        returncode = 0
        stdout = "Call graph:\n  2 main\n  2 read_char"
        stderr = ""

    monkeypatch.setattr(diagnostics.subprocess, "run",
                        lambda *a, **k: _Proc())
    out = diagnostics.sample_process(1234)
    assert out["available"] is True
    assert "Call graph" in out["backtrace"]


def test_eval_timeout_sample_reaches_json(
        elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    # main() must spread EvalTimeout.sample into the JSON, like it does for
    # RpcError.backtrace / WaitTimeout.state / ScreenshotError.reason.
    def boom(_args: object) -> None:
        raise EvalTimeout("timed out", sample={"available": True,
                                               "tool": "sample",
                                               "backtrace": "frames"})

    monkeypatch.setitem(cli._COMMANDS, "eval", boom)
    assert cli.main(["--json", "-s", "x", "eval", "(foo)"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["sample"]["backtrace"] == "frames"


# -- Tier 3: crash report + fatal-signal detection --------------------------

def test_signal_from_log_reads_fatal_line(tmp_path: Path) -> None:
    log = tmp_path / "emacs-stderr.log"
    log.write_text("booting\nFatal error 6: Aborted\nmore\n", encoding="utf-8")
    assert crash.signal_from_log(log) == "SIGABRT"


def test_signal_from_log_none_when_absent(tmp_path: Path) -> None:
    log = tmp_path / "e.log"
    log.write_text("nothing fatal here\n", encoding="utf-8")
    assert crash.signal_from_log(log) is None
    assert crash.signal_from_log(tmp_path / "missing.log") is None
    assert crash.signal_from_log(None) is None


def _write_ips(reports: Path, name: str, pid: int, code: int,
               indicator: str) -> Path:
    reports.mkdir(parents=True, exist_ok=True)
    ips = reports / name
    header = json.dumps({"app_name": "Emacs", "bug_type": "309"})
    body = json.dumps({"pid": pid, "procName": "Emacs",
                       "termination": {"namespace": "SIGNAL", "code": code,
                                       "indicator": indicator}})
    ips.write_text(header + "\n" + body, encoding="utf-8")
    return ips


def test_read_ips_parses_header_body_and_signal(tmp_path: Path) -> None:
    ips = _write_ips(tmp_path, "Emacs-2026.ips", 4242, 6, "Abort trap: 6")
    header, body = crash._read_ips(ips)
    assert header["app_name"] == "Emacs"
    assert body["pid"] == 4242
    # signal maps from the termination code (no faulting-frame parse).
    assert crash._ips_signal(header, body) == "SIGABRT"


def test_find_crash_report_attributes_by_pid(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    _write_ips(reports, "Emacs-mine.ips", 4242, 11, "Segmentation fault: 11")
    _write_ips(reports, "Emacs-other.ips", 9999, 6, "Abort trap: 6")
    out = crash._find_macos(4242, "Emacs", since=0.0)
    assert out is not None
    assert out["path"].endswith("Emacs-mine.ips")
    assert out["signal"] == "SIGSEGV"
    # A pid with no report → None (correct attribution under parallel crashes).
    assert crash._find_macos(1, "Emacs", since=0.0) is None


def test_find_macos_skips_reports_older_than_since(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    ips = _write_ips(reports, "Emacs-old.ips", 4242, 6, "Abort trap: 6")
    old = time.time() - 10_000
    os.utime(ips, (old, old))
    assert crash._find_macos(4242, "Emacs", since=time.time() - 100) is None


# -- Tier 4: process-group reaping start-time filter ------------------------

def _lstart(at: float) -> str:
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(at))


def _fake_ps(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    class _Proc:
        returncode = 0
        stderr = ""

        def __init__(self) -> None:
            self.stdout = stdout

    monkeypatch.setattr(gui.subprocess, "run", lambda *a, **k: _Proc())


def test_group_members_filters_by_start_time(
        monkeypatch: pytest.MonkeyPatch) -> None:
    base = time.time()
    out = (f"111 {_lstart(base + 5)}\n"      # fresh: after `since`
           f"222 {_lstart(base - 10_000)}\n")  # stale: predates the session
    _fake_ps(monkeypatch, out)
    members = gui._group_members(4242, since=base)
    assert [pid for pid, _ in members] == [111]


def test_count_group_excludes_leader(
        monkeypatch: pytest.MonkeyPatch) -> None:
    base = time.time()
    out = f"4242 {_lstart(base + 1)}\n333 {_lstart(base + 2)}\n"
    _fake_ps(monkeypatch, out)
    # Excluding Emacs itself leaves one leaked grandchild.
    assert gui.count_group(4242, base, exclude={4242}) == 1


def test_reap_group_kills_filtered_members(
        monkeypatch: pytest.MonkeyPatch) -> None:
    base = time.time()
    _fake_ps(monkeypatch, f"111 {_lstart(base + 5)}\n")
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(gui.os, "kill",
                        lambda pid, sig: killed.append((pid, sig)))
    assert gui.reap_group(4242, base) == [111]
    assert killed == [(111, 9)]


def test_reap_group_and_count_group_tolerate_no_pgid() -> None:
    assert gui.reap_group(None, 0.0) == []
    assert gui.count_group(None, 0.0) == 0
