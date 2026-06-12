"""Session lifecycle, registry, and high-level operations."""

from __future__ import annotations

import base64
import dataclasses
import json
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import gui, sandbox
from .errors import (
    ElateError,
    EvalTimeout,
    RpcError,
    SessionDead,
    SessionExists,
    SessionNotFound,
    TransportError,
    WaitTimeout,
)
from .paths import sessions_root
from .raw import RawChannel
from .semantic import SemanticChannel
from .transcript import log_event

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STARTUP_TIMEOUT = 30.0


@dataclass
class Session:
    name: str
    session_dir: str
    emacs: str
    emacsclient: str
    config: str
    cols: int
    rows: int
    created_at: float
    ui: str = "tty"
    tmux_socket: str = ""  # tty sessions only
    status: str = "running"
    emacs_pid: int | None = None
    emacs_identity: str | None = None  # gui: ps start-time+comm at spawn
    emacs_version: str | None = None
    init_file: str | None = None
    loads: list[str] = field(default_factory=list)
    evals: list[str] = field(default_factory=list)
    headless: bool = False  # gui sessions: running under our own Xvfb
    display: str | None = None  # gui sessions: X11 DISPLAY (Linux)
    xvfb_pid: int | None = None  # gui sessions: Xvfb we own (Linux headless)
    xvfb_identity: str | None = None  # ps start-time+comm of that Xvfb

    # -- paths ------------------------------------------------------------

    @property
    def dir(self) -> Path:
        return Path(self.session_dir)

    @property
    def socket_path(self) -> Path:
        return self.dir / "server" / "elate"

    @property
    def registry_path(self) -> Path:
        return self.dir / "session.json"

    @property
    def messages_cursor_path(self) -> Path:
        return self.dir / "messages.cursor"

    @property
    def init_error_path(self) -> Path:
        return self.dir / "init-error"

    def init_error(self) -> str | None:
        """Startup error recorded by elate-guard, if any."""
        try:
            return self.init_error_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    @property
    def gui_log_path(self) -> Path:
        return self.dir / "log" / "emacs-gui.log"

    @property
    def clean_install_path(self) -> Path:
        return self.dir / "clean-install.json"

    def clean_install_info(self) -> list[dict[str, Any]] | None:
        """Install records written by elate-clean-install, if any.

        File-based so it works for dead/stopped sessions too."""
        try:
            data = json.loads(self.clean_install_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        installed = data.get("installed")
        return installed if isinstance(installed, list) else None

    # -- channels ---------------------------------------------------------

    def raw(self) -> RawChannel:
        if self.ui == "gui":
            raise ElateError(
                f"session {self.name!r} is a GUI session: there is no raw "
                "terminal channel. Use semantic delivery instead (keys "
                "default/--events, type, mouse); if Emacs is wedged beyond "
                "the semantic channel, stop the session."
            )
        return RawChannel(self.tmux_socket)

    def semantic(self) -> SemanticChannel:
        return SemanticChannel(self.emacsclient, self.socket_path)

    # -- registry ---------------------------------------------------------

    def save(self) -> None:
        self.registry_path.write_text(
            json.dumps(dataclasses.asdict(self), indent=2) + "\n", encoding="utf-8"
        )

    # -- liveness ---------------------------------------------------------

    def is_alive(self) -> bool:
        if self.status != "running":
            return False
        if self.ui == "gui":
            # Identity-checked: a recycled pid (controller restarted after
            # the Emacs died and was reaped) must not read as "alive".
            return gui.pid_alive(self.emacs_pid, self.emacs_identity, "emacs")
        return self.raw().is_pane_alive()

    def is_busy(self, timeout: float = 1.0) -> bool:
        """Alive but not answering the semantic channel promptly."""
        return self.is_alive() and not self.semantic().ping(timeout=timeout)

    def computed_status(self) -> str:
        """Registry status with liveness folded in.

        A crashed Emacs leaves the stored status at "running"; report
        "dead" instead, matching what `list`/`info` show.
        """
        if self.status == "running" and not self.is_alive():
            return "dead"
        return self.status

    def require_alive(self) -> None:
        if not self.is_alive():
            # Registry status may still say "running" for a crashed Emacs;
            # report the computed status, matching `list`/`info`.
            status = "dead" if self.status == "running" else self.status
            postmortem = (
                f" the GUI process log is {self.gui_log_path}"
                if self.ui == "gui" else
                " a screenshot can still capture the final screen of the"
                " dead pane"
            )
            raise SessionDead(
                f"session {self.name!r} is not running"
                f" (status: {status}); see {self.dir}/log for the transcript;"
                + postmortem
            )

    def log(self, event: str, **data: Any) -> None:
        log_event(self.dir, event, data)


# ---------------------------------------------------------------------------
# Registry

def load_session(name: str) -> Session:
    path = sessions_root() / name / "session.json"
    if not path.is_file():
        raise SessionNotFound(f"no session named {name!r} (looked in {path.parent.parent})")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses.fields(Session)}
        return Session(**{k: v for k, v in data.items() if k in known})
    except (json.JSONDecodeError, TypeError) as exc:
        raise ElateError(f"corrupt session registry: {path}: {exc}") from exc


