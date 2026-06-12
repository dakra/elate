# elate CLI reference

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with: uv run python scripts/gen-skill-ref.py
     CI fails when this file drifts from the CLI. -->

Generated from the `elate` argparse tree. Every command also accepts the
global options below; `--json` makes the output machine-readable and is
the right default for programmatic use.

## Global options

- `--version` -- show program's version number and exit
- `--json` -- machine-readable JSON output
- `-s, --session NAME` -- session to operate on

## Commands

- [`elate start`](#elate-start)
- [`elate stop`](#elate-stop)
- [`elate list`](#elate-list)
- [`elate purge`](#elate-purge)
- [`elate info`](#elate-info)
- [`elate keys`](#elate-keys)
- [`elate type`](#elate-type)
- [`elate mouse`](#elate-mouse)
- [`elate resize`](#elate-resize)
- [`elate eval`](#elate-eval)
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
- [`elate describe`](#elate-describe)
- [`elate mcp`](#elate-mcp)
- [`elate screenshot`](#elate-screenshot)
- [`elate wait`](#elate-wait)
- [`elate run`](#elate-run)
- [`elate export-script`](#elate-export-script)
- [`elate record`](#elate-record)
- [`elate snap`](#elate-snap)
- [`elate matrix`](#elate-matrix)

## elate start

start a new sandboxed session

- `--name NAME` (required)
- `--ui {tty,gui}` (default: tty) -- session UI: tty (tmux-hosted terminal Emacs, default) or gui (windowed Emacs; PNG screenshots)
- `--headless` -- GUI only: run under a private Xvfb (Linux/CI)
- `--emacs PATH` -- emacs binary to use
- `--config {minimal,bare,init-file,clean-install}` (default: minimal) -- sandbox config mode (default: minimal; clean-install installs the --load package(s) for real via package-install-file)
- `--init-file PATH` -- user init file (implies --config init-file)
- `--load PATH` (repeatable) -- elisp file or directory to put on load-path (repeatable); with --config clean-install: the package to install (.el file, tar, or directory)
- `--eval FORM` (repeatable) -- elisp form to evaluate at startup (repeatable)
- `--size COLSxROWS` (default: 120x36)

## elate stop

stop a session

- `[name]` -- session name (or use -s NAME)

## elate list

list known sessions

(no arguments)

## elate purge

delete the sandboxes of stopped/dead sessions

Delete the sandbox directories (transcripts included) of sessions that are no longer running. Stopped sandboxes are inert but accumulate forever otherwise; purge is the supported cleanup. A running session is never purged: naming one is an error, and --all skips and reports it. Leftover processes of dead sessions are cleaned up before their files go.

- `[NAME]` (repeatable) -- session to purge (repeatable)
- `--all` -- purge every session that is not running

## elate info

show session details

- `[name]` -- session name (or use -s NAME)

## elate keys

send keys (Emacs kbd notation)

- `keys` -- key sequence in Emacs kbd notation, e.g. 'C-x C-f' or 'M-x foo RET'
- `--semantic` -- deliver via execute-kbd-macro (default)
- `--raw` -- deliver as raw terminal bytes via tmux
- `--events` -- semantic, but queue on unread-command-events (non-blocking; use for sequences that open a prompt)
- `--timeout TIMEOUT` (default: 15)
- mutually exclusive: `--semantic | --raw`

## elate type

type literal text (raw channel on tty; queued events on gui)

Type literal text as if at the keyboard. TTY: raw terminal bytes via tmux. GUI: queued key events through the command loop -- needs a responsive Emacs and is capped at 10000 characters (for bulk text, eval an insert instead). Text starting with a dash needs '--' first: elate -s N type -- '-foo'.

- `text`

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

## elate resize

resize a live session (tmux window or GUI frame)

- `COLSxROWS`

## elate eval

evaluate an elisp form

- `form`
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

## elate profile

drive Emacs's native profiler (start/stop/report, or one-shot 'run FORM')

Drive Emacs's native sampling profiler. 'start' begins sampling (--cpu default, --mem allocations, --both), resetting earlier logs; 'stop' ends it; 'report' renders the collected samples as top functions + a depth-limited calltree (works while profiling and after stop; --cpu/--mem select which collected section to show). 'profile run FORM' does start -> eval FORM (normal eval discipline incl. timeout + backtraces) -> stop -> report in one call. Profiles depend on session history (everything the session ran is in the samples) -- profile in a fresh throwaway session for authoritative numbers, like lint.

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

- `LINE:COL` -- 1-based line, 0-based column
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

(no arguments)

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

## elate wait

wait for a condition (exit 3 on timeout)

- `{idle,text,prompt}`
- `[args]` (repeatable) -- idle: [MIN_IDLE_SECS]; text: REGEXP (Python regex syntax, not elisp); prompt: none
- `--buffer BUFFER` -- buffer to search (wait text); may not exist yet
- `--timeout SECS` (default: 10)

## elate run

run a scenario script: fresh session, steps, assertions, exit 0/1 (CI)

Execute a JSON scenario script: create a fresh sandboxed session from the script's "session" config (or target an existing one with -s NAME, in which case that config is ignored and nothing is torn down), run the steps in order, evaluate the assertions, then stop the fresh session. Exits 0 when every step and assertion passed, 1 otherwise; a failed step embeds a state snapshot. Fresh sessions are the default deliberately: lint executes compile-time code and lint/test results depend on session history, so only a throwaway session gives reproducible verdicts.

- `script` -- path to the scenario file (JSON)
- `--keep` -- keep the fresh session running afterwards
- `--keep-on-failure` -- keep the fresh session running when the run fails (inspect it with state/screenshot, then stop it)
- `--emacs PATH` -- override the script's emacs binary (CI matrix)

## elate export-script

convert a session's transcript into a best-effort scenario script

Turn the session's JSONL transcript into a scenario file for `elate run`: inputs become steps, observations become skipped assertion stubs ("skip": true). A starting point for editing, not a faithful recording. Works on stopped sessions too.

- `-o, --output FILE` -- write the script here (default: stdout)

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

run a scenario script against several Emacs binaries

Run SCRIPT once per Emacs binary, each in a fresh session, and aggregate the per-version verdicts into one summary. Exits 0 only when every version passed. With a single binary this is a matrix of one -- the same scripts then scale to a CI matrix.

- `--emacs PATHS` (repeatable) -- emacs binary, or comma-separated list (repeatable)
- `--emacs-glob GLOB` -- glob matching emacs binaries, e.g. '/opt/emacs-*/bin/emacs'
- `script` -- path to the scenario file (JSON)
