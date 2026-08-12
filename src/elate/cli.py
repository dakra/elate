"""elate command-line interface."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

from . import __version__, install, session as S
from .errors import (
    ElateError,
    EvalTimeout,
    RpcError,
    ScreenshotError,
    TransportError,
    UsageError,
    WaitTimeout,
)


def _parse_size(value: str) -> tuple[int, int]:
    m = re.match(r"^(\d+)x(\d+)$", value)
    if not m:
        raise argparse.ArgumentTypeError(f"size must be COLSxROWS, got {value!r}")
    return int(m.group(1)), int(m.group(2))


def _parse_kv_pair(value: str) -> tuple[str, str]:
    key, sep, val = value.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(
            f"expected NAME=VALUE with a non-empty NAME, got {value!r}")
    return key, val


def _read_set_files(pairs: list[tuple[str, str]],
                    set_vars: list[tuple[str, str]]) -> dict[str, str]:
    """Resolve --set-file NAME=PATH pairs to {NAME: file contents}.

    The value is the file read verbatim (UTF-8) minus exactly one trailing
    newline -- editors append one, and a shell setup snippet must not grow
    a blank line -- interior newlines and any second trailing newline
    survive. Collisions are loud: the same NAME twice, or in both --set
    and --set-file, has no visible ordering, and last-wins would silently
    discard a deliberate file read.
    """
    out: dict[str, str] = {}
    set_names = {k for k, _ in set_vars}
    for name, path in pairs:
        if name in out:
            raise ElateError(f"--set-file {name!r} given more than once")
        if name in set_names:
            raise ElateError(
                f"{name!r} is bound by both --set and --set-file -- drop one")
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ElateError(
                f"cannot read --set-file {name!r} from {path!r}: "
                f"{exc}") from exc
        out[name] = re.sub(r"\r?\n\Z", "", text, count=1)
    return out


_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> float:
    """A duration like '30s', '15m', '2h', '1d', or a bare number, in seconds."""
    m = re.match(r"^(\d+(?:\.\d+)?)([smhd]?)$", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(
            f"duration must be a number with optional s/m/h/d suffix, "
            f"got {value!r}")
    return float(m.group(1)) * _DURATION_UNITS[m.group(2)]


def _fmt_duration(seconds: float) -> str:
    """Compact human duration, largest sensible unit (e.g. '45s', '3m', '2h')."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit}"
    return f"{seconds:.0f}s"


