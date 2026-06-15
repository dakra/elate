"""MCP stdio server: a thin adapter over the elate session layer.

Every tool returns structured JSON as text content with an "ok" flag.
Error responses ({"ok": false, ...}) embed a compact state snapshot
(window layout, prompt, echo area, screen tail) whenever a session is
available, so the model can see *why* something failed in the same
round-trip.

Tool invocations are transcript-logged into the session's JSONL exactly
like CLI commands, tagged with via="mcp".
"""

from __future__ import annotations

import base64
import functools
import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio.to_thread
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import Field

from . import session as S
from .errors import ElateError, RpcError, SessionNotFound, WaitTimeout

INSTRUCTIONS = """\
elate spawns disposable, sandboxed Emacs sessions (fresh fake $HOME,
generated init) and lets you drive them like a user would: send keys,
click the mouse, evaluate elisp, observe the rendered screen and the
editor state. Use it to test-drive Emacs packages interactively.

Sessions are TTY (default; tmux-hosted terminal Emacs, text screenshots,
raw-keys escape hatch) or GUI (windowed Emacs; PNG screenshots via
elate_screenshot; no raw channel -- keys/type/mouse go through the
semantic channel).

Workflow: elate_start -> act (elate_keys / elate_type / elate_mouse /
elate_eval) -> elate_wait for the effect -> elate_state to see the full
scene. Prefer elate_wait over polling; prefer elate_state over piecing
together buffer/echo/messages calls. All responses are JSON; responses
with "ok": false carry an "error" and usually a "state" snapshot
explaining the situation.

For repeatable/deterministic checks, prefer a scenario over the
imperative loop: elate_run_script runs a whole JSON scenario (session +
ordered steps + assertions) in one call -- fresh sandbox, pass/fail,
exit-coded -- and is what you commit to a project's CI. Bootstrap one
from a session you drove by hand with the CLI's `export-script`, and run
it across Emacs versions with the CLI's `matrix`. The act->wait->observe
tools above are for exploration; the scenario is the asset.

Sessions persist across MCP reconnects; always elate_stop sessions you
started when you are done (GUI sessions own a visible desktop window).
Clean up stopped ones with elate_purge so heavy parallel runs stay
readable.
"""

server = FastMCP("elate", instructions=INSTRUCTIONS)

_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")

# Annotation shorthands. Everything is local (openWorldHint=False).
_READONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                            openWorldHint=False)


def _threaded(fn):
    """Run a sync tool body in a worker thread (anyio.to_thread).

    FastMCP calls plain sync tools on the event loop thread, which would
    freeze the whole server (pings included) for the duration of e.g. an
    elate_wait. The wrapper keeps the sync signature visible to FastMCP's
    schema generation (functools.wraps -> __wrapped__) while the actual
    body runs off-loop. abandon_on_cancel lets a cancelled request (or a
    client disconnect) return immediately instead of zombie-ing the server
    until an in-flight wait's deadline; the abandoned worker thread is a
    daemon and dies with the process.
    """
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs), abandon_on_cancel=True
        )
    return wrapper


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ok(payload: dict[str, Any]) -> str:
    return _json({"ok": True, **payload})


def _fail(exc: Exception, sess: S.Session | None = None) -> str:
    """Structured error; embeds a compact state snapshot when possible.

    Every exception -- not just ElateError -- is converted to the
    {"ok": false, ...} shape so the model always gets the documented
    contract; unexpected ones additionally log their traceback to stderr.
    """
    if isinstance(exc, ElateError):
        message = str(exc)
    else:
        traceback.print_exc(file=sys.stderr)
        message = f"{type(exc).__name__}: {exc}"
    payload: dict[str, Any] = {"ok": False, "error": message}
    if isinstance(exc, RpcError) and exc.backtrace:
        payload["backtrace"] = exc.backtrace
    # State dumps are {"state": ..., "screen_tail": ...}; spread them so
    # "state" in the error payload is the actual snapshot (not state.state).
    if isinstance(exc, WaitTimeout):
        payload.update(exc.state)
    elif isinstance(exc, SessionNotFound):
        payload["known_sessions"] = [s["name"] for s in S.list_sessions()]
    elif sess is not None:
        try:
            payload.update(S.state_dump(sess))
        except Exception:  # diagnostics must never mask the real error
            pass
    return _json(payload)


def _load(session: str, require_alive: bool = True) -> S.Session:
    sess = S.load_session(session)
    if require_alive:
        sess.require_alive()
    return sess


# ---------------------------------------------------------------------------
# Lifecycle

@server.tool(annotations=ToolAnnotations(readOnlyHint=False,
                                         destructiveHint=False,
                                         openWorldHint=False))
@_threaded
def elate_start(
    name: Annotated[str, Field(description=(
        "Session name (letters, digits, . _ -). Used as the 'session' "
        "argument of every other tool."))],
    ui: Annotated[Literal["tty", "gui"], Field(description=(
        "Session UI. 'tty' (default): tmux-hosted terminal Emacs -- text "
        "screenshots, raw-keys escape hatch, works everywhere. 'gui': a "
        "windowed Emacs on the desktop -- PNG screenshots (elate_screenshot "
        "returns an image), real GUI rendering; input must go through the "
        "semantic channel (no delivery='raw')."))] = "tty",
    headless: Annotated[bool, Field(description=(
        "GUI only: run under a private Xvfb display (Linux/CI). Not "
        "available on macOS."))] = False,
    emacs_path: Annotated[str | None, Field(description=(
        "Emacs binary to use (default: 'emacs' on PATH). Use for version-"
        "matrix testing."))] = None,
    config: Annotated[Literal["minimal", "bare", "init-file", "clean-install"],
                      Field(description=(
        "Sandbox config: 'minimal' (default; no startup screen, "
        "debug-on-error, deterministic test settings), 'bare' (emacs -Q "
        "plus the agent only), 'init-file' (load init_file), "
        "'clean-install' (minimal defaults, then INSTALL the load_paths "
        "package(s) for real via package-install-file into a "
        "sandbox-local package-user-dir -- verifies autoload cookies, "
        "Package-Requires, and byte-compilation of the installed copy, "
        "which load-path injection cannot; the sandbox has no network, "
        "so a dependency that is not built in fails with a clear "
        "init_error naming it; the response's 'installed' field reports "
        "name/version/install dir/compile warnings)."))] = "minimal",
    init_file: Annotated[str | None, Field(description=(
        "Path to a user init file; implies config='init-file', and is a "
        "loud error combined with config='bare'/'clean-install'. Still "
        "sandboxed (fake $HOME)."))] = None,
    load_paths: Annotated[list[str] | None, Field(description=(
        "Files/directories to put on load-path at startup; .el files are "
        "also loaded. Point this at the package under test. With "
        "config='clean-install' these are the install targets instead "
        "(.el file, package tar, or package directory) and at least one "
        "is required."))] = None,
    eval_forms: Annotated[list[str] | None, Field(description=(
        "Elisp forms evaluated at startup, inside the generated init -- so "
        "they run BEFORE emacs-startup-hook fires (set vars a package's "
        "auto-launch hook reads here). Order: init_file -> load_paths -> "
        "eval_files -> profiles -> eval_forms (so eval_forms can override a "
        "profile). Errors are caught as init_error instead of killing the "
        "session."))] = None,
    eval_files: Annotated[list[str] | None, Field(description=(
        "Elisp files loaded at startup (before emacs-startup-hook), like a "
        "reusable eval_forms with no load-path side effects -- put a shared "
        "setup snippet in a file instead of re-pasting it into every "
        "session."))] = None,
    profiles: Annotated[list[str] | None, Field(description=(
        "Named startup snippets resolved from "
        "$XDG_CONFIG_HOME/elate/profiles/NAME.el (a value with a '/' or "
        "ending in '.el' is a literal path); loaded like eval_files. For "
        "reusing the same setup across many sessions."))] = None,
    home_seed: Annotated[str | None, Field(description=(
        "Copy this fixture directory tree into the sandbox's fake $HOME "
        "before Emacs launches, so rc files (.bashrc/.zshrc/.config/...) "
        "are in place before any subprocess the session spawns -- the way "
        "to test shell integration while keeping the sandbox isolated."))]
        = None,
    size: Annotated[str, Field(description=(
        "Terminal size as COLSxROWS, e.g. '120x36'."))] = "120x36",
) -> str:
    """Start a new sandboxed Emacs session (TTY or GUI).

    Creates a throwaway sandbox (fake $HOME, generated init, private
    emacsclient socket), boots Emacs inside it (tmux-hosted -nw for
    ui='tty'; a desktop window for ui='gui'), and waits for the in-Emacs
    agent to answer. Returns session info incl. emacs_version and the
    sandbox path. If startup elisp signalled an error the session still
    runs and the response carries it as "init_error" -- check it. Fails
    if a session of that name is already running. After starting, drive
    it with elate_keys/elate_type/elate_mouse/elate_eval, observe with
    elate_state (or elate_screenshot for pixels), and elate_stop it when
    done. 'size' is COLSxROWS characters for both UIs (the GUI frame is
    measured in characters too).
    """
    try:
        m = _SIZE_RE.match(size)
        if not m:
            raise ElateError(f"size must be COLSxROWS, got {size!r}")
        sess = S.start_session(
            name,
            emacs=emacs_path,
            config=config,
            init_file=init_file,
            loads=load_paths or [],
            evals=eval_forms or [],
            eval_files=eval_files or [],
            profiles=profiles or [],
            home_seed=home_seed,
            cols=int(m.group(1)),
            rows=int(m.group(2)),
            ui=ui,
            headless=headless,
        )
        sess.log("mcp-start", name=name, via="mcp")
        return _ok(S.session_info(sess.name))
    except Exception as exc:
        return _fail(exc)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False,
                                         destructiveHint=True,
                                         idempotentHint=True,
                                         openWorldHint=False))
