"""asciinema (asciicast v2) recording of TTY sessions.

The capture point is tmux `pipe-pane`: tmux spawns a tiny helper
(``python -m elate.record CAST T0 PIDFILE``) and feeds it every byte the
pane writes; the helper timestamps each chunk relative to T0 and appends
``[time, "o", data]`` event lines to the .cast file. The controller
writes the asciicast v2 header plus an initial event replaying the
current screen (capture-pane with escapes, cursor restored), so playback
starts from the correct picture rather than a blank frame. No external
recording tool is involved -- the format is written directly.

Helper lifetime: ``record stop`` closes the pipe (pipe-pane with no
command), the helper sees EOF, flushes, and exits -- stop waits for that
exit, so the reported event count is final. For panes tmux destroys, a
dying pane closes the pipe the same way. BUT elate's own tmux config
keeps a crashed Emacs's pane around (remain-on-exit=failed, for
post-mortem capture), and tmux refuses pipe-pane on such a dead pane --
the pipe would stay attached and the helper would run forever. For that
case the helper's pid (identity-checked against reuse) is recorded at
start: ``record stop`` kills it directly, finalizes the cast with
everything up to the crash, and clears the state; ``record status``
reports the recording as stale. ``elate stop`` reaps an orphaned helper
the same way. Output produced in the instant between the initial capture
and the pipe attach can be missed (and a byte race could duplicate a
few); asciicast playback is robust to both.

Render to GIF with the asciinema ecosystem (``agg file.cast out.gif``);
GUI sessions have no terminal byte stream -- use ``snap`` instead.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import gui
from .errors import ElateError, TransportError

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

STATE_FILE = "record.json"
PID_FILE = "record.pid"
# How long to wait for the helper to write its pidfile after the pipe
# attaches (tmux spawns it immediately; this is generous).
_HELPER_PID_TIMEOUT = 3.0
# How long stop waits for the helper to flush and exit after its EOF.
_HELPER_EXIT_TIMEOUT = 5.0
# Fallback flush grace when no helper pid was recorded (legacy state).
_STOP_FLUSH_GRACE = 0.3


def _state_path(sess: "Session") -> Path:
    return sess.dir / STATE_FILE


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def start_recording(sess: "Session", output: str | None = None) -> dict[str, Any]:
    """Start recording SESS's pane into an asciicast v2 file."""
    if sess.ui != "tty":
        raise ElateError(
            f"session {sess.name!r} is a GUI session: there is no terminal "
            "byte stream to record. Use 'snap start' for a PNG screenshot "
            "series instead (render it to a GIF/video with external tools)."
        )
    sess.require_alive()
    raw = sess.raw()
    state_path = _state_path(sess)
    if state_path.exists():
        if raw.pane_pipe_open():
            st = _read_state(state_path)
            raise ElateError(
                f"a recording is already active for {sess.name!r} "
                f"(writing {st.get('path')}); run 'record stop' first")
        state_path.unlink()  # stale: the pipe/helper is gone

    info = raw.pane_info() or {}
    try:
        width = int(info.get("width") or sess.cols)
        height = int(info.get("height") or sess.rows)
    except ValueError:
        width, height = sess.cols, sess.rows

    if output:
        out = Path(output).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = sess.dir / "log" / f"{sess.name}-{stamp}.cast"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ElateError(f"cannot create {out.parent}: {exc}") from exc

    t0 = time.time()
    header = {
        "version": 2,
        "width": width,
        "height": height,
        "timestamp": int(t0),
        "title": f"elate session {sess.name}",
        "env": {"TERM": "screen-256color", "SHELL": "/bin/sh"},
    }
    # Initial event: clear, replay the current screen (with escapes),
    # restore the cursor -- playback then starts from the real picture.
    screen = raw.capture_pane(ansi=True).rstrip("\n")
    init = "\x1b[H\x1b[2J" + "\r\n".join(screen.split("\n"))
    cursor = raw.cursor_pos()
    if cursor is not None:
        init += f"\x1b[{cursor[1] + 1};{cursor[0] + 1}H"
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            fh.write(json.dumps([0.0, "o", init], ensure_ascii=False) + "\n")
    except OSError as exc:
        raise ElateError(f"cannot write cast file {out}: {exc}") from exc

    pid_path = sess.dir / PID_FILE
    pid_path.unlink(missing_ok=True)
    command = (f"exec {shlex.quote(sys.executable)} -m elate.record "
               f"{shlex.quote(str(out))} {t0:.6f} {shlex.quote(str(pid_path))}")
    raw.pipe_pane(command)
    # The helper's pid (plus its process identity, so a recycled pid is
    # never signalled) lets stop kill it directly when tmux cannot close
    # the pipe -- the dead-but-kept pane of a crashed Emacs.
    helper_pid: int | None = None
    helper_identity: str | None = None
    deadline = time.monotonic() + _HELPER_PID_TIMEOUT
    while time.monotonic() < deadline:
        try:
            helper_pid = int(pid_path.read_text(encoding="utf-8").strip())
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    if helper_pid:
        helper_identity = gui.proc_identity(helper_pid)
    pid_path.unlink(missing_ok=True)
    state_path.write_text(
        json.dumps({"path": str(out), "started": t0,
                    "width": width, "height": height,
                    "helper_pid": helper_pid,
                    "helper_identity": helper_identity}) + "\n",
        encoding="utf-8")
    sess.log("record-start", path=str(out), width=width, height=height,
             helper_pid=helper_pid)
    return {"recording": True, "path": str(out),
            "width": width, "height": height}


