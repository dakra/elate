"""Region screenshots, pixel probes, image comparison, native menus,
and GUI frame placement.

The crop math and comparison are pure (synthetic frames and images); the
GUI tests open a real window (skipped without a window server or the
Screen Recording permission), the native-menu tests use a TTY session.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli, image
from elate import gui as G
from elate import screenshot as shot
from elate import session as S
from elate.errors import ElateError

from _gui_probe import GUI_UNAVAILABLE_REASON

Image = pytest.importorskip("PIL.Image")

HAVE_TTY = bool(shutil.which("emacs") and shutil.which("tmux")
                and shutil.which("emacsclient"))
needs_tty = pytest.mark.skipif(not HAVE_TTY, reason="needs emacs + tmux")
needs_gui = pytest.mark.skipif(
    GUI_UNAVAILABLE_REASON is not None or not shutil.which("emacs"),
    reason=str(GUI_UNAVAILABLE_REASON or "needs emacs"))
needs_capture = pytest.mark.skipif(
    shot.screen_recording_allowed() is False,
    reason="no Screen Recording permission")


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="el-", dir="/tmp"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# A frame at (100, 50) with a 28px title bar, and two windows.
FRAME = {
    "graphic": True,
    "outer-edges": [100, 50, 500, 378],
    "native-edges": [100, 78, 500, 378],
    "windows": [
        {"buffer": "a", "selected": True, "edges": [100, 78, 500, 228],
         "body-edges": [108, 78, 492, 210]},
        {"buffer": "b", "selected": False, "edges": [100, 228, 500, 378],
         "body-edges": [108, 228, 492, 360]},
    ],
}


def test_crop_box_maps_frame_pixels_to_device_pixels():
    # A Retina capture of the outer window: 2x.
    box, scale, rel = image.crop_box(FRAME, (800, 656), region=(10, 20, 30, 40))
    assert scale == 2.0
    assert box == (20, 2 * (28 + 20), 80, 2 * (28 + 60))
    assert rel == {"x": 10, "y": 20, "w": 30, "h": 40}
    # Window body of buffer b; the selected window by default.
    box, _, rel = image.crop_box(FRAME, (800, 656), buffer="b", window=True)
    assert box == (16, 2 * (228 - 50), 784, 2 * (360 - 50))
    _, _, rel_sel = image.crop_box(FRAME, (800, 656), window=True)
    assert rel_sel == {"x": 8, "y": 0, "w": 384, "h": 132}
    # No crop: the native area (title bar cut off).
    box, _, _ = image.crop_box(FRAME, (800, 656))
    assert box == (0, 56, 800, 656)


def test_crop_box_matches_a_capture_without_decorations():
    # An X11 capture that leaves out the title bar matches native edges.
    box, scale, _ = image.crop_box(FRAME, (400, 300), region=(0, 0, 10, 10))
    assert scale == 1.0 and box == (0, 0, 10, 10)


def test_crop_box_errors():
    with pytest.raises(ElateError, match="outside the frame"):
        image.crop_box(FRAME, (800, 656), region=(1000, 1000, 5, 5))
    with pytest.raises(ElateError, match="no window shows buffer"):
        image.crop_box(FRAME, (800, 656), buffer="zz", window=True)
    with pytest.raises(ElateError, match="X,Y,W,H"):
        image.parse_region("1,2,3")


def test_compare_tolerance_budget_and_diff(tmp_path: Path):
    base = Image.new("RGBA", (10, 10), (10, 20, 30, 255))
    near = base.copy()
    near.putpixel((0, 0), (14, 20, 30, 255))   # off by 4
    far = base.copy()
    for x in range(3):
        far.putpixel((x, 5), (200, 0, 0, 255))
    paths = {}
    for name, img in (("base", base), ("near", near), ("far", far)):
        paths[name] = tmp_path / f"{name}.png"
        img.save(paths[name])
    assert image.compare(paths["near"], paths["base"])["match"] is False
    assert image.compare(paths["near"], paths["base"], tolerance=4)["match"]
    verdict = image.compare(paths["far"], paths["base"],
                            diff_out=tmp_path / "d.png")
    assert verdict["differing_pixels"] == 3 and Path(verdict["diff"]).is_file()
    with Image.open(verdict["diff"]) as diff:
        assert diff.getpixel((1, 5)) == (255, 0, 0, 255)
    assert image.compare(paths["far"], paths["base"], max_diff=3)["match"]
    assert image.compare(paths["far"], paths["base"], max_diff=0.03)["match"]
    small = tmp_path / "small.png"
    Image.new("RGBA", (5, 5)).save(small)
    assert image.compare(small, paths["base"])["reason"] == "size"


def test_pixel_assert_and_image_options_validate():
    from elate import script as SC

    good = {"steps": [
        {"screenshot": "a.png", "window": "*scratch*", "points": True},
        {"assert": {"pixel": {"x": 1, "y": 2, "color": "#a0b0c0",
                              "tolerance": 12, "window": True}}},
        {"assert": {"snapshot": {"name": "s", "region": "0,0,10,10",
                                 "tolerance": 8, "max_diff": 0.01}}},
        {"menu": "choose", "item": "Copy"}, {"menu": "dismiss"}]}
    SC.validate_script(good)
    for bad, match in (
            ({"screenshot": None, "region": "1,2"}, "X,Y,W,H"),
            ({"assert": {"pixel": {"x": 1, "y": 2, "color": "red"}}}, "#rrggbb"),
            ({"assert": {"snapshot": {"name": "s", "of": "state",
                                      "tolerance": 3}}}, "GUI"),
            ({"menu": "choose"}, "needs an \"item\""),
            ({"menu": "open"}, "choose\" or \"dismiss")):
        with pytest.raises(ElateError, match=match):
            SC.validate_script({"steps": [bad]})


# -- native menus (TTY, with interception forced on: same code path) ----------

def _menu_session(name: str) -> S.Session:
    sess = S.start_session(name, cols=80, rows=24)
    sess.semantic().eval_raw("(setq elate-intercept-native-menus t)")
    return sess


def _later(sess: S.Session, form: str) -> None:
    """Run FORM from a timer: outside the request, like a real command."""
    sess.semantic().eval_raw(f"(run-at-time 0.1 nil (lambda () {form}))")


@needs_tty
def test_native_menu_choose_dismiss_and_popups(
        elate_home: Path, capsys: pytest.CaptureFixture[str]):
    sess = _menu_session("nm")
    try:
        sem = sess.semantic()
        _later(sess, "(setq r1 (x-popup-menu t '(\"T\" (\"P\" "
                     "(\"Copy\" . copy) (\"Paste\" . paste)))))")
        assert cli.main(["--json", "-s", "nm", "menu", "choose", "1"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["menu"]["chosen"] == "Paste"
        # choose returns once the caller has resumed.
        assert sem.eval_form("r1")["value"] == "paste"
        _later(sess, "(setq r2 (condition-case nil (x-popup-dialog t "
                     "'(\"Sure?\" (\"Yes\" . y))) (quit 'quit)))")
        S.wait_until(sess, "elate--native-menu", timeout=5)
        kinds = [p["kind"] for p in sem.rpc("popups")["popups"]]
        assert "native-dialog" in kinds
        S.native_menu(sess, "dismiss")
        assert sem.eval_form("r2")["value"] == "quit"
        assert S.native_menu(sess, "show")["menu"] is None
        with pytest.raises(ElateError, match="native menu"):
            S.native_menu(sess, "choose", "Copy", timeout=0.5)
    finally:
        S.stop_session("nm")


@needs_tty
def test_native_menu_keymaps_mouse_dismiss_and_misuse(elate_home: Path):
    sess = _menu_session("nk")
    try:
        sem = sess.semantic()
        sem.eval_raw(
            "(progn (setq km (make-sparse-keymap \"Ctx\"))"
            " (define-key km [a] '(menu-item \"Alpha\" ignore))"
            " (define-key km [off] '(menu-item \"Off\" ignore :enable nil))"
            " (define-key km [sep] '(menu-item \"--\"))"
            " (define-key km [old] (cons \"Old\" (cons \"help\" "
            "   (let ((m (make-sparse-keymap))) (define-key m [o] '(menu-item \"Inner\" ignore)) m))))"
            " (define-key km [flt] (list 'menu-item \"Dyn\" (make-sparse-keymap)"
            "   :filter (lambda (_) (let ((m (make-sparse-keymap)))"
            "     (define-key m [d] '(menu-item \"Made\" ignore)) m)))))")
        _later(sess, "(setq r (x-popup-menu (list 'down-mouse-3 (posn-at-point)) km))")
        menu = S.native_menu(sess, "show", timeout=5)
        S.wait_until(sess, "elate--native-menu", timeout=5)
        menu = S.native_menu(sess, "show")["menu"]
        labels = {i["label"]: i["enabled"] for i in menu["items"]}
        assert labels == {"Alpha": True, "Off": False, "Old > Inner": True,
                          "Dyn > Made": True}
        with pytest.raises(ElateError, match="disabled"):
            S.native_menu(sess, "choose", "Off")
        S.native_menu(sess, "choose", "Made")
        assert sem.eval_form("r")["value"] == "(flt d)"
        # Dismissing a mouse-opened menu returns nil, as the real one does.
        _later(sess, "(setq r (list (x-popup-menu (list 'down-mouse-3 "
                     "(posn-at-point)) km)))")
        S.native_menu(sess, "dismiss")
        assert sem.eval_form("r")["value"] == "(nil)"
        # A menu opened inside a request cannot be answered: fail fast.
        out = sem.eval_form("(x-popup-menu t km)")
        assert "asynchronous delivery" in (out.get("error") or "")
        # A second menu while one is pending: an error, not a real menu.
        _later(sess, "(setq r (x-popup-menu t km))")
        S.wait_until(sess, "elate--native-menu", timeout=5)
        _later(sess, "(setq r2 (condition-case e (x-popup-menu t km) "
                     "(error (error-message-string e))))")
        S.wait_until(sess, "(boundp 'r2)", timeout=5)
        assert "another one is pending" in sem.eval_form("r2")["value"]
        S.native_menu(sess, "dismiss")
    finally:
        S.stop_session("nk")


@needs_gui
def test_c_level_menus_and_dialogs_are_routed(elate_home: Path):
    # A GUI frame: dialogs exist only where popup menus do (as in C).
    sess = S.start_session("nc", ui="gui", cols=60, rows=15)
    try:
        sem = sess.semantic()
        # yes-or-no-p pops its dialog from C when invoked by the mouse.
        _later(sess, "(setq yn (let* ((last-nonmenu-event (list 'mouse-1 "
                     "(posn-at-point))) (last-input-event last-nonmenu-event) (use-dialog-box t)) "
                     "(yes-or-no-p \"Really? \")))")
        menu = S.native_menu(sess, "choose", "Yes", timeout=5)
        assert menu["menu"]["kind"] == "dialog"
        assert sem.eval_form("yn")["value"] == "t"
        # context-menu-mode's down-mouse-3 menu is popped by the key reader.
        sem.eval_raw("(context-menu-mode 1)")
        S.send_events(sess, ["down-mouse-3@1,0"], timeout=5)
        S.wait_until(sess, "elate--native-menu", timeout=5)
        assert S.native_menu(sess, "show")["menu"]["items"]
        S.native_menu(sess, "dismiss")
    finally:
        S.stop_session("nc")


# -- GUI --------------------------------------------------------------------

@needs_gui
@needs_capture
def test_region_window_screenshots_and_pixel(elate_home: Path, tmp_path: Path):
    sess = S.start_session("ri", ui="gui", cols=60, rows=15)
    try:
        sess.semantic().eval_raw(
            "(progn (set-face-background 'default \"#123456\") (redisplay t))")
        px = image.pixel(sess, 5, 5, window=True)
        # Captures are color-managed on macOS: near, not exact.
        want = image.parse_color("#123456")
        assert max(abs(a - b) for a, b in zip(px["rgba"], want)) <= 24
        full = image.gui_screenshot(sess, tmp_path / "full.png")
        assert full["scale"] >= 1 and full["units"]
        win = image.gui_screenshot(sess, tmp_path / "win.png", window=True)
        assert win["region"]["w"] > 0
        assert abs(win["width"] - win["region"]["w"] * win["scale"]) <= 2
        pts = image.gui_screenshot(sess, tmp_path / "pts.png",
                                   region=(0, 0, 40, 20), points=True)
        assert (pts["width"], pts["height"]) == (40, 20)
    finally:
        S.stop_session("ri")


def test_restore_focus_reactivates_the_previous_app(
        elate_home: Path, monkeypatch: pytest.MonkeyPatch):
    # Emacs grabs the focus twice while starting, then leaves it alone.
    seen = iter([7, 7, 99, 7, 99, 99, 99, 99])
    monkeypatch.setattr(G, "frontmost_app_pid", lambda: next(seen, 99))
    calls: list[int] = []
    monkeypatch.setattr(G, "activate_app", lambda pid: calls.append(pid) or True)
    sess = S.Session(name="f", session_dir=str(elate_home / "f"), emacs="e",
                     emacsclient="c", config="minimal", cols=80, rows=24,
                     created_at=0.0)
    (elate_home / "f" / "log").mkdir(parents=True)
    S._restore_focus(sess, front=99, ours={7})
    assert calls == [99, 99, 99]


@needs_gui
def test_monitor_placement_and_background(
        elate_home: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[int, set[int]]] = []
    monkeypatch.setattr(G, "frontmost_app_pid", lambda: 424242)
    monkeypatch.setattr(S, "_restore_focus",
                        lambda sess, front, ours: calls.append((front, ours)))
    sess = S.start_session("pl", ui="gui", cols=60, rows=15, monitor="0",
                           position=(30, 40), background=True)
    try:
        left = sess.semantic().eval_form(
            "(alist-get 'left initial-frame-alist)")["value"]
        monitor = sess.semantic().eval_form(
            "(nth 0 (alist-get 'workarea (car (display-monitor-attributes-list))))"
        )["value"]
        assert left == f"(+ {int(monitor) + 30})"
        assert calls and calls[0][0] == 424242  # focus handed back
        assert sess.emacs_pid in calls[0][1]
        info = S.session_info("pl")
        assert info["screen"]["scale_factor"] >= 1
        assert info["screen"]["monitors"]
    finally:
        S.stop_session("pl")
    with pytest.raises(ElateError, match="no monitor"):
        try:
            sess = S.start_session("pl2", ui="gui", cols=60, rows=15,
                                   monitor="no-such-monitor-xyz")
            raise ElateError(sess.init_error() or "started")
        finally:
            S.stop_session("pl2")


def test_gui_only_options_need_gui(elate_home: Path):
    with pytest.raises(ElateError, match="require --ui gui"):
        S.start_session("x", monitor=0)
