"""Unit tests for the dnd surfaces that need no display and no python-xlib.

Everything here validates before any channel I/O, so it runs everywhere
(macOS included); the real XDND protocol tests live in tests/test_gui.py
behind a Linux + python-xlib gate.
"""

from __future__ import annotations

import pytest

import elate.cli as cli
import elate.script as SC
import elate.session as S
import elate.xdnd as xdnd
from elate.errors import ElateError, UsageError


def _fake_session(ui: str = "gui") -> S.Session:
    return S.Session(name="fake", session_dir="/tmp/elate-fake", emacs="emacs",
                     emacsclient="emacsclient", config="", cols=80, rows=24,
                     created_at=0.0, ui=ui)


# -- dnd_drop validation (all raised before any channel I/O) ------------------

def test_tty_session_usage_error() -> None:
    with pytest.raises(UsageError, match="GUI session"):
        S.dnd_drop(_fake_session(ui="tty"), uris=["file:///a"])


def test_empty_uris_rejected() -> None:
    with pytest.raises(UsageError, match="at least one URI"):
        S.dnd_drop(_fake_session(), uris=[])


def test_uri_validation_rejects_bare_paths() -> None:
    with pytest.raises(UsageError, match="as_uri"):
        S.dnd_drop(_fake_session(), uris=["/tmp/some-file"])


def test_uri_validation_rejects_non_ascii() -> None:
    # text/uri-list is ASCII by spec; an unencoded URI would otherwise
    # crash mid-protocol when the payload is encoded.
    with pytest.raises(UsageError, match="ASCII"):
        S.dnd_drop(_fake_session(), uris=["file:///tmp/naïve.txt"])


def test_action_enum() -> None:
    with pytest.raises(UsageError, match="copy/move"):
        S.dnd_drop(_fake_session(), uris=["file:///a"], action="link")


def test_xy_must_pair() -> None:
    with pytest.raises(UsageError, match="both --x and --y"):
        S.dnd_drop(_fake_session(), uris=["file:///a"], x=10)


def test_xy_excludes_buffer_target() -> None:
    with pytest.raises(UsageError, match="not both"):
        S.dnd_drop(_fake_session(), uris=["file:///a"], x=10, y=10,
                   buffer="*scratch*")


def test_hover_ms_bounds() -> None:
    with pytest.raises(UsageError, match="hover-ms"):
        S.dnd_drop(_fake_session(), uris=["file:///a"], hover_ms=20000)


# -- pointer_action validation ------------------------------------------------

def test_pointer_tty_usage_error() -> None:
    with pytest.raises(UsageError, match="GUI session"):
        S.pointer_action(_fake_session(ui="tty"), "query")


def test_pointer_unknown_action() -> None:
    with pytest.raises(UsageError, match="warp/query"):
        S.pointer_action(_fake_session(), "drag")


def test_pointer_query_takes_no_target() -> None:
    with pytest.raises(UsageError, match="takes no target"):
        S.pointer_action(_fake_session(), "query", buffer="*scratch*")


def test_pointer_warp_needs_target() -> None:
    with pytest.raises(UsageError, match="needs a target"):
        S.pointer_action(_fake_session(), "warp")


def test_pointer_warp_xy_pairing() -> None:
    with pytest.raises(UsageError, match="both --x and --y"):
        S.pointer_action(_fake_session(), "warp", y=5)
    with pytest.raises(UsageError, match="not both"):
        S.pointer_action(_fake_session(), "warp", x=5, y=5, line=3)


def test_pointer_warp_col_needs_line() -> None:
    with pytest.raises(UsageError, match="--col needs --line"):
        S.pointer_action(_fake_session(), "warp", col=5)


def test_xdnd_rejects_non_ascii_uris_itself(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Belt and braces below the session layer: xdnd_drop refuses before
    # touching any display (checked after the import guard, so make the
    # import look successful even without python-xlib installed).
    monkeypatch.setattr(xdnd, "_XLIB_ERROR", None)
    with pytest.raises(xdnd.XdndError, match="ASCII") as exc:
        xdnd.xdnd_drop(":99", 1234, 0, 0, ["file:///tmp/naïve.txt"])
    assert exc.value.reason == "protocol"


# -- xdnd module (no X server needed) -----------------------------------------

def test_uri_list_payload_crlf() -> None:
    payload = xdnd.uri_list_payload(["file:///a", "file:///b"])
    assert payload == b"file:///a\r\nfile:///b\r\n"
    assert payload.endswith(b"\r\n")


def test_import_hint_names_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xdnd, "_XLIB_ERROR", ImportError("no Xlib"))
    with pytest.raises(xdnd.XdndError, match=r"elate\[dnd\]") as exc:
        xdnd.xdnd_drop(":99", 1234, 0, 0, ["file:///a"])
    assert exc.value.reason == "import"


def test_xdnd_error_carries_reason() -> None:
    err = xdnd.XdndError("boom", reason="not-aware")
    assert isinstance(err, ElateError)
    assert err.reason == "not-aware"


# -- CLI parser ---------------------------------------------------------------

def test_cli_dnd_parses_action_dest_separation() -> None:
    args = cli.build_parser().parse_args(
        ["-s", "x", "dnd", "drop", "--uris", "file:///a,file:///b",
         "--action", "move", "--hover"])
    assert args.action == "drop"
    assert args.dnd_action == "move"
    assert args.hover is True
    assert args.uris == "file:///a,file:///b"


def test_cli_dnd_registered() -> None:
    assert cli._COMMANDS["dnd"] is cli.cmd_dnd
    assert cli._COMMANDS["pointer"] is cli.cmd_pointer
    assert cli._COMMANDS["window-info"] is cli.cmd_window_info


def test_cli_pointer_parses() -> None:
    args = cli.build_parser().parse_args(
        ["-s", "x", "pointer", "warp", "--buffer", "*scratch*",
         "--line", "3", "--col", "5"])
    assert args.action == "warp"
    assert (args.buffer, args.line, args.col) == ("*scratch*", 3, 5)


# -- scenario-script step validation ------------------------------------------

def _validate(step: dict) -> None:
    SC._validate_step(step, 0)


def test_script_dnd_step_valid() -> None:
    _validate({"dnd": ["file:///a", "file:///b"], "buffer": "d",
               "line": 5, "col": 10, "action": "move", "hover": True,
               "hover_ms": 100, "allow_rejected": True})
    _validate({"dnd": "file:///one"})


def test_script_dnd_step_rejects_bad_values() -> None:
    with pytest.raises(ElateError, match="URI string or a non-empty list"):
        _validate({"dnd": []})
    with pytest.raises(ElateError, match="not a URI"):
        _validate({"dnd": "/bare/path"})
    with pytest.raises(ElateError, match="ASCII"):
        _validate({"dnd": "file:///tmp/naïve.txt"})
    with pytest.raises(ElateError, match="copy/move"):
        _validate({"dnd": "file:///a", "action": "link"})
    with pytest.raises(ElateError, match="unknown key"):
        _validate({"dnd": "file:///a", "hovering": True})


def test_script_dnd_export_mapping() -> None:
    step = SC._event_step({"event": "dnd", "uris": ["file:///a"],
                           "buffer": "d", "line": 5, "dnd_action": "move",
                           "hover": True})
    assert step == {"dnd": ["file:///a"], "buffer": "d", "line": 5,
                    "action": "move", "hover": True}
    # copy is the default and is not exported
    step = SC._event_step({"event": "dnd", "uris": ["file:///a"],
                           "dnd_action": "copy"})
    assert step == {"dnd": ["file:///a"]}
