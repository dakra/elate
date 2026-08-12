"""GUI session integration tests: a real windowed Emacs on the desktop.

macOS is the primary target (windows appear briefly during the run).
The Linux/Xvfb/X11 paths are written but cannot be exercised here; their
tests are skipped off-Linux. PNG screenshot tests skip gracefully when
the Screen Recording permission is missing (probed once, no prompts).

Semantic mouse tests run against BOTH a GUI and a TTY session -- the
mechanism is UI-independent by design.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import pytest

from elate import cli
from elate import gui as G
from elate import screenshot as shot
from elate import script as SC
from elate import session as S
from elate import xdnd
from elate.errors import ElateError, RpcError

from _gui_probe import GUI_UNAVAILABLE_REASON

HAVE_DEPS = bool(shutil.which("emacs") and shutil.which("emacsclient"))

# One skip gate, probed once per run: tool availability plus a working
# window server (see _gui_probe; e.g. an SSH login on macOS has none and
# GUI Emacs would die at startup -- skip honestly instead).
GUI_SKIP_REASON = (
    None if HAVE_DEPS else "GUI tests need emacs/emacsclient on PATH"
) or GUI_UNAVAILABLE_REASON

pytestmark = pytest.mark.skipif(
    GUI_SKIP_REASON is not None,
    reason=str(GUI_SKIP_REASON),
)

NAME = f"g{os.getpid()}"
TTY_NAME = f"{NAME}t"

# Probe the Screen Recording permission exactly once, without triggering
# any system prompt (CGPreflightScreenCaptureAccess only checks).
SCREEN_RECORDING = shot.screen_recording_allowed()


def _tiling_wm() -> str | None:
    """An auto-tiling window manager that will override frame geometry."""
    if sys.platform != "darwin":
        return None
    import subprocess
    try:
        procs = subprocess.run(["ps", "axo", "comm"], capture_output=True,
                               text=True, timeout=10).stdout.lower()
    except Exception:
        return None
    for wm in ("aerospace", "yabai", "amethyst"):
        if wm in procs:
            return wm
    return None


# Under a tiling WM every new window is immediately re-tiled, so the final
# frame geometry is the WM's decision, not Emacs's. Geometry tests then
# verify the plumbing (frame alists / resize request) instead of pixels.
TILING_WM = _tiling_wm()


@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elgui-")
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
def gui_sess(elate_home: str) -> Iterator[S.Session]:
    session = S.start_session(NAME, ui="gui", cols=90, rows=30)
    try:
        yield session
    finally:
        try:
            S.stop_session(NAME)
        except Exception:
            G.terminate_pid(session.emacs_pid)


@pytest.fixture(scope="module")
def tty_sess(elate_home: str) -> Iterator[S.Session]:
    session = S.start_session(TTY_NAME, ui="tty", cols=100, rows=30)
    try:
        yield session
    finally:
        try:
            S.stop_session(TTY_NAME)
        except Exception:
            session.raw().kill_server()


def wait_frame_size(sess: S.Session, cols: int, rows: int,
                    timeout: float = 8.0) -> str:
    """Poll until the GUI frame reaches COLS x ROWS (NS applies geometry
    asynchronously: the agent can answer before the frame settles)."""
    deadline = time.monotonic() + timeout
    value = ""
    while time.monotonic() < deadline:
        value = sess.semantic().eval_form(
            "(list (frame-width) (frame-height))")["value"]
        if value == f"({cols} {rows})":
            return value
        time.sleep(0.1)
    return value


def assert_frame_geometry(sess: S.Session, cols: int, rows: int) -> None:
    """The requested geometry reached the frame alists; and, when no
    tiling WM interferes, the actual frame."""
    data = sess.semantic().eval_form(
        "(list (alist-get 'width default-frame-alist)"
        " (alist-get 'height default-frame-alist)"
        " (alist-get 'width initial-frame-alist)"
        " (alist-get 'height initial-frame-alist))")
    assert data["value"] == f"({cols} {rows} {cols} {rows})"
    if TILING_WM:
        return  # final geometry is the window manager's decision
    assert wait_frame_size(sess, cols, rows) == f"({cols} {rows})"


def reset_scratch(sess: S.Session) -> None:
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (delete-other-windows)'
        ' (erase-buffer))'
    )


def setup_button(sess: S.Session) -> int:
    """A buffer 'btn' shown in a window with one button; returns its pos."""
    data = sess.semantic().eval_form(
        '(progn'
        ' (switch-to-buffer "btn") (delete-other-windows) (erase-buffer)'
        ' (setq elate-test-clicked nil)'
        ' (insert "some text ")'
        " (insert-button \"PRESS\" 'action"
        "  (lambda (_) (setq elate-test-clicked 'pressed)))"
        ' (insert " more\\n") (goto-char (point-min))'
        ' (button-start (next-button (point-min))))'
    )
    assert data["error"] is None, data
    return int(data["value"])


def clicked_value(sess: S.Session) -> str:
    return sess.semantic().eval_form("elate-test-clicked")["value"]


# -- lifecycle / info ---------------------------------------------------------

def test_gui_start_and_info(gui_sess: S.Session) -> None:
    assert gui_sess.ui == "gui"
    assert gui_sess.emacs_pid and G.pid_alive(gui_sess.emacs_pid)
    info = S.session_info(NAME)
    assert info["ui"] == "gui"
    assert info["alive"] is True
    assert info["busy"] is False
    assert info["pid"] == gui_sess.emacs_pid
    assert info["emacs_version"]
    assert info["size"] == [90, 30]
    assert info["tmux_socket"] is None
    assert "headless" not in info  # not headless on this machine


def test_gui_semantic_channel(gui_sess: S.Session) -> None:
    data = gui_sess.semantic().eval_form("(+ 20 22)")
    assert data["error"] is None and data["value"] == "42"
    data = gui_sess.semantic().eval_form("(display-graphic-p)")
    assert data["value"] == "t"
    state = gui_sess.semantic().rpc("state")
    assert state["buffer"]
    assert isinstance(state["windows"], dict)


def test_gui_frame_geometry(gui_sess: S.Session) -> None:
    assert_frame_geometry(gui_sess, 90, 30)


def test_gui_live_resize(gui_sess: S.Session) -> None:
    result = S.resize_session(gui_sess, 100, 32)
    assert result["cols"] == 100 and result["rows"] == 32
    assert "width" in result and "height" in result  # agent reported back
    assert S.session_info(NAME)["size"] == [100, 32]
    if not TILING_WM:
        assert wait_frame_size(gui_sess, 100, 32) == "(100 32)"
    S.resize_session(gui_sess, 90, 30)  # restore for later assertions


def test_mixed_tty_gui_list(gui_sess: S.Session, tty_sess: S.Session) -> None:
    listed = {e["name"]: e for e in S.list_sessions()}
    assert listed[NAME]["ui"] == "gui"
    assert listed[NAME]["status"] == "running"
    assert listed[TTY_NAME]["ui"] == "tty"
    assert listed[TTY_NAME]["status"] == "running"
    # CLI list renders the UI column for both.
    assert cli.main(["--json", "list"]) == 0


# -- raw-only operations on GUI ----------------------------------------------

def test_gui_raw_keys_clear_error(gui_sess: S.Session,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "keys", "C-g", "--raw"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is False
    assert "GUI session" in out["error"]
    assert "semantic" in out["error"]


def test_gui_semantic_keys_work(gui_sess: S.Session) -> None:
    reset_scratch(gui_sess)
    gui_sess.semantic().rpc("keys", "g u i RET x", "macro")
    data = gui_sess.semantic().rpc("buffer", "*scratch*")
    assert data["text"] == "gui\nx"


def test_gui_type_via_queued_events(gui_sess: S.Session,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    reset_scratch(gui_sess)
    code = cli.main(["--json", "-s", NAME, "type", "typed-on-gui"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["channel"] == "events"
    match = S.wait_text(gui_sess, "typed-on-gui", buffer="*scratch*", timeout=8.0)
    assert match["matched"] == "typed-on-gui"


def test_gui_type_size_cap(gui_sess: S.Session) -> None:
    # Regression (review bug 1): a 30k type used to leave the session busy
    # and unobservable for minutes, then permanently wedged. It must now be
    # rejected up front with an actionable error, leaving the session usable.
    with pytest.raises(ElateError, match="limited to") as exc_info:
        S.deliver_type(gui_sess, "q" * (S.GUI_TYPE_LIMIT + 1))
    assert "insert" in str(exc_info.value)  # points at the eval escape hatch
    assert gui_sess.semantic().ping(timeout=5.0)


def test_gui_type_chunked_delivery(gui_sess: S.Session) -> None:
    # Multi-chunk GUI type: each chunk is waited on, so the semantic
    # channel stays responsive and the full text still arrives in order.
    reset_scratch(gui_sess)
    text = ("0123456789" * 130)[:1300]  # 3 chunks at the 500-char chunk size
    result = S.deliver_type(gui_sess, text)
    assert result["channel"] == "events"
    assert result["chunks"] == 3
    assert result["queued"] == len(text)
    S.wait_idle(gui_sess, timeout=15.0)
    data = gui_sess.semantic().rpc("buffer", "*scratch*")
    assert data["text"] == text


def test_gui_dead_client_reply_does_not_wedge_server(gui_sess: S.Session) -> None:
    # Regression (review bug 1, debugger half): a timed-out probe leaves a
    # dead client socket; when the agent later answers it, the send error
    # inside server.el's filter used to trip debug-on-error and park
    # server.el in a recursive-edit debugger forever (GUI has no raw
    # channel to quit it). A tight elisp loop defeats the in-Emacs
    # with-timeout, so the controller-side timeout kills emacsclient.
    from elate.errors import EvalTimeout
    with pytest.raises(EvalTimeout):
        gui_sess.semantic().eval_form(
            "(let ((d (+ (float-time) 2))) (while (< (float-time) d)))",
            timeout=0.6)
    deadline = time.monotonic() + 15.0
    responsive = False
    while time.monotonic() < deadline:
        if gui_sess.semantic().ping(timeout=2.0):
            responsive = True
            break
        time.sleep(0.2)
    assert responsive, "semantic channel wedged after a reply to a dead client"
    # Backtrace capture must survive the debug-on-error shielding -- twice
    # in a row (no input events in between rearm the debugger latch).
    for marker in ("elate-no-such-fn-a", "elate-no-such-fn-b"):
        data = gui_sess.semantic().eval_form(f"({marker} 42)")
        assert data["error"] is not None
        assert data["backtrace"] and f"({marker} 42)" in data["backtrace"]


def test_oversized_argv_payload_is_clean_transport_error(
        gui_sess: S.Session) -> None:
    # Regression (review bug 2): payloads beyond ARG_MAX used to escape as
    # a raw OSError (E2BIG) traceback from subprocess.run.
    from elate.errors import TransportError
    with pytest.raises(TransportError, match="too large"):
        gui_sess.semantic().eval_form('"' + "x" * 800_000 + '"')
    assert gui_sess.semantic().ping(timeout=5.0)


def test_alternate_editor_not_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A connect failure must surface emacsclient's own error -- not run the
    # caller's $ALTERNATE_EDITOR (or, with ALTERNATE_EDITOR="", silently
    # spawn an emacs --daemon behind elate's back).
    from pathlib import Path

    from elate.errors import TransportError
    from elate.semantic import SemanticChannel

    monkeypatch.setenv("ALTERNATE_EDITOR", "echo POLLUTION-MARKER")
    channel = SemanticChannel(shutil.which("emacsclient"),
                              Path("/tmp/elate-no-such-dir/elate"))
    with pytest.raises(TransportError) as exc_info:
        channel.eval_raw("(+ 1 1)", timeout=5.0)
    assert "POLLUTION-MARKER" not in str(exc_info.value)


# -- semantic mouse (GUI and TTY: the mechanism is UI-independent) ------------

@pytest.fixture(params=["gui", "tty"])
def mouse_sess(request: pytest.FixtureRequest) -> S.Session:
    return request.getfixturevalue("gui_sess" if request.param == "gui"
                                   else "tty_sess")


def test_mouse_click_button_mouse1_follow_link(mouse_sess: S.Session) -> None:
    pos = setup_button(mouse_sess)
    result = S.mouse_event(mouse_sess, action="click", button=1,
                           buffer="btn", pos=pos)
    assert result["events"] == 2 and result["delivered"] == "macro"
    assert result["pos"] == pos
    assert clicked_value(mouse_sess) == "pressed"


def test_mouse_click_button_mouse2_push_button(mouse_sess: S.Session) -> None:
    pos = setup_button(mouse_sess)
    S.mouse_event(mouse_sess, action="click", button=2, buffer="btn", pos=pos)
    assert clicked_value(mouse_sess) == "pressed"


def test_mouse_click_events_delivery(mouse_sess: S.Session) -> None:
    pos = setup_button(mouse_sess)
    result = S.mouse_event(mouse_sess, action="click", button=2,
                           buffer="btn", pos=pos, delivery="events")
    assert result["delivered"] == "events"
    S.wait_idle(mouse_sess, timeout=8.0)
    assert clicked_value(mouse_sess) == "pressed"


def test_mouse_click_by_line_col(mouse_sess: S.Session) -> None:
    pos = setup_button(mouse_sess)
    col = pos - 1  # button is on line 1; columns are 0-based
    S.mouse_event(mouse_sess, action="click", button=2,
                  buffer="btn", line=1, col=col)
    assert clicked_value(mouse_sess) == "pressed"


def test_mouse_double_click(mouse_sess: S.Session) -> None:
    data = mouse_sess.semantic().eval_form(
        '(progn (switch-to-buffer "dbl") (delete-other-windows) (erase-buffer)'
        ' (insert "hello world\\n") (setq elate-dbl nil)'
        ' (with-current-buffer "dbl"'
        '  (local-set-key [double-mouse-1]'
        '   (lambda (e) (interactive "e")'
        '    (setq elate-dbl (posn-point (event-start e))))))'
        ' t)'
    )
    assert data["error"] is None
    S.mouse_event(mouse_sess, action="double", buffer="dbl", pos=3)
    assert mouse_sess.semantic().eval_form("elate-dbl")["value"] == "3"


def test_mouse_wheel_scrolls(mouse_sess: S.Session) -> None:
    data = mouse_sess.semantic().eval_form(
        '(progn (with-current-buffer (get-buffer-create "wheel")'
        '  (erase-buffer) (dotimes (i 200) (insert (format "line-%d\\n" i))))'
        ' (switch-to-buffer "wheel") (delete-other-windows)'
        ' (goto-char (point-min)) (set-window-start (selected-window) 1)'
        ' (window-start))'
    )
    assert data["value"] == "1"
    S.mouse_event(mouse_sess, action="wheel", direction="down", count=2,
                  buffer="wheel")
    data = mouse_sess.semantic().eval_form("(progn (redisplay) (window-start))")
    down_start = int(data["value"])
    assert down_start > 1
    S.mouse_event(mouse_sess, action="wheel", direction="up", count=1,
                  buffer="wheel")
    data = mouse_sess.semantic().eval_form("(progn (redisplay) (window-start))")
    assert int(data["value"]) < down_start


def test_mouse_drag_selects_region(mouse_sess: S.Session) -> None:
    data = mouse_sess.semantic().eval_form(
        '(progn (switch-to-buffer "drag") (delete-other-windows)'
        ' (erase-buffer) (insert "0123456789abcdef\\n") t)'
    )
    assert data["error"] is None
    result = S.mouse_event(mouse_sess, action="drag", buffer="drag",
                           pos=3, to_pos=9)
    assert result["events"] == 2
    data = mouse_sess.semantic().eval_form(
        '(with-current-buffer "drag"'
        ' (list (region-active-p) (region-beginning) (region-end)))'
    )
    assert data["value"] == "(t 3 9)"


def test_mouse_modeline_click_selects_window(mouse_sess: S.Session) -> None:
    data = mouse_sess.semantic().eval_form(
        '(progn (delete-other-windows) (switch-to-buffer "*scratch*")'
        ' (split-window-below)'
        ' (set-window-buffer (next-window) (get-buffer-create "other-buf"))'
        ' (buffer-name (window-buffer (selected-window))))'
    )
    assert data["value"] == '"*scratch*"'
    result = S.mouse_event(mouse_sess, action="click", button=1,
                           buffer="other-buf", part="mode-line", col=3)
    assert result["area"] == "mode-line"
    data = mouse_sess.semantic().eval_form(
        "(buffer-name (window-buffer (selected-window)))")
    assert data["value"] == '"other-buf"'
    mouse_sess.semantic().eval_form(
        '(progn (delete-other-windows) (switch-to-buffer "*scratch*"))')


def test_mouse_position_not_visible_is_actionable(mouse_sess: S.Session) -> None:
    mouse_sess.semantic().eval_form(
        '(progn (with-current-buffer (get-buffer-create "longbuf")'
        '  (erase-buffer) (dotimes (i 500) (insert (format "row-%d\\n" i))))'
        ' (switch-to-buffer "longbuf") (delete-other-windows)'
        ' (goto-char (point-min)) (set-window-start (selected-window) 1) t)'
    )
    with pytest.raises(RpcError, match="not visible"):
        S.mouse_event(mouse_sess, action="click", buffer="longbuf", pos=3000)


def test_mouse_buffer_not_displayed_is_actionable(mouse_sess: S.Session) -> None:
    mouse_sess.semantic().eval_form('(get-buffer-create "hidden-buf")')
    with pytest.raises(RpcError, match="not displayed"):
        S.mouse_event(mouse_sess, action="click", buffer="hidden-buf", pos=1)


def test_mouse_clamping_semantics(mouse_sess: S.Session) -> None:
    # Out-of-range targets clamp gracefully and the response reports the
    # resolved position (review: verified manually, previously unpinned).
    data = mouse_sess.semantic().eval_form(
        '(progn (switch-to-buffer "clamp") (delete-other-windows)'
        ' (erase-buffer) (insert "abcdefgh\\nsecond\\n")'
        ' (goto-char (point-min)) (point-max))')
    point_max = int(data["value"])  # 17
    # pos past end-of-buffer clamps to point-max
    result = S.mouse_event(mouse_sess, action="click", buffer="clamp", pos=9999)
    assert result["pos"] == point_max
    # line beyond the buffer clamps to the last line
    result = S.mouse_event(mouse_sess, action="click", buffer="clamp", line=999)
    assert result["pos"] == point_max  # trailing newline: EOB line
    # col past end-of-line stops at the line end
    result = S.mouse_event(mouse_sess, action="click", buffer="clamp",
                           line=1, col=500)
    assert result["pos"] == 9  # end of "abcdefgh"


def test_mouse_click_in_narrowed_buffer(mouse_sess: S.Session) -> None:
    # line/col targeting counts within the accessible (narrowed) region.
    data = mouse_sess.semantic().eval_form(
        '(progn (switch-to-buffer "narrowed") (delete-other-windows)'
        ' (erase-buffer) (dotimes (i 6) (insert (format "line-%d\\n" i)))'
        ' (goto-char (point-min)) (forward-line 2)'
        ' (narrow-to-region (point) (progn (forward-line 2) (point)))'
        ' (goto-char (point-min)) (point-min))')
    assert data["error"] is None
    narrowed_min = int(data["value"])
    result = S.mouse_event(mouse_sess, action="click", buffer="narrowed",
                           line=1, col=0)
    assert result["pos"] == narrowed_min  # line 1 = first *accessible* line
    mouse_sess.semantic().eval_form('(with-current-buffer "narrowed" (widen))')


def test_mouse_double_click_selects_word(mouse_sess: S.Session) -> None:
    # The click-count sequence does its real job: a default-bindings
    # double-click selects the word under the pointer.
    data = mouse_sess.semantic().eval_form(
        '(progn (switch-to-buffer "dblword") (delete-other-windows)'
        ' (erase-buffer) (insert "alpha beta gamma\\n")'
        ' (goto-char (point-min)) t)')
    assert data["error"] is None
    S.mouse_event(mouse_sess, action="double", buffer="dblword", pos=8)
    data = mouse_sess.semantic().eval_form(
        '(with-current-buffer "dblword"'
        ' (if (region-active-p)'
        '  (buffer-substring-no-properties (region-beginning) (region-end))'
        '  "NO-REGION"))')
    assert data["value"] == '"beta"'


def test_mouse_wheel_at_buffer_edges(mouse_sess: S.Session) -> None:
    # mwheel handles beginning/end-of-buffer itself: scrolling past either
    # edge is a graceful no-op, not an elisp error.
    mouse_sess.semantic().eval_form(
        '(progn (with-current-buffer (get-buffer-create "wedge")'
        '  (erase-buffer) (dotimes (i 200) (insert (format "w%d\\n" i))))'
        ' (switch-to-buffer "wedge") (delete-other-windows)'
        ' (goto-char (point-min)) (set-window-start (selected-window) 1) t)')
    S.mouse_event(mouse_sess, action="wheel", direction="up", count=3,
                  buffer="wedge")
    data = mouse_sess.semantic().eval_form("(progn (redisplay) (window-start))")
    assert data["value"] == "1"  # still at the top, no error
    mouse_sess.semantic().eval_form(
        '(progn (goto-char (point-max))'
        ' (set-window-start (selected-window) (point-max)) (redisplay) t)')
    S.mouse_event(mouse_sess, action="wheel", direction="down", count=3,
                  buffer="wedge")  # must not signal end-of-buffer


def test_mouse_click_while_minibuffer_prompt_open(mouse_sess: S.Session) -> None:
    # A click into a buffer window while a prompt is active lands normally
    # and leaves the prompt open.
    mouse_sess.semantic().eval_form(
        '(progn (switch-to-buffer "promptclick") (delete-other-windows)'
        ' (erase-buffer) (insert "click here\\n") t)')
    mouse_sess.semantic().rpc("keys", "M-x", "events")
    S.wait_prompt(mouse_sess, timeout=8.0)
    result = S.mouse_event(mouse_sess, action="click", buffer="promptclick",
                           pos=3)
    assert result["pos"] == 3
    state = mouse_sess.semantic().rpc("state")
    assert state["minibuffer-depth"] == 1  # prompt survived the click
    # Cancel: re-select the minibuffer, then a queued C-g aborts it.
    mouse_sess.semantic().eval_form(
        "(select-window (active-minibuffer-window))")
    mouse_sess.semantic().rpc("keys", "C-g", "events")
    S.wait_idle(mouse_sess, timeout=8.0)
    assert mouse_sess.semantic().rpc("state")["minibuffer-depth"] == 0


def test_mouse_click_help_button(mouse_sess: S.Session) -> None:
    # mouse-1 on a *Help* xref button follows the link (help-mode buttons
    # are the everyday target of the follow-link machinery).
    data = mouse_sess.semantic().eval_form(
        '(progn (defun elate-help-target () "See `car\'." nil)'
        " (describe-function 'elate-help-target) t)")
    assert data["error"] is None
    data = mouse_sess.semantic().eval_form(
        '(with-current-buffer "*Help*"'
        ' (save-excursion (goto-char (point-min)) (search-forward "See ")'
        '  (button-start (next-button (point)))))')
    assert data["error"] is None
    S.mouse_event(mouse_sess, action="click", buffer="*Help*",
                  pos=int(data["value"]))
    data = mouse_sess.semantic().eval_form(
        '(with-current-buffer "*Help*"'
        ' (buffer-substring-no-properties 1 (min 30 (point-max))))')
    assert "car is" in data["value"]  # *Help* now describes `car'
    mouse_sess.semantic().eval_form(
        '(progn (delete-other-windows) (switch-to-buffer "*scratch*"))')


def test_mouse_validation_errors(gui_sess: S.Session) -> None:
    with pytest.raises(ElateError, match="destination"):
        S.mouse_event(gui_sess, action="drag", pos=1)
    with pytest.raises(ElateError, match="button"):
        S.mouse_event(gui_sess, action="click", button=5)
    with pytest.raises(ElateError, match="action"):
        S.mouse_event(gui_sess, action="hover")


def test_mouse_coordinate_bounds_rejected(gui_sess: S.Session) -> None:
    # CLI/session layer mirrors the MCP schema bounds: negative or
    # zero-based-where-1-based coordinates fail with a friendly error
    # instead of a raw elisp wholenump complaint (review robustness 4).
    with pytest.raises(ElateError, match="col must be >= 0"):
        S.mouse_event(gui_sess, action="click", line=1, col=-5)
    with pytest.raises(ElateError, match="pos must be >= 1"):
        S.mouse_event(gui_sess, action="click", pos=0)
    with pytest.raises(ElateError, match="line must be >= 1"):
        S.mouse_event(gui_sess, action="click", line=0)
    with pytest.raises(ElateError, match="to_line must be >= 1"):
        S.mouse_event(gui_sess, action="drag", pos=1, to_line=-1)
    with pytest.raises(ElateError, match="count must be"):
        S.mouse_event(gui_sess, action="wheel", count=100000)


def test_mouse_cli(gui_sess: S.Session,
                   capsys: pytest.CaptureFixture[str]) -> None:
    pos = setup_button(gui_sess)
    code = cli.main(["--json", "-s", NAME, "mouse", "click", "--button", "2",
                     "--buffer", "btn", "--pos", str(pos)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True and out["events"] == 2
    assert clicked_value(gui_sess) == "pressed"


# -- PNG screenshots ----------------------------------------------------------

def test_gui_screenshot_png(gui_sess: S.Session, tmp_path,
                            capsys: pytest.CaptureFixture[str]) -> None:
    if SCREEN_RECORDING is False:
        pytest.skip("Screen Recording permission not granted (probed once); "
                    "grant it to the terminal app to run this test")
    gui_sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (delete-other-windows) t)')
    out_file = tmp_path / "shot.png"
    code = cli.main(["--json", "-s", NAME, "screenshot", "-o", str(out_file)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["ok"] is True and out["format"] == "png"
    raw = out_file.read_bytes()
    assert raw.startswith(shot.PNG_MAGIC)
    # Plausible dimensions: a 90x30-character frame is hundreds of px.
    assert out["width"] >= 300 and out["height"] >= 200
    assert (out["width"], out["height"]) == shot.png_dimensions(out_file)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS permission model")
def test_gui_screenshot_permission_denied_is_actionable(
        gui_sess: S.Session, tmp_path,
        capsys: pytest.CaptureFixture[str]) -> None:
    if SCREEN_RECORDING is not False:
        pytest.skip("Screen Recording permission is granted; the denial "
                    "path cannot be exercised")
    # The permission is probed before any capture attempt, so this errors
    # without ever popping a system dialog.
    code = cli.main(["--json", "-s", NAME, "screenshot",
                     "-o", str(tmp_path / "no.png")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is False
    assert "Screen Recording" in out["error"]
    assert "System Settings" in out["error"]


def test_macos_window_id_resolves(gui_sess: S.Session) -> None:
    if sys.platform != "darwin":
        pytest.skip("Quartz window lookup is macOS-only")
    window_id = shot.macos_window_id(gui_sess.emacs_pid)
    assert isinstance(window_id, int) and window_id > 0
    assert shot.macos_window_id(99999999) is None


def test_macos_window_id_two_sessions_distinct(elate_home: str,
                                               gui_sess: S.Session) -> None:
    # Two concurrent GUI sessions resolve to different windows: the
    # owner-pid match really disambiguates (review test gap).
    if sys.platform != "darwin":
        pytest.skip("Quartz window lookup is macOS-only")
    name2 = f"{NAME}two"
    sess2 = S.start_session(name2, ui="gui", config="bare", cols=80, rows=24)
    try:
        id1 = shot.macos_window_id(gui_sess.emacs_pid)
        id2 = shot.macos_window_id(sess2.emacs_pid)
        assert id1 and id2
        assert id1 != id2
    finally:
        S.stop_session(name2)


def test_screenshot_ansi_rejected_on_gui(gui_sess: S.Session,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "screenshot", "--ansi"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "TTY" in out["error"]


# -- window-info and the real pointer ------------------------------------------

def test_window_info_gui(gui_sess: S.Session) -> None:
    reset_scratch(gui_sess)
    info = S.window_info(gui_sess)
    frame = info["frames"][0]
    assert frame["selected"] is True and frame["graphic"] is True
    assert frame["units"] == "pixels"
    assert frame["window-system"] in ("x", "ns", "pgtk")
    # Ids are ints or null -- never the decimal strings frame-parameter
    # prints; on X11 the outer id is what an external XDND client targets.
    for key in ("outer-window-id", "window-id"):
        assert frame[key] is None or isinstance(frame[key], int)
    if frame["window-system"] == "x":
        assert isinstance(frame["outer-window-id"], int)
    assert frame["char-width"] > 1 and frame["char-height"] > 1
    ol, ot, orr, ob = frame["outer-edges"]
    assert orr > ol and ob > ot
    win = next(w for w in frame["windows"] if w["selected"])
    wl, wt, wr, wb = win["edges"]
    assert wr > wl and wb > wt
    # A window's absolute pixel edges sit inside its frame's outer edges.
    assert ol <= wl and wr <= orr and ot <= wt and wb <= ob


def test_pointer_warp_query_roundtrip(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    frame = S.window_info(gui_sess)["frames"][0]
    if sys.platform == "darwin" and min(frame["outer-edges"][:2]) < 0:
        # macOS declines warps to displays at negative global coordinates
        # (the agent now signals instead of no-opping); where the frame
        # opens depends on the desktop's monitor arrangement.
        pytest.skip("frame sits on a display at negative global "
                    "coordinates; macOS cannot warp the pointer there")
    before = S.pointer_action(gui_sess, "query")
    time.sleep(0.4)
    now = S.pointer_action(gui_sess, "query")
    if (before["x"], before["y"]) != (now["x"], now["y"]):
        # A physical mouse in motion overrides warps instantly; that is
        # a live human, not a bug. CI machines never hit this.
        pytest.skip("the physical pointer is moving (a human is using "
                    "this desktop); warps cannot stick")
    sem.eval_form(
        '(progn (switch-to-buffer "*scratch*") (delete-other-windows)'
        ' (erase-buffer)'
        ' (dotimes (i 6) (insert (format "line-%d-abcdefghij\\n" (1+ i)))))')
    try:
        landed = S.pointer_action(gui_sess, "warp", buffer="*scratch*",
                                  line=3, col=5)
        assert (landed["buffer"], landed["line"], landed["col"]) \
            == ("*scratch*", 3, 5)
        # The reply IS a fresh query: the real pointer moved.
        again = S.pointer_action(gui_sess, "query")
        assert (again["x"], again["y"]) == (landed["x"], landed["y"])
        # The landing point sits inside the window's absolute pixel edges.
        info = S.window_info(gui_sess)
        win = next(w for w in info["frames"][0]["windows"] if w["selected"])
        left, top, right, bottom = win["edges"]
        assert left <= landed["x"] < right and top <= landed["y"] < bottom
        # Root-absolute x/y warps work too.
        moved = S.pointer_action(gui_sess, "warp", x=landed["x"] + 3,
                                 y=landed["y"])
        assert moved["x"] == landed["x"] + 3
    finally:
        reset_scratch(gui_sess)
        if isinstance(before.get("x"), int):  # leave the desktop as found
            try:
                S.pointer_action(gui_sess, "warp",
                                 x=before["x"], y=before["y"])
            except ElateError:
                pass  # original spot may be un-warpable (other display)


def test_pointer_warp_invisible_position_errors(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    sem.eval_form(
        '(progn (switch-to-buffer "*scratch*") (delete-other-windows)'
        ' (erase-buffer) (dotimes (_ 200) (insert "filler\\n"))'
        ' (goto-char (point-min)) (redisplay))')
    try:
        with pytest.raises(RpcError, match="not visible"):
            S.pointer_action(gui_sess, "warp", buffer="*scratch*", line=200)
    finally:
        reset_scratch(gui_sess)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="Xvfb headless GUI is Linux-only")
def test_headless_xvfb_session(elate_home: str) -> None:  # pragma: no cover
    name = f"{NAME}xv"
    sess = S.start_session(name, ui="gui", headless=True, cols=80, rows=24)
    try:
        assert sess.display and sess.display.startswith(":")
        assert sess.xvfb_pid and G.pid_alive(sess.xvfb_pid)
        assert sess.semantic().eval_form("(display-graphic-p)")["value"] == "t"
        info = S.session_info(name)
        assert info["headless"] is True and info["display"] == sess.display
    finally:
        S.stop_session(name)
    assert not G.pid_alive(sess.xvfb_pid)


def test_headless_rejected_on_macos(elate_home: str) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS-only rejection")
    with pytest.raises(ElateError, match="Linux-only"):
        S.start_session(f"{NAME}hx", ui="gui", headless=True)
    assert not (S.sessions_root() / f"{NAME}hx" / "session.json").exists() or \
        S.load_session(f"{NAME}hx").status != "running"


# -- XDND: real drag-and-drop (Linux/X11 + python-xlib only) --------------------
#
# The one CI leg that can run these is "Linux GUI (Xvfb)" (it installs the
# elate[dnd] extra). Everything protocol-level below goes through
# S.dnd_drop -> elate.xdnd against the frame's real X window.

XDND_SKIP = (
    None if sys.platform.startswith("linux")
    else "XDND drops are X11-only (Linux)"
) or (None if importlib.util.find_spec("Xlib") is not None
      else "XDND tests need python-xlib (install the elate[dnd] extra)")

xdnd_only = pytest.mark.skipif(XDND_SKIP is not None, reason=str(XDND_SKIP))

# Observe received drops at the dnd.el layer: the handler fires only after
# the full path (C event dispatch -> special-event-map -> x-dnd.el ->
# dnd-protocol-alist) ran, which is exactly what these tests exist to prove.
DROP_RECORDER = (
    "(progn"
    " (setq elate-test-drops nil)"
    " (defvar elate-test-saved-dnd dnd-protocol-alist)"
    " (defun elate-test-record-drop (url &optional action)"
    "   (push (list url action (buffer-name)) elate-test-drops)"
    "   'private)"
    " (setq dnd-protocol-alist"
    "       '((\"\\\\`file:\" . elate-test-record-drop))))"
)

RESTORE_RECORDER = "(setq dnd-protocol-alist elate-test-saved-dnd)"


def _drop_target(sem: Any, name: str = "drop-target") -> None:
    sem.eval_form(
        f'(progn (switch-to-buffer "{name}") (delete-other-windows)'
        ' (erase-buffer)'
        ' (dotimes (i 8) (insert (format "row-%d\\n" i))))')


def _drops(sem: Any) -> str:
    return sem.eval_form('(format "%S" (reverse elate-test-drops))')["value"]


@xdnd_only
def test_dnd_drop_two_uris_dispatches_handlers(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    _drop_target(sem)
    sem.eval_form(DROP_RECORDER)
    try:
        data = S.dnd_drop(gui_sess, uris=["file:///tmp/elate-a",
                                          "file:///tmp/elate-b"],
                          buffer="drop-target", line=3, col=2)
        assert data["status"] == "accepted"
        assert data["dropped"] is True and data["finished"] is True
        assert data["finished-success"] is True
        assert data["served-selection"] is True
        assert data["in-debugger"] is False
        drops = _drops(sem)
        assert "file:///tmp/elate-a" in drops
        assert "file:///tmp/elate-b" in drops
        assert drops.index("elate-a") < drops.index("elate-b")  # in order
        # x-dnd dispatches dnd.el handlers with action `private` regardless
        # of the proposed XDND action (copy/move live at the protocol
        # level only).
        assert "private" in drops
        assert "drop-target" in drops  # dispatched in the target's buffer
    finally:
        sem.eval_form(RESTORE_RECORDER)
        reset_scratch(gui_sess)


@xdnd_only
def test_dnd_move_action_reaches_handler(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    _drop_target(sem)
    sem.eval_form(DROP_RECORDER)
    try:
        data = S.dnd_drop(gui_sess, uris=["file:///tmp/elate-m"],
                          buffer="drop-target", line=2, action="move")
        assert data["status"] == "accepted" and data["finished"] is True
        # The move action is protocol-level; the dnd.el handler still
        # fires (with action `private`, see the two-uris test above).
        assert "elate-m" in _drops(sem)
    finally:
        sem.eval_form(RESTORE_RECORDER)
        reset_scratch(gui_sess)


@xdnd_only
def test_dnd_drop_opens_local_file(gui_sess: S.Session, tmp_path) -> None:
    # Stock dnd-protocol-alist: the default dnd-open-local-file handler
    # visits the dropped file -- the full end-to-end a user would see.
    target = tmp_path / "dropped-file.txt"
    target.write_text("dropped payload\n", encoding="utf-8")
    uri = target.resolve().as_uri()
    sem = gui_sess.semantic()
    _drop_target(sem)
    try:
        data = S.dnd_drop(gui_sess, uris=[uri], buffer="drop-target", line=2)
        assert data["finished"] is True and data["in-debugger"] is False
        visiting = sem.eval_form(
            f'(and (find-buffer-visiting "{target}") t)')["value"]
        assert visiting == "t"
    finally:
        sem.eval_form(f'(let ((b (find-buffer-visiting "{target}")))'
                      ' (when b (kill-buffer b)))')
        reset_scratch(gui_sess)


@xdnd_only
def test_dnd_hover_status_without_drop(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    _drop_target(sem)
    sem.eval_form(DROP_RECORDER)
    try:
        data = S.dnd_drop(gui_sess, uris=["file:///tmp/elate-h"],
                          buffer="drop-target", line=2, hover=True,
                          hover_ms=200)
        assert data["status"] == "accepted"
        assert data["dropped"] is False and data["finished"] is False
        assert "elate-h" not in _drops(sem)  # nothing was dropped
        # The Leave reset x-dnd's state: a follow-up real drop works.
        data = S.dnd_drop(gui_sess, uris=["file:///tmp/elate-h"],
                          buffer="drop-target", line=2)
        assert data["finished"] is True
        assert "elate-h" in _drops(sem)
    finally:
        sem.eval_form(RESTORE_RECORDER)
        reset_scratch(gui_sess)


@xdnd_only
def test_dnd_rejected_by_target(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    _drop_target(sem)
    sem.eval_form(DROP_RECORDER)
    sem.eval_form("(progn"
                  " (defvar elate-test-saved-tf x-dnd-test-function)"
                  " (setq x-dnd-test-function (lambda (_w _a _t) nil)))")
    try:
        data = S.dnd_drop(gui_sess, uris=["file:///tmp/elate-r"],
                          buffer="drop-target", line=2)
        assert data["status"] == "rejected"
        assert data["dropped"] is False and data["finished"] is False
        assert "elate-r" not in _drops(sem)
    finally:
        sem.eval_form("(setq x-dnd-test-function elate-test-saved-tf)")
        sem.eval_form(RESTORE_RECORDER)
        reset_scratch(gui_sess)


@xdnd_only
def test_dnd_target_not_xdnd_aware(gui_sess: S.Session) -> None:
    from Xlib import display as xdisplay
    d = xdisplay.Display(os.environ["DISPLAY"])
    try:
        root_id = d.screen().root.id
    finally:
        d.close()
    with pytest.raises(xdnd.XdndError) as exc:
        xdnd.xdnd_drop(os.environ["DISPLAY"], root_id, 10, 10,
                       ["file:///tmp/elate-x"], timeout=5.0)
    assert exc.value.reason == "not-aware"
    assert "window-info" in str(exc.value)


@xdnd_only
def test_dnd_headless_session_uses_recorded_display(
        elate_home: str) -> None:
    # The module fixture exercises the os.environ DISPLAY fallback (under
    # xvfb-run); this one exercises the sess.display path a headless
    # session records in session.json.
    name = f"{NAME}dx"
    sess = S.start_session(name, ui="gui", headless=True, cols=90, rows=30)
    try:
        assert sess.display
        sem = sess.semantic()
        _drop_target(sem)
        sem.eval_form(DROP_RECORDER)
        data = S.dnd_drop(sess, uris=["file:///tmp/elate-hl"],
                          buffer="drop-target", line=2)
        assert data["display"] == sess.display
        assert data["finished"] is True
        assert "elate-hl" in _drops(sem)
    finally:
        S.stop_session(name)


@xdnd_only
def test_dnd_script_verb_runs(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    _drop_target(sem, "script-drop")
    sem.eval_form(DROP_RECORDER)
    try:
        script = {
            "name": "dnd-step",
            "steps": [
                {"dnd": ["file:///tmp/elate-s1", "file:///tmp/elate-s2"],
                 "buffer": "script-drop", "line": 2, "col": 0},
                {"assert": {"eval": "(= (length elate-test-drops) 2)"}},
            ],
        }
        result = SC.run_script(script, session=gui_sess)
        assert result["success"] is True, result
    finally:
        sem.eval_form(RESTORE_RECORDER)
        reset_scratch(gui_sess)


@xdnd_only
def test_dnd_handler_error_surfaces_in_debugger(gui_sess: S.Session) -> None:
    sem = gui_sess.semantic()
    _drop_target(sem)
    sem.eval_form("(progn"
                  " (defvar elate-test-saved-dnd-err dnd-protocol-alist)"
                  " (defun elate-test-err-drop (_url &optional _action)"
                  "   (error \"elate dnd handler boom\"))"
                  " (setq dnd-protocol-alist"
                  "       '((\"\\\\`file:\" . elate-test-err-drop)))"
                  " (setq debug-on-error t))")
    try:
        data = S.dnd_drop(gui_sess, uris=["file:///tmp/elate-e"],
                          buffer="drop-target", line=2, timeout=6.0)
        # Emacs catches drop-handler errors inside x-dnd (they never reach
        # the debugger, even with debug-on-error) and reports the failure
        # in XdndFinished's success bit; the error text goes to *Messages*.
        # The in-debugger field stays load-bearing for handlers that park
        # a recursive edit some other way; here it must simply be sane.
        assert data["finished"] is True
        assert data["finished-success"] is False
        msgs = sem.eval_form(
            '(with-current-buffer (messages-buffer)'
            ' (buffer-substring-no-properties (point-min) (point-max)))'
        )["value"]
        assert "elate dnd handler boom" in msgs
        if data["in-debugger"]:  # some port/version routed it here instead
            aborted = S.debug_session(gui_sess, "abort")
            assert aborted["depth-after"] == 0
    finally:
        sem.eval_form("(progn (setq debug-on-error nil)"
                      " (setq dnd-protocol-alist elate-test-saved-dnd-err))")
        reset_scratch(gui_sess)


# -- MCP: gui sessions through the server --------------------------------------

def _mcp_call(elate_home: str, tool: str, args: dict[str, Any]):
    """One tool call against a fresh stdio server; returns raw content list."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def main():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "elate.cli", "mcp"],
            env={**os.environ, "ELATE_HOME": elate_home},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as cs:
                await cs.initialize()
                result = await cs.call_tool(tool, args)
                return result.content

    return anyio.run(main)


