# elate CLI reference

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with: uv run python scripts/gen-skill-ref.py
     CI fails when this file drifts from the CLI. -->

Generated from the `elate` argparse tree. Every command also accepts the
global options below. Output is JSON automatically when stdout is not a
terminal (i.e. for programmatic use); `--json` / `--human` force either.

## Global options

- `--version` -- show program's version number and exit
- `--json` -- force machine-readable JSON output
- `--human` -- force the human-readable table, even when piped
- `--field NAME` -- print just this one field of the result, bare (no JSON envelope): strings unquoted, booleans true/false, null, objects/arrays as compact JSON -- so shell can test it with no parser: [ "$(elate --field name info X)" = X ]. On failure nothing goes to stdout (error on stderr, usual exit code); an unknown field is a usage error (exit 2) naming the available ones
- `-s, --session NAME` -- session to operate on
- mutually exclusive: `--json | --human`

## Commands

- [`elate start`](#elate-start)
- [`elate stop`](#elate-stop)
- [`elate interrupt`](#elate-interrupt)
- [`elate debug`](#elate-debug)
- [`elate list`](#elate-list)
- [`elate purge`](#elate-purge)
- [`elate prune`](#elate-prune)
- [`elate info`](#elate-info)
- [`elate path`](#elate-path)
- [`elate keys`](#elate-keys)
- [`elate type`](#elate-type)
- [`elate send-process`](#elate-send-process)
- [`elate mouse`](#elate-mouse)
- [`elate focus`](#elate-focus)
- [`elate send-events`](#elate-send-events)
- [`elate resize`](#elate-resize)
- [`elate attach`](#elate-attach)
- [`elate eval`](#elate-eval)
- [`elate trace`](#elate-trace)
- [`elate buffer`](#elate-buffer)
- [`elate test`](#elate-test)
- [`elate lint`](#elate-lint)
- [`elate profile`](#elate-profile)
- [`elate bench`](#elate-bench)
- [`elate faces-at`](#elate-faces-at)
- [`elate popups`](#elate-popups)
- [`elate messages`](#elate-messages)
- [`elate echo`](#elate-echo)
- [`elate state`](#elate-state)
- [`elate window-info`](#elate-window-info)
- [`elate pointer`](#elate-pointer)
- [`elate dnd`](#elate-dnd)
- [`elate describe`](#elate-describe)
- [`elate mcp`](#elate-mcp)
- [`elate screenshot`](#elate-screenshot)
- [`elate logs`](#elate-logs)
- [`elate stderr`](#elate-stderr)
- [`elate wait`](#elate-wait)
- [`elate run`](#elate-run)
- [`elate export-script`](#elate-export-script)
- [`elate record`](#elate-record)
- [`elate snap`](#elate-snap)
- [`elate matrix`](#elate-matrix)
- [`elate install`](#elate-install)
- [`elate update`](#elate-update)

## elate start

start a new sandboxed session

- `--name NAME` -- session name (default: an auto-generated elate-<hex>; the chosen name is in the result, so later commands can reference it)
- `--replace` -- if a session of this name is already running, stop and recreate it (dead/stopped sessions of that name are always replaced)
- `--ui {tty,gui}` (default: tty) -- session UI: tty (tmux-hosted terminal Emacs, default) or gui (windowed Emacs; PNG screenshots)
- `--headless` -- GUI only: run under a private Xvfb (Linux/CI)
- `--emacs PATH` -- emacs binary to use
- `--config {minimal,bare,init-file,clean-install}` (default: minimal) -- sandbox config mode (default: minimal; clean-install installs the --load package(s) for real via package-install-file)
- `--init-file PATH` -- user init file (implies --config init-file)
- `--load PATH` (repeatable) -- elisp file or directory to put on load-path (repeatable); with --config clean-install: the package to install (.el file, tar, or directory)
- `--require FEATURE` (repeatable) -- feature to (require 'FEATURE) at startup, after --load has wired load-path (repeatable); the shorthand for the usual follow-up --eval "(require 'FEATURE)"
- `--eval FORM` (repeatable) -- elisp form to evaluate at startup, before emacs-startup-hook (repeatable)
- `--eval-file PATH` (repeatable) -- elisp file to load at startup, before emacs-startup-hook (repeatable); like a reusable --eval, with no load-path side effects
- `--profile NAME` (repeatable) -- named startup snippet from $XDG_CONFIG_HOME/elate/profiles/NAME.el (or a path to a .el file); loaded like --eval-file (repeatable)
- `--home-seed DIR` -- copy this fixture tree into the sandbox's fake $HOME before launch (rc files in place before any subprocess spawns; keeps sandbox isolation)
- `--env KEY=VALUE` (repeatable) -- set an environment variable for the Emacs process and the subprocesses it spawns (repeatable); cannot override the sandbox's HOME/XDG_* isolation vars
- `--size COLSxROWS` (default: 120x36)
- `--owner NAME` -- tag the session with an owner (e.g. an agent id); list/stop/purge can then select by --owner, so concurrent agents manage only their own sessions
- `--ttl DUR` -- idle time-to-live (e.g. 30m, 2h; bare number = seconds, minimum 30s): once the session has seen no commands for this long it is stopped AND purged by an opportunistic sweep any later elate command runs -- so sessions leaked by a crashed agent clean themselves up instead of accumulating

## elate stop

stop a session (or --all / a filter)

- `[name]` -- session name (or use -s NAME)
- `--all` -- stop every running session (instead of a name)
- `--glob PATTERN` -- stop running sessions whose name matches this glob (e.g. 'rx-*') -- a bulk selector like --all
- `--name-prefix PREFIX` -- stop running sessions whose name starts with PREFIX -- a bulk selector like --all
- `--owner NAME` -- stop running sessions started with --owner NAME; combines with --glob/--name-prefix

## elate interrupt

unblock a wedged session (raw C-g / signal) without stopping it

Poke a busy-but-alive session without killing it. TTY: send raw C-g over tmux (works even when the semantic channel is blocked). GUI (no raw channel): signal Emacs -- --signal auto (default) breaks the running code into the Lisp debugger with SIGUSR2, then unwinds it back to top level once the semantic channel answers (the C-g-like recovery; result carries recovered/depth_after). --signal usr2 only enters the debugger, so `debug show` can reveal where it was stuck. --signal int sends SIGINT, which TERMINATES a GUI-only Emacs (no tty frame = quit request, not C-g) -- a deliberate shutdown, never a recovery.

- `[name]` -- session name (or use -s NAME)
- `--signal {auto,usr2,int}` (default: auto) -- GUI only: auto (SIGUSR2 + unwind to top level, default), usr2 (enter the Lisp debugger and stay), or int (SIGINT: terminates a GUI-only Emacs); ignored for TTY

## elate debug

inspect or unwind the Lisp debugger / a recursive edit

When code under test signals an error and *Backtrace* pops up, the session sits in a recursive edit: it looks idle, but every key and eval lands inside the debugger (`wait idle` refuses to succeed there and points here; `state` reports recursion-depth / in-debugger). 'show' returns the backtrace text and depth without touching anything; 'abort' throws back to top level -- ending the debugger and restoring the window layout it saved on entry -- with the session and its state intact. This complements `interrupt`, which is for a busy Emacs that is NOT answering the semantic channel.

- `[{show,abort}]` (default: show) -- show (default): backtrace + depth; abort: throw to top level (a no-op at recursion depth 0)
- `[name]` -- session name (or use -s NAME; requires an explicit action first)
- `--timeout SECS` (default: 15)

## elate list

list known sessions

- `[name]` -- only this session (or use -s NAME)
- `--status {running,stopped,all}` (default: all) -- filter by liveness: running, stopped (stopped/dead/corrupt), or all (default)
- `--owner NAME` -- only sessions started with --owner NAME
- `--older-than DUR` -- only show sessions inert at least this long (e.g. 30s, 15m, 2h, 1d; bare number = seconds) -- running sessions are excluded; pairs with `purge --stopped-older-than`

## elate purge

delete the sandboxes of stopped/dead sessions

Delete the sandbox directories (transcripts included) of sessions that are no longer running. Stopped sandboxes are inert but accumulate forever otherwise; purge is the supported cleanup. A running session is never purged: naming one is an error, and --all skips and reports it. Leftover processes of dead sessions are cleaned up before their files go.

- `[NAME]` (repeatable) -- session to purge (repeatable)
- `--all` -- purge every session that is not running
- `--stopped-older-than DUR` -- only purge sessions inert at least this long (e.g. 30s, 15m, 2h, 1d; bare number = seconds) -- keeps just-stopped sandboxes during heavy runs
- `--glob PATTERN` -- purge sessions whose name matches this glob (e.g. 'run-*') -- a bulk selector like --all; running matches are skipped
- `--name-prefix PREFIX` -- purge sessions whose name starts with PREFIX (e.g. 'run-') -- a bulk selector like --all
- `--owner NAME` -- purge sessions started with --owner NAME -- a bulk selector like --all; combines with the other filters

## elate prune

alias for `purge` (delete stopped/dead sandboxes)

Alias for `purge`: delete the sandbox directories of sessions that are no longer running. `purge` is the canonical spelling; `prune` exists for discoverability.

- `[NAME]` (repeatable) -- session to prune (repeatable)
- `--all` -- prune every session that is not running
- `--stopped-older-than DUR` -- only prune sessions inert at least this long (e.g. 30s, 15m, 2h, 1d; bare number = seconds)
- `--glob PATTERN` -- prune sessions whose name matches this glob (e.g. 'run-*') -- a bulk selector like --all
- `--name-prefix PREFIX` -- prune sessions whose name starts with PREFIX (e.g. 'run-') -- a bulk selector like --all
- `--owner NAME` -- prune sessions started with --owner NAME -- a bulk selector like --all

## elate info

show session details

- `[name]` -- session name (or use -s NAME)

## elate path

print a session's private scratch directory (or another sandbox path)

Print one on-disk path of the session, the scratch directory by default: a per-session private directory for setup files and artifacts, safe from concurrent agents (in-session code sees it as $ELATE_SCRATCH). Prints the bare path, so it substitutes cleanly: cp setup.el "$(elate -s NAME path)"/. Works for stopped sessions too.

- `[name]` -- session name (or use -s NAME)
- `--kind {scratch,dir,home,log}` (default: scratch) -- which path: scratch (default; created on demand), dir (the sandbox root), home (the fake $HOME), log (Emacs stderr/GUI logs)

## elate keys

send keys (Emacs kbd notation)

Send a key sequence. Semantic keys run through the command loop and obey the focused buffer's keymaps, so a buffer that intercepts keys (a terminal emulator in char mode, special-mode buffers) can swallow one and your intended command never runs. The result's `command` field is what the sequence resolves to in the focused buffer (null for an unbound key or a multi-command sequence); `eval` a command directly to run it regardless of bindings.

- `keys` -- key sequence in Emacs kbd notation, e.g. 'C-x C-f' or 'M-x foo RET'
- `--semantic` -- deliver via execute-kbd-macro (default)
- `--raw` -- deliver as raw terminal bytes via tmux
- `--events` -- semantic, but queue on unread-command-events (non-blocking; use for sequences that open a prompt)
- `--no-abort-on-bell` -- deliver via unread-command-events so a command that rings the bell (e.g. evil insert off the prompt row) beeps instead of aborting the whole sequence; asynchronous -- follow with a wait. Semantic keys run through the command loop and obey active keymaps (evil state etc.)
- `--timeout TIMEOUT` (default: 15)
- mutually exclusive: `--semantic | --raw`

## elate type

type literal text (raw channel on tty; queued events on gui)

Type literal text as if at the keyboard. TTY: raw terminal bytes via tmux. GUI: queued key events through the command loop -- needs a responsive Emacs and is capped at 10000 characters (for bulk text, eval an insert instead). Text starting with a dash needs '--' first: elate -s N type -- '-foo'.

- `text`

## elate send-process

send raw input to a buffer's subprocess (comint/REPL/shell)

Write bytes straight to the process behind a buffer (`process-send-string`), bypassing the command loop -- for driving shells/REPLs/terminals. Unlike keys/type (which talk to Emacs), this talks to the subprocess: send ^C to interrupt a job, seed shell history, feed a REPL. Targeting rule: the buffer must have exactly one live process -- none or several is an error naming the candidates (never a silent pick); disambiguate with --process. The result echoes the process's name and command line so a wrong target is visible.

- `[text]` -- literal text to send
- `--char KBD` -- send an Emacs kbd string, e.g. 'C-c' (^C / SIGINT), 'RET' (newline), 'TAB'
- `--file PATH` -- send the contents of PATH (read inside Emacs; for payloads past the argv size limit)
- `--buffer NAME` -- buffer whose process to target (default: current); must have exactly one live process unless --process picks one
- `--process NAME` -- target this process by name (`get-process`), for buffers with several processes; with --buffer the process must belong to that buffer
- mutually exclusive: `TEXT | --char | --file`

## elate mouse

synthesize a mouse interaction (semantic; works for tty and gui)

- `{click,double,drag,wheel}`
- `--button {1,2,3}` (default: 1)
- `--buffer NAME` -- target the window showing this buffer (default: selected window)
- `--pos N` -- buffer position
- `--line L` -- buffer line (1-based)
- `--col C` -- column (with --line, or offset into the mode line)
- `--mode-line` -- click the window's mode line instead of buffer text
- `--to-pos N` -- drag: end position
- `--to-line L` -- drag: end line
- `--to-col C` -- drag: end column
- `--direction {up,down}` (default: down) -- wheel: scroll direction
- `--count N` (default: 1) -- wheel: number of notches
- `--events` -- queue on unread-command-events instead of the synchronous default (use when the triggered command itself reads input)
- `--timeout SECS` (default: 15)

## elate focus

inject a window-system focus event (focus-in/out; semantic, tty and gui)

- `{in,out}` -- 'in' (focus-in) or 'out' (focus-out)
- `--frame NAME` -- target the frame with this name (default: selected)
- `--set-focus-state` -- also make (frame-focus-state) report the injected state -- a non-native shim, since injected events cannot move the real C-owned focus state (they do fire after-focus-change-function regardless)
- `--timeout SECS` (default: 15)

## elate send-events

inject an ordered stream of focus/mouse/key events (drains through the command loop in order)

- `EVENT...` (repeatable) -- event tokens in order: focus-in, focus-out, down-mouse-N, mouse-N, up-mouse-N, double-mouse-N, wheel-up, wheel-down (N=1..3, each with optional @LINE,COL [1-based line, 0-based col] or #POS), or key:KBD (e.g. key:RET). A focus event only fires at the head of a command-loop turn, so any focus events are delivered in separate drained batches automatically -- making any ordering faithful, including a mouse-down before a focus-in
- `--buffer NAME` -- target the window showing this buffer for mouse events (default: selected window)
- `--frame NAME` -- target frame for focus events (default: selected)
- `--set-focus-state` -- also make (frame-focus-state) report injected focus (non-native shim; see 'elate focus')
- `--timeout SECS` (default: 15)

## elate resize

resize a live session (tmux window or GUI frame)

- `COLSxROWS`

## elate attach

attach a human terminal to a live TTY session (hand off / take over)

Drop into the session's tmux client so a human can drive Emacs directly, then detach with C-b d to hand back -- the session keeps running (do NOT use C-x C-c, which kills Emacs). TTY sessions only: a GUI session's Emacs window is already on screen (use screenshot). Requires a real terminal; this replaces the elate process with `tmux attach`. Your terminal size temporarily drives the frame while attached.

- `[name]` -- session name (or use -s NAME)
- `--read-only, -r` -- attach read-only: watch without sending input (detach still works with C-b d)
- `--print-command` -- print the tmux command that would run, without attaching (scripting/tests)

## elate eval

evaluate an elisp form

- `[form]` -- elisp source: one or more forms, evaluated as (progn ...); omit when using --file
- `--file PATH` -- read the elisp source from PATH instead of the FORM argument ('-' reads stdin). Sidesteps shell quoting entirely, so forms containing ' or #' run verbatim -- use this to test snippets exactly as written. One or more forms, evaluated as (progn ...) like the positional FORM
- `--buffer NAME` -- evaluate in this buffer (default: the selected window's buffer, so current-buffer/point/line see what is on screen, not an arbitrary buffer)
- `--timeout SECS` (default: 15)
- `--backtrace` -- on error, also return structured backtrace frames (each frame's function + printed args), not just the rendered backtrace string
- `--on-timeout {none,sample}` (default: none) -- on timeout with Emacs still busy: 'sample' captures a thread backtrace of the wedged Emacs (macOS `sample`; Linux eu-stack/gdb) and attaches it to the error; 'none' (default) does not
- `--json-result` -- serialize the elisp value to real JSON inside the session, so `value` is a queryable object (jq .value.mode), not a printed sexp string. Plists/alists of atoms map cleanly (nil -> null, t -> true, symbols -> names); a value with no faithful JSON shape (buffers, markers, circular structures) falls back to the printed string -- the result's value-encoding says which came back ('json' or 'printed'), check it
- `--raw` -- print just the value, no JSON envelope -- shell tests it directly: [ "$(elate -s N eval --raw 'major-mode')" = fundamental-mode ]. On an elisp error nothing goes to stdout (error on stderr, exit 1). Shorthand for the global --field value; combine with --json-result for the bare JSON value

## elate trace

trace elisp functions (log calls/args/returns), then read the accumulated log

Wrap trace-function-background around one or more functions so each call records its args and return value. Tracing is invisible: the *trace-output* buffer is never displayed, so the window layout under test stays untouched. 'on FUNC...' starts tracing; drive the session (keys/eval/...); 'read' returns and clears the log so each read sees only new calls; 'off [FUNC...]' untraces the named functions (or all). Drives Emacs internals you cannot see on screen -- why an advice fires twice, what args a hook receives. 'read' JSON carries both the raw *trace-output* text ('output') and structured per-call records: 'records' is a list of {fn, depth, args: [printed...], ret, error} in call-completion order (a nested call precedes its caller; depth 1 = outermost), each value printed and clipped -- assert on records, not on regexed text.

- `{on,off,read}`
- `[FUNC]` (repeatable) -- function name(s): required for 'on', optional for 'off' (default: untrace all), unused for 'read'
- `--keep` -- trace read: keep the log instead of clearing it
- `--timeout SECS` (default: 15)

## elate buffer

print buffer contents

- `[name]`
- `--from L`
- `--to L`
- `--props` -- also dump run-length-encoded face/text-property runs and overlays for the range (verify font-lock, themes, overlay-based UI)

## elate test

run ERT tests interactively (structured per-test results)

- `[selector]` (default: t) -- ERT selector: t (default, all tests), a test name, a name regexp, '(tag NAME)', '(not "slow")', :failed, ... Tests must already be loaded (--load-file, start --load, or eval)
- `--load-file PATH` (repeatable) -- elisp test file to load (by path) before running (repeatable)
- `--timeout SECS` (default: 60) -- in-Emacs timeout for the whole run (default 60); a timed-out run returns partial results

## elate lint

byte-compile + checkdoc elisp files inside the session (WARNING: executes the files' compile-time code)

Byte-compile + checkdoc each FILE inside the live session, against its load-path. WARNING: byte-compilation EXECUTES compile-time code (eval-when-compile, macro expansion, top-level requires) in the session -- inherent to in-session linting. Lint untrusted code in a throwaway session.

- `FILE...` (repeatable)
- `--timeout SECS` (default: 60) -- per-file in-Emacs timeout (default 60); a lint whose compile-time code hangs is interrupted and reported as a clean error
- `--package-lint` -- ALSO run package-lint (additive; items tagged tool=package-lint). package-lint is installed into the sandbox elpa/ on demand. Without --archive-dir it refreshes the standard archives over the NETWORK (non-deterministic); a setup failure aborts with a clear error and the session survives
- `--archive-dir DIR` -- for --package-lint: a local directory holding an archive-contents index, used directly as a package archive (a plain path, not a file:// URL) -- offline and REPRODUCIBLE (the recommended/CI path; the answer to package-lint's archive non-determinism)

## elate profile

drive Emacs's native profiler (start/stop/report, or one-shot 'run FORM')

Drive Emacs's native sampling profiler. 'start' begins sampling (--cpu default, --mem allocations, --both), resetting earlier logs and running a GC first (pre-existing garbage is never charged to the window); 'stop' ends it; 'report' renders the collected samples as top functions + a depth-limited calltree (works while profiling and after stop; --cpu/--mem select which collected section to show). 'profile run FORM' does start -> eval FORM (normal eval discipline incl. timeout + backtraces) -> stop -> report in one call. Profiles depend on session history (everything the session ran is in the samples) -- profile in a fresh throwaway session for authoritative numbers, like lint.

- `{start,stop,report,run}`
- `[form]` -- for 'run': the elisp form to profile
- `--cpu` -- sample CPU time (SIGPROF; the default)
- `--mem` -- sample memory allocations
- `--both` -- CPU and memory together
- `--depth N` -- report/run: calltree depth limit (default 6, max 20)
- `--timeout SECS` -- run: eval timeout (default 15)
- mutually exclusive: `--cpu | --mem | --both`

## elate bench

benchmark an elisp form (benchmark-run-compiled wrapper)

Time FORM over --repetitions calls via Emacs's benchmark-call, byte-compiling the form first (interpreted fallback when compilation fails -- 'compiled' in the result says which path ran). Reports elapsed/mean seconds, GC runs + GC time, and memory-use-counts deltas (allocation context). Results depend on session history (loaded code, GC state) -- benchmark in a fresh throwaway session for authoritative numbers, like lint.

- `form`
- `-n, --repetitions N` (default: 1) -- number of repetitions (default 1)
- `--timeout SECS` (default: 60) -- in-Emacs timeout for the whole run (default 60)

## elate faces-at

faces, text properties, and overlays at a buffer position

- `[LINE:COL]` -- 1-based line, 0-based column (or use --pos)
- `--pos N` -- address by absolute buffer position instead of LINE:COL (handy from elisp, which holds positions)
- `--run K` (default: 1) -- dump K consecutive cells from the position in one call (default 1) -- e.g. compare a typed cell against the suggestion cell next to it
- `--buffer NAME` -- buffer to inspect (default: current)

## elate popups

capture visible popups as text (which-key, transient, hydra, completion previews, child frames)

(no arguments)

## elate messages

new *Messages* output since last call

(no arguments)

## elate echo

current echo area / minibuffer line

(no arguments)

## elate state

one-call scene snapshot (layout, prompt, point, modes, messages tail)

Full-snapshot shape: current-buffer facts (buffer, file, point, line, column, major-mode, minor-modes, region, narrowed, modified), echo, minibuffer (prompt/input/completions or null), input-pending, last-command, idle (secs or null), recursion-depth + in-debugger (non-zero/true = a recursive edit or the Lisp debugger is eating input -- see `debug`), popups, messages-tail, and 'windows': the window-tree as nested objects, NOT a flat list -- an inner node is {split: 'vertical'|'horizontal', children: [...]}, a leaf is one window {buffer, selected, width, height, line, column, start-line, end-line, mode-line, text, ...}; recurse until nodes have no 'children'. Sizes are character cells; for pixel geometry and X11 window ids see `window-info`.

- `--since TOKEN` -- return only what changed since the TOKEN from a prior state call (new/killed/modified buffers, point/selection movement, new *Messages* lines, minibuffer change) -- much cheaper than a full snapshot, and 'changed':false means your last action did nothing observable. Every state result carries a fresh 'token'; an unknown/stale one degrades to a full snapshot.

## elate window-info

window-system ids and pixel geometry per frame (for external X11 clients: drops, warps, screenshots)

Per frame (selected first): name, selected, graphic, window-system (x/ns/pgtk/... or null), units ('pixels' on GUI frames, 'chars' on TTY), outer-window-id + window-id as INTEGERS (null off X11), outer-edges + native-edges [left, top, right, bottom], char-width + char-height (the cell size for line/col-to-pixel math), and windows: [{buffer, selected, edges, body-edges}] with root-absolute pixel edges on GUI frames. Complements `state`, which has buffer content and character-cell layout but no window-system numbers.

- `--timeout SECS` (default: 15)

## elate pointer

move (warp) or read (query) the real window-system pointer (gui only)

Drives the REAL pointer, not a synthesized event: code dispatched below the command loop (drag-and-drop ClientMessages -- XdndDrop carries no coordinates, Emacs reads the live pointer position) only sees this. For clicking/dragging through the command loop use `mouse` instead. 'warp' targets root-absolute --x/--y or a buffer location (--buffer/--pos/--line/--col; buffer targets land mid-glyph) and replies with the post-warp position; 'query' reports {x, y, frame, buffer, pos, line, col, area} with nulls when the pointer is not over a session frame.

- `{warp,query}`
- `--x PX` -- warp: root-absolute pixel x (pairs with --y)
- `--y PY` -- warp: root-absolute pixel y (pairs with --x)
- `--buffer NAME` -- warp: buffer whose window to target (default: selected window; searched across all frames)
- `--pos N` -- warp: absolute buffer position
- `--line L` -- warp: buffer line (1-based)
- `--col C` -- warp: column on --line (0-based)
- `--timeout SECS` (default: 15)

## elate dnd

synthesize a real XDND drag-and-drop onto a buffer (X11 gui only; needs the elate[dnd] extra)

An external X client speaks the full XDND protocol at the Emacs frame, so the drop exercises the C-level event dispatch, special-event-map, and x-dnd.el -- the layers `mouse` (command-loop synthesis) bypasses. The pointer is warped to the target first (XdndDrop carries no coordinates; Emacs reads the live pointer). X11 sessions only (--ui gui --headless on Linux is the CI-friendly path); needs python-xlib: pip install 'elate[dnd]'. A drop the target refuses returns status 'rejected' with exit 0 -- assert via --field status. A drop handler that errors is reported, not hidden: Emacs catches handler errors and answers XdndFinished with its success bit clear -- 'finished-success': false (error text in `messages`); results also carry 'in-debugger' for handlers that park Emacs in the Lisp debugger (then a missing XdndFinished is returned as a normal result -- inspect with `debug show`). After a drop, `wait stable --buffer B` is the settle primitive.

- `{drop}`
- `--uris URI[,URI...]` (required) -- comma-separated URIs to drop (text/uri-list, so ASCII only -- percent-encode; a literal comma in a URI must be %%2C); file URIs for local files: file:///abs/path
- `--buffer NAME` -- buffer whose window to drop on (default: selected window)
- `--pos N` -- absolute buffer position to drop at
- `--line L` -- buffer line to drop at (1-based)
- `--col C` -- column on --line (0-based)
- `--x PX` -- root-absolute pixel x (pairs with --y; excludes buffer targeting)
- `--y PY` -- root-absolute pixel y (pairs with --x)
- `--action {copy,move}` (default: copy) -- XDND action to propose (default copy)
- `--hover` -- Enter + Position only -- assert drag feedback without dropping; dwells --hover-ms, then leaves
- `--hover-ms MS` (default: 500) -- hover dwell before XdndLeave (default 500, max 10000)
- `--timeout SECS` (default: 15)

## elate describe

structured docs/binding lookup

- `{key,function,variable,mode}`
- `name` -- kbd string for 'key', symbol name otherwise

## elate mcp

serve all of the above as MCP tools over stdio (for AI harnesses)

(no arguments)

## elate screenshot

capture the screen: text for tty sessions, PNG for gui sessions

- `-o, --output FILE` -- output file (tty default: stdout; gui default: ./elate-<session>-<time>.png)
- `--ansi` -- tty only: include ANSI color escapes

## elate logs

tail the driven Emacs's stderr/stdout log

Show the tail of the Emacs process log -- module panics, GC/native-comp warnings, and the fatal-signal line on a crash. TTY sessions capture stderr to a file (off the pane, so screenshots stay clean); GUI sessions log stdout+stderr. Works on dead/stopped sessions too.

- `[name]` -- session name (or use -s NAME)
- `-n, --lines N` (default: 40) -- number of trailing lines to show (default 40)

## elate stderr

Show the tail of the Emacs process log -- module panics, GC/native-comp warnings, and the fatal-signal line on a crash. TTY sessions capture stderr to a file (off the pane, so screenshots stay clean); GUI sessions log stdout+stderr. Works on dead/stopped sessions too.

- `[name]` -- session name (or use -s NAME)
- `-n, --lines N` (default: 40) -- number of trailing lines to show (default 40)

## elate wait

wait for a condition (exit 3 on timeout)

Block until a condition holds. `wait idle` fails immediately (rather than timing out) when Emacs is parked in the Lisp debugger's recursive edit -- that session will never be idle; recover with `debug abort`.

- `{idle,text,prompt,stable,until,dead}`
- `[args]` (repeatable) -- idle: [MIN_IDLE_SECS]; text: REGEXP (Python regex syntax, not elisp); until: an elisp predicate form, polled until non-nil (an elisp error fails the wait -- wrap in ignore-errors if an error means "not yet"); prompt/stable/dead: none
- `--buffer BUFFER` -- buffer to search (wait text), watch (wait stable; may not exist yet), or evaluate the predicate in (wait until)
- `--quiet-ms MS` (default: 300) -- wait stable: settle threshold -- the buffer must be unchanged for this many ms (default 300)
- `--timeout SECS` (default: 10)

## elate run

run a scenario script: fresh session, steps, assertions, exit 0/1 (CI)

Execute a JSON scenario script: create a fresh sandboxed session from the script's "session" config (or target an existing one with -s NAME, in which case that config is ignored and nothing is torn down), run the steps in order, evaluate the assertions, then stop the fresh session. Exits 0 when every step and assertion passed, 1 otherwise; a failed step embeds a state snapshot. Fresh sessions are the default deliberately: lint executes compile-time code and lint/test results depend on session history, so only a throwaway session gives reproducible verdicts.

- `script` -- path to the scenario file (JSON)
- `--keep` -- keep the fresh session running afterwards (named from the scenario's "name", or --name)
- `--name NAME` -- name for a kept session (default with --keep: the scenario's "name"); a running name collision is an error
- `--no-purge` -- on success, keep the throwaway sandbox on disk (stopped) instead of removing it; failed runs are always kept for post-mortem
- `--keep-on-failure` -- keep the fresh session running when the run fails (inspect it with state/screenshot, then stop it)
- `--keep-going` -- run every step even after a failure instead of stopping at the first (a failed run still exits non-zero); use for a matrix that must report every check. Per-step "optional": true never gates.
- `--emacs PATH` -- override the script's emacs binary (CI matrix)
- `--update-snapshots` -- write/overwrite golden artifacts for snapshot assertions instead of comparing them; the run still executes every step (review the diff before committing)
- `--snapshot-dir DIR` -- base directory for golden snapshots (default: <scenario-dir>/__snapshots__)
- `--set NAME=VALUE` (repeatable) -- bind a {{NAME}} template variable in the scenario (repeatable); overrides a scenario "params" default, so one scenario can drive many configs
- `--set-file NAME=PATH` (repeatable) -- bind a {{NAME}} template variable to PATH's contents verbatim (one trailing newline stripped) -- multi-line/quote-heavy values without shell quoting; binding a NAME in both --set and --set-file is an error
- `--variant NAME` -- select one entry of the scenario's "variants" block: its bindings overlay the "params" defaults as a set ({{variant}} binds the name; --set still wins per variable). `matrix` runs every variant.
- `--format {json,human,junit,tap}` -- output format: 'human' (default when not piped) a summary + per-group verdicts, 'json' the full result, 'junit' a JUnit XML testsuite (one testcase per group / ungrouped step), 'tap' TAP version 13. Overrides the global --json/--human for this run.

## elate export-script

convert a session's transcript into a best-effort scenario script

Turn the session's JSONL transcript into a scenario file for `elate run`: inputs become steps, observations become skipped assertion stubs ("skip": true). A starting point for editing, not a faithful recording. Works on stopped sessions too.

- `-o, --output FILE` -- write the script here (default: stdout)
- `--clean` -- prune transient temp-path references so the export replays elsewhere: drop session load/eval entries under a temp root, and mark an eval step that references one "skip" with a comment

## elate record

asciinema (.cast v2) recording of a TTY session

Record the session's terminal output as an asciicast v2 file (play it with `asciinema play`, render a GIF with `agg`). TTY sessions only -- for GUI sessions use 'snap'. Capture rides tmux pipe-pane; the first event replays the current screen so playback starts from the correct picture.

- `{start,stop,status}`
- `-o, --output FILE.cast` -- start: output file (default: <session>/log/<name>-<time>.cast)

## elate snap

periodic screenshot series (PNG for gui, text for tty)

Capture a frame every INTERVAL seconds into frame-NNNN.png/.txt plus a manifest.json with per-frame timestamps -- demo/GIF source material. Runs as a detached snapper process that only ever reads the session: a dying snapper cannot harm the session, and 'snap stop' is idempotent.

- `{start,stop,status}`
- `--interval SECS` (default: 0.5) -- start: seconds between frames (default 0.5)
- `-o, --output DIR` -- start: frame directory (default: <session>/snap-<time>/)
- `--ansi` -- tty only: ANSI-colored text frames

## elate matrix

run a scenario across Emacs binaries, variants, and parameter axes

Run SCRIPT once per combination of the Emacs binary axis, the scenario's "variants" (every declared variant by default; a variant is a NAMED set of co-varying {{var}} bindings), and any --param axes -- their Cartesian product -- each in a fresh session, and aggregate the verdicts into one grid. Each --param NAME=v1,v2 binds the scenario's {{NAME}} template per combo, so one file drives many shells/configs x Emacs versions. Exits 0 only when every combo passed (xfail honored).

- `--emacs PATHS` (repeatable) -- emacs binary, or comma-separated list (repeatable)
- `--emacs-glob GLOB` -- glob matching emacs binaries, e.g. '/opt/emacs-*/bin/emacs'
- `--param NAME=V1,V2` (repeatable) -- a parameter axis: bind {{NAME}} to each comma-separated value in turn (repeatable; every axis is crossed with the Emacs axis)
- `--variant NAMES` (repeatable) -- filter the variant axis to these "variants" entries (comma-separated, repeatable); default: every declared variant
- `--set-file NAME=PATH` (repeatable) -- bind a {{NAME}} template variable to PATH's contents verbatim (one trailing newline stripped), constant across every combo -- not an axis
- `--format {json,human}` -- output format: 'human' (default) the grid, 'json' the structured results. Overrides --json/--human.
- `--update-snapshots` -- write/overwrite golden snapshots (per Emacs version and param combo) instead of comparing
- `--snapshot-dir DIR` -- base directory for golden snapshots (default: <scenario-dir>/__snapshots__)
- `script` -- path to the scenario file (JSON)

## elate install

install the elate skill into AI coding harnesses

Copy elate's Agent Skill (SKILL.md) into one or more AI coding harnesses so they learn to drive the elate CLI. Targets: claude, codex, opencode, pi, antigravity (or 'all'). With no target, installs for every harness detected on this machine. By default the skill lands in the current project's skills dirs (e.g. .claude/skills); --global installs user-wide instead. The skill is the CLI-centric integration that works everywhere; --mcp additionally registers the optional MCP server where it is supported.

- `[HARNESS]` (repeatable) -- harness(es) to install for: claude codex opencode pi antigravity or 'all' (default: auto-detect)
- `--global` -- install into the user-global skills dirs (e.g. ~/.claude/skills) instead of the current project's
- `--mcp` -- also wire the MCP server: `mcp add` where the harness has that CLI (Claude Code, Codex), a paste-ready snippet otherwise (opencode, Antigravity); pi has no MCP
- `--dry-run` -- show what would be installed without writing anything

## elate update

upgrade elate and refresh installed skill copies

Upgrade the elate package via whatever installed it (Homebrew, uvx, uv tool, pipx, pip), then rerun `elate install` for every skill copy found on this machine so the copies match the new CLI. Prints the plan and asks before running anything; already-running sessions keep the old in-Emacs agent until restarted.

- `--yes` -- run without the interactive confirmation (required when stdin is not a terminal)
- `--dry-run` -- print the plan without executing anything