@_threaded
def elate_stop(
    session: Annotated[str, Field(description="Session name to stop.")],
) -> str:
    """Stop a session: kill its Emacs (and, for TTY sessions, the tmux
    server hosting it), keep the sandbox.

    The transcript and logs stay on disk (path was in elate_start's
    response). Safe to call on an already-dead session. Always stop the
    sessions you started.
    """
    try:
        return _ok(S.stop_session(session, via="mcp"))
    except Exception as exc:
        return _fail(exc)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False,
                                         destructiveHint=True,
                                         idempotentHint=True,
                                         openWorldHint=False))
@_threaded
def elate_purge(
    names: Annotated[list[str] | None, Field(description=(
        "Sessions to purge -- each must be stopped/dead. Naming a running "
        "session is an error."))] = None,
    all_sessions: Annotated[bool, Field(description=(
        "Purge every session that is not running (running ones are skipped "
        "and reported). Use instead of names to clean up everything."))]
        = False,
    stopped_older_than: Annotated[float | None, Field(ge=0, description=(
        "Only purge sessions inert at least this many seconds (by their "
        "idle_for); fresher ones are kept and reported under skipped_recent. "
        "Lets a heavy parallel run GC stale sandboxes without removing "
        "just-stopped ones."))] = None,
) -> str:
    """Delete the sandboxes (transcripts included) of stopped/dead sessions.

    The supported cleanup for sessions you elate_stop'd: stopped sandboxes
    are inert but pile up in elate_list otherwise. A running session is
    NEVER purged -- naming one is an error; with all_sessions it is skipped
    and reported. Leftover processes of dead sessions are cleaned up first;
    only directories directly under the sessions root are removed (a
    symlinked session dir is unlinked, not followed). Returns purged /
    skipped_running / skipped_recent / freed_bytes. Pass names or
    all_sessions (one required); stopped_older_than narrows either.
    """
    try:
        names = names or []
        if not names and not all_sessions:
            raise ElateError("elate_purge needs names or all_sessions=true")
        return _ok(S.purge_sessions(names, all_sessions=all_sessions,
                                    stopped_older_than=stopped_older_than))
    except Exception as exc:
        return _fail(exc)


@server.tool(annotations=_READONLY)
@_threaded
def elate_list() -> str:
    """List all known elate sessions with status (running/dead/stopped).

    Sessions survive MCP reconnects -- use this to rediscover a session
    you started earlier, or to find leftovers to elate_stop. Summary
    fields only; elate_info has the full details (incl. init_error).
    """
    try:
        return _ok({"sessions": S.list_sessions()})
    except Exception as exc:
        return _fail(exc)


@server.tool(annotations=_READONLY)
@_threaded
def elate_info(
    session: Annotated[str, Field(description="Session name.")],
) -> str:
    """Full details for one session, alive or not.

    Returns status, pid, emacs binary + version, config mode, terminal
    size, sandbox path, tmux socket, uptime, and init_error (an error
    signalled by startup elisp -- re-check it after a reconnect, it is
    otherwise only reported in the elate_start response). Works on dead
    and stopped sessions too.
    """
    try:
        return _ok(S.session_info(session))
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Input