def test_mcp_screenshot_gui_image_content(elate_home: str,
                                          gui_sess: S.Session) -> None:
    content = _mcp_call(elate_home, "elate_screenshot", {"session": NAME})
    if SCREEN_RECORDING is False:
        assert content[0].type == "text"
        payload = json.loads(content[0].text)
        assert payload["ok"] is False
        assert "Screen Recording" in payload["error"]
        return
    assert len(content) == 2
    assert content[0].type == "text"
    payload = json.loads(content[0].text)
    assert payload["ok"] is True
    assert payload["width"] >= 300 and payload["height"] >= 200
    assert os.path.isfile(payload["path"])  # saved into the session sandbox
    assert content[1].type == "image"
    assert content[1].mimeType == "image/png"
    png = base64.b64decode(content[1].data)
    assert png.startswith(shot.PNG_MAGIC)


def test_mcp_screenshot_tty_stays_text(elate_home: str,
                                       tty_sess: S.Session) -> None:
    content = _mcp_call(elate_home, "elate_screenshot", {"session": TTY_NAME})
    assert len(content) == 1 and content[0].type == "text"
    payload = json.loads(content[0].text)
    assert payload["ok"] is True and "screen" in payload


def test_mcp_mouse_end_to_end(elate_home: str, gui_sess: S.Session) -> None:
    pos = setup_button(gui_sess)
    content = _mcp_call(elate_home, "elate_mouse", {
        "session": NAME, "action": "click", "button": 1,
        "buffer": "btn", "pos": pos,
    })
    payload = json.loads(content[0].text)
    assert payload["ok"] is True, payload
    assert payload["events"] == 2 and payload["buffer"] == "btn"
    assert clicked_value(gui_sess) == "pressed"
    # Validation errors surface as structured ok:false with the elisp hint.
    content = _mcp_call(elate_home, "elate_mouse", {
        "session": NAME, "action": "drag", "pos": 1,
    })
    payload = json.loads(content[0].text)
    assert payload["ok"] is False and "destination" in payload["error"]