class _ElateParser(argparse.ArgumentParser):
    """Argparse parser that hints at the global-flag ordering footgun.

    The global flags (`-s/--session`, `--json`, `--human`) live on the
    top-level parser, so they must precede the subcommand: `elate -s NAME
    stop`, not `elate stop -s NAME`. The latter trips argparse's
    "unrecognized arguments" path with a bare error; we append a pointer
    to the right ordering when a session flag is what got stranded.
    """

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments") and (
            "-s" in message.split() or "--session" in message
        ):
            message += ("\n(global flags go before the subcommand: "
                        "`elate -s NAME <command>`)")
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    p = _ElateParser(
        prog="elate",
        description="Drive sandboxed, observable Emacs sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "For repeatable/deterministic checks, prefer the declarative path:\n"
            "  write a JSON scenario (session + ordered steps + assertions) and\n"
            "  `elate run` it (exit 0/1, CI-able); bootstrap one from a live\n"
            "  session with `elate export-script`; run it across Emacs versions\n"
            "  with `elate matrix`. The act->wait->observe verbs below are for\n"
            "  exploration; the scenario you check into a project is the asset.\n"
            "\n"
            "Output: human tables on a terminal, JSON when stdout is piped\n"
            "  (i.e. for agents); force either with --json / --human."),
    )
    p.add_argument("--version", action="version", version=f"elate {__version__}")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true",
                     help="force machine-readable JSON output")
    out.add_argument("--human", action="store_true",
                     help="force the human-readable table, even when piped")
    p.add_argument("--field", metavar="NAME",
                   help="print just this one field of the result, bare (no "
                        "JSON envelope): strings unquoted, booleans "
                        "true/false, null, objects/arrays as compact JSON -- "
                        "so shell can test it with no parser: "
                        "[ \"$(elate --field name info X)\" = X ]. On "
                        "failure nothing goes to stdout (error on stderr, "
                        "usual exit code); an unknown field is a usage "
                        "error (exit 2) naming the available ones")
    p.add_argument("-s", "--session", metavar="NAME", help="session to operate on")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("start", help="start a new sandboxed session")
    sp.add_argument("--name",
                    help="session name (default: an auto-generated "
                         "elate-<hex>; the chosen name is in the result, so "
                         "later commands can reference it)")
    sp.add_argument("--replace", action="store_true",
                    help="if a session of this name is already running, stop "
                         "and recreate it (dead/stopped sessions of that name "
                         "are always replaced)")
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
    sp.add_argument("--require", action="append", default=[],
                    metavar="FEATURE",
                    help="feature to (require 'FEATURE) at startup, after "
                         "--load has wired load-path (repeatable); the "
                         "shorthand for the usual follow-up "
                         "--eval \"(require 'FEATURE)\"")
    sp.add_argument("--eval", action="append", default=[], metavar="FORM",
                    help="elisp form to evaluate at startup, before "
                         "emacs-startup-hook (repeatable)")
    sp.add_argument("--eval-file", action="append", default=[], metavar="PATH",
                    help="elisp file to load at startup, before "
                         "emacs-startup-hook (repeatable); like a reusable "
                         "--eval, with no load-path side effects")
    sp.add_argument("--profile", action="append", default=[], metavar="NAME",
                    help="named startup snippet from "
                         "$XDG_CONFIG_HOME/elate/profiles/NAME.el "
                         "(or a path to a .el file); loaded like --eval-file "
                         "(repeatable)")
    sp.add_argument("--home-seed", metavar="DIR",
                    help="copy this fixture tree into the sandbox's fake "
                         "$HOME before launch (rc files in place before any "
                         "subprocess spawns; keeps sandbox isolation)")
    sp.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    type=_parse_kv_pair,
                    help="set an environment variable for the Emacs process "
                         "and the subprocesses it spawns (repeatable); cannot "
                         "override the sandbox's HOME/XDG_* isolation vars")
    sp.add_argument("--size", type=_parse_size, default=(120, 36), metavar="COLSxROWS")
    sp.add_argument("--owner", metavar="NAME",
                    help="tag the session with an owner (e.g. an agent id); "
                         "list/stop/purge can then select by --owner, so "
                         "concurrent agents manage only their own sessions")
    sp.add_argument("--ttl", metavar="DUR", type=_parse_duration,
                    help="idle time-to-live (e.g. 30m, 2h; bare number = "
                         "seconds, minimum 30s): once the session has seen "
                         "no commands for this long it is stopped AND purged "
                         "by an opportunistic sweep any later elate command "
                         "runs -- so sessions leaked by a crashed agent "
                         "clean themselves up instead of accumulating")

    sp = sub.add_parser("stop", help="stop a session (or --all / a filter)")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")
    sp.add_argument("--all", action="store_true", dest="all_sessions",
                    help="stop every running session (instead of a name)")
    sp.add_argument("--glob", metavar="PATTERN", dest="name_glob",
                    help="stop running sessions whose name matches this "
                         "glob (e.g. 'rx-*') -- a bulk selector like --all")
    sp.add_argument("--name-prefix", metavar="PREFIX",
                    help="stop running sessions whose name starts with "
                         "PREFIX -- a bulk selector like --all")
    sp.add_argument("--owner", metavar="NAME",
                    help="stop running sessions started with --owner NAME; "
                         "combines with --glob/--name-prefix")

    sp = sub.add_parser(
        "interrupt",
        help="unblock a wedged session (raw C-g / signal) without stopping it",
        description="Poke a busy-but-alive session without killing it. TTY: "
                    "send raw C-g over tmux (works even when the semantic "
                    "channel is blocked). GUI (no raw channel): signal Emacs "
                    "-- --signal auto (default) breaks the running code into "
                    "the Lisp debugger with SIGUSR2, then unwinds it back to "
                    "top level once the semantic channel answers (the "
                    "C-g-like recovery; result carries recovered/depth_after)"
                    ". --signal usr2 only enters the debugger, so `debug "
                    "show` can reveal where it was stuck. --signal int sends "
                    "SIGINT, which TERMINATES a GUI-only Emacs (no tty frame "
                    "= quit request, not C-g) -- a deliberate shutdown, "
                    "never a recovery.")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")
    sp.add_argument("--signal", choices=["auto", "usr2", "int"],
                    default="auto",
                    help="GUI only: auto (SIGUSR2 + unwind to top level, "
                         "default), usr2 (enter the Lisp debugger and stay), "
                         "or int (SIGINT: terminates a GUI-only Emacs); "
                         "ignored for TTY")

    sp = sub.add_parser(
        "debug",
        help="inspect or unwind the Lisp debugger / a recursive edit",
        description="When code under test signals an error and *Backtrace* "
                    "pops up, the session sits in a recursive edit: it looks "
                    "idle, but every key and eval lands inside the debugger "
                    "(`wait idle` refuses to succeed there and points here; "
                    "`state` reports recursion-depth / in-debugger). 'show' "
                    "returns the backtrace text and depth without touching "
                    "anything; 'abort' throws back to top level -- ending "
                    "the debugger and restoring the window layout it saved "
                    "on entry -- with the session and its state intact. "
                    "This complements `interrupt`, which is for a busy Emacs "
                    "that is NOT answering the semantic channel.")
    sp.add_argument("action", nargs="?", choices=["show", "abort"],
                    default="show",
                    help="show (default): backtrace + depth; abort: throw "
                         "to top level (a no-op at recursion depth 0)")
    sp.add_argument("name", nargs="?",
                    help="session name (or use -s NAME; requires an "
                         "explicit action first)")
    sp.add_argument("--timeout", type=float, default=15.0, metavar="SECS")

    sp = sub.add_parser("list", help="list known sessions")
    sp.add_argument("name", nargs="?",
                    help="only this session (or use -s NAME)")
    sp.add_argument("--status", choices=["running", "stopped", "all"],
                    default="all",
                    help="filter by liveness: running, stopped (stopped/dead/"
                         "corrupt), or all (default)")
    sp.add_argument("--owner", metavar="NAME",
                    help="only sessions started with --owner NAME")
    sp.add_argument("--older-than", metavar="DUR", type=_parse_duration,
                    help="only show sessions inert at least this long "
                         "(e.g. 30s, 15m, 2h, 1d; bare number = seconds) -- "
                         "running sessions are excluded; pairs with `purge "
                         "--stopped-older-than`")

    sp = sub.add_parser(
        "purge",
        help="delete the sandboxes of stopped/dead sessions",
        description="Delete the sandbox directories (transcripts included) "
                    "of sessions that are no longer running. Stopped "
                    "sandboxes are inert but accumulate forever otherwise; "
                    "purge is the supported cleanup. A running session is "
                    "never purged: naming one is an error, and --all skips "
                    "and reports it. Leftover processes of dead sessions "
                    "are cleaned up before their files go.")
    sp.add_argument("names", nargs="*", metavar="NAME",
                    help="session to purge (repeatable)")
    sp.add_argument("--all", action="store_true", dest="all_sessions",
                    help="purge every session that is not running")
    sp.add_argument("--stopped-older-than", metavar="DUR", type=_parse_duration,
                    help="only purge sessions inert at least this long "
                         "(e.g. 30s, 15m, 2h, 1d; bare number = seconds) -- "
                         "keeps just-stopped sandboxes during heavy runs")
    sp.add_argument("--glob", metavar="PATTERN", dest="name_glob",
                    help="purge sessions whose name matches this glob (e.g. "
                         "'run-*') -- a bulk selector like --all; running "
                         "matches are skipped")
    sp.add_argument("--name-prefix", metavar="PREFIX",
                    help="purge sessions whose name starts with PREFIX (e.g. "
                         "'run-') -- a bulk selector like --all")
    sp.add_argument("--owner", metavar="NAME",
                    help="purge sessions started with --owner NAME -- a bulk "
                         "selector like --all; combines with the other "
                         "filters")

    sp = sub.add_parser(
        "prune",
        help="alias for `purge` (delete stopped/dead sandboxes)",
        description="Alias for `purge`: delete the sandbox directories of "
                    "sessions that are no longer running. `purge` is the "
                    "canonical spelling; `prune` exists for discoverability.")
    sp.add_argument("names", nargs="*", metavar="NAME",
                    help="session to prune (repeatable)")
    sp.add_argument("--all", action="store_true", dest="all_sessions",
                    help="prune every session that is not running")
    sp.add_argument("--stopped-older-than", metavar="DUR", type=_parse_duration,
                    help="only prune sessions inert at least this long "
                         "(e.g. 30s, 15m, 2h, 1d; bare number = seconds)")
    sp.add_argument("--glob", metavar="PATTERN", dest="name_glob",
                    help="prune sessions whose name matches this glob "
                         "(e.g. 'run-*') -- a bulk selector like --all")
    sp.add_argument("--name-prefix", metavar="PREFIX",
                    help="prune sessions whose name starts with PREFIX "
                         "(e.g. 'run-') -- a bulk selector like --all")
    sp.add_argument("--owner", metavar="NAME",
                    help="prune sessions started with --owner NAME -- a "
                         "bulk selector like --all")

    sp = sub.add_parser("info", help="show session details")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")

    sp = sub.add_parser(
        "path",
        help="print a session's private scratch directory (or another "
             "sandbox path)",
        description="Print one on-disk path of the session, the scratch "
                    "directory by default: a per-session private directory "
                    "for setup files and artifacts, safe from concurrent "
                    "agents (in-session code sees it as $ELATE_SCRATCH). "
                    "Prints the bare path, so it substitutes cleanly: "
                    "cp setup.el \"$(elate -s NAME path)\"/. Works for "
                    "stopped sessions too.")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")
    sp.add_argument("--kind", choices=["scratch", "dir", "home", "log"],
                    default="scratch",
                    help="which path: scratch (default; created on demand), "
                         "dir (the sandbox root), home (the fake $HOME), "
                         "log (Emacs stderr/GUI logs)")

    sp = sub.add_parser(
        "keys", help="send keys (Emacs kbd notation)",
        description="Send a key sequence. Semantic keys run through the "
                    "command loop and obey the focused buffer's keymaps, so "
                    "a buffer that intercepts keys (a terminal emulator in "
                    "char mode, special-mode buffers) can swallow one and "
                    "your intended command never runs. The result's "
                    "`command` field is what the sequence resolves to in the "
                    "focused buffer (null for an unbound key or a "
                    "multi-command sequence); `eval` a command directly to "
                    "run it regardless of bindings.")
    sp.add_argument("keys", help="key sequence in Emacs kbd notation, "
                                 "e.g. 'C-x C-f' or 'M-x foo RET'")
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--semantic", action="store_true",
                     help="deliver via execute-kbd-macro (default)")
    grp.add_argument("--raw", action="store_true",
                     help="deliver as raw terminal bytes via tmux")
    sp.add_argument("--events", action="store_true",
                    help="semantic, but queue on unread-command-events "
                         "(non-blocking; use for sequences that open a prompt)")
    sp.add_argument("--no-abort-on-bell", action="store_true",
                    help="deliver via unread-command-events so a command "
                         "that rings the bell (e.g. evil insert off the "
                         "prompt row) beeps instead of aborting the whole "
                         "sequence; asynchronous -- follow with a wait. "
                         "Semantic keys run through the command loop and "
                         "obey active keymaps (evil state etc.)")
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

    sp = sub.add_parser(
        "send-process",
        help="send raw input to a buffer's subprocess (comint/REPL/shell)",
        description="Write bytes straight to the process behind a buffer "
                    "(`process-send-string`), bypassing the command loop -- "
                    "for driving shells/REPLs/terminals. Unlike keys/type "
                    "(which talk to Emacs), this talks to the subprocess: "
                    "send ^C to interrupt a job, seed shell history, feed a "
                    "REPL. Targeting rule: the buffer must have exactly one "
                    "live process -- none or several is an error naming the "
                    "candidates (never a silent pick); disambiguate with "
                    "--process. The result echoes the process's name and "
                    "command line so a wrong target is visible.")
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("text", nargs="?", help="literal text to send")
    grp.add_argument("--char", metavar="KBD",
                     help="send an Emacs kbd string, e.g. 'C-c' (^C / SIGINT), "
                          "'RET' (newline), 'TAB'")
    grp.add_argument("--file", metavar="PATH",
                     help="send the contents of PATH (read inside Emacs; for "
                          "payloads past the argv size limit)")
    sp.add_argument("--buffer", metavar="NAME",
                    help="buffer whose process to target (default: current); "
                         "must have exactly one live process unless "
                         "--process picks one")
    sp.add_argument("--process", metavar="NAME",
                    help="target this process by name (`get-process`), for "
                         "buffers with several processes; with --buffer the "
                         "process must belong to that buffer")

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

    sp = sub.add_parser("focus", help="inject a window-system focus event "
                                      "(focus-in/out; semantic, tty and gui)")
    sp.add_argument("direction", choices=["in", "out"],
                    help="'in' (focus-in) or 'out' (focus-out)")
    sp.add_argument("--frame", metavar="NAME",
                    help="target the frame with this name (default: selected)")
    sp.add_argument("--set-focus-state", action="store_true",
                    help="also make (frame-focus-state) report the injected "
                         "state -- a non-native shim, since injected events "
                         "cannot move the real C-owned focus state (they do "
                         "fire after-focus-change-function regardless)")
    sp.add_argument("--timeout", type=float, default=15.0, metavar="SECS")

    sp = sub.add_parser("send-events",
                        help="inject an ordered stream of focus/mouse/key "
                             "events (drains through the command loop in order)")
    sp.add_argument("events", nargs="+", metavar="EVENT",
                    help="event tokens in order: focus-in, focus-out, "
                         "down-mouse-N, mouse-N, up-mouse-N, double-mouse-N, "
                         "wheel-up, wheel-down (N=1..3, each with optional "
                         "@LINE,COL [1-based line, 0-based col] or #POS), or "
                         "key:KBD (e.g. key:RET). A focus event only fires at "
                         "the head of a command-loop turn, so any focus events "
                         "are delivered in separate drained batches "
                         "automatically -- making any ordering faithful, "
                         "including a mouse-down before a focus-in")
    sp.add_argument("--buffer", metavar="NAME",
                    help="target the window showing this buffer for mouse "
                         "events (default: selected window)")
    sp.add_argument("--frame", metavar="NAME",
                    help="target frame for focus events (default: selected)")
    sp.add_argument("--set-focus-state", action="store_true",
                    help="also make (frame-focus-state) report injected focus "
                         "(non-native shim; see 'elate focus')")
    sp.add_argument("--timeout", type=float, default=15.0, metavar="SECS")

    sp = sub.add_parser("resize", help="resize a live session (tmux window "
                                       "or GUI frame)")
    sp.add_argument("size", type=_parse_size, metavar="COLSxROWS")

    sp = sub.add_parser(
        "attach",
        help="attach a human terminal to a live TTY session (hand off / "
             "take over)",
        description="Drop into the session's tmux client so a human can "
                    "drive Emacs directly, then detach with C-b d to hand "
                    "back -- the session keeps running (do NOT use C-x C-c, "
                    "which kills Emacs). TTY sessions only: a GUI session's "
                    "Emacs window is already on screen (use screenshot). "
                    "Requires a real terminal; this replaces the elate "
                    "process with `tmux attach`. Your terminal size "
                    "temporarily drives the frame while attached.")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")
    sp.add_argument("--read-only", "-r", action="store_true",
                    help="attach read-only: watch without sending input "
                         "(detach still works with C-b d)")
    sp.add_argument("--print-command", action="store_true",
                    help="print the tmux command that would run, without "
                         "attaching (scripting/tests)")

    sp = sub.add_parser("eval", help="evaluate an elisp form")
    sp.add_argument("form", nargs="?", default=None,
                    help="elisp source: one or more forms, evaluated as "
                         "(progn ...); omit when using --file")
    sp.add_argument("--file", metavar="PATH", dest="source_file",
                    help="read the elisp source from PATH instead of the "
                         "FORM argument ('-' reads stdin). Sidesteps shell "
                         "quoting entirely, so forms containing ' or #' run "
                         "verbatim -- use this to test snippets exactly as "
                         "written. One or more forms, evaluated as "
                         "(progn ...) like the positional FORM")
    sp.add_argument("--buffer", metavar="NAME",
                    help="evaluate in this buffer (default: the selected "
                         "window's buffer, so current-buffer/point/line see "
                         "what is on screen, not an arbitrary buffer)")
    sp.add_argument("--timeout", type=float, default=15.0, metavar="SECS")
    sp.add_argument("--backtrace", action="store_true",
                    help="on error, also return structured backtrace frames "
                         "(each frame's function + printed args), not just "
                         "the rendered backtrace string")
    sp.add_argument("--on-timeout", choices=["none", "sample"], default="none",
                    help="on timeout with Emacs still busy: 'sample' captures "
                         "a thread backtrace of the wedged Emacs (macOS "
                         "`sample`; Linux eu-stack/gdb) and attaches it to "
                         "the error; 'none' (default) does not")
    sp.add_argument("--json-result", action="store_true",
                    help="serialize the elisp value to real JSON inside the "
                         "session, so `value` is a queryable object "
                         "(jq .value.mode), not a printed sexp string. "
                         "Plists/alists of atoms map cleanly (nil -> null, "
                         "t -> true, symbols -> names); a value with no "
                         "faithful JSON shape (buffers, markers, circular "
                         "structures) falls back to the printed string -- "
                         "the result's value-encoding says which came back "
                         "('json' or 'printed'), check it")
    sp.add_argument("--raw", action="store_true", dest="raw_value",
                    help="print just the value, no JSON envelope -- shell "
                         "tests it directly: [ \"$(elate -s N eval --raw "
                         "'major-mode')\" = fundamental-mode ]. On an elisp "
                         "error nothing goes to stdout (error on stderr, "
                         "exit 1). Shorthand for the global --field value; "
                         "combine with --json-result for the bare JSON value")

    sp = sub.add_parser(
        "trace",
        help="trace elisp functions (log calls/args/returns), then read "
             "the accumulated log",
        description="Wrap trace-function-background around one or more "
                    "functions so each call records its args and return "
                    "value. Tracing is invisible: the *trace-output* buffer "
                    "is never displayed, so the window layout under test "
                    "stays untouched. 'on FUNC...' starts tracing; drive the "
                    "session "
                    "(keys/eval/...); 'read' returns and clears the log so "
                    "each read sees only new calls; 'off [FUNC...]' "
                    "untraces the named functions (or all). Drives Emacs "
                    "internals you cannot see on screen -- why an advice "
                    "fires twice, what args a hook receives.")
    sp.add_argument("action", choices=["on", "off", "read"])
    sp.add_argument("functions", nargs="*", metavar="FUNC",
                    help="function name(s): required for 'on', optional for "
                         "'off' (default: untrace all), unused for 'read'")
    sp.add_argument("--keep", action="store_true",
                    help="trace read: keep the log instead of clearing it")
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
    sp.add_argument("--package-lint", action="store_true",
                    help="ALSO run package-lint (additive; items tagged "
                         "tool=package-lint). package-lint is installed "
                         "into the sandbox elpa/ on demand. Without "
                         "--archive-dir it refreshes the standard "
                         "archives over the NETWORK (non-deterministic); "
                         "a setup failure aborts with a clear error and "
                         "the session survives")
    sp.add_argument("--archive-dir", metavar="DIR",
                    help="for --package-lint: a local directory holding "
                         "an archive-contents index, used directly as a "
                         "package archive (a plain path, not a file:// "
                         "URL) -- offline and REPRODUCIBLE (the "
                         "recommended/CI path; the answer to "
                         "package-lint's archive non-determinism)")

    sp = sub.add_parser(
        "profile",
        help="drive Emacs's native profiler (start/stop/report, or "
             "one-shot 'run FORM')",
        description="Drive Emacs's native sampling profiler. 'start' "
                    "begins sampling (--cpu default, --mem allocations, "
                    "--both), resetting earlier logs and running a GC "
                    "first (pre-existing garbage is never charged to "
                    "the window); 'stop' ends it; "
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
    sp.add_argument("position", metavar="LINE:COL", nargs="?",
                    help="1-based line, 0-based column (or use --pos)")
    sp.add_argument("--pos", type=int, metavar="N",
                    help="address by absolute buffer position instead of "
                         "LINE:COL (handy from elisp, which holds positions)")
    sp.add_argument("--run", type=int, default=1, metavar="K",
                    help="dump K consecutive cells from the position in one "
                         "call (default 1) -- e.g. compare a typed cell "
                         "against the suggestion cell next to it")
    sp.add_argument("--buffer", metavar="NAME",
                    help="buffer to inspect (default: current)")

    sub.add_parser("popups", help="capture visible popups as text "
                                  "(which-key, transient, hydra, "
                                  "completion previews, child frames)")

    sub.add_parser("messages", help="new *Messages* output since last call")

    sub.add_parser("echo", help="current echo area / minibuffer line")

    sp = sub.add_parser(
        "state",
        help="one-call scene snapshot (layout, prompt, point, modes, "
             "messages tail)",
        description="Full-snapshot shape: current-buffer facts (buffer, "
                    "file, point, line, column, major-mode, minor-modes, "
                    "region, narrowed, modified), echo, minibuffer (prompt/"
                    "input/completions or null), input-pending, "
                    "last-command, idle (secs or null), recursion-depth + "
                    "in-debugger (non-zero/true = a recursive edit or the "
                    "Lisp debugger is eating input -- see `debug`), popups, "
                    "messages-tail, and 'windows': the window-tree as "
                    "nested objects, NOT a flat list -- an inner node is "
                    "{split: 'vertical'|'horizontal', children: [...]}, a "
                    "leaf is one window {buffer, selected, width, height, "
                    "line, column, start-line, end-line, mode-line, text, "
                    "...}; recurse until nodes have no 'children'.")
    sp.add_argument("--since", metavar="TOKEN",
                    help="return only what changed since the TOKEN from a "
                         "prior state call (new/killed/modified buffers, "
                         "point/selection movement, new *Messages* lines, "
                         "minibuffer change) -- much cheaper than a full "
                         "snapshot, and 'changed':false means your last "
                         "action did nothing observable. Every state result "
                         "carries a fresh 'token'; an unknown/stale one "
                         "degrades to a full snapshot.")

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

    sp = sub.add_parser(
        "logs", aliases=["stderr"],
        help="tail the driven Emacs's stderr/stdout log",
        description="Show the tail of the Emacs process log -- module "
                    "panics, GC/native-comp warnings, and the fatal-signal "
                    "line on a crash. TTY sessions capture stderr to a file "
                    "(off the pane, so screenshots stay clean); GUI sessions "
                    "log stdout+stderr. Works on dead/stopped sessions too.")
    sp.add_argument("name", nargs="?", help="session name (or use -s NAME)")
    sp.add_argument("-n", "--lines", type=int, default=40, metavar="N",
                    help="number of trailing lines to show (default 40)")

    sp = sub.add_parser(
        "wait", help="wait for a condition (exit 3 on timeout)",
        description="Block until a condition holds. `wait idle` fails "
                    "immediately (rather than timing out) when Emacs is "
                    "parked in the Lisp debugger's recursive edit -- that "
                    "session will never be idle; recover with `debug abort`.")
    sp.add_argument("condition",
                    choices=["idle", "text", "prompt", "stable", "until",
                             "dead"])
    sp.add_argument("args", nargs="*",
                    help="idle: [MIN_IDLE_SECS]; text: REGEXP (Python regex "
                         "syntax, not elisp); until: an elisp predicate "
                         "form, polled until non-nil (an elisp error fails "
                         "the wait -- wrap in ignore-errors if an error "
                         "means \"not yet\"); prompt/stable/dead: none")
    sp.add_argument("--buffer", help="buffer to search (wait text), watch "
                                     "(wait stable; may not exist yet), or "
                                     "evaluate the predicate in (wait until)")
    sp.add_argument("--quiet-ms", type=int, default=300, metavar="MS",
                    help="wait stable: settle threshold -- the buffer must be "
                         "unchanged for this many ms (default 300)")
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
                    help="keep the fresh session running afterwards (named "
                         "from the scenario's \"name\", or --name)")
    sp.add_argument("--name", metavar="NAME",
                    help="name for a kept session (default with --keep: the "
                         "scenario's \"name\"); a running name collision is "
                         "an error")
    sp.add_argument("--no-purge", action="store_true",
                    help="on success, keep the throwaway sandbox on disk "
                         "(stopped) instead of removing it; failed runs are "
                         "always kept for post-mortem")
    sp.add_argument("--keep-on-failure", action="store_true",
                    help="keep the fresh session running when the run "
                         "fails (inspect it with state/screenshot, then "
                         "stop it)")
    sp.add_argument("--keep-going", action="store_true",
                    help="run every step even after a failure instead of "
                         "stopping at the first (a failed run still exits "
                         "non-zero); use for a matrix that must report every "
                         "check. Per-step \"optional\": true never gates.")
    sp.add_argument("--emacs", metavar="PATH",
                    help="override the script's emacs binary (CI matrix)")
    sp.add_argument("--update-snapshots", action="store_true",
                    help="write/overwrite golden artifacts for snapshot "
                         "assertions instead of comparing them; the run still "
                         "executes every step (review the diff before "
                         "committing)")
    sp.add_argument("--snapshot-dir", metavar="DIR",
                    help="base directory for golden snapshots "
                         "(default: <scenario-dir>/__snapshots__)")
    sp.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    type=_parse_kv_pair, dest="set_vars",
                    help="bind a {{NAME}} template variable in the scenario "
                         "(repeatable); overrides a scenario \"params\" "
                         "default, so one scenario can drive many configs")
    sp.add_argument("--set-file", action="append", default=[],
                    metavar="NAME=PATH", type=_parse_kv_pair,
                    dest="set_file_vars",
                    help="bind a {{NAME}} template variable to PATH's "
                         "contents verbatim (one trailing newline stripped) "
                         "-- multi-line/quote-heavy values without shell "
                         "quoting; binding a NAME in both --set and "
                         "--set-file is an error")
    sp.add_argument("--variant", metavar="NAME",
                    help="select one entry of the scenario's \"variants\" "
                         "block: its bindings overlay the \"params\" defaults "
                         "as a set ({{variant}} binds the name; --set still "
                         "wins per variable). `matrix` runs every variant.")
    sp.add_argument("--format", choices=("json", "human", "junit", "tap"),
                    help="output format: 'human' (default when not piped) a "
                         "summary + per-group verdicts, 'json' the full "
                         "result, 'junit' a JUnit XML testsuite (one testcase "
                         "per group / ungrouped step), 'tap' TAP version 13. "
                         "Overrides the global --json/--human for this run.")

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
    sp.add_argument("--clean", action="store_true",
                    help="prune transient temp-path references so the export "
                         "replays elsewhere: drop session load/eval entries "
                         "under a temp root, and mark an eval step that "
                         "references one \"skip\" with a comment")

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
        help="run a scenario across Emacs binaries, variants, and parameter "
             "axes",
        description="Run SCRIPT once per combination of the Emacs binary "
                    "axis, the scenario's \"variants\" (every declared "
                    "variant by default; a variant is a NAMED set of "
                    "co-varying {{var}} bindings), and any --param axes -- "
                    "their Cartesian product -- each in a fresh session, "
                    "and aggregate the verdicts into one grid. Each --param "
                    "NAME=v1,v2 binds the scenario's {{NAME}} template per "
                    "combo, so one file drives many shells/configs x Emacs "
                    "versions. Exits 0 only when every combo passed (xfail "
                    "honored).")
    sp.add_argument("--emacs", action="append", default=[], metavar="PATHS",
                    help="emacs binary, or comma-separated list (repeatable)")
    sp.add_argument("--emacs-glob", metavar="GLOB",
                    help="glob matching emacs binaries, e.g. "
                         "'/opt/emacs-*/bin/emacs'")
    sp.add_argument("--param", action="append", default=[], metavar="NAME=V1,V2",
                    help="a parameter axis: bind {{NAME}} to each "
                         "comma-separated value in turn (repeatable; every "
                         "axis is crossed with the Emacs axis)")
    sp.add_argument("--variant", action="append", default=[], metavar="NAMES",
                    help="filter the variant axis to these \"variants\" "
                         "entries (comma-separated, repeatable); default: "
                         "every declared variant")
    sp.add_argument("--set-file", action="append", default=[],
                    metavar="NAME=PATH", type=_parse_kv_pair,
                    dest="set_file_vars",
                    help="bind a {{NAME}} template variable to PATH's "
                         "contents verbatim (one trailing newline stripped), "
                         "constant across every combo -- not an axis")
    sp.add_argument("--format", choices=("json", "human"),
                    help="output format: 'human' (default) the grid, 'json' "
                         "the structured results. Overrides --json/--human.")
    sp.add_argument("--update-snapshots", action="store_true",
                    help="write/overwrite golden snapshots (per Emacs "
                         "version and param combo) instead of comparing")
    sp.add_argument("--snapshot-dir", metavar="DIR",
                    help="base directory for golden snapshots "
                         "(default: <scenario-dir>/__snapshots__)")
    sp.add_argument("script", help="path to the scenario file (JSON)")

    sp = sub.add_parser(
        "install",
        help="install the elate skill into AI coding harnesses",
        description="Copy elate's Agent Skill (SKILL.md) into one or more AI "
                    "coding harnesses so they learn to drive the elate CLI. "
                    "Targets: " + ", ".join(install.HARNESS_KEYS) + " (or "
                    "'all'). With no target, installs for every harness "
                    "detected on this machine. By default the skill lands in "
                    "the current project's skills dirs (e.g. .claude/skills); "
                    "--global installs user-wide instead. The skill is the "
                    "CLI-centric integration that works everywhere; --mcp "
                    "additionally registers the optional MCP server where it "
                    "is supported.")
    sp.add_argument("harness", nargs="*", metavar="HARNESS",
                    help="harness(es) to install for: "
                         + " ".join(install.HARNESS_KEYS) + " or 'all' "
                         "(default: auto-detect)")
    sp.add_argument("--global", dest="global_scope", action="store_true",
                    help="install into the user-global skills dirs (e.g. "
                         "~/.claude/skills) instead of the current project's")
    sp.add_argument("--mcp", action="store_true",
                    help="also wire the MCP server: `mcp add` where the "
                         "harness has that CLI (Claude Code, Codex), a "
                         "paste-ready snippet otherwise (opencode, "
                         "Antigravity); pi has no MCP")
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be installed without writing anything")

    sp = sub.add_parser(
        "update",
        help="upgrade elate and refresh installed skill copies",
        description="Upgrade the elate package via whatever installed it "
                    "(Homebrew, uvx, uv tool, pipx, pip), then rerun `elate "
                    "install` for every skill copy found on this machine so "
                    "the copies match the new CLI. Prints the plan and asks "
                    "before running anything; already-running sessions keep "
                    "the old in-Emacs agent until restarted.")
    sp.add_argument("--yes", action="store_true",
                    help="run without the interactive confirmation "
                         "(required when stdin is not a terminal)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the plan without executing anything")

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