def _finalize(st: dict[str, Any]) -> dict[str, Any]:
    """Scan the cast named by state ST; never raises.

    A vanished/unreadable cast must not wedge stop/status with the
    state file stuck -- it degrades to events=0 plus a "note".
    """
    path = Path(st.get("path") or "")
    out: dict[str, Any] = {"path": str(path)}
    try:
        events, duration = _scan_cast(path)
        out.update(events=events, duration=duration)
    except ElateError as exc:
        out.update(events=0, duration=0.0, note=str(exc))
    return out


def _reap_helper(st: dict[str, Any], wait_for_exit: bool) -> None:
    """Make sure the pipe helper recorded in ST is gone.

    With WAIT_FOR_EXIT the helper already got its EOF: give it time to
    flush and exit by itself (so the event count is final) before any
    signalling; otherwise kill it directly (identity-checked)."""
    helper_pid = st.get("helper_pid")
    identity = st.get("helper_identity")
    if not helper_pid:
        if wait_for_exit:
            time.sleep(_STOP_FLUSH_GRACE)  # legacy state without a pid
        return
    if wait_for_exit:
        deadline = time.monotonic() + _HELPER_EXIT_TIMEOUT
        while (time.monotonic() < deadline
               and gui.pid_alive(helper_pid, identity, "python")):
            time.sleep(0.05)
    gui.terminate_pid(helper_pid, grace=1.0, identity=identity,
                      comm_hint="python")


def stop_recording(sess: "Session") -> dict[str, Any]:
    """Stop the active recording; return path + event count + duration."""
    state_path = _state_path(sess)
    if not state_path.exists():
        raise ElateError(
            f"no recording is active for session {sess.name!r} "
            "(start one with 'record start')")
    st = _read_state(state_path)
    pipe_closed = False
    try:
        sess.raw().pipe_pane(None)
        pipe_closed = True
    except (ElateError, TransportError):
        # tmux refuses pipe-pane on the dead-but-kept pane of a crashed
        # Emacs (and a stopped session has no tmux at all): the helper
        # got no EOF here, so it is killed directly below.
        pass
    _reap_helper(st, wait_for_exit=pipe_closed)
    result = _finalize(st)
    state_path.unlink(missing_ok=True)
    sess.log("record-stop", **result)
    return {"recording": False, **result}


def recording_status(sess: "Session") -> dict[str, Any]:
    """Status of the (possibly finished) recording, without changing it."""
    state_path = _state_path(sess)
    if not state_path.exists():
        return {"recording": False}
    st = _read_state(state_path)
    active = False
    note = None
    try:
        raw = sess.raw()
        info = raw.pane_info()
        if info is not None and info.get("pane_dead") == "0":
            active = raw.pane_pipe_open()
        elif info is not None:
            # The pipe is still attached to the post-mortem pane, but
            # nothing more can ever arrive: the recording is over.
            note = ("the session's Emacs died mid-recording; the cast holds "
                    "everything up to the crash -- 'record stop' finalizes it")
    except (ElateError, TransportError):
        active = False
    result = {"recording": active, **_finalize(st)}
    if note:
        result["note"] = (f"{note}; {result['note']}"
                          if result.get("note") else note)
    # Pipe gone (or pane dead/tmux killed) but state present:
    # 'record stop' finalizes and clears the state.
    result["stale"] = not active
    return result


def reap_orphan(sess: "Session") -> None:
    """Kill a recorder helper the session may have left behind.

    Called from the session teardown path: killing the tmux server EOFs
    a healthily attached helper anyway, but a helper attached to a
    crashed (remain-on-exit) pane may already be orphaned -- reap it
    explicitly so `elate stop` always upholds the no-stray-recorder
    guarantee. The state file is kept: `record stop`/`status` still
    finalize/report the cast post-mortem.
    """
    state_path = _state_path(sess)
    if state_path.exists():
        _reap_helper(_read_state(state_path), wait_for_exit=False)


def _scan_cast(path: Path) -> tuple[int, float]:
    """(event count, last timestamp) of a cast file; tolerant of damage."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ElateError(f"cannot read cast file {path}: {exc}") from exc
    events = 0
    last = 0.0
    for line in lines[1:]:  # line 0 is the header
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, list) and len(ev) >= 3:
            events += 1
            try:
                last = float(ev[0])
            except (TypeError, ValueError):
                pass
    return events, last


# ---------------------------------------------------------------------------
# pipe-pane helper (runs as `python -m elate.record CAST T0 [PIDFILE]`)

def _pipe_main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: python -m elate.record CAST_FILE T0 [PID_FILE]",
              file=sys.stderr)
        return 2
    path, t0 = argv[0], float(argv[1])
    import codecs
    import os

    if len(argv) == 3:
        try:  # pid bookkeeping is best-effort
            Path(argv[2]).write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    # Incremental decoder: a multibyte character split across two pipe
    # reads must not turn into replacement garbage.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    last = 0.0
    with open(path, "a", encoding="utf-8") as fh:
        while True:
            try:
                chunk = os.read(0, 65536)
            except OSError:
                break
            final = not chunk
            text = decoder.decode(chunk, final)
            if text:
                t = time.time() - t0
                if t < last:  # clock hiccup: keep timestamps monotonic
                    t = last
                last = t
                fh.write(json.dumps([round(t, 6), "o", text],
                                    ensure_ascii=False) + "\n")
                fh.flush()
            if final:
                break
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via tmux
    sys.exit(_pipe_main(sys.argv[1:]))