def test_mcp_keys_raw_on_gui_fails_clearly(elate_home: str,
                                           gui_sess: S.Session) -> None:
    content = _mcp_call(elate_home, "elate_keys", {
        "session": NAME, "keys": "C-g", "delivery": "raw"})
    payload = json.loads(content[0].text)
    assert payload["ok"] is False
    assert "GUI session" in payload["error"]
    assert "semantic" in payload["error"]


def test_mcp_list_mixed_ui(elate_home: str, gui_sess: S.Session,
                           tty_sess: S.Session) -> None:
    content = _mcp_call(elate_home, "elate_list", {})
    payload = json.loads(content[0].text)
    sessions = {s["name"]: s for s in payload["sessions"]}
    assert sessions[NAME]["ui"] == "gui"
    assert sessions[TTY_NAME]["ui"] == "tty"


# -- pid identity (pid-reuse hazard) -------------------------------------------

def test_gui_identity_recorded(gui_sess: S.Session) -> None:
    # The registry records the process identity at spawn; it matches the
    # live process and round-trips through the registry file.
    assert gui_sess.emacs_identity
    assert G.proc_identity(gui_sess.emacs_pid) == gui_sess.emacs_identity
    assert "emacs" in gui_sess.emacs_identity.lower()
    assert S.load_session(NAME).emacs_identity == gui_sess.emacs_identity


