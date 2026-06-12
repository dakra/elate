"""elate command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__, session as S
from .errors import ElateError, EvalTimeout, RpcError, WaitTimeout


def _parse_size(value: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)x(\d+)$", value)
    if not m:
        raise argparse.ArgumentTypeError(f"size must be COLSxROWS, got {value!r}")
    return int(m.group(1)), int(m.group(2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="elate",
        description="Drive sandboxed, observable Emacs sessions.",
    )
    p.add_argument("--version", action="version", version=f"elate {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("-s", "--session", metavar="NAME", help="session to operate on")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("start", help="start a new sandboxed session")
    sp.add_argument("--name", required=True)
    sp.add_argument("--ui", choices=["tty", "gui"], default="tty",
                    help="session UI: tty (tmux-hosted terminal Emacs, "
                         "default) or gui (windowed Emacs; PNG screenshots)")
    sp.add_argument("--headless", action="store_true",
                    help="GUI only: run under a private Xvfb (Linux/CI)")
    sp.add_argument("--emacs", metavar="PATH", help="emacs binary to use")
    sp.add_argument("--config", choices=list(S.sandbox.CONFIG_MODES), default="minimal",
                    help="sandbox config mode (default: minimal; "
                         "clean-install installs the --load package(s) "
                         "for real via package-install-file)")
    sp.add_argument("--init-file", metavar="PATH",
                    help="user init file (implies --config init-file)")
    sp.add_argument("--load", action="append", default=[], metavar="PATH",
                    help="elisp file or directory to put on load-path "
                         "(repeatable); with --config clean-install: the "
                         "package to install (.el file, tar, or directory)")
    sp.add_argument("--eval", action="append", default=[], metavar="FORM",
                    help="elisp form to evaluate at startup (repeatable)")
    sp.add_argument("--size", type=_parse_size, default=(120, 36), metavar="COLSxROWS")

    sp = sub.add_parser("stop", help="stop a session")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")

    sub.add_parser("list", help="list known sessions")

    sp = sub.add_parser("info", help="show session details")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")

    sp = sub.add_parser("keys", help="send keys (Emacs kbd notation)")
    sp.add_argument("keys")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--semantic", action="store_true",
                     help="deliver via execute-kbd-macro (default)")
    grp.add_argument("--raw", action="store_true",
                     help="deliver as raw terminal bytes via tmux")
    sp.add_argument("--events", action="store_true",
                    help="semantic, but queue on unread-command-events "
                         "(non-blocking; use for sequences that open a prompt)")
    sp.add_argument("--timeout", type=float, default=15.0)

    sp = sub.add_parser(
        "type",
        help="type literal text (raw channel on tty; queued events on gui)",
        description="Type literal text as if at the keyboard. TTY: raw "
                    "terminal bytes via tmux. GUI: queued key events "
                    "through the command loop -- needs a responsive Emacs "
                    f"and is capped at {S.GUI_TYPE_LIMIT} characters (for "
                    "bulk text, eval an insert instead). Text starting "
                    "with a dash needs '--' first: elate -s N type -- "
                    "'-foo'.")
    sp.add_argument("text")

    sp = sub.add_parser("mouse", help="synthesize a mouse interaction "
                                      "(semantic; works for tty and gui)")
    sp.add_argument("action", choices=list(S.MOUSE_ACTIONS))
    sp.add_argument("--button", type=int, choices=[1, 2, 3], default=1)
    sp.add_argument("--buffer", metavar="NAME",
                    help="target the window showing this buffer "
                         "(default: selected window)")
    sp.add_argument("--pos", type=int, metavar="N", help="buffer position")
    sp.add_argument("--line", type=int, metavar="L", help="buffer line (1-based)")
    sp.add_argument("--col", type=int, metavar="C",
                    help="column (with --line, or offset into the mode line)")
    sp.add_argument("--mode-line", action="store_true",
                    help="click the window's mode line instead of buffer text")
    sp.add_argument("--to-pos", type=int, metavar="N", help="drag: end position")
    sp.add_argument("--to-line", type=int, metavar="L", help="drag: end line")
    sp.add_argument("--to-col", type=int, metavar="C", help="drag: end column")
    sp.add_argument("--direction", choices=["up", "down"], default="down",
                    help="wheel: scroll direction")
    sp.add_argument("--count", type=int, default=1, metavar="N",
                    help="wheel: number of notches")
    sp.add_argument("--events", action="store_true",
                    help="queue on unread-command-events instead of the "
                         "synchronous default (use when the triggered "
                         "command itself reads input)")
    sp.add_argument("--timeout", type=float, default=15.0, metavar="SECS")

    sp = sub.add_parser("resize", help="resize a live session (tmux window "
                                       "or GUI frame)")
    sp.add_argument("size", type=_parse_size, metavar="COLSxROWS")

    sp = sub.add_parser("eval", help="evaluate an elisp form")
    sp.add_argument("form")
    sp.add_argument("--timeout", type=float, default=15.0, metavar="SECS")

    sp = sub.add_parser("buffer", help="print buffer contents")
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument("--from", dest="from_line", type=int, metavar="L")
    sp.add_argument("--to", dest="to_line", type=int, metavar="L")
    sp.add_argument("--props", action="store_true",
                    help="also dump run-length-encoded face/text-property "
                         "runs and overlays for the range (verify "
                         "font-lock, themes, overlay-based UI)")

    sp = sub.add_parser("test", help="run ERT tests interactively "
                                     "(structured per-test results)")
    sp.add_argument("selector", nargs="?", default="t",
                    help="ERT selector: t (default, all tests), a test "
                         "name, a name regexp, '(tag NAME)', "
                         "'(not \"slow\")', :failed, ... Tests must "
                         "already be loaded (--load-file, start --load, "
                         "or eval)")
    sp.add_argument("--load-file", action="append", default=[],
                    metavar="PATH",
                    help="elisp test file to load (by path) before "
                         "running (repeatable)")
    sp.add_argument("--timeout", type=float, default=60.0, metavar="SECS",
                    help="in-Emacs timeout for the whole run (default 60); "
                         "a timed-out run returns partial results")

    sp = sub.add_parser(
        "lint",
        help="byte-compile + checkdoc elisp files inside the session "
             "(WARNING: executes the files' compile-time code)",
        description="Byte-compile + checkdoc each FILE inside the live "
                    "session, against its load-path. WARNING: "
                    "byte-compilation EXECUTES compile-time code "
                    "(eval-when-compile, macro expansion, top-level "
                    "requires) in the session -- inherent to in-session "
                    "linting. Lint untrusted code in a throwaway session.")
    sp.add_argument("files", nargs="+", metavar="FILE")
    sp.add_argument("--timeout", type=float, default=60.0, metavar="SECS",
                    help="per-file in-Emacs timeout (default 60); a lint "
                         "whose compile-time code hangs is interrupted "
                         "and reported as a clean error")

    sp = sub.add_parser(
        "profile",
        help="drive Emacs's native profiler (start/stop/report, or "
             "one-shot 'run FORM')",
        description="Drive Emacs's native sampling profiler. 'start' "
                    "begins sampling (--cpu default, --mem allocations, "
                    "--both), resetting earlier logs; 'stop' ends it; "
                    "'report' renders the collected samples as top "
                    "functions + a depth-limited calltree (works while "
                    "profiling and after stop; --cpu/--mem select which "
                    "collected section to show). 'profile run FORM' does "
                    "start -> eval FORM (normal eval discipline incl. "
                    "timeout + backtraces) -> stop -> report in one call. "
                    "Profiles depend on session history (everything the "
                    "session ran is in the samples) -- profile in a fresh "
                    "throwaway session for authoritative numbers, like "
                    "lint.")
    sp.add_argument("action", choices=["start", "stop", "report", "run"])
    sp.add_argument("form", nargs="?",
                    help="for 'run': the elisp form to profile")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--cpu", dest="mode", action="store_const", const="cpu",
                     help="sample CPU time (SIGPROF; the default)")
    grp.add_argument("--mem", dest="mode", action="store_const", const="mem",
                     help="sample memory allocations")
    grp.add_argument("--both", dest="mode", action="store_const", const="both",
                     help="CPU and memory together")
    sp.set_defaults(mode=None)
    # None defaults so options on the wrong action are loud, not ignored.
    sp.add_argument("--depth", type=int, default=None, metavar="N",
                    help="report/run: calltree depth limit "
                         "(default 6, max 20)")
    sp.add_argument("--timeout", type=float, default=None, metavar="SECS",
                    help="run: eval timeout (default 15)")

    sp = sub.add_parser(
        "bench",
        help="benchmark an elisp form (benchmark-run-compiled wrapper)",
        description="Time FORM over --repetitions calls via Emacs's "
                    "benchmark-call, byte-compiling the form first "
                    "(interpreted fallback when compilation fails -- "
                    "'compiled' in the result says which path ran). "
                    "Reports elapsed/mean seconds, GC runs + GC time, and "
                    "memory-use-counts deltas (allocation context). "
                    "Results depend on session history (loaded code, GC "
                    "state) -- benchmark in a fresh throwaway session for "
                    "authoritative numbers, like lint.")
    sp.add_argument("form")
    sp.add_argument("-n", "--repetitions", type=int, default=1, metavar="N",
                    help="number of repetitions (default 1)")
    sp.add_argument("--timeout", type=float, default=60.0, metavar="SECS",
                    help="in-Emacs timeout for the whole run (default 60)")

    sp = sub.add_parser("faces-at", help="faces, text properties, and "
                                         "overlays at a buffer position")
    sp.add_argument("position", metavar="LINE:COL",
                    help="1-based line, 0-based column")
    sp.add_argument("--buffer", metavar="NAME",
                    help="buffer to inspect (default: current)")

    sub.add_parser("popups", help="capture visible popups as text "
                                  "(which-key, transient, hydra, "
                                  "completion previews, child frames)")

    sub.add_parser("messages", help="new *Messages* output since last call")

    sub.add_parser("echo", help="current echo area / minibuffer line")

    sub.add_parser("state", help="one-call scene snapshot (layout, prompt, "
                                 "point, modes, messages tail)")

    sp = sub.add_parser("describe", help="structured docs/binding lookup")
    sp.add_argument("kind", choices=["key", "function", "variable", "mode"])
    sp.add_argument("name", help="kbd string for 'key', symbol name otherwise")

    sub.add_parser("mcp", help="serve all of the above as MCP tools over "
                               "stdio (for AI harnesses)")

    sp = sub.add_parser("screenshot", help="capture the screen: text for tty "
                                           "sessions, PNG for gui sessions")
    sp.add_argument("-o", "--output", metavar="FILE",
                    help="output file (tty default: stdout; gui default: "
                         "./elate-<session>-<time>.png)")
    sp.add_argument("--ansi", action="store_true",
                    help="tty only: include ANSI color escapes")

    sp = sub.add_parser("wait", help="wait for a condition (exit 3 on timeout)")
    sp.add_argument("condition", choices=["idle", "text", "prompt"])
    sp.add_argument("args", nargs="*",
                    help="idle: [MIN_IDLE_SECS]; text: REGEXP (Python regex "
                         "syntax, not elisp); prompt: none")
    sp.add_argument("--buffer", help="buffer to search (wait text); may not exist yet")
    sp.add_argument("--timeout", type=float, default=10.0, metavar="SECS")

    sp = sub.add_parser(
        "run",
        help="run a scenario script: fresh session, steps, assertions, "
             "exit 0/1 (CI)",
        description="Execute a JSON scenario script: create a fresh "
                    "sandboxed session from the script's \"session\" "
                    "config (or target an existing one with -s NAME, in "
                    "which case that config is ignored and nothing is "
                    "torn down), run the steps in order, evaluate the "
                    "assertions, then stop the fresh session. Exits 0 "
                    "when every step and assertion passed, 1 otherwise; "
                    "a failed step embeds a state snapshot. Fresh "
                    "sessions are the default deliberately: lint "
                    "executes compile-time code and lint/test results "
                    "depend on session history, so only a throwaway "
                    "session gives reproducible verdicts.")
    sp.add_argument("script", help="path to the scenario file (JSON)")
    sp.add_argument("--keep", action="store_true",
                    help="keep the fresh session running afterwards")
    sp.add_argument("--keep-on-failure", action="store_true",
                    help="keep the fresh session running when the run "
                         "fails (inspect it with state/screenshot, then "
                         "stop it)")
    sp.add_argument("--emacs", metavar="PATH",
                    help="override the script's emacs binary (CI matrix)")

    sp = sub.add_parser(
        "export-script",
        help="convert a session's transcript into a best-effort scenario "
             "script",
        description="Turn the session's JSONL transcript into a scenario "
                    "file for `elate run`: inputs become steps, "
                    "observations become skipped assertion stubs "
                    "(\"skip\": true). A starting point for editing, not "
                    "a faithful recording. Works on stopped sessions too.")
    sp.add_argument("-o", "--output", metavar="FILE",
                    help="write the script here (default: stdout)")

    sp = sub.add_parser(
        "record",
        help="asciinema (.cast v2) recording of a TTY session",
        description="Record the session's terminal output as an asciicast "
                    "v2 file (play it with `asciinema play`, render a GIF "
                    "with `agg`). TTY sessions only -- for GUI sessions "
                    "use 'snap'. Capture rides tmux pipe-pane; the first "
                    "event replays the current screen so playback starts "
                    "from the correct picture.")
    sp.add_argument("action", choices=["start", "stop", "status"])
    sp.add_argument("-o", "--output", metavar="FILE.cast",
                    help="start: output file (default: "
                         "<session>/log/<name>-<time>.cast)")

    sp = sub.add_parser(
        "snap",
        help="periodic screenshot series (PNG for gui, text for tty)",
        description="Capture a frame every INTERVAL seconds into "
                    "frame-NNNN.png/.txt plus a manifest.json with "
                    "per-frame timestamps -- demo/GIF source material. "
                    "Runs as a detached snapper process that only ever "
                    "reads the session: a dying snapper cannot harm the "
                    "session, and 'snap stop' is idempotent.")
    sp.add_argument("action", choices=["start", "stop", "status"])
    sp.add_argument("--interval", type=float, default=0.5, metavar="SECS",
                    help="start: seconds between frames (default 0.5)")
    sp.add_argument("-o", "--output", metavar="DIR",
                    help="start: frame directory (default: "
                         "<session>/snap-<time>/)")
    sp.add_argument("--ansi", action="store_true",
                    help="tty only: ANSI-colored text frames")

    sp = sub.add_parser(
        "matrix",
        help="run a scenario script against several Emacs binaries",
        description="Run SCRIPT once per Emacs binary, each in a fresh "
                    "session, and aggregate the per-version verdicts "
                    "into one summary. Exits 0 only when every version "
                    "passed. With a single binary this is a matrix of "
                    "one -- the same scripts then scale to a CI matrix.")
    sp.add_argument("--emacs", action="append", default=[], metavar="PATHS",
                    help="emacs binary, or comma-separated list (repeatable)")
    sp.add_argument("--emacs-glob", metavar="GLOB",
                    help="glob matching emacs binaries, e.g. "
                         "'/opt/emacs-*/bin/emacs'")
    sp.add_argument("script", help="path to the scenario file (JSON)")

    return p


# ---------------------------------------------------------------------------
# Command implementations: each returns (result_dict, human_text, exit_code)

Result = tuple[dict[str, Any], str, int]


def _get_session(args: argparse.Namespace) -> S.Session:
    if not args.session:
        raise ElateError("this command needs a session: elate -s NAME ...")
    return S.load_session(args.session)


def _require_session(args: argparse.Namespace) -> S.Session:
    sess = _get_session(args)
    sess.require_alive()
    return sess


def _name_arg(args: argparse.Namespace) -> str:
    name = getattr(args, "name", None) or args.session
    if not name:
        raise ElateError(f"{args.command} needs a session name: elate {args.command} NAME")
    return name


def cmd_start(args: argparse.Namespace) -> Result:
    cols, rows = args.size
    sess = S.start_session(
        args.name,
        emacs=args.emacs,
        config=args.config,
        init_file=args.init_file,
        loads=args.load,
        evals=args.eval,
        cols=cols,
        rows=rows,
        ui=args.ui,
        headless=args.headless,
    )
    info = S.session_info(sess.name)
    human = (
        f"started session {sess.name!r}: Emacs {sess.emacs_version} "
        f"(pid {sess.emacs_pid}), {cols}x{rows} {sess.ui}"
        f"{' headless ' + (sess.display or '') if sess.headless else ''}\n"
        f"sandbox: {sess.session_dir}"
    )
    if info.get("init_error"):
        warning = (
            "WARNING: startup code signalled an error "
            f"(session is up regardless):\n{info['init_error']}"
        )
        print(f"elate: {warning}", file=sys.stderr)
        human += f"\n{warning}"
    return info, human, 0


def cmd_stop(args: argparse.Namespace) -> Result:
    name = _name_arg(args)
    result = S.stop_session(name)
    return result, f"stopped session {name!r}", 0


def cmd_list(args: argparse.Namespace) -> Result:
    sessions = S.list_sessions()
    if not sessions:
        return {"sessions": []}, "no sessions", 0
    lines = [f"{'NAME':<20} {'UI':<4} {'STATUS':<9} {'EMACS':<10} UPTIME"]
    for s in sessions:
        uptime = f"{s['uptime']:.0f}s" if s.get("uptime") is not None else "-"
        lines.append(
            f"{s['name']:<20} {s.get('ui') or '-':<4} {s['status']:<9} "
            f"{s.get('emacs_version') or '-':<10} {uptime}"
        )
    return {"sessions": sessions}, "\n".join(lines), 0


def cmd_info(args: argparse.Namespace) -> Result:
    info = S.session_info(_name_arg(args))
    human = "\n".join(f"{k}: {v}" for k, v in info.items())
    return info, human, 0


def cmd_keys(args: argparse.Namespace) -> Result:
    if args.raw and args.events:
        raise ElateError("--events is a semantic delivery mode; drop --raw")
    sess = _require_session(args)
    sess.log("keys", keys=args.keys,
             channel="raw" if args.raw else "semantic",
             method="events" if args.events else "macro")
    if args.raw:
        sess.raw().send_kbd(args.keys)
        result = {"keys": args.keys, "channel": "raw"}
    else:
        method = "events" if args.events else "macro"
        try:
            data = sess.semantic().rpc("keys", args.keys, method, timeout=args.timeout)
        except EvalTimeout as exc:
            raise EvalTimeout(
                f"{exc}\nHint: the key sequence may have left Emacs reading input. "
                "Retry with --events (queued delivery) or --raw."
            ) from exc
        result = {"keys": args.keys, "channel": "semantic", **data}
    return result, f"sent {args.keys!r} ({result['channel']})", 0


def cmd_type(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("type", text=args.text, channel="raw" if sess.ui == "tty" else "events")
    result = S.deliver_type(sess, args.text)
    return result, f"typed {len(args.text)} chars ({result['channel']})", 0


def cmd_mouse(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    kwargs = dict(
        action=args.action,
        button=args.button,
        buffer=args.buffer,
        pos=args.pos,
        line=args.line,
        col=args.col,
        part="mode-line" if args.mode_line else "text",
        to_pos=args.to_pos,
        to_line=args.to_line,
        to_col=args.to_col,
        direction=args.direction,
        count=args.count,
        delivery="events" if args.events else "macro",
    )
    sess.log("mouse", **kwargs)
    data = S.mouse_event(sess, timeout=args.timeout, **kwargs)
    where = data.get("area") or f"pos {data.get('pos')}"
    human = (f"{args.action} mouse-{args.button} at {where} "
             f"in {data.get('buffer')} ({data.get('delivered')})")
    return data, human, 0


def cmd_resize(args: argparse.Namespace) -> Result:
    sess = _get_session(args)
    cols, rows = args.size
    data = S.resize_session(sess, cols, rows)
    return data, f"resized to {cols}x{rows}", 0


def cmd_eval(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("eval", form=args.form, timeout=args.timeout)
    try:
        data = sess.semantic().eval_form(args.form, timeout=args.timeout)
    except EvalTimeout as exc:
        busy = sess.is_busy()
        sess.log("eval-timeout", form=args.form)
        if busy and sess.ui == "tty":
            hint = (" -- Emacs is still busy; raw C-g may unblock it:"
                    f" elate -s {sess.name} keys C-g --raw")
        elif busy:
            hint = (" -- Emacs is still busy; a GUI session has no raw "
                    "channel to unblock it, stop the session if it stays "
                    "wedged")
        else:
            hint = ""
        raise EvalTimeout(f"{exc}{hint}") from exc
    sess.log("eval-result", **data)
    parts = []
    if data.get("error"):
        parts.append(f"error: {data['error']}")
        if data.get("backtrace"):
            parts.append(f"backtrace:\n{data['backtrace']}")
        # Exit 1 below; make the JSON "ok" flag agree with the exit code.
        data = {**data, "ok": False}
    else:
        parts.append(str(data.get("value")))
        if data.get("truncated"):
            parts.append(
                f"(value truncated to {len(data.get('value') or '')} chars; "
                f"full printed length {data.get('value-length')})"
            )
    if data.get("messages"):
        parts.append(f"messages:\n{data['messages'].rstrip()}")
    return data, "\n".join(parts), 1 if data.get("error") else 0


def _human_overlay(o: dict[str, Any]) -> str:
    bits = []
    if o.get("face"):
        bits.append("face=" + ",".join(o["face"]))
    for key in ("display", "invisible", "before-string", "after-string",
                "priority"):
        if o.get(key) is not None:
            bits.append(f"{key}={o[key]!r}")
    return f"{o.get('start')}-{o.get('end')} {' '.join(bits) or '-'}"


def _human_props(data: dict[str, Any]) -> str:
    lines = [(data.get("text") or "").rstrip("\n")]
    runs = (data.get("props") or {}).get("runs") or []
    lines.append(f"-- property runs ({len(runs)}) --")
    for r in runs:
        bits = []
        if r.get("face"):
            bits.append("face=" + ",".join(r["face"]))
        for key in ("display", "invisible", "field"):
            if r.get(key) is not None:
                bits.append(f"{key}={r[key]}")
        for flag in ("button", "keymap"):
            if r.get(flag):
                bits.append(flag)
        text = (r.get("text") or "").replace("\n", "\\n")
        if len(text) > 48:
            text = text[:48] + "…"
        lines.append(f"  {r['start']}-{r['end']} (L{r.get('line')}) "
                     f"{' '.join(bits) or '-'} {text!r}")
    if (data.get("props") or {}).get("truncated"):
        lines.append("  ... (run list truncated)")
    overlays = data.get("overlays") or []
    lines.append(f"-- overlays ({len(overlays)}) --")
    lines.extend(f"  {_human_overlay(o)}" for o in overlays)
    if data.get("overlays-truncated"):
        lines.append("  ... (overlay list truncated)")
    return "\n".join(lines)


def cmd_buffer(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    data = sess.semantic().rpc("buffer", args.name, args.from_line, args.to_line,
                               True if args.props else None)
    sess.log("buffer", name=args.name, from_line=args.from_line, to_line=args.to_line,
             props=bool(args.props), bytes=len(data.get("text") or ""))
    human = _human_props(data) if args.props else data.get("text", "")
    return data, human, 0


def cmd_test(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("test", selector=args.selector, load_files=args.load_file,
             timeout=args.timeout)
    data = S.run_ert(sess, selector=args.selector, load_files=args.load_file,
                     timeout=args.timeout)
    sess.log("test-result", total=data.get("total"),
             unexpected=data.get("unexpected"),
             timed_out=data.get("timed-out"))
    failed = bool(data.get("unexpected") or data.get("timed-out"))
    lines = [
        "Ran {} test(s) in {:.2f}s: {} passed, {} failed, {} errored, "
        "{} skipped".format(
            data.get("total", 0), data.get("duration") or 0.0,
            data.get("passed", 0), data.get("failed", 0),
            data.get("errors", 0), data.get("skipped", 0),
        )
    ]
    for t in data.get("tests") or []:
        if t.get("expected"):
            continue
        lines.append(f"{(t.get('status') or '?').upper()}: {t.get('name')} "
                     f"({t.get('duration') or 0:.3f}s)")
        if t.get("condition"):
            lines.append(f"  condition: {t['condition']}")
        if t.get("backtrace"):
            lines.append("  backtrace:")
            lines.extend("    " + ln for ln in t["backtrace"].splitlines())
    if data.get("timed-out"):
        lines.append(data.get("error") or "ERT run timed out")
    if failed:
        # Make the JSON "ok" flag agree with the exit code, like eval.
        data = {**data, "ok": False}
    return data, "\n".join(lines), 1 if failed else 0


def cmd_lint(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("lint", files=args.files, timeout=args.timeout)
    data = S.lint_files(sess, args.files, timeout=args.timeout)
    sess.log("lint-result", files=len(data["files"]), items=len(data["items"]))
    clean = data["clean"]
    lines = []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in data["items"]:
        by_file.setdefault(item.get("file") or "?", []).append(item)
    for fname, items in by_file.items():
        lines.append(f"{fname}:")
        for it in items:
            place = (f"{it['line']}:{it.get('col') if it.get('col') is not None else '?'}"
                     if it.get("line") is not None else "-")
            lines.append(f"  {place} [{it.get('tool')}] "
                         f"{it.get('severity')}: {it.get('message')}")
    lines.append(
        ("clean" if clean else f"{len(data['items'])} finding(s)")
        + f" in {len(data['files'])} file(s)"
    )
    if not clean:
        data = {**data, "ok": False}
    return data, "\n".join(lines), 0 if clean else 1


def _human_profile_section(label: str, sec: dict[str, Any]) -> str:
    lines = [f"== {label}: {sec.get('total')} {sec.get('units')} =="]
    funcs = sec.get("functions") or []
    if funcs:
        lines.append("top functions (self% / total%):")
        for f in funcs:
            lines.append(f"  {f.get('self-percent', 0):6.1f}%  "
                         f"{f.get('total-percent', 0):6.1f}%  {f.get('name')}")
        if sec.get("functions-truncated"):
            lines.append("  ... (function list truncated)")
    lines.append(f"calltree (depth {sec.get('depth')}):")

    def walk(nodes: list[dict[str, Any]], indent: int) -> None:
        for n in nodes:
            lines.append(f"  {'  ' * indent}{n.get('percent', 0):5.1f}% "
                         f"{n.get('count'):>9}  {n.get('name')}")
            walk(n.get("children") or [], indent + 1)
            if n.get("children-truncated"):
                lines.append(f"  {'  ' * (indent + 1)}...")

    walk(sec.get("tree") or [], 0)
    if sec.get("tree-truncated"):
        lines.append("  ... (calltree truncated)")
    return "\n".join(lines)


def _human_profile(data: dict[str, Any]) -> str:
    parts = []
    if data.get("cpu"):
        parts.append(_human_profile_section("CPU", data["cpu"]))
    if data.get("mem"):
        parts.append(_human_profile_section("memory", data["mem"]))
    if data.get("running"):
        parts.append("(profiler still running; `profile stop` to end it)")
    return "\n".join(parts)


def cmd_profile(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    mode = args.mode or "cpu"
    # Options on an action they cannot affect are loud, like the form
    # check below -- a silently ignored --depth/--timeout teaches wrong
    # expectations.
    if args.action in ("start", "stop") and args.depth is not None:
        raise ElateError(
            f"--depth applies to 'profile report'/'profile run', "
            f"not 'profile {args.action}'")
    if args.action != "run" and args.timeout is not None:
        raise ElateError(
            f"--timeout applies to 'profile run', not 'profile {args.action}'")
    depth = args.depth if args.depth is not None else 6
    timeout = args.timeout if args.timeout is not None else 15.0
    if args.action == "run":
        if not args.form:
            raise ElateError(
                "profile run needs a form: elate -s NAME profile run '(form)'")
        sess.log("profile", action="run", mode=mode, form=args.form,
                 timeout=timeout, depth=depth)
        data = S.profile_run(sess, args.form, mode=mode,
                             timeout=timeout, depth=depth)
        ev = data.get("eval") or {}
        parts = []
        code = 0
        if ev.get("error"):
            parts.append(f"eval error: {ev['error']}")
            if ev.get("backtrace"):
                parts.append(f"backtrace:\n{ev['backtrace']}")
            # Like eval: the JSON ok flag agrees with the exit code.
            data = {**data, "ok": False}
            code = 1
        else:
            parts.append(f"value: {ev.get('value')}")
        parts.append(_human_profile(data))
        return data, "\n".join(parts), code
    if args.form:
        raise ElateError(
            f"profile {args.action} takes no form (only 'profile run' does)")
    if args.action == "start":
        sess.log("profile", action="start", mode=mode)
        data = S.profile_start(sess, mode)
        si = data.get("sampling-interval")
        human = (f"profiler started ({data.get('started')}"
                 + (f"; one cpu sample every {si} ns)" if si else ")"))
        return data, human, 0
    if args.action == "stop":
        sess.log("profile", action="stop")
        data = S.profile_stop(sess)
        if not data.get("stopped"):
            return data, "no profiler was running", 0
        bits = []
        if data.get("cpu"):
            bits.append(f"{data.get('cpu-samples')} cpu sample(s)")
        if data.get("mem"):
            bits.append(f"{data.get('mem-bytes')} byte(s) sampled")
        return data, f"profiler stopped ({', '.join(bits)}); see `profile report`", 0
    # report
    sess.log("profile", action="report", depth=depth)
    data = S.profile_report(sess, depth=depth)
    if args.mode in ("cpu", "mem"):
        other = "mem" if args.mode == "cpu" else "cpu"
        data.pop(other, None)
        if args.mode not in data:
            # The agent errors loudly on NO data at all; an explicit
            # --cpu/--mem filter that strips the only collected section
            # must be equally loud, not a silent empty success.
            raise ElateError(
                f"no {args.mode} profile data was collected (the profile "
                f"has only {other} data); use `profile start --{args.mode}` "
                f"or `profile run --{args.mode}`")
    return data, _human_profile(data), 0


def cmd_bench(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("bench", form=args.form, repetitions=args.repetitions,
             timeout=args.timeout)
    data = S.bench_form(sess, args.form, repetitions=args.repetitions,
                        timeout=args.timeout)
    if data.get("error"):
        lines = [f"error: {data['error']}"]
        if data.get("backtrace"):
            lines.append(f"backtrace:\n{data['backtrace']}")
        return {**data, "ok": False}, "\n".join(lines), 1
    lines = [
        "{} repetition(s): total {:.6f}s, mean {:.6f}s/rep ({})".format(
            data.get("repetitions"), data.get("elapsed") or 0.0,
            data.get("mean") or 0.0,
            "byte-compiled" if data.get("compiled") else "interpreted")
    ]
    if data.get("compile-error"):
        lines.append("byte-compile failed (ran interpreted): "
                     f"{data['compile-error']}")
    lines.append(f"GC: {data.get('gc-runs', 0)} run(s), "
                 f"{data.get('gc-elapsed') or 0.0:.6f}s")
    deltas = data.get("memory-deltas") or {}
    nonzero = [f"{k} {v:+d}" for k, v in deltas.items() if v]
    if nonzero:
        lines.append("allocations: " + ", ".join(nonzero))
    return data, "\n".join(lines), 0


def cmd_faces_at(args: argparse.Namespace) -> Result:
    m = re.match(r"^(\d+):(\d+)$", args.position)
    if not m:
        raise ElateError(f"position must be LINE:COL, got {args.position!r}")
    line, col = int(m.group(1)), int(m.group(2))
    if line < 1:
        raise ElateError("faces-at line must be >= 1")
    sess = _require_session(args)
    data = sess.semantic().rpc("faces-at", line, col, args.buffer)
    sess.log("faces-at", line=line, col=col, buffer=args.buffer)
    lines = [f"{data.get('buffer')} {data.get('line')}:{data.get('column')} "
             f"(pos {data.get('pos')}) char {data.get('char')!r}"]
    if data.get("face"):
        lines.append("face: " + ", ".join(data["face"]))
    if data.get("char-face") and data.get("char-face") != data.get("face"):
        lines.append("char-face (incl. overlays): "
                     + ", ".join(data["char-face"]))
    for key in ("display", "invisible", "field"):
        if data.get(key) is not None:
            lines.append(f"{key}: {data[key]}")
    for flag in ("button", "keymap"):
        if data.get(flag):
            lines.append(f"{flag}: yes")
    if data.get("properties"):
        lines.append("text properties: " + " ".join(data["properties"]))
    overlays = data.get("overlays") or []
    if overlays:
        lines.append(f"overlays ({len(overlays)}):")
        lines.extend(f"  {_human_overlay(o)}" for o in overlays)
    return data, "\n".join(lines), 0


def cmd_popups(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    data = sess.semantic().rpc("popups")
    pops = data.get("popups") or []
    sess.log("popups", kinds=[p.get("kind") for p in pops])
    if not pops:
        return data, "no popups", 0
    sections = []
    for p in pops:
        head = f"== {p.get('kind')}" + (
            f" ({p['buffer']})" if p.get("buffer") else "") + " =="
        text = (p.get("text") or "").rstrip("\n")
        sections.append(head + (f"\n{text}" if text else ""))
    return data, "\n\n".join(sections), 0


def cmd_messages(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    data = S.messages_delta(sess)
    sess.log("messages", cursor=data.get("cursor"), bytes=len(data.get("text") or ""))
    return data, (data.get("text") or "").rstrip("\n"), 0


def cmd_echo(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    state = sess.semantic().rpc("echo")
    mb = state.get("minibuffer")
    result = {"echo": state.get("echo"), "minibuffer": mb}
    sess.log("echo", **result)
    if mb:
        human = f"{mb.get('prompt') or ''}{mb.get('contents') or ''}"
    else:
        human = state.get("echo") or ""
    return result, human, 0


def _flat_windows(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
    if "children" in node:
        for child in node["children"]:
            _flat_windows(child, out)
    else:
        out.append(node)


def _human_state(data: dict[str, Any]) -> str:
    lines = [
        "buffer: {} ({}){}  point {}:{}".format(
            data.get("buffer"), data.get("major-mode"),
            " [narrowed]" if data.get("narrowed") else "",
            data.get("line"), data.get("column"),
        )
    ]
    region = data.get("region")
    if region:
        lines.append(f"region: {region['start']}..{region['end']} "
                     f"({region['size']} chars)")
    mb = data.get("minibuffer")
    if mb:
        line = (f"minibuffer [{mb.get('depth')}]: "
                f"{mb.get('prompt') or ''}{mb.get('contents') or ''}")
        comp = mb.get("completions")
        if comp and comp.get("candidates"):
            cands = comp["candidates"]
            more = " ..." if comp.get("truncated") else ""
            line += f"\ncompletions: {' '.join(cands[:10])}{more}"
        lines.append(line)
    if data.get("echo"):
        lines.append(f"echo: {data['echo']}")
    if data.get("input-pending") or data.get("unread"):
        lines.append(f"input pending (unread: {data.get('unread')})")
    if data.get("popups"):
        lines.append(f"popups: {', '.join(data['popups'])} "
                     "(use 'popups' to capture)")
    windows: list[dict[str, Any]] = []
    if isinstance(data.get("windows"), dict):
        _flat_windows(data["windows"], windows)
    lines.append(f"windows ({len(windows)}):")
    for w in windows:
        sel = "*" if w.get("selected") else " "
        lines.append(f" {sel} {w.get('buffer')}  {w.get('width')}x{w.get('height')}"
                     f"  point {w.get('line')}:{w.get('column')}"
                     f"  lines {w.get('start-line')}-{w.get('end-line')}")
    if data.get("last-command"):
        lines.append(f"last-command: {data['last-command']}")
    tail = (data.get("messages-tail") or "").rstrip("\n")
    if tail:
        lines.append("messages tail:")
        lines.extend(f"  {ln}" for ln in tail.splitlines())
    return "\n".join(lines)


def cmd_state(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    data = sess.semantic().rpc("state")
    sess.log("state", buffer=data.get("buffer"))
    return data, _human_state(data), 0


def cmd_describe(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    data = sess.semantic().rpc("describe", args.kind, args.name)
    sess.log("describe", kind=args.kind, name=args.name)
    lines = []
    for key, value in data.items():
        if value is None or value == []:
            continue
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {value}")
    return data, "\n".join(lines), 0


def cmd_screenshot(args: argparse.Namespace) -> Result:
    # Deliberately no require_alive for tty: a crashed Emacs leaves its
    # dead pane behind (remain-on-exit=failed) precisely so it can be
    # captured post-mortem. Only a fully stopped session has nothing to
    # show. GUI sessions need a live window (no post-mortem there).
    sess = _get_session(args)
    if sess.ui == "gui":
        from . import screenshot as shot
        from datetime import datetime

        if args.ansi:
            raise ElateError("--ansi applies to TTY text screenshots only")
        sess.require_alive()
        # Microseconds included so two shots within a second never overwrite.
        out = args.output or (
            f"elate-{sess.name}-{datetime.now():%Y%m%d-%H%M%S-%f}.png")
        result = shot.capture_gui(sess, Path(out).expanduser().resolve())
        sess.log("screenshot", output=result["path"],
                 width=result["width"], height=result["height"])
        human = (f"screenshot written to {result['path']} "
                 f"({result['width']}x{result['height']})")
        return result, human, 0
    if sess.raw().pane_info() is None:
        raise ElateError(
            f"session {sess.name!r} has no tmux pane left to capture "
            f"(status: {sess.computed_status()})"
        )
    screen = sess.raw().capture_pane(ansi=args.ansi)
    sess.log("screenshot", ansi=args.ansi, output=args.output)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(screen)
        except OSError as exc:
            raise ElateError(f"cannot write screenshot to {args.output}: {exc}") from exc
        return (
            {"written": args.output, "ansi": args.ansi},
            f"screenshot written to {args.output}",
            0,
        )
    return {"screen": screen, "ansi": args.ansi}, screen.rstrip("\n"), 0


def cmd_wait(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    # buffer must be logged or the transcript->script exporter would
    # silently retarget replayed waits at the then-current buffer.
    sess.log("wait", condition=args.condition, args=args.args,
             buffer=args.buffer, timeout=args.timeout)
    if args.condition == "idle":
        if args.args:
            try:
                min_idle = float(args.args[0])
            except ValueError:
                raise ElateError(
                    f"wait idle takes an optional number of seconds, got {args.args[0]!r}"
                ) from None
        else:
            min_idle = 0.2
        data = S.wait_idle(sess, min_idle=min_idle, timeout=args.timeout)
        return data, f"idle ({data.get('idle'):.2f}s)", 0
    if args.condition == "text":
        if not args.args:
            raise ElateError("wait text needs a REGEXP argument")
        data = S.wait_text(sess, args.args[0], buffer=args.buffer, timeout=args.timeout)
        return data, f"matched {data['matched']!r} in {data['buffer']}", 0
    # prompt
    data = S.wait_prompt(sess, timeout=args.timeout)
    return data, f"prompt: {data.get('prompt')!r} (contents: {data.get('contents')!r})", 0


# -- Phase 5: scripts, recording, snap series, matrix -------------------------

_STEP_MARK = {"ok": "ok", "failed": "FAIL", "skipped": "skipped",
              "not-run": "not run"}


def _step_line(rec: dict[str, Any], total: int) -> str:
    line = f"[{rec['index']}/{total}] {rec['summary']} ... {_STEP_MARK[rec['status']]}"
    if rec.get("duration") is not None:
        line += f" ({rec['duration']:.2f}s)"
    if rec["status"] == "failed":
        line += f"\n  error: {rec.get('error')}"
    return line


def _run_summary(result: dict[str, Any]) -> str:
    bits = [f"{result['passed']} passed"]
    for key, label in (("failed", "failed"), ("skipped", "skipped"),
                       ("not_run", "not run")):
        if result.get(key):
            bits.append(f"{result[key]} {label}")
    line = (("PASS" if result["success"] else "FAIL")
            + f": {', '.join(bits)} in {result['duration']:.2f}s")
    if result.get("error"):
        line += f"\nerror: {result['error']}"
    elif result.get("init_error"):
        # allow_init_error runs: surface the tolerated error anyway.
        line += ("\nWARNING: session startup code signalled an error: "
                 f"{result['init_error']}")
    if result.get("kept") and result.get("fresh_session"):
        line += (f"\nsession kept: elate -s {result['session']} state; "
                 f"elate stop {result['session']}")
    return line


def cmd_run(args: argparse.Namespace) -> Result:
    from . import script as SC

    if args.session and args.emacs:
        # Loud, never silent: an existing session already runs its own
        # binary; --emacs cannot retroactively apply to it.
        raise ElateError(
            "--emacs cannot apply to an existing session (-s NAME); drop "
            "-s to run a fresh session with that binary")
    script, base = SC.load_script(args.script)
    target = None
    if args.session:
        target = S.load_session(args.session)
    total = len(script.get("steps") or [])
    on_step = None
    if not args.json:
        def on_step(rec: dict[str, Any]) -> None:
            print(_step_line(rec, total), flush=True)
    result = SC.run_script(
        script, base_dir=base, session=target, emacs=args.emacs,
        keep=args.keep, keep_on_failure=args.keep_on_failure,
        on_step=on_step,
    )
    return result, _run_summary(result), 0 if result["success"] else 1


def cmd_export_script(args: argparse.Namespace) -> Result:
    from . import script as SC

    # No require_alive: the transcript outlives the Emacs (stopped and
    # crashed sessions export fine).
    sess = _get_session(args)
    script = SC.export_script(sess)
    text = json.dumps(script, indent=2, ensure_ascii=False) + "\n"
    steps = len(script["steps"])
    stubs = sum(1 for s in script["steps"] if s.get("skip"))
    if args.output:
        path = Path(args.output).expanduser()
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise ElateError(
                f"cannot write script to {args.output}: {exc}") from exc
        human = (f"wrote {steps} step(s) ({stubs} skipped stub(s)) to {path}\n"
                 "Best-effort export: edit the stubs into real assertions "
                 "before trusting it as a regression test.")
        return {"written": str(path), "steps": steps, "stubs": stubs}, human, 0
    return ({"script": script, "steps": steps, "stubs": stubs},
            text.rstrip("\n"), 0)


def cmd_record(args: argparse.Namespace) -> Result:
    from . import record as R

    sess = _get_session(args)
    if args.action == "start":
        data = R.start_recording(sess, output=args.output)
        human = (f"recording {sess.name!r} ({data['width']}x{data['height']}) "
                 f"to {data['path']}")
        return data, human, 0
    if args.output:
        raise ElateError("-o/--output applies to 'record start' only")
    if args.action == "stop":
        data = R.stop_recording(sess)
        human = (f"recording stopped: {data['path']} "
                 f"({data['events']} event(s), {data['duration']:.1f}s)")
        if data.get("note"):
            human += f"\nnote: {data['note']}"
        return data, human, 0
    data = R.recording_status(sess)
    if not data.get("path"):
        return data, "no recording", 0
    state = "recording" if data["recording"] else \
        "finished (pipe closed; 'record stop' clears the state)"
    human = (f"{state}: {data['path']} ({data['events']} event(s), "
             f"{data['duration']:.1f}s)")
    if data.get("note"):
        human += f"\nnote: {data['note']}"
    return data, human, 0


def cmd_snap(args: argparse.Namespace) -> Result:
    from . import snap as SN

    sess = _get_session(args)
    if args.action == "start":
        data = SN.start_snap(sess, interval=args.interval,
                             output=args.output, ansi=args.ansi)
        human = (f"snapping {sess.name!r} every {data['interval']:g}s "
                 f"({data['format']} frames) into {data['dir']}")
        return data, human, 0
    if args.output:
        raise ElateError("-o/--output applies to 'snap start' only")
    if args.action == "stop":
        data = SN.stop_snap(sess)
        if not data["stopped"]:
            return data, data.get("note", "no snapper is running"), 0
        return data, f"snap stopped: {data['frames']} frame(s) in {data['dir']}", 0
    data = SN.snap_status(sess)
    if not data.get("dir"):
        return data, "no snapper", 0
    state = "snapping" if data["snapping"] else \
        "snapper gone ('snap stop' clears the state)"
    return data, f"{state}: {data['frames']} frame(s) in {data['dir']}", 0


def cmd_matrix(args: argparse.Namespace) -> Result:
    import glob as globlib
    import shutil

    from . import script as SC

    binaries: list[str] = []
    for spec in args.emacs:
        binaries.extend(p for p in (s.strip() for s in spec.split(",")) if p)
    if args.emacs_glob:
        matches = sorted(globlib.glob(str(Path(args.emacs_glob).expanduser())))
        if not matches:
            raise ElateError(f"--emacs-glob matched nothing: {args.emacs_glob}")
        binaries.extend(matches)
    seen: set[str] = set()
    uniq: list[str] = []
    for b in binaries:  # resolve + fail fast before booting anything
        resolved = str(Path(b).expanduser())
        if not (Path(resolved).is_file() and os.access(resolved, os.X_OK)):
            # Bare PATH names ("emacs") work on every other --emacs
            # surface; resolve them here too before rejecting.
            which = shutil.which(resolved)
            if which is None:
                raise ElateError(f"not an executable emacs binary: {b} "
                                 "(not a file, and not found on PATH)")
            resolved = which
        # Dedup on the real path so the same binary via different
        # spellings (relative/absolute/symlink/PATH name) runs once.
        key = os.path.realpath(resolved)
        if key not in seen:
            seen.add(key)
            uniq.append(resolved)
    if not uniq:
        raise ElateError("matrix needs at least one --emacs PATH "
                         "(or --emacs-glob)")

    script, base = SC.load_script(args.script)
    total = len(script.get("steps") or [])
    on_step = None
    if not args.json:
        def on_step(rec: dict[str, Any]) -> None:
            print(_step_line(rec, total), flush=True)
    results: list[dict[str, Any]] = []
    for b in uniq:
        if not args.json:
            print(f"=== {b} ===", flush=True)
        try:
            run = SC.run_script(script, base_dir=base, emacs=b,
                                on_step=on_step)
            entry = {
                "emacs": b,
                "version": run.get("emacs_version"),
                "success": run["success"],
                "passed": run["passed"],
                "failed": run["failed"],
                "duration": run["duration"],
                "failed_step": next(
                    (r["summary"] for r in run["steps"]
                     if r["status"] == "failed"), None),
            }
        except ElateError as exc:
            # One broken binary must not abort the rest of the matrix.
            entry = {"emacs": b, "version": None, "success": False,
                     "error": str(exc)}
        results.append(entry)
    success = all(r["success"] for r in results)
    lines = [f"{'EMACS':<44} {'VERSION':<10} {'RESULT':<7} TIME"]
    for r in results:
        took = f"{r['duration']:.1f}s" if r.get("duration") is not None else "-"
        lines.append(f"{r['emacs']:<44} {r.get('version') or '-':<10} "
                     f"{'pass' if r['success'] else 'FAIL':<7} {took}")
        if r.get("error"):
            lines.append(f"  error: {r['error']}")
        elif r.get("failed_step"):
            lines.append(f"  failed at: {r['failed_step']}")
    passed = sum(1 for r in results if r["success"])
    lines.append(f"{passed}/{len(results)} version(s) passed")
    return ({"success": success, "script": args.script, "results": results},
            "\n".join(lines), 0 if success else 1)


_COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "list": cmd_list,
    "info": cmd_info,
    "keys": cmd_keys,
    "type": cmd_type,
    "mouse": cmd_mouse,
    "resize": cmd_resize,
    "eval": cmd_eval,
    "buffer": cmd_buffer,
    "test": cmd_test,
    "lint": cmd_lint,
    "profile": cmd_profile,
    "bench": cmd_bench,
    "faces-at": cmd_faces_at,
    "popups": cmd_popups,
    "messages": cmd_messages,
    "echo": cmd_echo,
    "state": cmd_state,
    "describe": cmd_describe,
    "screenshot": cmd_screenshot,
    "wait": cmd_wait,
    "run": cmd_run,
    "export-script": cmd_export_script,
    "record": cmd_record,
    "snap": cmd_snap,
    "matrix": cmd_matrix,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        # Serve MCP over stdio. Imported lazily so plain CLI use never
        # pays for (or requires) the mcp package import machinery, and
        # nothing may touch stdout here -- it is the MCP transport.
        from .mcp_server import run_stdio

        run_stdio()
        return 0
    try:
        result, human, code = _COMMANDS[args.command](args)
    except WaitTimeout as exc:
        # Exit 3: distinct from generic errors (1) and argparse usage (2),
        # so scripts can tell "condition did not happen" apart.
        if args.json:
            # exc.state is a state_dump: {"state": ..., "screen_tail": ...,
            # "last_probe_error": ...}; spread it so "state" is the actual
            # snapshot rather than nesting as state.state.
            print(json.dumps({"ok": False, "error": str(exc), **exc.state},
                             ensure_ascii=False))
        else:
            print(f"elate: {exc}", file=sys.stderr)
            print(json.dumps(exc.state, ensure_ascii=False, indent=2), file=sys.stderr)
        return 3
    except RpcError as exc:
        payload: dict[str, Any] = {"ok": False, "error": str(exc)}
        if exc.backtrace:
            payload["backtrace"] = exc.backtrace
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"elate: elisp error: {exc}", file=sys.stderr)
        return 1
    except ElateError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"elate: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    else:
        if human:
            print(human)
    return code


if __name__ == "__main__":
    sys.exit(main())