def list_sessions() -> list[dict[str, Any]]:
    """Scan the sessions directory; report each session with live status."""
    root = sessions_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not (entry / "session.json").is_file():
            continue
        try:
            sess = load_session(entry.name)
        except ElateError as exc:
            out.append({"name": entry.name, "status": "corrupt", "error": str(exc)})
            continue
        alive = sess.is_alive()
        status = "running" if alive else ("dead" if sess.status == "running" else sess.status)
        out.append(
            {
                "name": sess.name,
                "ui": sess.ui,
                "status": status,
                "emacs_version": sess.emacs_version,
                "uptime": round(time.time() - sess.created_at, 1) if alive else None,
                "session_dir": sess.session_dir,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Lifecycle

def _resolve_emacs(emacs: str | None) -> tuple[str, str]:
    """Resolve the emacs binary and a matching emacsclient."""
    emacs_path = emacs or shutil.which("emacs")
    if not emacs_path:
        raise ElateError("no emacs binary found (use --emacs PATH)")
    emacs_path = str(Path(emacs_path).expanduser())
    sibling = Path(emacs_path).parent / "emacsclient"
    if sibling.is_file():
        return emacs_path, str(sibling)
    client = shutil.which("emacsclient")
    if not client:
        raise ElateError("no emacsclient found next to emacs or on PATH")
    return emacs_path, client


def _force_cleanup(sess: Session) -> None:
    """Kill whatever processes a (possibly half-dead) session left behind.

    Pid kills are identity-checked: after a controller restart a stale
    registry pid may have been recycled by an unrelated process, which
    must not be signalled.
    """
    if sess.ui == "gui":
        gui.terminate_pid(sess.emacs_pid, identity=sess.emacs_identity,
                          comm_hint="emacs")
        gui.terminate_pid(sess.xvfb_pid, identity=sess.xvfb_identity,
                          comm_hint="xvfb")
    else:
        sess.raw().kill_server()
        from . import record as _record  # local: keep module load light

        _record.reap_orphan(sess)


def start_session(
    name: str,
    *,
    emacs: str | None = None,
    config: str = "minimal",
    init_file: str | None = None,
    loads: Sequence[str] = (),
    evals: Sequence[str] = (),
    cols: int = 120,
    rows: int = 36,
    ui: str = "tty",
    headless: bool = False,
) -> Session:
    if not _NAME_RE.match(name):
        raise ElateError(f"invalid session name: {name!r}")
    if cols < 10 or rows < 4:
        # Same bounds as resize_session; without this, sizes like 0x0
        # reach tmux/the frame code and die with a cryptic boot error.
        raise ElateError(f"implausible size {cols}x{rows} (minimum 10x4)")
    if ui not in ("tty", "gui"):
        raise ElateError(f"unknown ui {ui!r} (use 'tty' or 'gui')")
    if headless and ui != "gui":
        raise ElateError("--headless requires --ui gui")
    session_dir = sessions_root() / name

    if (session_dir / "session.json").is_file():
        old = load_session(name)
        if old.is_alive():
            raise SessionExists(f"session {name!r} is already running")
        # Stale/dead session: clean up and rebuild.
        _force_cleanup(old)
        shutil.rmtree(session_dir)
    elif session_dir.exists():
        shutil.rmtree(session_dir)

    emacs_path, client_path = _resolve_emacs(emacs)
    if init_file:
        # An init file implies config 'init-file' (the documented
        # convenience over the 'minimal' default) -- but it must not
        # silently override an explicitly conflicting mode: 'bare'
        # means "no init", 'clean-install' means "install, don't
        # load-path inject", and a quiet downgrade would fake-pass
        # exactly the checks those modes exist for.
        if config not in ("minimal", "init-file"):
            raise ElateError(
                f"an init file conflicts with config {config!r}: an init "
                "file implies config 'init-file'; drop one of the two")
        config = "init-file"
    session_dir.mkdir(parents=True)

    try:
        emacs_args = sandbox.build_sandbox(
            session_dir,
            config=config,
            init_file=init_file,
            loads=loads,
            evals=evals,
            ui=ui,
            cols=cols,
            rows=rows,
        )
        if ui == "tty":
            # tmux options must be in place before the pane is created, so
            # an instantly crashing Emacs still leaves a post-mortem pane.
            tmux_conf = session_dir / "tmux.conf"
            tmux_conf.write_text(
                "set -g status off\nset -g remain-on-exit failed\n",
                encoding="utf-8",
            )
    except ElateError:
        # Nothing registered yet: do not leave an orphan directory behind.
        shutil.rmtree(session_dir, ignore_errors=True)
        raise

    sess = Session(
        name=name,
        session_dir=str(session_dir),
        emacs=emacs_path,
        emacsclient=client_path,
        config=config,
        ui=ui,
        # The tmux socket lives inside the sandbox so equally-named
        # sessions under different sessions roots can never collide.
        tmux_socket=str(session_dir / "tmux.sock") if ui == "tty" else "",
        cols=cols,
        rows=rows,
        created_at=time.time(),
        status="starting",
        init_file=init_file,
        loads=list(loads),
        evals=list(evals),
        headless=headless,
    )
    sess.save()
    sess.log("start", emacs=emacs_path, args=emacs_args, config=config,
             ui=ui, size=[cols, rows])

    if ui == "gui":
        _boot_gui(sess, emacs_args)
    else:
        _boot_tty(sess, emacs_args, tmux_conf)

    sess.status = "running"
    sess.save()
    init_error = sess.init_error()
    sess.log("started", pid=sess.emacs_pid, version=sess.emacs_version,
             init_error=init_error)
    return sess


def _boot_tty(sess: Session, emacs_args: list[str], tmux_conf: Path) -> None:
    env = sandbox.environment(sess.dir)
    env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    command = "exec env {} {} {}".format(
        env_prefix, shlex.quote(sess.emacs),
        " ".join(shlex.quote(a) for a in emacs_args)
    )

    raw = sess.raw()
    try:
        raw.new_session(command, sess.cols, sess.rows, config_file=str(tmux_conf))
        _wait_for_agent(sess, raw.is_pane_alive)
        info = sess.semantic().rpc("emacs-info", timeout=10.0)
    except ElateError as exc:
        sess.status = "failed"
        sess.save()
        crash = _crash_report(raw)
        sess.log("start-failed", error=str(exc), crash=crash)
        raw.kill_server()
        # Keep the underlying cause: e.g. tmux refusing a unix-socket path
        # longer than ~104 bytes is invisible in the (empty) screen capture.
        raise ElateError(
            f"session {sess.name!r} failed to start: {exc}\nLast screen:\n{crash}"
        ) from None

    sess.emacs_pid = int(info.get("pid") or 0) or None
    sess.emacs_version = info.get("version")


def _boot_gui(sess: Session, emacs_args: list[str]) -> None:
    try:
        if sess.headless:
            # Size the virtual screen to the requested frame (generous
            # cell estimate) so --size is not clamped by the default
            # 1280x800 screen.  [Linux-only; runtime-unverified on macOS.]
            screen = f"{max(1280, sess.cols * 12)}x{max(800, sess.rows * 28)}x24"
            sess.xvfb_pid, sess.display = gui.start_xvfb(sess.dir, screen=screen)
            sess.xvfb_identity = gui.proc_identity(sess.xvfb_pid)
            sess.save()
        proc = gui.spawn_emacs(
            sess.emacs, emacs_args, sandbox.environment(sess.dir),
            sess.gui_log_path, display=sess.display,
        )
        sess.emacs_pid = proc.pid
        sess.emacs_identity = gui.proc_identity(proc.pid)
        sess.save()

        # A wrapper script may exec (keeping the pid) or fork-and-exit
        # (e.g. a launcher that backgrounds the real Emacs): when the
        # spawned process exits during startup, allow a grace window for
        # the socket to appear before declaring the boot dead.  The
        # agent's own emacs-info pid below is authoritative either way.
        exited_at: float | None = None

        def alive() -> bool:
            nonlocal exited_at
            if proc.poll() is None:
                return True
            if exited_at is None:
                exited_at = time.monotonic()
            return time.monotonic() - exited_at < 5.0

        _wait_for_agent(sess, alive)
        info = sess.semantic().rpc("emacs-info", timeout=10.0)
    except ElateError as exc:
        sess.status = "failed"
        sess.save()
        crash = gui.log_tail(sess.gui_log_path)
        sess.log("start-failed", error=str(exc), crash=crash)
        gui.terminate_pid(sess.emacs_pid, identity=sess.emacs_identity,
                          comm_hint="emacs")
        gui.terminate_pid(sess.xvfb_pid, identity=sess.xvfb_identity,
                          comm_hint="xvfb")
        raise ElateError(
            f"session {sess.name!r} failed to start: {exc}\n"
            f"GUI process log tail:\n{crash}"
        ) from None

    pid = int(info.get("pid") or 0) or sess.emacs_pid
    sess.emacs_pid = pid  # wrapper forked: the agent's pid is the real one
    # Re-record the identity now that startup is over: an exec-chain
    # launcher (e.g. the emacsformacosx.com binary execs a per-arch
    # child) renames the command between spawn and here while keeping
    # the pid, so the spawn-time comm would mismatch forever after.
    sess.emacs_identity = gui.proc_identity(pid) or sess.emacs_identity
    sess.emacs_version = info.get("version")


def _wait_for_agent(sess: Session, alive, timeout: float = STARTUP_TIMEOUT) -> None:
    """Wait until the agent socket answers; ALIVE() reports process health."""
    deadline = time.monotonic() + timeout
    sem = sess.semantic()
    while time.monotonic() < deadline:
        if sess.socket_path.exists() and sem.ping(timeout=2.0):
            return
        if not alive():
            raise ElateError("Emacs exited during startup")
        time.sleep(0.1)
    raise ElateError(f"agent did not come up within {timeout:g}s")


def _crash_report(raw: RawChannel) -> str:
    try:
        return raw.capture_pane().rstrip("\n")
    except ElateError:
        return "(no screen capture available)"


def stop_session(name: str, via: str | None = None) -> dict[str, Any]:
    sess = load_session(name)
    was_alive = sess.is_alive()
    if was_alive:
        try:
            sess.semantic().eval_raw("(kill-emacs)", timeout=3.0)
        except (EvalTimeout, TransportError):
            pass  # fall through to a hard kill
    if sess.ui == "gui":
        # Give kill-emacs a moment, then escalate (TERM -> KILL).  The
        # escalation grace is short: the clean-exit window was already
        # spent here (avoids ~6s of doubled grace on a wedged Emacs).
        # Kills are identity-checked against pid reuse.
        deadline = time.monotonic() + 3.0
        while (time.monotonic() < deadline
               and gui.pid_alive(sess.emacs_pid, sess.emacs_identity, "emacs")):
            time.sleep(0.1)
        gui.terminate_pid(sess.emacs_pid, grace=1.0,
                          identity=sess.emacs_identity, comm_hint="emacs")
        gui.terminate_pid(sess.xvfb_pid, identity=sess.xvfb_identity,
                          comm_hint="xvfb")
    else:
        sess.raw().kill_server()
        # Killing the tmux server EOFs a healthily attached recorder
        # helper; one attached to a crashed (remain-on-exit) pane is
        # orphaned instead -- reap it so stop never leaves a recorder.
        from . import record as _record  # local: keep module load light

        _record.reap_orphan(sess)
    sess.status = "stopped"
    sess.save()
    sess.log("stop", was_alive=was_alive, **({"via": via} if via else {}))
    return {"name": name, "stopped": True, "was_alive": was_alive}


def _dir_size(path: Path) -> int:
    """Total file bytes under path (best effort; 0 on any trouble)."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def purge_sessions(names: Sequence[str] | None = None,
                   all_sessions: bool = False) -> dict[str, Any]:
    """Delete the sandbox directories of sessions that are not running.

    Running sessions are NEVER purged: naming one is a loud error, and
    under ``all_sessions`` they are skipped and reported.  Before the
    sandbox is removed, non-running sessions get the same force-cleanup
    as ``stop`` (identity-checked pid kills, tmux kill-server, recorder
    reaping), so a half-dead tmux server or recorder helper cannot
    outlive its socket directory.  Corrupt registries (unreadable
    session.json) get a best-effort ``tmux -S <dir>/tmux.sock
    kill-server`` instead, then the same removal.

    Only directories directly under :func:`sessions_root` are deleted --
    the registry's stored ``session_dir`` is deliberately not trusted
    with an ``rmtree``.  A session-dir entry that is itself a symlink
    (something elate never creates) is not followed: the link itself is
    removed, the target is preserved, and the report says so.

    Like the rest of elate, purge assumes a single controller: a
    concurrent ``start`` of a name just classified as not-running can
    race the removal (same disposition as the record-start TOCTOU).
    """
    if not names and not all_sessions:
        raise ElateError(
            "purge needs explicit session names or --all "
            "(purge --all removes every stopped/dead sandbox)")
    root = sessions_root()
    listing = {s["name"]: s for s in list_sessions()}
    if names:
        names = list(dict.fromkeys(names))  # dedupe, keep order
        unknown = [n for n in names if n not in listing]
        if unknown:
            raise SessionNotFound(
                f"no session named {', '.join(repr(n) for n in unknown)} "
                f"(looked in {root})")
        targets = [listing[n] for n in names]
        running = [t["name"] for t in targets if t["status"] == "running"]
        if running:
            raise ElateError(
                f"session(s) still running: {', '.join(running)} -- purge "
                "never removes a running session; stop first "
                "(elate stop NAME)")
    else:
        targets = list(listing.values())
    purged: list[dict[str, Any]] = []
    skipped: list[str] = []
    freed = 0
    for entry in targets:
        if entry["status"] == "running":
            skipped.append(entry["name"])
            continue
        path = root / entry["name"]
        if entry["status"] == "corrupt":
            # No loadable registry: kill any tmux server still bound to
            # the sandbox's conventional socket path, best effort.
            sock = path / "tmux.sock"
            if sock.exists():
                try:
                    RawChannel(str(sock)).kill_server()
                except ElateError:
                    pass  # no tmux on PATH etc.: still purge the files
        else:
            try:
                _force_cleanup(load_session(entry["name"]))
            except ElateError:
                pass  # already gone / unreadable mid-scan: still purge
        if path.is_symlink():
            # A hand-made symlinked session dir (elate never creates
            # one): rmtree refuses symlinks, and following the link
            # would escape sessions_root. Remove the link itself; the
            # target -- and everything in it -- is preserved, and the
            # report must not pretend otherwise (no freed bytes).
            try:
                path.unlink()
            except OSError:
                pass
            purged.append({
                "name": entry["name"], "status": entry["status"],
                "note": "session dir was a symlink; removed the link, "
                        "kept the target"})
            continue
        freed += _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        purged.append({"name": entry["name"], "status": entry["status"]})
    return {"purged": purged, "skipped_running": skipped,
            "freed_bytes": freed}


def session_info(name: str) -> dict[str, Any]:
    sess = load_session(name)
    alive = sess.is_alive()
    busy = sess.is_busy() if alive else False
    return {
        "name": sess.name,
        "ui": sess.ui,
        "alive": alive,
        "busy": busy,
        "status": "running" if alive else ("dead" if sess.status == "running" else sess.status),
        "pid": sess.emacs_pid,
        "emacs": sess.emacs,
        "emacs_version": sess.emacs_version,
        "config": sess.config,
        "uptime": round(time.time() - sess.created_at, 1) if alive else None,
        "size": [sess.cols, sess.rows],
        "session_dir": sess.session_dir,
        "tmux_socket": sess.tmux_socket or None,
        "socket_path": str(sess.socket_path),
        "init_error": sess.init_error(),
        **({"headless": True, "display": sess.display}
           if sess.ui == "gui" and sess.headless else {}),
        **({"package_user_dir": str(sess.dir / "elpa"),
            "installed": sess.clean_install_info()}
           if sess.config == "clean-install" else {}),
    }


# ---------------------------------------------------------------------------
# Messages cursor (persisted per session)

def messages_delta(sess: Session, timeout: float = 10.0) -> dict[str, Any]:
    """Everything in *Messages* since the last call, advancing the cursor."""
    cursor: int | None = None
    try:
        cursor = int(sess.messages_cursor_path.read_text().strip())
    except (OSError, ValueError):
        cursor = None
    data = sess.semantic().rpc("messages", cursor, timeout=timeout)
    new_cursor = data.get("cursor")
    if isinstance(new_cursor, int):
        sess.messages_cursor_path.write_text(str(new_cursor))
    return data


# ---------------------------------------------------------------------------
# Testing & lint (Phase 4)

# Slack between the agent's in-Emacs with-timeout (which interrupts a
# timer-servicing test/compile and returns a clean result/error) and our
# hard subprocess timeout (the backstop for code stuck in a tight elisp
# loop). Shared by the ERT and lint RPCs.
ERT_RPC_SLACK = 10.0

LINT_NOTES = [
    "byte-compilation runs IN the live session: the file's compile-time "
    "code (eval-when-compile, macro expansion, top-level requires) is "
    "executed and can mutate session state -- lint untrusted code in a "
    "throwaway session",
    "lint results can depend on session history: functions defined by "
    "an earlier load, or by an earlier lint's compile-time code "
    "(eval-when-compile, requires), silence undefined-function warnings "
    "a fresh session would emit (plain defmacro/defun in a linted file "
    "do NOT leak; their definitions stay compile-local)",
    "native-comp warnings are not collected (native compilation is "
    "asynchronous; its warnings would race the lint run)",
    "package-lint is not run (it needs the package archives, and the "
    "sandbox deliberately has no network access)",
]


def run_ert(
    sess: Session,
    selector: str = "t",
    load_files: Sequence[str] = (),
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run ERT tests in the session, interactively, with structured results.

    LOAD_FILES are loaded (by path -- contents never travel over the argv
    transport) before the run; a load error surfaces as an RpcError with
    backtrace. SELECTOR is ERT selector syntax as a string ("t", a test
    name, a name regexp, "(tag foo)", ":failed", ...). TIMEOUT arms the
    agent's in-Emacs with-timeout for the whole run; the subprocess
    timeout sits ERT_RPC_SLACK above it as the hard backstop.
    """
    sem = sess.semantic()
    loaded: list[str] = []
    for entry in load_files:
        path = Path(entry).expanduser().resolve()
        if not path.is_file():
            raise ElateError(f"test file does not exist: {entry}")
        data = sem.rpc("load-file", str(path), timeout=60.0)
        loaded.append(data.get("loaded") or str(path))
    b64 = base64.b64encode((selector or "t").encode("utf-8")).decode("ascii")
    data = sem.rpc("ert", b64, round(float(timeout), 3),
                   timeout=timeout + ERT_RPC_SLACK)
    if loaded:
        data = {"loaded": loaded, **data}
    return data


def lint_files(
    sess: Session,
    files: Sequence[str],
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Byte-compile + checkdoc each of FILES inside the session.

    Paths only ever cross the transport (never contents). Items are
    {file, tool, line, col, severity, message}; "clean" is true when
    there are none. The .elc is compiled into the sandbox's lint/ dir
    and deleted -- never written next to the source.

    WARNING: byte-compilation runs in the live session, so each file's
    compile-time code (eval-when-compile, macro expansion, top-level
    requires) is EXECUTED there -- inherent to in-session linting. Lint
    untrusted code in a throwaway session. TIMEOUT arms the agent's
    in-Emacs with-timeout per file (fires while the compile services
    timers); the subprocess timeout sits ERT_RPC_SLACK above it as the
    hard backstop for tight-loop compiles.
    """
    if not files:
        raise ElateError("lint needs at least one file")
    sem = sess.semantic()
    items: list[dict[str, Any]] = []
    checked: list[str] = []
    for entry in files:
        path = Path(entry).expanduser().resolve()
        if not path.is_file():
            raise ElateError(f"lint file does not exist: {entry}")
        data = sem.rpc("lint", str(path), round(float(timeout), 3),
                       timeout=timeout + ERT_RPC_SLACK)
        checked.append(data.get("file") or str(path))
        items.extend(data.get("items") or [])
    return {"files": checked, "items": items, "clean": not items,
            "notes": LINT_NOTES}


# ---------------------------------------------------------------------------
# Profiler & benchmark (Phase 6)

# CLI/MCP spelling -> profiler.el mode symbol.
PROFILE_MODES = {"cpu": "cpu", "mem": "mem", "both": "cpu+mem"}
# Mirrors the agent's elate--profiler-max-depth: json-serialize caps
# nesting at ~50 levels and each tree level costs two, so depths past
# ~20 made deep-recursion reports fail to encode.
PROFILE_MAX_DEPTH = 20
BENCH_MAX_REPETITIONS = 1_000_000

PROFILE_NOTE = (
    "profiles and benchmarks depend on session history (loaded code, "
    "GC state, elate's own RPC servicing is sampled too) -- use a fresh "
    "session for authoritative numbers, like lint")


def _check_profile_mode(mode: str) -> str:
    if mode not in PROFILE_MODES:
        raise ElateError(
            f"unknown profile mode {mode!r} (use {'/'.join(PROFILE_MODES)})")
    return PROFILE_MODES[mode]


def _check_profile_depth(depth: int) -> int:
    if not isinstance(depth, int) or not 1 <= depth <= PROFILE_MAX_DEPTH:
        raise ElateError(
            f"profile depth must be between 1 and {PROFILE_MAX_DEPTH}, "
            f"got {depth!r}")
    return depth


def profile_start(sess: Session, mode: str = "cpu") -> dict[str, Any]:
    """Start Emacs's native profiler ('cpu, 'mem, or 'cpu+mem).

    Starting resets previously collected logs and runs a GC first (so
    pre-existing garbage is never charged to the window); a profile
    covers exactly one start..stop window. Errors if already running.
    """
    return sess.semantic().rpc("profiler", "start", _check_profile_mode(mode))


def profile_stop(sess: Session) -> dict[str, Any]:
    """Stop the profiler(s); collected logs are kept for `profile report`."""
    return sess.semantic().rpc("profiler", "stop")


def profile_report(sess: Session, depth: int = 6,
                   timeout: float = 30.0) -> dict[str, Any]:
    """Structured report over the collected profiler logs.

    Returns a "cpu" and/or "mem" section: total samples/bytes, a
    top-function list (self/total counts + percentages), and a
    depth-limited calltree (profiler.el's own unified tree) with
    truncation flags. Works while profiling and after `profile stop`.
    """
    data = sess.semantic().rpc("profiler", "report", None,
                               _check_profile_depth(depth), timeout=timeout)
    return {**data, "note": PROFILE_NOTE}


def profile_run(
    sess: Session,
    form: str,
    mode: str = "cpu",
    timeout: float = 15.0,
    depth: int = 6,
) -> dict[str, Any]:
    """One-shot profile: start, eval FORM, stop, report, in one result.

    FORM runs under the normal eval discipline (in-Emacs with-timeout +
    hard subprocess timeout, error + backtrace capture); its outcome is
    under "eval". If Emacs wedges during the eval, the profiler is
    stopped best-effort and the timeout propagates.
    """
    _check_profile_depth(depth)
    sem = sess.semantic()
    started = profile_start(sess, mode)
    try:
        ev = sem.eval_form(form, timeout=timeout)
    except ElateError as exc:
        # Busy/wedged Emacs: don't leave the sampler running forever.
        try:
            sem.rpc("profiler", "stop", timeout=5.0)
        except ElateError:
            # The best-effort stop also failed (Emacs still busy): the
            # sampler is likely STILL RUNNING -- say so in the error,
            # with the way out, instead of leaving it to be discovered
            # via the next start's "already running".
            raise type(exc)(
                f"{exc} -- note: the profiler is likely still running; "
                f"once Emacs is responsive (raw C-g can unblock a TTY "
                f"session), run: elate -s {sess.name} profile stop"
            ) from exc
        raise
    profile_stop(sess)
    report = profile_report(sess, depth=depth)
    return {"mode": mode,
            "sampling-interval": started.get("sampling-interval"),
            "eval": ev, **report}


def bench_form(
    sess: Session,
    form: str,
    repetitions: int = 1,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Benchmark FORM via the benchmark-run-compiled mechanism.

    The form is wrapped in a lambda and byte-compiled (interpreted
    fallback when compilation fails; "compiled"/"compile-error" report
    which path ran), then timed over REPETITIONS calls with
    benchmark-call: elapsed/mean seconds, GC runs + GC seconds, plus
    memory-use-counts and gcs-done/gc-elapsed deltas as allocation
    context. The in-Emacs timeout follows the eval discipline (fires at
    timer-servicing points; tight loops fall to the subprocess timeout,
    which sits ERT_RPC_SLACK above).
    """
    if not isinstance(repetitions, int) or not 1 <= repetitions <= BENCH_MAX_REPETITIONS:
        raise ElateError(
            f"bench repetitions must be between 1 and "
            f"{BENCH_MAX_REPETITIONS}, got {repetitions!r}")
    b64 = base64.b64encode(form.encode("utf-8")).decode("ascii")
    data = sess.semantic().rpc("bench", b64, repetitions,
                               round(float(timeout), 3),
                               timeout=timeout + ERT_RPC_SLACK)
    return {**data, "note": PROFILE_NOTE}


# ---------------------------------------------------------------------------
# Waiters

def state_dump(sess: Session, compact: bool = True) -> dict[str, Any]:
    """Best-effort state snapshot for timeout/error diagnostics.

    With COMPACT (the default), the bulky per-window visible text is
    dropped from the snapshot -- diagnostics need the prompt/echo/buffer
    facts, not a second copy of the screen.
    """
    dump: dict[str, Any] = {}
    try:
        state = sess.semantic().rpc("state", timeout=2.0)
        if compact:
            state = _compact_state(state)
        dump["state"] = state
    except ElateError as exc:
        dump["state_error"] = f"{exc} (Emacs busy?)"
    if sess.ui == "tty":
        try:
            screen = sess.raw().capture_pane().rstrip("\n").splitlines()
            dump["screen_tail"] = screen[-8:]
        except ElateError:
            pass
    return dump


def _compact_state(state: dict[str, Any]) -> dict[str, Any]:
    """STATE with per-window visible text replaced by a size marker."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            node = dict(node)
            if "text" in node and "buffer" in node:  # a window leaf
                node["text"] = f"({len(node.pop('text') or '')} chars elided)"
            if isinstance(node.get("children"), list):
                node["children"] = [strip(c) for c in node["children"]]
            return node
        return node

    out = dict(state)
    if "windows" in out:
        out["windows"] = strip(out["windows"])
    return out


def _wait_loop(sess: Session, timeout: float, what: str, probe) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_err: str | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
        except (EvalTimeout, TransportError, RpcError) as exc:
            # Busy Emacs, or a condition that cannot hold *yet* (e.g. the
            # waited-for buffer does not exist): keep polling until the
            # deadline; the timeout dump carries the last probe error.
            last_err = str(exc)
            result = None
        if result is not None:
            return result
        time.sleep(0.1)
    dump = state_dump(sess)
    if last_err:
        dump["last_probe_error"] = last_err
    raise WaitTimeout(f"timed out after {timeout:g}s waiting for {what}", state=dump)


def wait_idle(sess: Session, min_idle: float = 0.2, timeout: float = 10.0) -> dict[str, Any]:
    """Wait until Emacs answers promptly, has no pending input, and has
    been idle for at least MIN_IDLE seconds."""
    def probe() -> dict[str, Any] | None:
        data = sess.semantic().rpc("idle", timeout=2.0)
        idle = data.get("idle")
        if (
            not data.get("input-pending")
            and not data.get("unread")
            and isinstance(idle, (int, float))
            and idle >= min_idle
        ):
            return data
        return None

    return _wait_loop(sess, timeout, f"idle >= {min_idle:g}s", probe)


def wait_text(
    sess: Session,
    regexp: str,
    buffer: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Wait until REGEXP (Python regex syntax) matches BUFFER (default: current).

    A buffer that does not exist yet counts as "no match yet" and is
    polled until the deadline.
    """
    try:
        pattern = re.compile(regexp, re.MULTILINE)
    except re.error as exc:
        raise ElateError(f"invalid regexp {regexp!r}: {exc}") from exc

    def probe() -> dict[str, Any] | None:
        data = sess.semantic().rpc("buffer", buffer, timeout=3.0)
        m = pattern.search(data.get("text") or "")
        if m:
            return {"matched": m.group(0), "buffer": data.get("name"),
                    "start": m.start(), "end": m.end()}
        return None

    where = f"/{regexp}/ in buffer {buffer or '(current)'}"
    return _wait_loop(sess, timeout, where, probe)


def wait_prompt(sess: Session, timeout: float = 10.0) -> dict[str, Any]:
    """Wait until a minibuffer prompt is active; return its description."""
    def probe() -> dict[str, Any] | None:
        data = sess.semantic().rpc("state", timeout=2.0)
        mb = data.get("minibuffer")
        if mb:
            return {"prompt": mb.get("prompt"), "contents": mb.get("contents"),
                    "depth": mb.get("depth")}
        return None

    return _wait_loop(sess, timeout, "an active minibuffer prompt", probe)


# ---------------------------------------------------------------------------
# Input helpers

# GUI typing replays every character through the command loop; the cost is
# superlinear in the queue length (measured: 1k chars = 0.3s, 30k > 180s),
# and while the queue drains the session is busy and unobservable. Hence a
# hard cap, and chunked delivery (waiting for each chunk to be consumed)
# below it, so the semantic channel stays responsive throughout.
GUI_TYPE_LIMIT = 10000
_GUI_TYPE_CHUNK = 500
_GUI_TYPE_DRAIN_TIMEOUT = 30.0


def _wait_type_drained(sess: Session, timeout: float) -> None:
    """Wait until the queued (unread) input events have been consumed."""
    def probe() -> dict[str, Any] | None:
        data = sess.semantic().rpc("idle", timeout=2.0)
        return data if not data.get("unread") else None

    _wait_loop(sess, timeout, "queued type input to be consumed", probe)


def deliver_type(sess: Session, text: str) -> dict[str, Any]:
    """Type TEXT into the session, picking the channel by ui.

    TTY: raw terminal bytes via tmux (works even when Emacs is wedged).
    GUI: queued on unread-command-events via the semantic channel --
    behaves like typing (command loop, auto-indent, minibuffer submit on
    newline) but requires a responsive Emacs. GUI text is capped at
    GUI_TYPE_LIMIT characters and delivered in chunks, waiting for each
    chunk to drain, so the session never accumulates a minutes-long
    input backlog.
    """
    if sess.ui == "gui":
        if len(text) > GUI_TYPE_LIMIT:
            raise ElateError(
                f"GUI type is limited to {GUI_TYPE_LIMIT} characters "
                f"(got {len(text)}): GUI typing replays every character "
                "through the command loop, so large text leaves the session "
                "busy and uninterruptible for minutes. For bulk text, eval "
                "an insert instead, e.g. (with-current-buffer ... (insert ...))"
            )
        chunks = [text[i:i + _GUI_TYPE_CHUNK]
                  for i in range(0, len(text), _GUI_TYPE_CHUNK)] or [""]
        data: dict[str, Any] = {}
        queued = 0
        for i, chunk in enumerate(chunks):
            if i:  # the previous chunk must be consumed before queueing more
                _wait_type_drained(sess, _GUI_TYPE_DRAIN_TIMEOUT)
            b64 = base64.b64encode(chunk.encode("utf-8")).decode("ascii")
            data = sess.semantic().rpc("type", b64)
            queued += int(data.get("queued") or 0)
        return {"typed": text, "channel": "events", **data,
                "queued": queued, "chunks": len(chunks)}
    sess.raw().type_text(text)
    return {"typed": text, "channel": "raw"}


MOUSE_ACTIONS = ("click", "double", "drag", "wheel")


def mouse_event(
    sess: Session,
    *,
    action: str,
    button: int = 1,
    buffer: str | None = None,
    pos: int | None = None,
    line: int | None = None,
    col: int | None = None,
    part: str = "text",
    to_pos: int | None = None,
    to_line: int | None = None,
    to_col: int | None = None,
    direction: str = "down",
    count: int = 1,
    delivery: str = "macro",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Synthesize a mouse event inside the session's Emacs.

    Works for both TTY and GUI sessions and needs no OS permissions: the
    agent builds a real posn (posn-at-point / posn-at-x-y) at the target
    and dispatches a complete event sequence through the command loop, so
    the bindings that would fire for a human click fire here too.

    Targets: a buffer position (pos), a buffer line/column (line, col), or
    the mode line (part='mode-line', col = character offset into it); each
    optionally in the window showing BUFFER (default: selected window).
    Actions: click/double (button 1-3), drag (needs to_pos or
    to_line/to_col), wheel (direction up/down, count notches).
    """
    if action not in MOUSE_ACTIONS:
        raise ElateError(
            f"unknown mouse action {action!r} (use {'/'.join(MOUSE_ACTIONS)})")
    if button not in (1, 2, 3):
        raise ElateError(f"mouse button must be 1, 2, or 3, got {button}")
    if part not in ("text", "mode-line"):
        raise ElateError(f"unknown mouse target part {part!r} (text/mode-line)")
    if direction not in ("up", "down"):
        raise ElateError(f"wheel direction must be 'up' or 'down', got {direction!r}")
    if delivery not in ("macro", "events"):
        raise ElateError(f"unknown mouse delivery {delivery!r} (macro/events)")
    if not 1 <= count <= 50:
        raise ElateError(f"count must be between 1 and 50, got {count}")
    # Mirror the MCP schema bounds so CLI callers get a friendly error
    # instead of a raw elisp wholenump complaint.
    for label, value, minimum in (("pos", pos, 1), ("line", line, 1),
                                  ("col", col, 0), ("to_pos", to_pos, 1),
                                  ("to_line", to_line, 1), ("to_col", to_col, 0)):
        if value is not None and value < minimum:
            raise ElateError(f"mouse {label} must be >= {minimum}, got {value}")
    if action == "drag" and to_pos is None and to_line is None:
        raise ElateError("mouse drag needs a destination: to_pos or to_line/to_col")
    if action == "drag" and part == "mode-line":
        raise ElateError("mouse drag targets buffer text, not the mode line")
    payload = {
        "action": action,
        "button": button,
        "direction": direction,
        "count": count,
        "delivery": delivery,
        "target": {"buffer": buffer, "pos": pos, "line": line, "col": col,
                   "part": part},
        "to": {"pos": to_pos, "line": to_line, "col": to_col},
    }
    b64 = base64.b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii")
    return sess.semantic().rpc("mouse", b64, timeout=timeout)


def resize_session(sess: Session, cols: int, rows: int) -> dict[str, Any]:
    """Resize the live session to COLS x ROWS characters."""
    if cols < 10 or rows < 4:
        raise ElateError(f"implausible size {cols}x{rows}")
    sess.require_alive()
    if sess.ui == "gui":
        data = sess.semantic().rpc("resize", cols, rows)
    else:
        sess.raw().resize(cols, rows)
        data = {}
    sess.cols, sess.rows = cols, rows
    sess.save()
    sess.log("resize", cols=cols, rows=rows)
    return {"cols": cols, "rows": rows, **data}