def test_stale_registry_pid_reuse_is_not_killed(elate_home: str) -> None:
    # A registry whose pid was recycled by an unrelated process (GUI Emacs
    # died + was reaped after a controller restart; the OS reused the pid)
    # must read as dead, and stop must never signal the impostor.
    import subprocess
    decoy = subprocess.Popen(["sleep", "60"])
    name = f"{NAME}reuse"
    try:
        root = S.sessions_root() / name
        (root / "log").mkdir(parents=True)
        stale = S.Session(
            name=name, session_dir=str(root), emacs="/usr/bin/emacs",
            emacsclient="/usr/bin/emacsclient", config="minimal",
            cols=80, rows=24, created_at=time.time(), ui="gui",
            status="running", emacs_pid=decoy.pid,
            emacs_identity="Mon Jan  1 00:00:00 2024|/long/gone/Emacs",
        )
        stale.save()
        loaded = S.load_session(name)
        assert loaded.is_alive() is False  # identity mismatch = dead
        result = S.stop_session(name)
        assert result["was_alive"] is False
        time.sleep(0.3)
        assert decoy.poll() is None, "stop_session killed an unrelated process"
        # Same protection without a recorded identity (comm hint fallback).
        stale.emacs_identity = None
        stale.status = "running"
        stale.save()
        assert S.load_session(name).is_alive() is False  # sleep != emacs
        S.stop_session(name)
        time.sleep(0.3)
        assert decoy.poll() is None
    finally:
        decoy.kill()
        decoy.wait()