def run_attach(args: argparse.Namespace) -> int:
    """Hand off to a human by exec'ing `tmux attach` into the session.

    Handled outside the normal command dispatch (like `mcp`): on success it
    replaces the process and never returns. GUI/dead sessions raise
    ElateError (exit 1); a non-interactive stdio raises UsageError (exit 2)
    rather than exec'ing into nothing.
    """
    sess = S.load_session(_name_arg(args))
    # Raises ElateError for GUI (no tmux) or a missing socket. Built before
    # the liveness check so a GUI error wins over a "not running" one.
    argv = sess.tmux_attach_argv(read_only=args.read_only)
    if args.print_command:
        # Dry run: still surfaces the GUI/socket error above, but never
        # touches liveness or the terminal -- the testable seam.
        print(" ".join(shlex.quote(a) for a in argv))
        return 0
    sess.require_alive()  # a wedged-but-alive Emacs is fine (send it C-g)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise UsageError(
            "attach needs a real terminal; it hands you an interactive tmux "
            "client. Run it from a terminal, or observe non-interactively "
            f"with: elate -s {sess.name} screenshot / record")
    sess.log("attach", read_only=bool(args.read_only))
    try:
        os.execvp("tmux", argv)  # replaces this process; does not return
    except OSError as exc:
        raise ElateError(f"cannot exec tmux: {exc}") from exc
    return 0  # unreachable; keeps the type checker happy


