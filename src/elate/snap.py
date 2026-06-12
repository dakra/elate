"""Periodic screenshot series ("snap"): demo/GIF frame source.

A detached snapper process (``python -m elate.snap NAME DIR INTERVAL
FORMAT``) captures one frame every INTERVAL seconds: PNGs for GUI
sessions (reusing the screenshot machinery, permission handling
included), plain or ANSI text for TTY sessions. Frames land in DIR as
``frame-NNNN.png``/``.txt`` next to a ``manifest.json`` with per-frame
timestamps (rewritten atomically after every frame, so it is sane even
if the snapper is killed outright).

The snapper only ever *reads* the session (capture-pane /
screencapture): if it dies, the session is unaffected; if the session
dies, the snapper notices and finalizes. ``snap stop`` is idempotent
and identity-checks the recorded pid before signalling it (pid-reuse
hazard, same discipline as the GUI process management).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import gui
from .errors import ElateError
from .paths import sessions_root

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

STATE_FILE = "snap.json"
MIN_INTERVAL = 0.05
MAX_INTERVAL = 60.0


def _state_path(sess: "Session") -> Path:
    return sess.dir / STATE_FILE


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _manifest(state: dict[str, Any]) -> dict[str, Any]:
    try:
        path = Path(state.get("dir") or "") / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def start_snap(sess: "Session", interval: float = 0.5,
               output: str | None = None, ansi: bool = False) -> dict[str, Any]:
    """Start a detached snapper for SESS; return its parameters."""
    if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise ElateError(
            f"snap interval must be between {MIN_INTERVAL:g} and "
            f"{MAX_INTERVAL:g} seconds, got {interval:g}")
    sess.require_alive()
    state_path = _state_path(sess)
    if state_path.exists():
        st = _read_state(state_path)
        if gui.pid_alive(st.get("pid"), st.get("identity"), "python"):
            raise ElateError(
                f"a snapper is already running for {sess.name!r} "
                f"(pid {st.get('pid')}, into {st.get('dir')}); "
                "run 'snap stop' first")
        state_path.unlink()  # stale: the snapper is gone

    if sess.ui == "gui":
        if ansi:
            raise ElateError("--ansi applies to TTY text frames only")
        from .screenshot import PERMISSION_HINT, screen_recording_allowed

        # Probe up front: a permission failure must surface here, not
        # silently produce an empty frame directory in the background.
        if screen_recording_allowed() is False:
            raise ElateError(f"cannot snap a GUI session: {PERMISSION_HINT}")
        fmt = "png"
    else:
        fmt = "ansi" if ansi else "txt"

    if output:
        outdir = Path(output).expanduser().resolve()
    else:
        outdir = sess.dir / f"snap-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        outdir.mkdir(parents=True, exist_ok=True)
        log = open(outdir / "snapper.log", "ab")
    except OSError as exc:
        raise ElateError(f"cannot prepare snap directory {outdir}: {exc}") from exc

    # ELATE_HOME pins the sessions root for the child explicitly, so the
    # snapper resolves the same session even if the env changes later.
    env = {**os.environ, "ELATE_HOME": str(sessions_root().parent)}
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "elate.snap",
             sess.name, str(outdir), f"{interval:g}", fmt],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            env=env, start_new_session=True,
        )
    except OSError as exc:
        raise ElateError(f"cannot start the snapper: {exc}") from exc
    finally:
        log.close()

    state = {
        "pid": proc.pid,
        "identity": gui.proc_identity(proc.pid),
        "dir": str(outdir),
        "interval": interval,
        "format": fmt,
        "started": time.time(),
    }
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    sess.log("snap-start", dir=str(outdir), interval=interval, format=fmt)
    return {"snapping": True, "pid": proc.pid, "dir": str(outdir),
            "interval": interval, "format": fmt}


def stop_snap(sess: "Session") -> dict[str, Any]:
    """Stop the snapper (idempotent); return frame count + directory."""
    state_path = _state_path(sess)
    if not state_path.exists():
        return {"snapping": False, "stopped": False,
                "note": "no snapper is running"}
    st = _read_state(state_path)
    gui.terminate_pid(st.get("pid"), grace=3.0,
                      identity=st.get("identity"), comm_hint="python")
    state_path.unlink(missing_ok=True)
    manifest = _manifest(st)
    frames = len(manifest.get("frames") or [])
    sess.log("snap-stop", dir=st.get("dir"), frames=frames)
    return {"snapping": False, "stopped": True, "dir": st.get("dir"),
            "frames": frames, "format": st.get("format")}


def snap_status(sess: "Session") -> dict[str, Any]:
    """Status of the snapper without changing anything."""
    state_path = _state_path(sess)
    if not state_path.exists():
        return {"snapping": False}
    st = _read_state(state_path)
    alive = gui.pid_alive(st.get("pid"), st.get("identity"), "python")
    manifest = _manifest(st)
    return {
        "snapping": alive,
        "pid": st.get("pid"),
        "dir": st.get("dir"),
        "interval": st.get("interval"),
        "format": st.get("format"),
        "frames": len(manifest.get("frames") or []),
        # Snapper gone but state present (it crashed, or the session
        # died): 'snap stop' clears the state and reports the frames.
        "stale": not alive,
    }


# ---------------------------------------------------------------------------
# Snapper process (runs as `python -m elate.snap NAME DIR INTERVAL FORMAT`)

def _write_manifest(outdir: Path, manifest: dict[str, Any]) -> None:
    tmp = outdir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, outdir / "manifest.json")


def _snap_main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: python -m elate.snap NAME DIR INTERVAL FORMAT",
              file=sys.stderr)
        return 2
    name, outdir, interval, fmt = argv[0], Path(argv[1]), float(argv[2]), argv[3]
    from . import session as S  # heavy import only in the helper process

    sess = S.load_session(name)
    stopping = False

    def on_signal(signum: int, frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    started = time.time()
    frames: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "session": name,
        "ui": sess.ui,
        "format": fmt,
        "interval": interval,
        "started_at": started,
        "frames": frames,
    }
    _write_manifest(outdir, manifest)
    index = 0
    exit_code = 0
    while not stopping:
        if not sess.is_alive():
            manifest["ended_reason"] = "session died"
            break
        now = time.time()
        suffix = "png" if fmt == "png" else "txt"
        # 6 digits: lexicographic file order stays correct for ~14h at
        # the minimum 0.05s interval (4 digits broke after 8.3 minutes).
        fname = f"frame-{index:06d}.{suffix}"
        try:
            if fmt == "png":
                from .screenshot import capture_gui

                capture_gui(sess, outdir / fname)
            else:
                text = sess.raw().capture_pane(ansi=(fmt == "ansi"))
                (outdir / fname).write_text(text, encoding="utf-8")
            frames.append({"file": fname, "ts": round(now, 6),
                           "elapsed": round(now - started, 6)})
            _write_manifest(outdir, manifest)
        except (ElateError, OSError) as exc:
            # A capture OR write failure (outdir deleted mid-series,
            # disk full, ...) ends the series cleanly; the session
            # itself is untouched (snapping only ever reads it).
            print(f"snap: frame {index} failed: {exc}", file=sys.stderr)
            manifest["ended_reason"] = f"frame {index} failed: {exc}"
            exit_code = 1
            break
        index += 1
        deadline = now + interval
        # Sleep in small slices so a SIGTERM is honored promptly.
        while not stopping and time.time() < deadline:
            time.sleep(min(0.05, max(deadline - time.time(), 0.001)))
    manifest["ended_at"] = time.time()
    manifest["frames_total"] = len(frames)
    try:
        _write_manifest(outdir, manifest)
    except OSError:
        pass  # outdir is gone: nothing left to finalize into
    return exit_code


if __name__ == "__main__":  # pragma: no cover - runs as a detached process
    sys.exit(_snap_main(sys.argv[1:]))