# -- death / cleanup (keep last: they tear sessions down) ----------------------

def test_stop_during_startup(elate_home: str) -> None:
    # stop racing a not-yet-finished start: "starting" is not alive, so
    # stop hard-kills; the racing start then fails (or completes and the
    # final stop cleans up). Either way nothing survives (review test gap).
    import threading
    name = f"{NAME}race"
    start_error: list[Exception] = []

    def starter() -> None:
        try:
            S.start_session(name, ui="gui", config="bare", cols=80, rows=24)
        except ElateError as exc:
            start_error.append(exc)

    thread = threading.Thread(target=starter)
    thread.start()
    try:
        registry = S.sessions_root() / name / "session.json"
        deadline = time.monotonic() + 15.0
        seen_starting = False
        while time.monotonic() < deadline:
            try:
                if json.loads(registry.read_text())["status"] == "starting":
                    seen_starting = True
                    break
            except (OSError, json.JSONDecodeError, KeyError):
                pass
            time.sleep(0.02)
        assert seen_starting
        S.stop_session(name)
    finally:
        thread.join(timeout=60.0)
    assert not thread.is_alive()
    # Outcome may be either "start failed" or "started anyway"; a final
    # stop must leave nothing running in both cases.
    try:
        S.stop_session(name)
    except ElateError:
        pass
    sess = S.load_session(name)
    assert not sess.is_alive()
    assert sess.status in ("stopped", "failed")
    assert not G.pid_alive(sess.emacs_pid)