def cmd_start(args: argparse.Namespace) -> Result:
    cols, rows = args.size
    sess = S.start_session(
        args.name,
        emacs=args.emacs,
        config=args.config,
        init_file=args.init_file,
        loads=args.load,
        requires=args.require,
        evals=args.eval,
        eval_files=args.eval_file,
        profiles=args.profile,
        home_seed=args.home_seed,
        env=dict(args.env),
        cols=cols,
        rows=rows,
        ui=args.ui,
        headless=args.headless,
        replace=args.replace,
        owner=args.owner,
        ttl=args.ttl,
    )
    info = S.session_info(sess.name)
    # Transient GTK/dbus helpers spawned during GUI startup read as
    # "orphans" seconds after launch; the count means something for a
    # long-lived session (`info`/`list`), not here.
    info.pop("orphans", None)
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
    if sess.ui == "gui":
        wm_warning = S.gui_wm_warning(sess)
        if wm_warning:
            info["wm_warning"] = wm_warning
            print(f"elate: WARNING: {wm_warning}", file=sys.stderr)
            human += f"\nWARNING: {wm_warning}"
    notice = install.staleness_notice()
    if notice:
        print(notice, file=sys.stderr)
    return info, human, 0


def cmd_stop(args: argparse.Namespace) -> Result:
    filtered = (args.name_glob is not None or args.name_prefix is not None
                or args.owner is not None)
    if args.all_sessions or filtered:
        if args.name or args.session:
            raise ElateError(
                "give a session name or a bulk selector "
                "(--all/--glob/--name-prefix/--owner), not both")
        running = [s for s in S.list_sessions() if s["status"] == "running"]
        if args.name_glob is not None:
            running = [s for s in running
                       if fnmatch.fnmatch(s["name"], args.name_glob)]
        if args.name_prefix is not None:
            running = [s for s in running
                       if s["name"].startswith(args.name_prefix)]
        if args.owner is not None:
            running = [s for s in running if s.get("owner") == args.owner]
        names = [s["name"] for s in running]
        for n in names:
            S.stop_session(n)
        what = "" if args.all_sessions and not filtered else "matching "
        human = (f"stopped {len(names)} {what}session(s): {', '.join(names)}"
                 if names else f"no running {what}sessions to stop")
        return {"stopped": names}, human, 0
    name = _name_arg(args)
    result = S.stop_session(name)
    if result.get("stopped"):
        human = f"stopped session {name!r}"
    else:
        human = f"no such session {name!r} (nothing to stop)"
    return result, human, 0


def cmd_interrupt(args: argparse.Namespace) -> Result:
    name = _name_arg(args)
    result = S.interrupt_session(name, sig=args.signal)
    human = f"interrupted {name!r} via {result['delivered']}"
    if result.get("recovered"):
        after = result.get("depth_after")
        human += (" and unwound to top level" if after == 0
                  else f" and left the debugger (recursion depth {after} -- "
                       "likely an open minibuffer)")
    elif result.get("recovered") is False:
        human += (" -- semantic channel still not answering; if it stays "
                  f"wedged, `elate -s {name} stop`")
    return result, human, 0