@server.tool(annotations=_MUTATING)
@_threaded
def elate_keys(
    session: Annotated[str, Field(description="Session name.")],
    keys: Annotated[str, Field(description=(
        "Key sequence in Emacs kbd notation, e.g. 'C-x C-f', "
        "'M-x my-mode RET', 'a b RET', 'SPC', '<down>'."))],
    delivery: Annotated[Literal["semantic", "events", "raw"], Field(description=(
        "How to deliver the keys. 'semantic' (default): execute-kbd-macro "
        "inside Emacs -- synchronous and precise. It runs through the command "
        "loop, so the keys obey active keymaps (in evil normal state plain "
        "letters are commands, not text), a sequence ending with an open "
        "prompt does NOT hold it open (a bare 'M-x' errors), and a command "
        "that rings the bell aborts the whole sequence (the error names the "
        "culprit). 'events': queue on unread-command-events -- asynchronous; "
        "USE THIS to open a minibuffer prompt and leave it open, or to "
        "deliver past a command that rings the bell (events is not a macro, "
        "so a bell merely beeps). 'raw': real terminal bytes via tmux -- the "
        "escape hatch that works even when Emacs is busy/wedged (e.g. send "
        "'C-g' raw to unblock); cannot encode every chord (e.g. C-%), and is "
        "TTY-only (GUI sessions have no raw channel)."))] = "semantic",
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Seconds before a semantic delivery is declared blocked "
        "(0 < timeout <= 120)."))] = 15.0,
) -> str:
    """Send a key sequence to the session.

    After sending, elate_wait (condition='prompt' if you opened one, else
    'idle') and then elate_state to observe the effect. If a semantic
    delivery times out, the keys probably left Emacs reading input --
    retry with delivery='events' or 'raw'.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("keys", keys=keys, channel="raw" if delivery == "raw" else "semantic",
                 method="events" if delivery == "events" else "macro", via="mcp")
        if delivery == "raw":
            sess.raw().send_kbd(keys)
            return _ok({"keys": keys, "channel": "raw"})
        method = "events" if delivery == "events" else "macro"
        data = sess.semantic().rpc("keys", keys, method, timeout=timeout)
        return _ok({"keys": keys, "channel": "semantic", **data})
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_type(
    session: Annotated[str, Field(description="Session name.")],
    text: Annotated[str, Field(description=(
        "Literal text to type -- no kbd parsing, every character is sent "
        "as-is. Avoid ESC and other control characters: ESC acts as Meta "
        "and an undefined escape sequence can drop the session into the "
        "elisp debugger (use elate_keys for chords/named keys). Newlines "
        "press RET, so auto-indent and minibuffer submission happen as "
        f"if typed. GUI sessions accept at most {S.GUI_TYPE_LIMIT} "
        "characters: GUI typing is per-character through the command "
        "loop, so large text means a long-busy session -- use elate_eval "
        "with insert for bulk text."))],
) -> str:
    """Type literal text into the session as if at the keyboard.

    TTY sessions: raw terminal bytes via tmux (works even when Emacs is
    wedged). GUI sessions: queued on unread-command-events through the
    semantic channel -- same typing semantics, but needs a responsive
    Emacs, is delivered in chunks (each waited on, keeping the session
    observable), and is capped because per-character command-loop
    delivery makes large text slow. Use for filling in prompts or
    buffers with arbitrary text (including text that would be awkward
    in kbd notation). For key chords or named keys use elate_keys. For
    bulk text setup, an elate_eval insert is faster and does not go
    through the command loop.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("type", text=text, via="mcp",
                 channel="raw" if sess.ui == "tty" else "events")
        return _ok(S.deliver_type(sess, text))
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_send_process(
    session: Annotated[str, Field(description="Session name.")],
    buffer: Annotated[str | None, Field(description=(
        "Buffer whose subprocess to target. Default: the current (selected "
        "window's) buffer. Errors if the buffer has no live process."))] = None,
    text: Annotated[str | None, Field(description=(
        "Literal text to send to the process (e.g. a shell command plus a "
        "trailing newline). Give exactly one of text/char/file."))] = None,
    char: Annotated[str | None, Field(description=(
        "An Emacs kbd string to send instead of literal text: 'C-c' sends "
        "^C (SIGINT to a shell's foreground job), 'RET' a newline, 'TAB' a "
        "tab."))] = None,
    file: Annotated[str | None, Field(description=(
        "Path whose contents to send (read inside Emacs, so it is not bound "
        "by the argv size limit -- use for large payloads)."))] = None,
) -> str:
    """Send raw input to a buffer's subprocess (comint/REPL/shell/terminal).

    Writes straight to the process behind the buffer (process-send-string),
    bypassing the command loop. Unlike elate_keys/elate_type -- which drive
    Emacs -- this drives the *subprocess*: interrupt a job with char='C-c',
    feed a REPL, or seed shell input. Errors when the buffer has no live
    process. Returns the process name, buffer, and bytes sent.
    """
    sess = None
    try:
        given = [x for x in (text, char, file) if x is not None]
        if len(given) != 1:
            raise ElateError("elate_send_process needs exactly one of "
                             "text/char/file")
        sess = _load(session)
        chan = sess.semantic()
        if file is not None:
            data = chan.rpc("send-process-file", file, buffer)
            kind = "file"
        else:
            payload = char if char is not None else text
            b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
            data = chan.rpc("send-process", buffer, b64, char is not None)
            kind = "char" if char is not None else "text"
        sess.log("send-process", buffer=buffer, kind=kind, via="mcp")
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_mouse(
    session: Annotated[str, Field(description="Session name.")],
    action: Annotated[Literal["click", "double", "drag", "wheel"], Field(
        description=(
            "'click'/'double': press the given button at the target. "
            "'drag': button down at the target, drag event to "
            "to_pos/to_line (selects a region with button 1). 'wheel': "
            "scroll 'count' notches in 'direction' over the target."))],
    button: Annotated[int, Field(ge=1, le=3, description=(
        "Mouse button: 1 (left; follows links/buttons via follow-link), "
        "2 (middle; push-button on widgets), 3 (right/context). Ignored "
        "for wheel."))] = 1,
    buffer: Annotated[str | None, Field(description=(
        "Target the window displaying this buffer (it must be visible "
        "in a window). Default: the selected window."))] = None,
    pos: Annotated[int | None, Field(ge=1, description=(
        "Buffer position to click (must be visible in the window; "
        "out-of-range values clamp to the buffer bounds). Default: the "
        "window's point."))] = None,
    line: Annotated[int | None, Field(ge=1, description=(
        "Buffer line (1-based, counted within the accessible/narrowed "
        "region; clamps to the last line) to click, alternative to "
        "pos."))] = None,
    col: Annotated[int | None, Field(ge=0, description=(
        "Column (0-based; clamps to end of line): with 'line', the "
        "buffer column; with part='mode-line', the character offset "
        "into the mode line."))] = None,
    part: Annotated[Literal["text", "mode-line"], Field(description=(
        "'text' (default): buffer text. 'mode-line': the window's mode "
        "line (e.g. mouse-1 selects that window). Caveat: double-click "
        "on a real mode line is faithful Emacs and runs "
        "mouse-delete-other-windows -- your window layout will "
        "change."))] = "text",
    to_pos: Annotated[int | None, Field(ge=1, description=(
        "For drag: end buffer position."))] = None,
    to_line: Annotated[int | None, Field(ge=1, description=(
        "For drag: end line (with to_col)."))] = None,
    to_col: Annotated[int | None, Field(ge=0, description=(
        "For drag: end column."))] = None,
    direction: Annotated[Literal["up", "down"], Field(description=(
        "For wheel: scroll direction ('down' moves text up)."))] = "down",
    count: Annotated[int, Field(ge=1, le=50, description=(
        "For wheel: number of wheel notches."))] = 1,
    delivery: Annotated[Literal["macro", "events"], Field(description=(
        "'macro' (default): dispatch synchronously and return when the "
        "triggered command finished. 'events': queue on "
        "unread-command-events -- use when the triggered command itself "
        "reads input (menus, prompts)."))] = "macro",
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Seconds before a synchronous dispatch is declared blocked."))] = 15.0,
) -> str:
    """Synthesize a mouse interaction at a buffer position or mode line.

    Works for both TTY and GUI sessions and needs no OS permissions: a
    real posn is built at the target inside Emacs and a complete event
    sequence (down + click, drag, or wheel) is dispatched through the
    command loop, so exactly the bindings a human click would trigger
    fire here (buttons, mouse-1 follow-link, mode-line maps, mwheel
    scrolling, region-by-drag). The target must be visible in a window.
    After 'events' delivery, elate_wait condition='idle' then elate_state
    to observe the effect. Caveat (config='bare' only): stock Emacs
    silently ignores a mouse-2 click within 0.35s of a wheel scroll
    (mouse-wheel-inhibit-click-time); the default 'minimal' config
    disables that for deterministic runs.
    """
    sess = None
    try:
        sess = _load(session)
        kwargs = dict(action=action, button=button, buffer=buffer, pos=pos,
                      line=line, col=col, part=part, to_pos=to_pos,
                      to_line=to_line, to_col=to_col, direction=direction,
                      count=count, delivery=delivery)
        sess.log("mouse", via="mcp", **kwargs)
        return _ok(S.mouse_event(sess, timeout=timeout, **kwargs))
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_focus(
    session: Annotated[str, Field(description="Session name.")],
    direction: Annotated[Literal["in", "out"], Field(description=(
        "'in' injects a focus-in event, 'out' a focus-out event."))],
    frame: Annotated[str | None, Field(description=(
        "Target the frame with this name (its 'name' frame parameter). "
        "Default: the selected frame."))] = None,
    set_focus_state: Annotated[bool, Field(description=(
        "Also make (frame-focus-state) report the injected state. This is a "
        "NON-NATIVE shim (advice on frame-focus-state): an injected event "
        "cannot move the real C-owned focus state, though it always fires "
        "after-focus-change-function and sets the last-focus-update frame "
        "parameter. Enable only if the code under test reads "
        "(frame-focus-state)."))] = False,
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Seconds before delivery/drain is declared blocked."))] = 15.0,
) -> str:
    """Inject a window-system focus-in/focus-out event.

    Runs handle-focus-in / handle-focus-out through special-event-map
    exactly as a real window-system focus change would -- firing
    after-focus-change-function and setting the last-focus-update frame
    parameter -- by queueing the (focus-in FRAME) / (focus-out FRAME) event
    on unread-command-events and draining it. Works for TTY and GUI
    sessions. For an event ordered against clicks/keys, use
    elate_send_events. After it returns the event has fired; observe with
    elate_state.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("focus", via="mcp", direction=direction, frame=frame,
                 set_focus_state=set_focus_state)
        return _ok(S.focus_event(sess, direction, frame=frame,
                                 set_focus_state=set_focus_state,
                                 timeout=timeout))
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_send_events(
    session: Annotated[str, Field(description="Session name.")],
    events: Annotated[list[str], Field(description=(
        "Ordered event tokens. Each is one of: 'focus-in' / 'focus-out'; a "
        "mouse event 'down-mouse-N' / 'mouse-N' / 'up-mouse-N' / "
        "'double-mouse-N' / 'wheel-up' / 'wheel-down' (N=1..3) with an "
        "optional location '@LINE,COL' (1-based line, 0-based col) or '#POS' "
        "(1-based buffer position; default: the window's point); or 'key:KBD' "
        "for a key sequence, e.g. 'key:RET', 'key:C-x'. Example: "
        "['focus-in', 'down-mouse-1@10,5', 'mouse-1@10,5']."))],
    buffer: Annotated[str | None, Field(description=(
        "Target the window displaying this buffer for mouse events (it must "
        "be visible). Default: the selected window."))] = None,
    frame: Annotated[str | None, Field(description=(
        "Target frame for focus events. Default: the selected frame."))] = None,
    set_focus_state: Annotated[bool, Field(description=(
        "Also make (frame-focus-state) report injected focus (non-native "
        "shim; see elate_focus)."))] = False,
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Seconds before delivery/drain of a batch is declared blocked."))] = 15.0,
) -> str:
    """Inject an ordered stream of focus/mouse/key events.

    The events drain through the real command loop in order, so a focus
    event's after-focus-change-function hooks run before a following click's
    command -- the ordering that distinguishes a click-to-refocus from a
    plain click. A focus event only fires at the head of a command-loop
    turn, so any focus events are delivered in separate, drained batches
    automatically; this makes any ordering faithful, including a mouse-down
    dispatched before a focus-in. Lower-level than elate_mouse: each mouse
    token is exactly one event (no implicit down+click pair). Works for TTY
    and GUI sessions; observe the effect afterwards with elate_state.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("send-events", via="mcp", events=events, buffer=buffer,
                 frame=frame, set_focus_state=set_focus_state)
        return _ok(S.send_events(sess, events, buffer=buffer, frame=frame,
                                 set_focus_state=set_focus_state,
                                 timeout=timeout))
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_eval(
    session: Annotated[str, Field(description="Session name.")],
    form: Annotated[str, Field(description=(
        "Elisp source: one or more forms, evaluated as (progn ...)."))],
    timeout: Annotated[float, Field(gt=0, le=600, description=(
        "Hard timeout in seconds (0 < timeout <= 600). A blocking form is "
        "interrupted (or, if truly wedged, reported as busy)."))] = 15.0,
    backtrace: Annotated[bool, Field(description=(
        "On error, also return structured 'frames' (each: function name + "
        "printed args) alongside the rendered 'backtrace' string. Off by "
        "default to keep replies small."))] = False,
) -> str:
    """Evaluate elisp in the session; the precision instrument.

    Returns the printed value, the *Messages* delta it produced, and on
    failure "error" + a full "backtrace" plus a state snapshot (with
    backtrace=true, also structured "frames"). Values longer than 64 KiB
    come back with truncated=true and the full value-length -- narrow your
    form instead of re-fetching. The form runs in the live interactive
    Emacs (not batch), so UI side effects are real.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("eval", form=form, timeout=timeout, via="mcp")
        data = sess.semantic().eval_form(form, timeout=timeout,
                                         backtrace=backtrace)
        sess.log("eval-result", **data)
        if data.get("error"):
            payload: dict[str, Any] = {"ok": False, **data}
            try:
                payload.update(S.state_dump(sess))
            except Exception:
                pass
            return _json(payload)
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


# ---------------------------------------------------------------------------
# Testing & lint

@server.tool(annotations=_MUTATING)
@_threaded
def elate_test(
    session: Annotated[str, Field(description="Session name.")],
    selector: Annotated[str, Field(description=(
        "ERT selector: 't' (all tests, default), a test name, a name "
        "regexp (e.g. 'my-pkg-'), '(tag NAME)', '(not \"slow\")', "
        "':failed', ':new', or any compound selector."))] = "t",
    load_files: Annotated[list[str] | None, Field(description=(
        "Elisp test files to load (by path) before the run. Tests must "
        "be loaded -- via this, elate_start load_paths, or elate_eval -- "
        "before a selector can match them. A load error fails the call "
        "with a backtrace."))] = None,
    timeout: Annotated[float, Field(gt=0, le=600, description=(
        "In-Emacs timeout for the whole run in seconds (0 < timeout <= "
        "600). A test stuck in a timer-servicing wait is interrupted; "
        "the response then carries timed-out=true, the partial results, "
        "and the interrupted test (its name in 'interrupted', its entry "
        "in 'tests' with status 'aborted')."))] = 60.0,
) -> str:
    """Run ERT tests interactively inside the live session.

    Unlike batch ERT, tests run in the real interactive Emacs (live
    redisplay, real window/frame state, working minibuffer), so UI bugs
    that `emacs --batch' cannot see are caught. Results are structural
    (collected from ERT's result objects, never scraped from the *ert*
    buffer): counts (total/passed/failed/errors/skipped/unexpected,
    duration) plus per-test name, status, duration, captured *Messages*
    output, and -- for failures/errors -- the condition and a trimmed
    backtrace. Test failures are data, not tool errors: the response
    stays ok=true; check "unexpected" (and "timed-out") to judge the
    run. A test that signals quit (keyboard-quit, or a raw C-g hitting
    its body) is recorded with status "quit" and the run simply moves
    on -- no prompt, no hang. ok=false means infrastructure trouble
    (load error, dead session, hard timeout).
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("test", selector=selector, load_files=load_files,
                 timeout=timeout, via="mcp")
        data = S.run_ert(sess, selector=selector,
                         load_files=load_files or [], timeout=timeout)
        sess.log("test-result", total=data.get("total"),
                 unexpected=data.get("unexpected"),
                 timed_out=data.get("timed-out"))
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_lint(
    session: Annotated[str, Field(description="Session name.")],
    files: Annotated[list[str], Field(description=(
        "Elisp files to lint, as paths (the agent reads them in-session; "
        "file contents never travel over the transport)."))],
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Per-file in-Emacs timeout in seconds (0 < timeout <= 120); a "
        "lint whose compile-time code hangs is interrupted and reported "
        "as a clean error, leaving no residue."))] = 60.0,
    package_lint: Annotated[bool, Field(description=(
        "ALSO run package-lint (off by default; additive -- items are "
        "tagged tool='package-lint'). package-lint is an external "
        "package installed into the sandbox elpa/ on first use. Without "
        "'archive_dir' it refreshes the standard archives (GNU + nongnu "
        "+ MELPA) over the NETWORK, which is non-deterministic (archive "
        "contents move over time); prefer 'archive_dir' for reproducible "
        "results. A setup failure (offline, package-lint not in the "
        "archive, an indexless archive) aborts the lint with a clear "
        "error and a state snapshot -- the session and channel "
        "survive."))] = False,
    archive_dir: Annotated[str | None, Field(description=(
        "For package_lint=true: a local directory holding an "
        "archive-contents index, used directly as a package archive (a "
        "plain path, not a file:// URL). This is the OFFLINE, "
        "REPRODUCIBLE path (the recommended way to run package-lint, and "
        "the answer to its archive non-determinism); without it the "
        "network archives are refreshed live. Ignored unless "
        "package_lint is true."))] = None,
) -> str:
    """Lint elisp files inside the session: byte-compile + checkdoc.

    WARNING: byte-compilation runs IN the live session, so each file's
    compile-time code (eval-when-compile, macro expansion, top-level
    requires) is EXECUTED there and can mutate session state -- that is
    inherent to in-session linting against the session's load-path (it
    is also why the package under test resolves). Lint untrusted code
    in a throwaway session you stop afterwards. Results can likewise
    depend on session history (functions defined by an earlier load or
    by an earlier lint's eval-when-compile/require code silence
    undefined-function warnings a fresh session would emit).

    Returns "items": a list of {file, tool, line, col, severity,
    message} ('tool' is byte-compile, checkdoc, or -- with
    package_lint=true -- package-lint; line/col may be null for
    file-level findings), plus "clean" (true when there are none). The
    byte-compilation writes its .elc into the sandbox and deletes it --
    never next to the source. native-comp warnings are not collected;
    "notes" explains the omissions and the package-lint
    network/archive tradeoff.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("lint", files=files, timeout=timeout,
                 package_lint=package_lint, archive_dir=archive_dir,
                 via="mcp")
        data = S.lint_files(sess, files, timeout=timeout,
                            package_lint=package_lint, archive_dir=archive_dir)
        sess.log("lint-result", files=len(data["files"]),
                 items=len(data["items"]))
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


# ---------------------------------------------------------------------------
# Profiler & benchmark (Phase 6)

@server.tool(annotations=_MUTATING)
@_threaded
def elate_profile(
    session: Annotated[str, Field(description="Session name.")],
    action: Annotated[Literal["start", "stop", "report", "run"], Field(
        description=(
            "'run' (recommended): start -> evaluate 'form' -> stop -> "
            "report, one structured result. 'start'/'stop' bracket a "
            "manual window (e.g. around keys/mouse interactions); "
            "'report' renders what was collected -- it works while "
            "profiling and after stop."))],
    mode: Annotated[Literal["cpu", "mem", "both"], Field(description=(
        "What to sample (for start/run): 'cpu' (default; periodic SIGPROF "
        "samples), 'mem' (a sample at every allocation, counts are "
        "bytes), or 'both'."))] = "cpu",
    form: Annotated[str | None, Field(description=(
        "For action='run': the elisp to profile, evaluated like "
        "elate_eval. Its value/error/backtrace come back under 'eval'; "
        "a form that signalled keeps ok=true (check eval.error) -- the "
        "profile up to the error is still reported."))] = None,
    depth: Annotated[int, Field(ge=1, le=20, description=(
        "Calltree depth limit for report/run (default 6). The tree is "
        "also capped in total nodes; truncation is flagged per "
        "node ('children-truncated') and per tree ('tree-truncated')."))] = 6,
    timeout: Annotated[float, Field(gt=0, le=600, description=(
        "For action='run': eval timeout in seconds (0 < timeout <= "
        "600)."))] = 15.0,
) -> str:
    """Profile elisp with Emacs's native sampling profiler.

    Reports are structured, not the profiler-report UI buffer: per mode
    ('cpu' in samples, 'mem' in bytes) you get 'total', a 'functions'
    list (name, self/total counts and percentages, sorted by self
    time), and a depth-limited 'tree' (profiler.el's unified calltree)
    with truncation flags. 'start' resets earlier logs and runs a GC
    first (pre-existing garbage is never charged to the window), so a
    profile covers exactly one start..stop window; report after stop
    keeps working until the next start. IMPORTANT: profiles are
    session-history dependent -- every piece of code the session runs
    (including elate's own request servicing) lands in the samples, so
    profile in a fresh throwaway session for authoritative numbers,
    the same advice as elate_lint.
    """
    sess = None
    try:
        sess = _load(session)
        if action == "run":
            if not form:
                raise ElateError("action='run' needs a 'form' to profile")
            sess.log("profile", action="run", mode=mode, form=form,
                     timeout=timeout, depth=depth, via="mcp")
            return _ok(S.profile_run(sess, form, mode=mode,
                                     timeout=timeout, depth=depth))
        if form is not None:
            raise ElateError(
                f"action='{action}' takes no 'form' (only 'run' does)")
        sess.log("profile", action=action, mode=mode, via="mcp")
        if action == "start":
            return _ok(S.profile_start(sess, mode))
        if action == "stop":
            return _ok(S.profile_stop(sess))
        return _ok(S.profile_report(sess, depth=depth))
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_bench(
    session: Annotated[str, Field(description="Session name.")],
    form: Annotated[str, Field(description=(
        "Elisp to benchmark: one or more forms, wrapped in a lambda and "
        "byte-compiled before timing (the benchmark-run-compiled "
        "mechanism); when compilation fails the interpreted closure is "
        "timed instead and 'compiled'/'compile-error' say so."))],
    repetitions: Annotated[int, Field(ge=1, le=1_000_000, description=(
        "How many times to call the form (default 1). Use enough "
        "repetitions that 'elapsed' is well above timer resolution; "
        "'mean' is elapsed/repetitions."))] = 1,
    timeout: Annotated[float, Field(gt=0, le=600, description=(
        "In-Emacs timeout for the whole run in seconds (0 < timeout <= "
        "600); like eval it fires at timer-servicing points, a tight "
        "loop falls to the hard subprocess timeout just above it."))] = 60.0,
) -> str:
    """Benchmark an elisp form: elapsed/mean time, GC and allocation cost.

    Returns 'elapsed' (total seconds), 'mean' (per repetition), GC
    activity during the run ('gc-runs', 'gc-elapsed', plus
    gcs-done/gc-elapsed deltas), and 'memory-deltas': the
    memory-use-counts deltas (conses, floats, vector-cells, symbols,
    string-chars, intervals, strings allocated) -- the allocation
    profile of the form. Errors signalled by the form come back with a
    backtrace, like elate_eval. IMPORTANT: numbers depend on session
    history (loaded code, GC state) -- benchmark in a fresh throwaway
    session for authoritative results, the same advice as elate_lint.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("bench", form=form, repetitions=repetitions,
                 timeout=timeout, via="mcp")
        data = S.bench_form(sess, form, repetitions=repetitions,
                            timeout=timeout)
        if data.get("error"):
            payload: dict[str, Any] = {"ok": False, **data}
            try:
                payload.update(S.state_dump(sess))
            except Exception:
                pass
            return _json(payload)
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_trace(
    session: Annotated[str, Field(description="Session name.")],
    action: Annotated[Literal["on", "off", "read"], Field(description=(
        "'on': start tracing the named functions (functions required). "
        "'off': untrace the named functions, or ALL of them when none "
        "named. 'read': return the accumulated call/arg/return log and "
        "(unless keep=true) clear it, so the next read sees only new "
        "calls. Typical flow: trace on -> drive the session "
        "(elate_keys/elate_eval) -> trace read."))],
    functions: Annotated[list[str] | None, Field(description=(
        "Function names. Required for action='on'; for 'off' the functions "
        "to untrace (omit to untrace everything); ignored for 'read'."))]
        = None,
    keep: Annotated[bool, Field(description=(
        "For action='read': keep the log instead of clearing it."))] = False,
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Seconds before the call is declared blocked (0 < timeout <= "
        "120)."))] = 15.0,
) -> str:
    """Trace elisp functions: log each call's args and return value.

    'on' wraps trace-function around the named functions; drive the
    session, then 'read' returns the *trace-output* log (and clears it
    unless keep=true, so each read sees only new calls). 'off' untraces
    the named functions or all of them. Surfaces internals you cannot see
    on screen -- why an advice fires twice, what args a hook receives.
    Tracing a macro or an undefined function is an error; already-traced
    functions are reported under 'already', not re-armed.
    """
    sess = None
    try:
        sess = _load(session)
        sess.log("trace", action=action, functions=functions, keep=keep,
                 via="mcp")
        data = S.trace_functions(sess, action, functions=functions,
                                 keep=keep, timeout=timeout)
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


# ---------------------------------------------------------------------------
# Scenario scripts & recording (Phase 5)

@server.tool(annotations=_MUTATING)
@_threaded
def elate_run_script(
    script: Annotated[str, Field(description=(
        "Path to a JSON scenario file (see the README's 'Scenario "
        "scripts' section): {\"name\": ..., \"session\": {ui, size, "
        "config, load, eval}, \"steps\": [...]}. Steps mirror the other "
        "tools (keys/type/eval/wait/mouse/focus/send_events/test/lint/"
        "screenshot/resize) "
        "plus \"assert\" steps (buffer_contains, buffer_matches, state, "
        "messages_match, popup, tests, lint_clean, eval). Pass a file "
        "path -- the server reads the file; relative paths inside the "
        "script resolve against the script's directory. NOTE: a "
        "\"screenshot\" step with an output path creates/overwrites the "
        "file wherever that path points."))],
    keep_on_failure: Annotated[bool, Field(description=(
        "Keep the fresh session running when the script fails, so you "
        "can inspect it (elate_state / elate_screenshot) -- then "
        "elate_stop it yourself; the response's 'session' field has its "
        "name. On success the session is always torn down."))] = False,
    timeout: Annotated[float, Field(gt=0, le=600, description=(
        "Overall wall-clock budget for the whole run in seconds "
        "(0 < timeout <= 600). Steps not started by the deadline fail; "
        "each step additionally honors its own timeout."))] = 300.0,
    emacs: Annotated[str | None, Field(description=(
        "Override the script's emacs binary: an absolute path or a bare "
        "PATH name. Call once per installed Emacs to version-matrix a "
        "script (the CLI's 'matrix' verb wraps exactly this)."))] = None,
    update_snapshots: Annotated[bool, Field(description=(
        "For 'snapshot' assert steps: write/overwrite the golden artifacts "
        "instead of comparing (the run still executes every step). This "
        "writes files under the scenario's __snapshots__ directory in the "
        "repo -- review the diff and commit deliberately."))] = False,
    snapshot_dir: Annotated[str | None, Field(description=(
        "Base directory for golden snapshots; resolved against the "
        "scenario's directory. Default: <scenario-dir>/__snapshots__."))]
        = None,
) -> str:
    """Execute a whole scenario script in one call: fresh session, steps,
    assertions, teardown.

    The script runs in a fresh throwaway session built from its
    "session" config (deliberate: lint executes compile-time code and
    lint/test results depend on session history, so only a fresh session
    gives reproducible verdicts), executes the steps in order, and stops
    at the first failure. Script failures are data, not tool errors: the
    response stays ok=true -- judge the run by "success" and the
    per-step "steps" list (a failed step embeds the error and a state
    snapshot; later steps are recorded as not-run). A run whose session
    startup eval signalled an error fails with no steps run (set
    "allow_init_error": true in the script's "session" block to run
    anyway). ok=false means the script could not run at all
    (unreadable/invalid script file, session boot failure). Use this
    instead of many single-step calls when a scenario is already written
    down -- one round-trip runs it all.
    """
    try:
        from . import script as SC

        sc, base = SC.load_script(script)
        sdir = (base / snapshot_dir) if snapshot_dir else None
        result = SC.run_script(sc, base_dir=base, emacs=emacs,
                               keep_on_failure=keep_on_failure,
                               deadline=time.monotonic() + timeout,
                               origin="mcp",
                               update_snapshots=update_snapshots,
                               snapshot_dir=sdir,
                               snapshot_stem=Path(script).stem)
        return _ok(result)
    except Exception as exc:
        return _fail(exc)


@server.tool(annotations=_MUTATING)
@_threaded
def elate_record(
    session: Annotated[str, Field(description="Session name.")],
    action: Annotated[Literal["start", "stop", "status"], Field(description=(
        "'start': begin recording the session's terminal output. "
        "'stop': finish the recording and report the file, event count, "
        "and duration. 'status': inspect the active recording without "
        "changing it."))],
    output: Annotated[str | None, Field(description=(
        "For 'start': the .cast output path -- the file is created or "
        "OVERWRITTEN wherever this points (default: "
        "<session>/log/<name>-<time>.cast; the default response always "
        "tells you the path)."))] = None,
) -> str:
    """Record a TTY session's terminal output as an asciicast v2 file.

    Captures everything the session's tmux pane outputs (keystrokes'
    effects, redisplay, colors) with timestamps; the first event replays
    the screen as it looked at start, so playback begins from the
    correct picture. Play the file with `asciinema play`, render a GIF
    with `agg` -- useful as a package demo generator. One recording per
    session at a time; recording survives this MCP connection (it is
    attached to the tmux pane) until 'stop'. If the session's Emacs dies
    mid-recording, 'status' reports stale=true and 'stop' finalizes the
    cast with everything up to the crash. TTY sessions only: GUI
    sessions have no terminal byte stream (use elate_screenshot, or the
    CLI's 'snap' series, instead).
    """
    sess = None
    try:
        from . import record as R

        sess = _load(session, require_alive=False)
        if action == "start":
            return _ok(R.start_recording(sess, output=output))
        if output is not None:
            raise ElateError("'output' applies to action='start' only")
        if action == "stop":
            return _ok(R.stop_recording(sess))
        return _ok(R.recording_status(sess))
    except Exception as exc:
        return _fail(exc, sess)


# ---------------------------------------------------------------------------
# Observation

@server.tool(annotations=_READONLY)
@_threaded
def elate_state(
    session: Annotated[str, Field(description="Session name.")],
    since: Annotated[str | None, Field(description=(
        "Opaque token from a prior elate_state result. When given, return "
        "only what changed since then -- buffers added/removed/modified, "
        "point and selected-buffer movement, new *Messages* lines, and "
        "minibuffer open/close/prompt changes -- as a compact 'delta' "
        "(much cheaper than a full snapshot; 'changed':false means your "
        "last action did nothing observable). Every result carries a fresh "
        "'token' for the next call; an unknown/stale token degrades to a "
        "full snapshot with since_status='unknown'."))] = None,
) -> str:
    """Full scene snapshot in one round-trip -- the main observation tool.

    Returns: current buffer (name, major/minor modes, point line:column,
    mark/region, narrowing); the window layout tree ("windows": leaves are
    windows with buffer, size, point, mode-line string, and the visible
    text between window-start and window-end; inner nodes have split:
    'vertical' = stacked top-to-bottom, 'horizontal' = side by side);
    echo-area contents; the active minibuffer (prompt, current input,
    completion candidates when a completion session is active, depth);
    input-pending/unread flags; last-command; "popups" (the kinds of
    popup currently visible -- which-key/transient/child frames/...;
    non-empty means elate_popups has something to show you); and the
    last ~10 lines of *Messages* as messages-tail. Call this after every
    action whose effect you need to see. Visible text is capped per
    window; use elate_buffer for full buffer contents.
    """
    sess = None
    try:
        sess = _load(session)
        data = sess.semantic().rpc("state", since)
        sess.log("state", buffer=data.get("buffer"), via="mcp",
                 since=bool(since), mode=data.get("mode"))
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_READONLY, structured_output=False)
@_threaded
def elate_screenshot(
    session: Annotated[str, Field(description="Session name.")],
    ansi: Annotated[bool, Field(description=(
        "TTY only: include ANSI color escape sequences (verify "
        "faces/themes rendered in the terminal)."))] = False,
) -> str | list[str | Image]:
    """Capture the rendered screen (what a human would see).

    TTY sessions: the screen as text in the JSON response ("screen"),
    optionally ANSI-colored; works even after Emacs crashed (the dead
    pane is kept for post-mortem capture), making this the right tool to
    inspect a session that died. GUI sessions: a PNG of the Emacs window,
    returned as actual image content alongside a JSON text block with
    the saved path and pixel dimensions; needs a live session, and on
    macOS the Screen Recording permission (a missing permission comes
    back as an actionable error). For structured facts prefer
    elate_state; use the screenshot to check actual rendering, faces,
    layout glitches, or a wedged Emacs.
    """
    sess = None
    try:
        sess = _load(session, require_alive=False)
        if sess.ui == "gui":
            import datetime as _dt

            from . import screenshot as shot

            if ansi:
                raise ElateError("ansi applies to TTY text screenshots only")
            sess.require_alive()
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            out = sess.dir / "log" / f"screenshot-{stamp}.png"
            result = shot.capture_gui(sess, out)
            sess.log("screenshot", via="mcp", output=result["path"],
                     width=result["width"], height=result["height"])
            return [_ok(result), Image(path=result["path"])]
        if sess.raw().pane_info() is None:
            raise ElateError(
                f"session {session!r} has no tmux pane left to capture "
                f"(status: {sess.computed_status()})"
            )
        screen = sess.raw().capture_pane(ansi=ansi)
        sess.log("screenshot", ansi=ansi, via="mcp")
        return _ok({"screen": screen, "ansi": ansi})
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_READONLY)
@_threaded
def elate_buffer(
    session: Annotated[str, Field(description="Session name.")],
    buffer: Annotated[str | None, Field(description=(
        "Buffer name, e.g. '*scratch*' or '*Messages*'. Default: the "
        "current (selected window's) buffer."))] = None,
    from_line: Annotated[int | None, Field(description=(
        "First line to include (1-based, inclusive)."))] = None,
    to_line: Annotated[int | None, Field(description=(
        "Last line to include (1-based, inclusive)."))] = None,
    props: Annotated[bool, Field(description=(
        "Also return run-length-encoded face/text-property runs and the "
        "overlays for the range. Each run: start/end/line/text plus face "
        "(named faces, face lists, and anonymous plist faces), "
        "display/invisible/field values, and button/keymap presence. "
        "Each overlay: start/end plus face, invisible, display, "
        "before-string, after-string, priority. font-lock is ensured on "
        "the range first. Use this to verify font-lock, themes, and "
        "overlay-based UI (hl-line, company, ...). For a single position "
        "query, pass from_line=to_line."))] = False,
) -> str:
    """Read a buffer's text (full or a line range), plus total-lines.

    Reads the real buffer contents regardless of what is visible on
    screen. For big buffers pass from_line/to_line and use total-lines to
    page. With props=true, additionally dumps faces/text properties
    (run-length encoded) and overlays -- the way to check rendering
    facts structurally instead of eyeballing a screenshot. Errors if the
    buffer does not exist (elate_wait with condition='text' polls a
    not-yet-existing buffer instead).
    """
    sess = None
    try:
        sess = _load(session)
        data = sess.semantic().rpc("buffer", buffer, from_line, to_line,
                                   True if props else None)
        sess.log("buffer", name=buffer, from_line=from_line, to_line=to_line,
                 props=bool(props), bytes=len(data.get("text") or ""),
                 via="mcp")
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_READONLY)
@_threaded
def elate_faces_at(
    session: Annotated[str, Field(description="Session name.")],
    line: Annotated[int | None, Field(ge=1, description=(
        "1-based line number (pair with col). Omit when using pos."))] = None,
    col: Annotated[int | None, Field(ge=0, description=(
        "0-based column, clamped to the line (pair with line)."))] = None,
    pos: Annotated[int | None, Field(ge=1, description=(
        "Absolute buffer position instead of line+col -- convenient when "
        "you already hold a position. Give line+col OR pos, not both."))] = None,
    run: Annotated[int, Field(ge=1, le=500, description=(
        "Return this many consecutive cells from the position in one call "
        "(default 1). >1 returns {buffer, start, count, cells:[...]} -- e.g. "
        "to compare a typed cell against the suggestion cell beside it."))] = 1,
    buffer: Annotated[str | None, Field(description=(
        "Buffer to inspect. Default: the current (selected window's) "
        "buffer."))] = None,
) -> str:
    """Faces, text properties (with values), and overlays at a position.

    The point-query companion to elate_buffer's props dump: returns the
    char, the text-property face, char-face (face after overlays resolve --
    what the user actually sees), display/invisible/field, button/keymap
    presence, the overlays at the position, and -- key for verifying a
    package's own text properties -- "properties" (every property name) plus
    "property-values" (each name paired with its clipped printed value, so a
    flag t reads differently from a number or a symbol). Use this instead of
    repeated elate_eval (get-text-property ...) calls. font-lock is ensured
    first. Address by line+col or by pos; set run>1 to dump a run of
    adjacent cells in one call.
    """
    sess = None
    try:
        sess = _load(session)
        chan = sess.semantic()
        if pos is not None and (line is not None or col is not None):
            raise ElateError("give line+col or pos, not both")
        if pos is not None:
            start = pos
            if run == 1:
                data = chan.rpc("faces-at-pos", pos, buffer)
                sess.log("faces-at", pos=pos, buffer=buffer, via="mcp")
                return _ok(data)
        elif line is not None and col is not None:
            data = chan.rpc("faces-at", line, col, buffer)
            if run == 1:
                sess.log("faces-at", line=line, col=col, buffer=buffer, via="mcp")
                return _ok(data)
            start = data["pos"]  # resolve line:col to a position for the range
        else:
            raise ElateError("elate_faces_at needs line+col or pos")
        data = chan.rpc("faces-range", start, run, buffer)
        sess.log("faces-range", start=start, count=run, buffer=buffer, via="mcp")
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_READONLY)
@_threaded
def elate_popups(
    session: Annotated[str, Field(description="Session name.")],
) -> str:
    """Capture currently visible popups as text.

    Detects the common popup mechanisms -- which-key, transient, hydra's
    lv window, corfu/company completion popups, completion-preview, and
    any child frame (posframe & friends) -- and returns
    {"popups": [{kind, buffer?, text}]}; an empty list means no popup is
    showing. Mechanisms not installed in the session simply never match.
    elate_state's "popups" field lists the active kinds, so you know
    when calling this is worthwhile.
    """
    sess = None
    try:
        sess = _load(session)
        data = sess.semantic().rpc("popups")
        sess.log("popups", via="mcp",
                 kinds=[p.get("kind") for p in data.get("popups") or []])
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_READONLY)
@_threaded
def elate_messages(
    session: Annotated[str, Field(description="Session name.")],
) -> str:
    """New *Messages* output since the last elate_messages call (cursor-based).

    The cursor is persisted per session and shared with the CLI: each call
    returns only what arrived since the previous call and advances the
    cursor, so calling twice in a row returns nothing new the second time.
    The first call returns the whole backlog. For just the last few lines
    without consuming the cursor, use elate_state's messages-tail.
    """
    sess = None
    try:
        sess = _load(session)
        data = S.messages_delta(sess)
        sess.log("messages", cursor=data.get("cursor"),
                 bytes=len(data.get("text") or ""), via="mcp")
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


@server.tool(annotations=_READONLY)
@_threaded
def elate_echo(
    session: Annotated[str, Field(description="Session name.")],
) -> str:
    """Current echo area message and active minibuffer (prompt + input).

    A cheap targeted read; elate_state returns the same fields plus the
    full scene.
    """
    sess = None
    try:
        sess = _load(session)
        data = sess.semantic().rpc("echo")
        result = {"echo": data.get("echo"), "minibuffer": data.get("minibuffer")}
        sess.log("echo", echo=result["echo"], minibuffer=result["minibuffer"],
                 via="mcp")
        return _ok(result)
    except Exception as exc:
        return _fail(exc, sess)


# ---------------------------------------------------------------------------
# Synchronization

@server.tool(annotations=_READONLY)
@_threaded
def elate_wait(
    session: Annotated[str, Field(description="Session name.")],
    condition: Annotated[Literal["idle", "text", "prompt", "stable"], Field(
        description=(
        "'stable': BUFFER's text stopped changing for quiet_ms ms -- the "
        "right wait for subprocess/REPL output (comint, compilation, "
        "terminal, async LSP); this is usually what you want, not 'idle'. "
        "'idle': Emacs command loop has been idle >= min_idle s with no "
        "pending input -- use after keys/eval to let UI effects settle (it "
        "says nothing about whether buffer OUTPUT finished). 'text': a "
        "pattern appeared in a buffer -- use to await known output. "
        "'prompt': a minibuffer prompt became active -- use after keys "
        "that should ask a question."))],
    pattern: Annotated[str | None, Field(description=(
        "For condition='text': a PYTHON regular expression (not elisp "
        "syntax) matched against the buffer text."))] = None,
    buffer: Annotated[str | None, Field(description=(
        "For condition='text' (buffer to search) or 'stable' (buffer to "
        "watch); default: current. May not exist yet -- it is polled until "
        "the deadline."))] = None,
    timeout: Annotated[float, Field(gt=0, le=120, description=(
        "Overall deadline in seconds (0 < timeout <= 120). Prefer several "
        "short waits over one long one."))] = 10.0,
    min_idle: Annotated[float, Field(ge=0, le=60, description=(
        "For condition='idle': minimum idle time in seconds."))] = 0.2,
    quiet_ms: Annotated[int, Field(ge=50, le=10000, description=(
        "For condition='stable': the buffer must be unchanged for this many "
        "milliseconds to count as settled (default 300)."))] = 300,
) -> str:
    """Wait for a condition instead of sleep-and-poll.

    Returns what matched (settled buffer + edits seen / idle time / matched
    text + position / prompt string + current input). On timeout, ok=false
    with a "state" snapshot embedded so you can see what Emacs was doing
    instead -- read it before retrying.
    """
    sess = None
    try:
        sess = _load(session)
        # min_idle/quiet_ms must be logged or the transcript->script
        # exporter would silently lose them from replayed waits.
        sess.log("wait", condition=condition, pattern=pattern, buffer=buffer,
                 min_idle=min_idle, quiet_ms=quiet_ms, timeout=timeout,
                 via="mcp")
        if condition == "idle":
            data = S.wait_idle(sess, min_idle=min_idle, timeout=timeout)
        elif condition == "text":
            if not pattern:
                raise ElateError("condition='text' needs a pattern")
            data = S.wait_text(sess, pattern, buffer=buffer, timeout=timeout)
        elif condition == "stable":
            data = S.wait_stable(sess, buffer=buffer, quiet_ms=quiet_ms,
                                 timeout=timeout)
        else:
            data = S.wait_prompt(sess, timeout=timeout)
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


# ---------------------------------------------------------------------------
# Docs / bindings

@server.tool(annotations=_READONLY)
@_threaded
def elate_describe(
    session: Annotated[str, Field(description="Session name.")],
    kind: Annotated[Literal["key", "function", "variable", "mode"], Field(
        description=(
            "'key': resolve a key sequence to its command in the current "
            "context (like describe-key). 'function'/'variable'/'mode': "
            "look up a symbol."))],
    name: Annotated[str, Field(description=(
        "For 'key': a kbd string like 'C-x C-f'. Otherwise the symbol "
        "name, e.g. 'find-file', 'fill-column', 'org-mode'."))],
) -> str:
    """Structured docs/binding lookup inside the session's Emacs.

    Key lookups return the bound command (or 'prefix keymap'), its
    docstring, arglist, file of definition, and the keys it is on.
    Functions/variables/modes return docstring + definition file;
    functions also obsolescence info, and autoloaded=true with
    arglist=null when the definition is not loaded yet; variables also
    their current (buffer-local) value; modes whether they are enabled in
    the current buffer (enabled=null means the mode's state could not be
    determined -- not 'disabled'). Unknown symbols return defined=false
    rather than an error. Resolution happens in the live session, so it
    sees the package under test.
    """
    sess = None
    try:
        sess = _load(session)
        data = sess.semantic().rpc("describe", kind, name)
        sess.log("describe", kind=kind, name=name, via="mcp")
        return _ok(data)
    except Exception as exc:
        return _fail(exc, sess)


def run_stdio() -> None:
    """Run the MCP server over stdio (blocking until the client hangs up)."""
    server.run("stdio")