def test_gui_crash_detected_and_restartable(elate_home: str) -> None:
    name = f"{NAME}crash"
    sess = S.start_session(name, ui="gui", config="bare", cols=80, rows=24)
    try:
        assert sess.emacs_pid
        os.kill(sess.emacs_pid, 9)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and sess.is_alive():
            time.sleep(0.1)
        # pid_alive must see through the zombie child of this test process.
        assert not sess.is_alive()
        # ... and the exited child is actually reaped (no zombie lingering
        # until a gc-driven subprocess._active sweep happens to run).
        import subprocess
        state = subprocess.run(["ps", "-o", "state=", "-p", str(sess.emacs_pid)],
                               capture_output=True, text=True).stdout.strip()
        assert not state, f"expected the child reaped, ps says {state!r}"
        listed = {e["name"]: e for e in S.list_sessions()}
        assert listed[name]["status"] == "dead"
        # Live-session commands report dead and point at the GUI log.
        with pytest.raises(ElateError, match="not running"):
            sess.require_alive()
        # Restart under the same name cleans the stale sandbox up.
        sess2 = S.start_session(name, ui="gui", config="bare", cols=80, rows=24)
        assert sess2.is_alive()
    finally:
        S.stop_session(name)


def test_gui_bare_config_frame_size(elate_home: str) -> None:
    # bare (-Q) has no init file; geometry comes from a --eval set-frame-size.
    name = f"{NAME}bare"
    sess = S.start_session(name, ui="gui", config="bare", cols=80, rows=24)
    try:
        assert_frame_geometry(sess, 80, 24)
    finally:
        S.stop_session(name)


def test_gui_stop(elate_home: str, gui_sess: S.Session) -> None:
    result = S.stop_session(NAME)
    assert result["stopped"] is True and result["was_alive"] is True
    assert not G.pid_alive(gui_sess.emacs_pid)
    listed = {e["name"]: e for e in S.list_sessions()}
    assert listed[NAME]["status"] == "stopped"