def cmd_debug(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    data = S.debug_session(sess, args.action, timeout=args.timeout)
    depth = data.get("recursion-depth")
    if args.action == "abort":
        after = data.get("depth-after")
        if not data.get("scheduled"):
            human = "already at top level (recursion depth 0); nothing to abort"
        elif after == 0:
            human = f"aborted to top level (was at recursive edit depth {depth})"
        elif after is None:
            human = (f"abort scheduled at depth {depth}, but Emacs stopped "
                     "answering before it could be confirmed -- check "
                     "`state`")
        else:
            human = (f"abort scheduled; recursion depth {depth} -> {after} "
                     "(not yet at top level -- re-run to unwind further)")
        return data, human, 0
    if data.get("in-debugger"):
        human = (f"in the Lisp debugger (recursive edit depth {depth}):\n"
                 f"{data.get('backtrace') or '(empty *Backtrace*)'}")
    elif depth:
        human = f"in a recursive edit (depth {depth}), no debugger buffer"
    else:
        human = "not in the debugger (recursion depth 0)"
    return data, human, 0


def cmd_purge(args: argparse.Namespace) -> Result:
    result = S.purge_sessions(args.names, all_sessions=args.all_sessions,
                              stopped_older_than=args.stopped_older_than,
                              name_glob=args.name_glob,
                              name_prefix=args.name_prefix,
                              owner=args.owner)
    purged = result["purged"]
    skipped = result["skipped_running"]
    recent = result.get("skipped_recent") or []
    if purged:
        mib = result["freed_bytes"] / (1024 * 1024)
        names = ", ".join(p["name"] for p in purged)
        human = f"purged {len(purged)} sandbox(es), {mib:.1f} MiB: {names}"
        for p in purged:
            if p.get("note"):
                human += f"\n{p['name']}: {p['note']}"
    else:
        human = "nothing to purge"
    if recent:
        human += (f"\nkept (stopped too recently): {', '.join(recent)}")
    if skipped:
        human += (f"\nskipped (still running): {', '.join(skipped)} "
                  "-- stop them first")
    return result, human, 0


def cmd_list(args: argparse.Namespace) -> Result:
    sessions = S.list_sessions()
    name = getattr(args, "name", None) or args.session
    if name:
        sessions = [s for s in sessions if s["name"] == name]
    if args.status == "running":
        sessions = [s for s in sessions if s["status"] == "running"]
    elif args.status == "stopped":  # everything inert: stopped/dead/corrupt
        sessions = [s for s in sessions if s["status"] != "running"]
    if args.owner is not None:
        sessions = [s for s in sessions if s.get("owner") == args.owner]
    older_than = getattr(args, "older_than", None)
    if older_than is not None:
        # Running sessions have idle_for=None -> excluded, so this lists
        # only inert sandboxes at least this old (the `purge` companion).
        sessions = [s for s in sessions
                    if s.get("idle_for") is not None
                    and s["idle_for"] >= older_than]
    if not sessions:
        what = (f"no session named {name!r}" if name else
                f"no sessions inert for {_fmt_duration(older_than)}"
                if older_than is not None else
                f"no sessions owned by {args.owner!r}"
                if args.owner is not None else
                "no sessions" if args.status == "all" else
                f"no {args.status} sessions")
        return {"sessions": sessions}, what, 0
    # The OWNER column appears only when some session carries an owner
    # tag, so single-agent listings keep the compact four-column table.
    owned = any(s.get("owner") for s in sessions)
    header = f"{'NAME':<20} {'UI':<4} {'STATUS':<11} {'EMACS':<10} "
    header += f"{'OWNER':<12} AGE" if owned else "AGE"
    lines = [header]
    for s in sessions:
        if s.get("uptime") is not None:
            age = f"up {_fmt_duration(s['uptime'])}"
        elif s.get("idle_for") is not None:
            age = f"idle {_fmt_duration(s['idle_for'])}"
        else:
            age = "-"
        if s.get("expires_in") is not None:
            age += f" (ttl: {_fmt_duration(s['expires_in'])} left)"
        # A dead session renders its fatal signal inline ("dead (SIGABRT)");
        # the JSON keeps the stable status + a separate signal field.
        status = s["status"]
        if s.get("signal"):
            status = f"{status} ({s['signal']})"
        elif s.get("orphans"):
            status = f"{status} +{s['orphans']} orphan(s)"
        line = (f"{s['name']:<20} {s.get('ui') or '-':<4} {status:<11} "
                f"{s.get('emacs_version') or '-':<10} ")
        if owned:
            line += f"{s.get('owner') or '-':<12} "
        lines.append(line + age)
    inert = sum(1 for s in sessions if s["status"] != "running")
    if inert >= 5:
        lines.append(f"\n{inert} inert session(s) -- reclaim their sandboxes "
                     "with `elate purge --stopped-older-than 1h`")
    return {"sessions": sessions}, "\n".join(lines), 0


def cmd_info(args: argparse.Namespace) -> Result:
    info = S.session_info(_name_arg(args))
    human = "\n".join(f"{k}: {v}" for k, v in info.items())
    return info, human, 0


def cmd_path(args: argparse.Namespace) -> Result:
    sess = S.load_session(_name_arg(args))
    if args.kind == "scratch":
        path = sess.scratch_dir()
    elif args.kind == "dir":
        path = sess.dir
    elif args.kind == "home":
        path = sess.dir / "home"
    else:  # log
        path = sess.dir / "log"
    data = {"name": sess.name, "kind": args.kind, "path": str(path)}
    return data, str(path), 0


def cmd_keys(args: argparse.Namespace) -> Result:
    queued = args.events or args.no_abort_on_bell
    if args.raw and queued:
        raise ElateError("--events/--no-abort-on-bell are semantic delivery "
                         "modes; drop --raw")
    sess = _require_session(args)
    method = "events" if queued else "macro"
    sess.log("keys", keys=args.keys,
             channel="raw" if args.raw else "semantic", method=method)
    if args.raw:
        sess.raw().send_kbd(args.keys)
        result = {"keys": args.keys, "channel": "raw"}
    else:
        try:
            data = sess.semantic().rpc("keys", args.keys, method, timeout=args.timeout)
        except EvalTimeout as exc:
            raise EvalTimeout(
                f"{exc}\nHint: the key sequence may have left Emacs reading input. "
                "Retry with --events (queued delivery) or --raw."
            ) from exc
        result = {"keys": args.keys, "channel": "semantic", **data}
    msg = f"sent {args.keys!r} ({result['channel']})"
    if result.get("command"):
        msg += f" -> {result['command']}"
    return result, msg, 0


def cmd_type(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("type", text=args.text, channel="raw" if sess.ui == "tty" else "events")
    result = S.deliver_type(sess, args.text)
    return result, f"typed {len(args.text)} chars ({result['channel']})", 0


def cmd_send_process(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    if args.file is not None:
        data = sess.semantic().rpc("send-process-file", args.file, args.buffer,
                                   args.process)
        what = f"file {args.file!r}"
        kind = "file"
    else:
        payload = args.char if args.char is not None else args.text
        as_kbd = args.char is not None
        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        data = sess.semantic().rpc("send-process", args.buffer, b64, as_kbd,
                                   args.process)
        what = f"{args.char!r} (kbd)" if as_kbd else f"{len(payload)} chars"
        kind = "char" if as_kbd else "text"
    sess.log("send-process", buffer=args.buffer, process=args.process,
             kind=kind)
    human = (f"sent {what} to {data.get('process')} "
             f"({data.get('bytes')} bytes) in {data.get('buffer')}")
    return data, human, 0


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


def cmd_focus(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("focus", direction=args.direction, frame=args.frame,
             set_focus_state=args.set_focus_state)
    data = S.focus_event(sess, args.direction, frame=args.frame,
                         set_focus_state=args.set_focus_state,
                         timeout=args.timeout)
    human = (f"focus-{args.direction} on frame "
             f"{data.get('frame') or '(selected)'} "
             f"({data.get('queued')} event queued)")
    return data, human, 0


def cmd_send_events(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("send-events", events=args.events, buffer=args.buffer,
             frame=args.frame, set_focus_state=args.set_focus_state)
    data = S.send_events(sess, args.events, buffer=args.buffer,
                         frame=args.frame,
                         set_focus_state=args.set_focus_state,
                         timeout=args.timeout)
    human = (f"queued {data.get('queued')} event(s) from "
             f"{data.get('specs')} token(s) in {data.get('batches')} "
             f"batch(es) -> {data.get('buffer')}")
    return data, human, 0


def cmd_resize(args: argparse.Namespace) -> Result:
    sess = _get_session(args)
    cols, rows = args.size
    data = S.resize_session(sess, cols, rows)
    human = f"resized to {cols}x{rows}"
    if data.get("wm_warning"):
        human += f"\nWARNING: {data['wm_warning']}"
    return data, human, 0


_EVAL_SOURCE_CAP = 512 * 1024  # bytes of elisp source; travels base64 in argv


def _eval_source(args: argparse.Namespace) -> str:
    """Resolve eval's elisp source: the FORM argument, --file PATH, or stdin."""
    if args.source_file is None:
        if args.form is None:
            raise UsageError("eval needs a FORM argument or --file PATH")
        return args.form
    if args.form is not None:
        raise UsageError("eval takes a FORM argument or --file, not both")
    if args.source_file == "-":
        source = sys.stdin.read()
    else:
        try:
            source = Path(args.source_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ElateError(
                f"cannot read --file {args.source_file}: {exc}") from exc
    if len(source.encode("utf-8")) > _EVAL_SOURCE_CAP:
        raise ElateError(
            f"--file {args.source_file}: source exceeds the eval channel's "
            f"{_EVAL_SOURCE_CAP // 1024} KiB cap (it travels over argv); "
            "eval a (load \"/path/to/file.el\") form instead")
    return source


def cmd_eval(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    form = _eval_source(args)
    args.form = form  # downstream messages/logging see the real source
    sess.log("eval", form=form, timeout=args.timeout, buffer=args.buffer)
    try:
        data = sess.semantic().eval_form(form, timeout=args.timeout,
                                          backtrace=args.backtrace,
                                          buffer=args.buffer,
                                          json_result=args.json_result)
    except EvalTimeout as exc:
        busy = sess.is_busy()
        sess.log("eval-timeout", form=args.form)
        sample = None
        if busy and args.on_timeout == "sample":
            from . import diagnostics
            sample = diagnostics.sample_process(sess.emacs_pid)
            sess.log("eval-timeout-sample", available=sample.get("available"))
        if busy and sess.ui == "tty":
            hint = (" -- Emacs is still busy; unwedge it with "
                    f"`elate -s {sess.name} interrupt` (raw C-g), or pass a "
                    "bigger --timeout for a legitimately slow form")
        elif busy:
            hint = (" -- Emacs is still busy; unwedge it with "
                    f"`elate -s {sess.name} interrupt` (breaks the GUI Emacs "
                    "into the debugger and unwinds it; --signal usr2 to stay "
                    "in the debugger and read the backtrace with `debug "
                    "show`), or pass a bigger --timeout for a legitimately "
                    "slow form; stop the session if it stays wedged")
        else:
            hint = ""
        raise EvalTimeout(f"{exc}{hint}", sample=sample) from exc
    except TransportError as exc:
        # The semantic socket vanished mid-eval -- usually Emacs just died
        # (e.g. a crash the form triggered). Turn the opaque "connection
        # refused" into a death verdict with the signal + crash report.
        if S.died_during(sess):
            enrich = S.crash_enrichment(sess)
            msg = S.death_message(enrich)
            sess.log("eval-died", **enrich)
            # ok:False here wins over main()'s {"ok": True, **result} spread
            # (later keys win), so the JSON correctly reads ok:false at exit 1.
            data = {"ok": False, "session_died": True, "error": msg, **enrich}
            return data, msg, 1
        raise
    sess.log("eval-result", **data)
    parts = []
    if data.get("error"):
        parts.append(f"error: {data['error']}")
        if data.get("backtrace"):
            parts.append(f"backtrace:\n{data['backtrace']}")
        if data.get("frames"):
            parts.append("frames:")
            for fr in data["frames"]:
                a = fr.get("args")
                shown = " ".join(a) if a else ""
                parts.append(f"  {fr.get('fun')}{(' ' + shown) if shown else ''}")
        # Exit 1 below; make the JSON "ok" flag agree with the exit code.
        data = {**data, "ok": False}
    else:
        if data.get("value-encoding") == "json":
            parts.append(json.dumps(data.get("value"), ensure_ascii=False))
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
    sess.log("lint", files=args.files, timeout=args.timeout,
             package_lint=args.package_lint, archive_dir=args.archive_dir)
    data = S.lint_files(sess, args.files, timeout=args.timeout,
                        package_lint=args.package_lint,
                        archive_dir=args.archive_dir)
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


def cmd_trace(args: argparse.Namespace) -> Result:
    sess = _require_session(args)
    sess.log("trace", action=args.action, functions=args.functions,
             keep=args.keep)
    data = S.trace_functions(sess, args.action, functions=args.functions,
                             keep=args.keep, timeout=args.timeout)
    if args.action == "read":
        lines = []
        out = (data.get("output") or "").rstrip("\n")
        if out:
            lines.append(out)
        else:
            lines.append("(no trace output)")
        if data.get("truncated"):
            lines.append(f"(output truncated to {len(data.get('output') or '')}"
                         f" of {data.get('output-length')} chars)")
        if data.get("active"):
            lines.append("tracing: " + " ".join(data["active"]))
        return data, "\n".join(lines), 0
    if args.action == "on":
        bits = []
        if data.get("traced"):
            bits.append("traced " + " ".join(data["traced"]))
        if data.get("already"):
            bits.append("already traced " + " ".join(data["already"]))
        return data, "; ".join(bits) or "nothing to trace", 0
    # off
    if data.get("all"):
        return data, "untraced all functions", 0
    return data, "untraced " + " ".join(data.get("untraced") or []), 0


def _faces_cell_human(data: dict[str, Any]) -> list[str]:
    """Human lines for one faces cell (a faces-at result, or a range cell)."""
    head = (f"{data.get('line')}:{data.get('column')} "
            f"(pos {data.get('pos')}) char {data.get('char')!r}")
    if data.get("buffer"):
        head = f"{data['buffer']} " + head
    lines = [head]
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
    if data.get("property-values"):
        lines.append("text properties: " + " ".join(
            f"{p['name']}={p['value']}" for p in data["property-values"]))
    elif data.get("properties"):
        lines.append("text properties: " + " ".join(data["properties"]))
    overlays = data.get("overlays") or []
    if overlays:
        lines.append(f"overlays ({len(overlays)}):")
        lines.extend(f"  {_human_overlay(o)}" for o in overlays)
    return lines


def cmd_faces_at(args: argparse.Namespace) -> Result:
    if args.run < 1:
        raise ElateError("faces-at --run must be >= 1")
    if args.pos is not None and args.position is not None:
        raise ElateError("give LINE:COL or --pos, not both")
    if args.pos is None and args.position is None:
        raise ElateError("faces-at needs LINE:COL or --pos N")
    sess = _require_session(args)
    if args.pos is not None:
        if args.pos < 1:
            raise ElateError("faces-at --pos must be >= 1")
        start = args.pos
        if args.run == 1:
            data = sess.semantic().rpc("faces-at-pos", args.pos, args.buffer)
            sess.log("faces-at", pos=args.pos, buffer=args.buffer)
            return data, "\n".join(_faces_cell_human(data)), 0
    else:
        m = re.match(r"^(\d+):(\d+)$", args.position)
        if not m:
            raise ElateError(f"position must be LINE:COL, got {args.position!r}")
        line, col = int(m.group(1)), int(m.group(2))
        if line < 1:
            raise ElateError("faces-at line must be >= 1")
        data = sess.semantic().rpc("faces-at", line, col, args.buffer)
        sess.log("faces-at", line=line, col=col, buffer=args.buffer)
        if args.run == 1:
            return data, "\n".join(_faces_cell_human(data)), 0
        start = data["pos"]  # resolve LINE:COL to a position for the range
    data = sess.semantic().rpc("faces-range", start, args.run, args.buffer)
    sess.log("faces-range", start=start, count=args.run, buffer=args.buffer)
    cells = data.get("cells") or []
    lines = [f"{data.get('buffer')}: {data.get('count')} cell(s) from pos "
             f"{data.get('start')}"]
    for c in cells:
        lines.append("")
        lines.extend("  " + ln for ln in _faces_cell_human(c))
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
    data = sess.semantic().rpc("state", args.since)
    sess.log("state", buffer=data.get("buffer"), since=bool(args.since),
             mode=data.get("mode"))
    human = (_human_delta(data) if data.get("mode") == "delta"
             else _human_state(data))
    return data, human, 0


def _human_delta(data: dict[str, Any]) -> str:
    """Compact human view of a `state --since` delta."""
    if not data.get("changed"):
        return "no change"
    lines: list[str] = []
    bufs = data.get("buffers") or {}
    marks = ([f"+{b}" for b in bufs.get("added") or []]
             + [f"-{b}" for b in bufs.get("removed") or []]
             + [f"~{b}" for b in bufs.get("modified") or []])
    if marks:
        lines.append("buffers: " + " ".join(marks))
    sel = (data.get("selection") or {}).get("buffer")
    if sel:
        lines.append(f"selected: {sel.get('from')} -> {sel.get('to')}")
    cur = data.get("current") or {}
    pt = cur.get("point")
    if pt:
        lines.append(f"point: {pt.get('from')} -> {pt.get('to')} "
                     f"({pt.get('line')}:{pt.get('column')})")
    if cur.get("modified"):
        lines.append(f"modified: {cur.get('buffer')}")
    mb = data.get("minibuffer") or {}
    if mb.get("change"):
        prompt = f" {mb['prompt']!r}" if mb.get("prompt") else ""
        lines.append(f"minibuffer: {mb['change']}{prompt}")
    tail = (data.get("messages") or "").rstrip("\n")
    if tail:
        lines.append("messages:")
        lines.extend(f"  {ln}" for ln in tail.splitlines())
    return "\n".join(lines)


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


def cmd_logs(args: argparse.Namespace) -> Result:
    from . import gui

    # No require_alive: the log outlives the Emacs, so a crashed/stopped
    # session's stderr is exactly what you want to read here.
    sess = S.load_session(_name_arg(args))
    log = sess.emacs_log_path
    text = gui.log_tail(log, lines=args.lines)
    sess.log("logs", lines=args.lines, ui=sess.ui)
    return ({"path": str(log), "ui": sess.ui, "lines": args.lines, "log": text},
            text, 0)


def cmd_wait(args: argparse.Namespace) -> Result:
    # `wait dead` must not require a live session -- it is waiting for the
    # opposite (and the session may already be gone).
    sess = (_get_session(args) if args.condition == "dead"
            else _require_session(args))
    # buffer must be logged or the transcript->script exporter would
    # silently retarget replayed waits at the then-current buffer.
    sess.log("wait", condition=args.condition, args=args.args,
             buffer=args.buffer, quiet_ms=args.quiet_ms, timeout=args.timeout)
    if args.condition == "dead":
        data = S.wait_dead(sess, timeout=args.timeout)
        return data, S.death_message(data), 0
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
        return data, f"idle {data.get('idle'):.2f}s since last activity", 0
    if args.condition == "text":
        if not args.args:
            raise ElateError("wait text needs a REGEXP argument")
        data = S.wait_text(sess, args.args[0], buffer=args.buffer, timeout=args.timeout)
        return data, f"matched {data['matched']!r} in {data['buffer']}", 0
    if args.condition == "stable":
        data = S.wait_stable(sess, buffer=args.buffer,
                             quiet_ms=args.quiet_ms, timeout=args.timeout)
        return (data, f"stable: {data['buffer']} unchanged for "
                      f"{data['quiet_ms']}ms ({data['ticks_seen']} edits seen)", 0)
    if args.condition == "until":
        if len(args.args) != 1:
            raise ElateError("wait until needs exactly one elisp predicate "
                             "form (quote it as a single argument)")
        data = S.wait_until(sess, args.args[0], buffer=args.buffer,
                            timeout=args.timeout)
        return data, f"until: predicate returned {data.get('value')}", 0
    # prompt
    data = S.wait_prompt(sess, timeout=args.timeout)
    return data, f"prompt: {data.get('prompt')!r} (contents: {data.get('contents')!r})", 0


# -- Phase 5: scripts, recording, snap series, matrix -------------------------

_STEP_MARK = {"ok": "ok", "failed": "FAIL", "skipped": "skipped",
              "not-run": "not run", "comment": "note",
              "xfail": "xfail (known)", "xpass": "XPASS"}


def _step_line(rec: dict[str, Any], total: int) -> str:
    mark = _STEP_MARK[rec["status"]]
    if rec["status"] == "failed" and rec.get("optional"):
        mark += " (optional)"
    if rec["status"] in ("xfail", "xpass") and rec.get("reason"):
        mark += f" [{rec['reason']}]"
    line = f"[{rec['index']}/{total}] {rec['summary']} ... {mark}"
    if rec.get("duration") is not None:
        line += f" ({rec['duration']:.2f}s)"
    if rec["status"] == "failed":
        line += f"\n  error: {rec.get('error')}"
        detail = rec.get("detail") or {}
        if detail.get("diff"):
            line += "\n" + "\n".join(f"  {ln}"
                                     for ln in detail["diff"].splitlines())
        elif detail.get("actual_written"):
            line += (f"\n  golden {detail.get('golden_bytes')} bytes vs actual "
                     f"{detail.get('actual_bytes')} bytes; wrote "
                     f"{detail['actual_written']}")
    return line


def _run_summary(result: dict[str, Any]) -> str:
    bits = [f"{result['passed']} passed"]
    # result["failed"] counts every failed-status step; optional failures
    # are broken out so a passing run never reads "PASS ... 1 failed".
    optional_failed = result.get("optional_failed", 0)
    gating_failed = result.get("failed", 0) - optional_failed
    for key, label, val in (
            ("failed", "failed", gating_failed),
            ("xpass", "XPASS", result.get("xpass", 0)),
            ("optional_failed", "optional-failed", optional_failed),
            ("xfail", "xfail", result.get("xfail", 0)),
            ("skipped", "skipped", result.get("skipped", 0)),
            ("not_run", "not run", result.get("not_run", 0))):
        if val:
            bits.append(f"{val} {label}")
    line = (("PASS" if result["success"] else "FAIL")
            + f": {', '.join(bits)} in {result['duration']:.2f}s")
    if result.get("groups"):
        line += "\n" + " · ".join(
            f"{g['name']}: {g['status']}" for g in result["groups"])
    if result.get("error"):
        line += f"\nerror: {result['error']}"
    elif result.get("init_error"):
        # allow_init_error runs: surface the tolerated error anyway.
        line += ("\nWARNING: session startup code signalled an error: "
                 f"{result['init_error']}")
    if result.get("kept") and result.get("fresh_session"):
        line += (f"\nsession kept (running): elate -s {result['session']} "
                 f"state; elate stop {result['session']}")
    elif result.get("fresh_session") and not result.get("purged"):
        # Stopped but the sandbox (transcript/logs) is on disk: the default on
        # a FAILED run (for post-mortem), or a successful run with --no-purge.
        line += (f"\nsandbox kept: elate -s {result['session']} logs; "
                 f"elate purge {result['session']} (or purge --glob 'run-*')")
    return line


# XML 1.0 forbids the C0 control chars except tab/newline/CR. A failing
# assert against a terminal buffer routinely puts a raw ESC/NUL/BEL into the
# error message, so strip them or the "valid XML" promise breaks.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_safe(s: str) -> str:
    return _XML_ILLEGAL.sub("", s)


def _group_outcome(g: dict[str, Any]) -> tuple[str, str]:
    """Normalize a group verdict to (outcome, message) for junit/tap."""
    return {
        "PASS": ("pass", ""),
        "FAIL": ("fail", "group failed"),
        "XPASS": ("xpass", "a known-failing step passed -- drop the xfail marker"),
        "XFAIL": ("xfail", "known failure"),
        "OPT-FAIL": ("optfail", "optional failure"),
        "not-run": ("notrun", "not run"),
        "SKIP": ("skip", ""),
        # An unknown verdict surfaces as a failure rather than a silent pass.
    }.get(g["status"], ("fail", f"unknown group verdict {g['status']!r}"))


def _step_outcome(rec: dict[str, Any]) -> tuple[str, str]:
    """Normalize a step status to (outcome, message) for junit/tap."""
    s = rec["status"]
    if s == "ok":
        return "pass", ""
    if s == "failed":
        if rec.get("optional"):
            return "optfail", rec.get("error", "optional failure")
        return "fail", rec.get("error", "failed")
    if s == "xpass":
        return "xpass", "unexpected pass -- drop the xfail marker"
    if s == "xfail":
        return "xfail", rec.get("reason", "known failure")
    if s == "not-run":
        return "notrun", "not run"
    if s == "skipped":
        return "skip", ""
    # Unknown/future status: surface it, never silently green.
    return "fail", f"unknown status {s!r}"


def _testcases(result: dict[str, Any]) -> list[tuple[str, str, str]]:
    """One (name, outcome, message) per test case, in step order.

    A group becomes a single case (emitted at its first step); every
    ungrouped, non-comment step becomes its own case. Comment/marker steps
    are annotations, not tests.
    """
    groups = result.get("groups") or []
    first = {g["steps"][0]: g for g in groups if g.get("steps")}
    grouped = {i for g in groups for i in g.get("steps", [])}
    cases: list[tuple[str, str, str]] = []
    for rec in result.get("steps") or []:
        idx = rec["index"]
        if idx in first:
            g = first[idx]
            cases.append((f"group: {g['name']}", *_group_outcome(g)))
        elif idx in grouped or rec["status"] == "comment":
            continue
        else:
            cases.append((f"step {idx}: {rec['summary']}", *_step_outcome(rec)))
    return cases


def _format_junit(result: dict[str, Any]) -> str:
    from xml.sax.saxutils import quoteattr
    cases = _testcases(result)
    fail_kinds = {"fail", "xpass"}
    n_fail = sum(1 for _, o, _ in cases if o in fail_kinds)
    n_skip = sum(1 for _, o, _ in cases if o not in fail_kinds and o != "pass")
    body: list[str] = []
    for name, outcome, msg in cases:
        attr = f"name={quoteattr(_xml_safe(name))}"
        if outcome == "pass":
            body.append(f"  <testcase {attr}/>")
        elif outcome in fail_kinds:
            body.append(f"  <testcase {attr}>")
            body.append(
                f"    <failure message={quoteattr(_xml_safe(msg or outcome))}/>")
            body.append("  </testcase>")
        else:
            body.append(f"  <testcase {attr}>")
            body.append(f"    <skipped message={quoteattr(_xml_safe(msg))}/>"
                        if msg else "    <skipped/>")
            body.append("  </testcase>")
    head = (f"<testsuite name={quoteattr(_xml_safe(result.get('name') or 'elate'))} "
            f'tests="{len(cases)}" failures="{n_fail}" skipped="{n_skip}" '
            f'time="{result.get("duration", 0):.3f}">')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + head
            + ("\n" + "\n".join(body) if body else "") + "\n</testsuite>")


def _format_tap(result: dict[str, Any]) -> str:
    cases = _testcases(result)
    lines = ["TAP version 13", f"1..{len(cases)}"]
    for i, (name, outcome, msg) in enumerate(cases, 1):
        ok, directive = {
            "fail": ("not ok", ""),
            "xpass": ("not ok", f"xpass: {msg}" if msg else "xpass"),
            "xfail": ("ok", f"TODO xfail: {msg}" if msg else "TODO xfail"),
            # A tolerated failure: "not ok" (it failed) but TODO (non-gating),
            # mirroring JUnit's <skipped> -- a plain "ok" would read as a pass.
            "optfail": ("not ok", "TODO optional failure"),
            "skip": ("ok", "SKIP"),
            "notrun": ("ok", "SKIP not run"),
        }.get(outcome, ("ok", ""))
        # '#' would start a spurious directive; a newline would split the line
        # into a phantom extra test -- keep both out of the description AND the
        # directive (msg may be a multi-line, script-authored reason).
        desc = name.replace("#", "").replace("\n", " ").replace("\r", " ")
        directive = directive.replace("\n", " ").replace("\r", " ")
        line = f"{ok} {i} - {desc}"
        if directive:
            line += f" # {directive}"
        lines.append(line)
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> Result:
    from . import script as SC

    if args.session and args.emacs:
        # Loud, never silent: an existing session already runs its own
        # binary; --emacs cannot retroactively apply to it.
        raise ElateError(
            "--emacs cannot apply to an existing session (-s NAME); drop "
            "-s to run a fresh session with that binary")
    if args.name and not args.keep:
        # A name is only meaningful for a session that survives the run;
        # otherwise it just leaves a non-run-* leftover on failure.
        raise ElateError("--name requires --keep (it names the kept session)")
    overrides = {**_read_set_files(args.set_file_vars, args.set_vars),
                 **dict(args.set_vars)}
    script, base = SC.load_script(args.script, overrides,
                                  variant=args.variant)
    target = None
    if args.session:
        target = S.load_session(args.session)
    total = len(script.get("steps") or [])
    fmt = args.format
    # Per-step streaming lines only make sense for the human summary; JSON,
    # the junit/tap documents, and --field's single bare value are printed
    # whole at the end (streamed lines would pollute a $(...) capture).
    stream = (fmt == "human" or (fmt is None and not args.json)) \
        and not getattr(args, "field_mode", False)
    on_step = None
    if stream:
        def on_step(rec: dict[str, Any]) -> None:
            print(_step_line(rec, total), flush=True)
    # A kept session's name: --name wins, else (with --keep) the scenario's
    # "name". Purge the throwaway sandbox on a successful run unless kept or
    # --no-purge.
    keep_as_name = args.name or (script.get("name") if args.keep else None)
    result = SC.run_script(
        script, base_dir=base, session=target, emacs=args.emacs,
        keep=args.keep, keep_on_failure=args.keep_on_failure,
        keep_going=args.keep_going,
        keep_as_name=keep_as_name,
        purge=not args.no_purge,
        on_step=on_step,
        update_snapshots=args.update_snapshots,
        snapshot_dir=(base / Path(args.snapshot_dir).expanduser()
                      if args.snapshot_dir else None),
        # Fold the variant into the stem so `--variant a --update-snapshots`
        # and `--variant b` never share goldens (matches matrix's stems).
        snapshot_stem=Path(args.script).stem
        + (f"+variant-{args.variant}" if args.variant else ""),
        variant=args.variant,
    )
    if fmt == "junit":
        text = _format_junit(result)
    elif fmt == "tap":
        text = _format_tap(result)
    else:  # human / json / None -- the dispatcher picks json vs the summary
        text = _run_summary(result)
    return result, text, 0 if result["success"] else 1


def cmd_export_script(args: argparse.Namespace) -> Result:
    from . import script as SC

    # No require_alive: the transcript outlives the Emacs (stopped and
    # crashed sessions export fine).
    sess = _get_session(args)
    script = SC.export_script(sess, clean=args.clean)
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


def _safe_param(value: str) -> str:
    """A param value reduced to a filename-safe token (for snapshot stems)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "x"


def _matrix_label(emacs: str, params: dict[str, str],
                  version: str | None = None) -> str:
    head = f"emacs {version}" if version else Path(emacs).name
    tail = "  ".join(f"{k}={v}" for k, v in params.items())
    return f"{head}  {tail}".rstrip()


def cmd_matrix(args: argparse.Namespace) -> Result:
    import glob as globlib
    import itertools
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

    # Parameter axes: --param NAME=v1,v2 (repeatable), crossed with the
    # Emacs axis into a Cartesian product.
    axes: dict[str, list[str]] = {}
    for spec in args.param:
        name, sep, vals = spec.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ElateError(f"--param expects NAME=v1,v2,..., got {spec!r}")
        if name == "emacs":
            raise ElateError(
                "--param 'emacs' is reserved (the Emacs binary is its own "
                "axis; use --emacs / --emacs-glob)")
        if name == "variant":
            raise ElateError(
                "--param 'variant' is reserved (variants are their own "
                'axis; declare a "variants" block in the scenario and '
                "filter with --variant)")
        if name in axes:
            raise ElateError(f"--param {name!r} given more than once")
        values = [v for v in (s.strip() for s in vals.split(",")) if v]
        if not values:
            raise ElateError(f"--param {name!r} lists no values")
        axes[name] = values

    # --set-file vars are constant bindings, identical in every combo (the
    # counterpart of run's --set/--set-file; matrix's per-combo bindings are
    # the axes). A name on both sides would make the axis silently dead.
    file_vars = _read_set_files(args.set_file_vars, [])
    fclash = sorted(n for n in file_vars if n in axes)
    if fclash:
        raise ElateError(
            f"--set-file name(s) {fclash} collide with a --param axis of "
            "the same name -- a constant binding cannot also be an axis")

    raw, base = SC.load_raw(args.script)
    if not isinstance(raw, dict):
        raise ElateError('a script must be a JSON object with a "steps" list')
    total = len(raw.get("steps") or [])

    # The variant axis: every declared "variants" entry by default (the
    # whole point is "one file, one command, out pops the grid");
    # --variant filters. [None] when the scenario declares none.
    variants = SC.declared_variants(raw)
    selected: list[str] = []
    for spec in args.variant:
        selected.extend(v for v in (s.strip() for s in spec.split(",")) if v)
    if selected:
        if not variants:
            raise ElateError(
                '--variant given but the scenario declares no "variants" '
                "block")
        unknown_v = [n for n in selected if n not in variants]
        if unknown_v:
            raise ElateError(
                f"unknown --variant name(s) {unknown_v}; the scenario "
                f"declares {sorted(variants)}")
        if len(set(selected)) != len(selected):
            dup = sorted({n for n in selected if selected.count(n) > 1})
            raise ElateError(f"--variant name(s) {dup} given more than once")
    variant_axis: list[str | None] = list(selected or variants) or [None]

    # An axis the scenario never references is almost certainly a mistake (a
    # typo'd name, or a spurious axis silently multiplying the run). A
    # deliberate "repeat" axis can reference {{name}} in a comment. The
    # same goes for a variant binding a never-referenced variable.
    referenced = SC.template_vars({k: v for k, v in raw.items()
                                   if k not in ("params", "variants")})
    unused = [n for n in axes if n not in referenced]
    if unused:
        raise ElateError(
            f"--param axis/axes {sorted(unused)} are never referenced by a "
            "{{name}} template in the scenario")
    dead_files = sorted(n for n in file_vars if n not in referenced)
    if dead_files:
        raise ElateError(
            f"--set-file name(s) {dead_files} are never referenced by a "
            "{{name}} template in the scenario")
    for vname in variant_axis:
        if vname is None:
            continue
        dead = [k for k in variants[vname] if k not in referenced]
        if dead:
            raise ElateError(
                f"variant {vname!r} binds {dead} never referenced by a "
                "{{name}} template in the scenario")
        # A --param axis crossing a variant-bound variable would silently
        # mask that binding in EVERY variant (documented precedence),
        # making the variant columns identical -- loud, never silent.
        clash = [n for n in axes if n in variants[vname]]
        if clash:
            raise ElateError(
                f"--param axis/axes {clash} collide with binding(s) of "
                f"variant {vname!r}: a variant's bindings co-vary -- drop "
                "the --param axis or the variant key")

    axis_names = list(axes)
    combos: list[tuple[str, str | None, dict[str, str]]] = []
    for point in itertools.product(uniq, variant_axis,
                                   *(axes[n] for n in axis_names)):
        params = {n: point[i + 2] for i, n in enumerate(axis_names)}
        combos.append((point[0], point[1], params))

    # Fail once per VARIANT, up front, on a combo-independent
    # template/params error (an unknown {{var}} depends only on the axis
    # NAMES plus the variant's binding keys, identical across that
    # variant's combos) instead of the same error N times.
    if combos:
        first_params = {**file_vars,
                        **{n: axes[n][0] for n in axis_names}}
        for vname in variant_axis:
            SC.render_script(raw, first_params, variant=vname)

    # Snapshot stems fold in the variant + param combo (the Emacs axis is
    # separated by @<version> in the filename). _safe_param is lossy, so two
    # distinct param combos can sanitize to the same suffix -- disambiguate
    # only the ones that actually collide with a short hash, keeping clean
    # stems for the common (no-collision) case, including no params at all.
    # The variant rides the same machinery as a pseudo-param ("variant" is
    # reserved, so it can never clash with a real axis), yielding
    # "+variant-nu". The name goes in verbatim, NOT through _safe_param:
    # its charset is already filename-safe by construction, and _safe_param's
    # edge-stripping would make a name like "nu-" produce a stem that `run
    # --variant nu-` (which appends the name verbatim) could never match.
    import hashlib
    from collections import Counter

    def combo_key(vname: str | None,
                  params: dict[str, str]) -> tuple[tuple[str, str], ...]:
        merged = {**params, **({"variant": vname} if vname else {})}
        return tuple(sorted(merged.items()))

    raw_suffix: dict[tuple, str] = {}
    for _, vname, params in combos:
        key = combo_key(vname, params)
        raw_suffix.setdefault(key, "".join(
            f"+{k}-{v if k == 'variant' else _safe_param(v)}"
            for k, v in key))
    dupes = Counter(raw_suffix.values())
    suffix = {
        key: (suf + "-" + hashlib.sha1(repr(key).encode()).hexdigest()[:6]
              if suf and dupes[suf] > 1 else suf)
        for key, suf in raw_suffix.items()
    }

    fmt = args.format
    stream = (fmt == "human" or (fmt is None and not args.json)) \
        and not getattr(args, "field_mode", False)
    on_step = None
    if stream:
        def on_step(rec: dict[str, Any]) -> None:
            print(_step_line(rec, total), flush=True)
    if len(combos) > 1:
        # To stderr so it surfaces even in --json/CI mode without dirtying
        # the machine-readable stdout.
        print(f"matrix: running {len(combos)} combo(s)", file=sys.stderr,
              flush=True)

    base_stem = Path(args.script).stem
    results: list[dict[str, Any]] = []
    for emacs_bin, vname, params in combos:
        # The variant leads in labels/axes: it names the whole binding set
        # the params merely refine.
        shown = {**({"variant": vname} if vname else {}), **params}
        if stream:
            print(f"=== {_matrix_label(emacs_bin, shown)} ===", flush=True)
        stem = base_stem + suffix[combo_key(vname, params)]
        axes_out = {"emacs": emacs_bin, **shown}
        try:
            rendered = SC.render_script(raw, {**file_vars, **params},
                                        variant=vname)
            run = SC.run_script(
                rendered, base_dir=base, emacs=emacs_bin, on_step=on_step,
                purge=True,  # purge a passing combo; failed combos are kept
                variant=vname,
                update_snapshots=args.update_snapshots,
                snapshot_dir=(base / Path(args.snapshot_dir).expanduser()
                              if args.snapshot_dir else None),
                snapshot_stem=stem)
            entry = {
                "axes": axes_out,
                "emacs": emacs_bin,
                "version": run.get("emacs_version"),
                "success": run["success"],
                "passed": run["passed"],
                "failed": run["failed"],
                "xpass": run.get("xpass", 0),
                "xfail": run.get("xfail", 0),
                "duration": run["duration"],
                # The step that caused the FAIL: a real (non-optional)
                # failure, or an xpass -- both gate; an optional failure and a
                # plain xfail do not.
                "failed_step": next(
                    (r["summary"] for r in run["steps"]
                     if (r["status"] == "failed" and not r.get("optional"))
                     or r["status"] == "xpass"), None),
            }
        except ElateError as exc:
            # One broken combo must not abort the rest of the matrix.
            entry = {"axes": axes_out, "emacs": emacs_bin, "version": None,
                     "success": False, "error": str(exc)}
        results.append(entry)

    success = all(r["success"] for r in results)
    passed = sum(1 for r in results if r["success"])
    lines = [f"{'RESULT':<6} {'TIME':>7}  COMBO"]
    for r in results:
        took = f"{r['duration']:.1f}s" if r.get("duration") is not None else "-"
        params = {k: v for k, v in r["axes"].items() if k != "emacs"}
        lines.append(
            f"{'pass' if r['success'] else 'FAIL':<6} {took:>7}  "
            f"{_matrix_label(r['emacs'], params, r.get('version'))}")
        if r.get("error"):
            lines.append(f"    error: {r['error']}")
        elif r.get("failed_step"):
            lines.append(f"    failed at: {r['failed_step']}")
    lines.append(f"{passed}/{len(results)} combo(s) passed")
    return ({"success": success, "script": args.script,
             "axes": (["variant"] if variants else []) + axis_names,
             "results": results},
            "\n".join(lines), 0 if success else 1)


def cmd_install(args: argparse.Namespace) -> Result:
    result = install.run_install(
        args.harness,
        project=not args.global_scope,
        with_mcp=args.mcp,
        dry_run=args.dry_run,
    )
    for notice in result["notices"]:
        print(f"elate: {notice}", file=sys.stderr)
    return result, install.format_summary(result), 0


def cmd_update(args: argparse.Namespace) -> Result:
    channel = install.install_channel()
    steps = install.update_steps(channel)
    for step in steps:
        step["pretty"] = " ".join(shlex.quote(word) for word in step["cmd"])
    plan = "\n".join(
        [f"update plan (installed via {channel}):"]
        + [f"  $ {s['pretty']}"
           + (f"   (in {s['cwd']})" if s.get("cwd") else "")
           for s in steps])
    running = [s for s in S.list_sessions() if s["status"] == "running"]
    if args.dry_run:
        return ({"channel": channel,
                 "steps": [{"cmd": s["pretty"], "cwd": s.get("cwd"),
                            "status": "planned"} for s in steps],
                 "sessions_running": len(running), "dry_run": True},
                plan, 0)
    # The plan goes to stderr (also under --yes, where it is the only
    # trace of what ran until the steps finish) so a piped stdout stays
    # clean for the final result.
    print(plan, file=sys.stderr)
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError(
                "elate update runs upgrade commands; confirm with --yes "
                "(or preview with --dry-run)")
        # input() echoes its prompt to stdout, so keep it bare and put
        # the question on stderr too; EOF (^D) declines.
        print("proceed? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            # ok:False here wins over main()'s {"ok": True, **result}
            # spread -- the declined run must not read as success.
            return ({"ok": False, "channel": channel, "steps": [],
                     "aborted": True},
                    "update aborted", 1)
    executed: list[dict] = []
    for step in steps:
        try:
            proc = subprocess.run(step["cmd"], capture_output=True,
                                  text=True, cwd=step.get("cwd"))
        except OSError as exc:
            raise ElateError(
                f"update step failed: {step['pretty']}: {exc}") from exc
        if proc.returncode != 0:
            tail = "\n".join(
                (proc.stderr or proc.stdout or "").strip().splitlines()[-5:])
            raise ElateError(
                f"update step failed (exit {proc.returncode}): "
                f"{step['pretty']}\n{tail}")
        executed.append({"cmd": step["pretty"], "cwd": step.get("cwd"),
                         "status": "ok"})
    human_lines = [f"updated via {channel}:"] + [
        f"  ✓ {e['cmd']}" for e in executed]
    if running:
        human_lines.append(
            f"{len(running)} running session(s) still use the old in-Emacs "
            "agent — `elate stop --all` and restart them")
    human_lines.append(
        "if `elate mcp` is registered, restart the MCP client to pick up "
        "the new server")
    return ({"channel": channel, "steps": executed,
             "sessions_running": len(running)},
            "\n".join(human_lines), 0)


_COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "interrupt": cmd_interrupt,
    "debug": cmd_debug,
    "list": cmd_list,
    "purge": cmd_purge,
    "prune": cmd_purge,  # alias
    "info": cmd_info,
    "path": cmd_path,
    "logs": cmd_logs,
    "stderr": cmd_logs,  # alias
    "keys": cmd_keys,
    "type": cmd_type,
    "mouse": cmd_mouse,
    "focus": cmd_focus,
    "send-events": cmd_send_events,
    "resize": cmd_resize,
    "eval": cmd_eval,
    "buffer": cmd_buffer,
    "test": cmd_test,
    "lint": cmd_lint,
    "profile": cmd_profile,
    "bench": cmd_bench,
    "trace": cmd_trace,
    "faces-at": cmd_faces_at,
    "send-process": cmd_send_process,
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
    "install": cmd_install,
    "update": cmd_update,
}


def _field_text(result: dict[str, Any], field: str) -> str:
    """One result field rendered bare for direct shell consumption.

    Strings print unquoted, booleans as true/false, None as null,
    numbers plainly, and anything structured as compact JSON -- so
    `[ "$(elate --field name info X)" = X ]` needs no parser at all.
    """
    if field not in result:
        have = ", ".join(sorted(result))
        raise UsageError(
            f"--field {field!r}: the result has no such field (it has: "
            f"{have})")
    v = result[field]
    if isinstance(v, str):
        return v
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Resolve the output mode once: explicit --json/--human win; otherwise
    # emit JSON when stdout is not a TTY (an agent or a pipe) and the human
    # table on a real terminal. Assigning back to args.json keeps every
    # downstream check (including run/matrix progress streaming) correct.
    # --field NAME (and eval --raw = --field value) prints one bare result
    # field for direct shell consumption; it owns the output, so an
    # explicit --json/--human alongside is a contradiction, not a merge.
    field = args.field or ("value" if getattr(args, "raw_value", False) else None)
    if field is not None and (args.json or args.human):
        # A redundant flag is not worth failing a scripted pipeline over:
        # warn and let --field/--raw own the output as documented.
        print("elate: --field/--raw already choose the output; "
              "ignoring --json/--human", file=sys.stderr)
    args.json = args.json or (not args.human and not sys.stdout.isatty())
    if field is not None:
        # Failures must keep stdout empty -- an error blob inside $(...)
        # would be compared as a value -- so force the human/stderr error
        # path; field_mode also suppresses run/matrix progress streaming.
        args.json = False
    args.field_mode = field is not None
    if args.command == "mcp":
        # Serve MCP over stdio. Imported lazily so plain CLI use never
        # pays for (or requires) the mcp package import machinery, and
        # nothing may touch stdout here -- it is the MCP transport.
        from .mcp_server import run_stdio

        run_stdio()
        return 0
    if args.command == "attach":
        # Interactive terminal takeover: bypasses the Result/JSON path (an
        # exec leaves nothing to print) with its own error handling so a
        # usage mistake maps to exit 2.
        try:
            return run_attach(args)
        except UsageError as exc:
            print(f"elate: {exc}", file=sys.stderr)
            return 2
        except ElateError as exc:
            print(f"elate: {exc}", file=sys.stderr)
            return 1
    # Opportunistic TTL sweep (throttled; only sessions started with --ttl):
    # any elate invocation reaps sessions leaked by a crashed agent. The
    # session this command targets is exempt -- it must not vanish between
    # two of its owner's own calls.
    reaped = S.maybe_reap_expired(
        exclude=args.session or getattr(args, "name", None))
    if reaped:
        names = ", ".join(r["name"] for r in reaped)
        print(f"elate: reaped {len(reaped)} session(s) idle past their "
              f"--ttl: {names}", file=sys.stderr)
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
    except EvalTimeout as exc:
        # A subclass of ElateError; handled first so an attached --on-timeout
        # `sample` backtrace is spread into the JSON (and printed for humans).
        payload: dict[str, Any] = {"ok": False, "error": str(exc)}
        if getattr(exc, "sample", None):
            payload["sample"] = exc.sample
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"elate: {exc}", file=sys.stderr)
            sample = getattr(exc, "sample", None)
            if sample and sample.get("backtrace"):
                print(f"--- sample ({sample.get('tool')}) ---", file=sys.stderr)
                print(sample["backtrace"], file=sys.stderr)
        return 1
    except RpcError as exc:
        payload: dict[str, Any] = {"ok": False, "error": str(exc)}
        if exc.backtrace:
            payload["backtrace"] = exc.backtrace
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"elate: elisp error: {exc}", file=sys.stderr)
        return 1
    except ScreenshotError as exc:
        # Spread the machine-readable reason ("locked" / "display_asleep" /
        # "permission" / "window_gone") so a caller can branch without
        # parsing the message.
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc),
                              "reason": exc.reason}, ensure_ascii=False))
        else:
            print(f"elate: {exc}", file=sys.stderr)
        return 1
    except UsageError as exc:
        print(f"elate: {exc}", file=sys.stderr)
        return 2
    except ElateError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"elate: {exc}", file=sys.stderr)
        return 1
    if field is not None:
        # A non-zero code (e.g. an elisp eval error) prints nothing to
        # stdout: `[ "$(elate … --raw)" = x ]` must compare against
        # emptiness, not an error rendering.
        if code != 0:
            print(f"elate: {human}" if human else "elate: failed",
                  file=sys.stderr)
            return code
        try:
            print(_field_text(result, field))
        except UsageError as exc:
            print(f"elate: {exc}", file=sys.stderr)
            return 2
        return code
    # A command-local --format (run) overrides the global --json/--human:
    # 'json' forces JSON, 'human'/'junit'/'tap' force the text in `human`.
    fmt = getattr(args, "format", None)
    if fmt == "json" or (fmt is None and args.json):
        print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    elif human:
        print(human)
    return code


if __name__ == "__main__":
    sys.exit(main())
