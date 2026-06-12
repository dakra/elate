"""PNG screenshots of GUI sessions.

macOS: resolve the Emacs window via Quartz (kCGWindowOwnerPID == the
session's Emacs pid) and capture it with `screencapture -l`. Requires
the Screen Recording permission; the permission is probed up front
(CGPreflightScreenCaptureAccess) so we fail with instructions instead
of looping on system prompts.

Linux/X11: ask the session's Emacs for its X window id
(frame-parameter 'outer-window-id) and capture it with ImageMagick
`import`, falling back to `xwd` + `convert`. DISPLAY comes from the
session (Xvfb headless sessions record theirs).
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ElateError, RpcError

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

PERMISSION_HINT = (
    "grant Screen Recording permission to the application running elate "
    "(your terminal or its parent): System Settings -> Privacy & Security "
    "-> Screen Recording, enable it, then restart that application"
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_dimensions(path: Path) -> tuple[int, int]:
    """(width, height) of a PNG file; raises ElateError if it is not a PNG."""
    try:
        header = path.open("rb").read(24)
    except OSError as exc:
        raise ElateError(f"cannot read screenshot {path}: {exc}") from exc
    if len(header) < 24 or not header.startswith(PNG_MAGIC):
        raise ElateError(f"{path} is not a PNG file")
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def capture_gui(sess: "Session", out_path: Path) -> dict[str, Any]:
    """Capture the session's Emacs window as PNG into OUT_PATH.

    Returns {"path", "width", "height", "format": "png"}.
    """
    if sys.platform == "darwin":
        _capture_macos(sess, out_path)
    elif sys.platform.startswith("linux"):
        _capture_x11(sess, out_path)
    else:
        raise ElateError(
            f"GUI screenshots are not supported on {sys.platform} "
            "(macOS and Linux/X11 only)"
        )
    width, height = png_dimensions(out_path)
    if width < 10 or height < 10:
        raise ElateError(
            f"screenshot {out_path} is implausibly small ({width}x{height}); "
            f"the window may be hidden or the capture failed -- on macOS, "
            f"{PERMISSION_HINT}"
        )
    return {"path": str(out_path), "width": width, "height": height,
            "format": "png"}


# ---------------------------------------------------------------------------
# macOS

def screen_recording_allowed() -> bool | None:
    """True/False for the Screen Recording permission; None when unknowable.

    Only meaningful on macOS; uses CGPreflightScreenCaptureAccess, which
    checks without triggering the system permission prompt.
    """
    if sys.platform != "darwin":
        return None
    try:
        import Quartz
    except ImportError:
        return None
    preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    if preflight is None:  # pre-10.15 macOS: no permission system
        return True
    return bool(preflight())


def macos_window_id(pid: int) -> int | None:
    """CGWindowID of the largest on-screen window owned by PID, or None."""
    try:
        import Quartz
    except ImportError as exc:
        raise ElateError(
            "pyobjc-framework-Quartz is required for GUI screenshots on "
            f"macOS (reinstall elate): {exc}"
        ) from exc
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    best: tuple[float, int] | None = None
    for win in windows or []:
        if win.get("kCGWindowOwnerPID") != pid:
            continue
        if win.get("kCGWindowLayer", 0) != 0:  # skip menus/tooltips/etc.
            continue
        bounds = win.get("kCGWindowBounds") or {}
        area = float(bounds.get("Width", 0)) * float(bounds.get("Height", 0))
        number = win.get("kCGWindowNumber")
        if number is not None and (best is None or area > best[0]):
            best = (area, int(number))
    return best[1] if best else None


def _capture_macos(sess: "Session", out_path: Path) -> None:
    if screen_recording_allowed() is False:
        raise ElateError(f"cannot capture the screen: {PERMISSION_HINT}")
    if not sess.emacs_pid:
        raise ElateError(f"session {sess.name!r} has no recorded Emacs pid")
    window_id = macos_window_id(sess.emacs_pid)
    if window_id is None:
        raise ElateError(
            f"no on-screen window found for Emacs pid {sess.emacs_pid} "
            f"(session {sess.name!r}); the frame may be minimized, on "
            "another Space, or the process dead"
        )
    # -x: no sound, -o: no window shadow, -l: capture window by CGWindowID.
    proc = subprocess.run(
        ["screencapture", "-x", "-o", "-l", str(window_id), str(out_path)],
        capture_output=True, text=True, timeout=15.0,
    )
    if proc.returncode != 0:
        raise ElateError(
            f"screencapture failed ({proc.returncode}): "
            f"{proc.stderr.strip() or 'no output'} -- if this is a "
            f"permission problem, {PERMISSION_HINT}"
        )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise ElateError(
            f"screencapture produced no image at {out_path} -- "
            f"{PERMISSION_HINT}"
        )


# ---------------------------------------------------------------------------
# Linux / X11  (written for CI; cannot be exercised on macOS dev machines)

def _x11_window_id(sess: "Session") -> str:
    try:
        data = sess.semantic().rpc(
            "frame-parameter", "outer-window-id", timeout=10.0)
    except RpcError as exc:
        raise ElateError(
            f"cannot resolve the Emacs X window id: {exc}") from exc
    window_id = data.get("value")
    if not window_id:
        raise ElateError(
            "Emacs reported no outer-window-id; is this really an X11 "
            "GUI frame?"
        )
    return str(window_id)


def _capture_x11(sess: "Session", out_path: Path) -> None:
    window_id = _x11_window_id(sess)
    env = None
    if sess.display:
        env = {**os.environ, "DISPLAY": sess.display}
    if shutil.which("import"):
        proc = subprocess.run(
            ["import", "-silent", "-window", window_id, str(out_path)],
            capture_output=True, text=True, timeout=15.0, env=env,
        )
        if proc.returncode != 0:
            raise ElateError(
                f"import failed ({proc.returncode}): "
                f"{proc.stderr.strip() or 'no output'}"
            )
        return
    converter = shutil.which("convert") or shutil.which("magick")
    if shutil.which("xwd") and converter:
        xwd = subprocess.run(
            ["xwd", "-silent", "-id", window_id],
            capture_output=True, timeout=15.0, env=env,
        )
        if xwd.returncode != 0:
            raise ElateError(
                f"xwd failed ({xwd.returncode}): "
                f"{xwd.stderr.decode(errors='replace').strip() or 'no output'}"
            )
        conv = subprocess.run(
            [converter, "xwd:-", f"png:{out_path}"],
            input=xwd.stdout, capture_output=True, timeout=15.0,
        )
        if conv.returncode != 0:
            raise ElateError(
                f"convert failed ({conv.returncode}): "
                f"{conv.stderr.decode(errors='replace').strip() or 'no output'}"
            )
        return
    raise ElateError(
        "no X11 screenshot tool found: install ImageMagick ('import', or "
        "'xwd' plus 'convert')"
    )
