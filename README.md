# elate — Emacs Lisp Automation Tool

[![CI](https://github.com/dakra/elate/actions/workflows/ci.yml/badge.svg)](https://github.com/dakra/elate/actions/workflows/ci.yml)

Spawn disposable, sandboxed Emacs sessions — terminal (tmux-hosted TTY) or
windowed GUI — drive them with keys, mouse, and elisp, and observe the
result as text, structured data, or PNG screenshots. Built for test-driving
Emacs packages interactively — the things `emacs --batch` + ERT can't see.
See `CHANGELOG.md` for what each release added; ready-to-run walkthroughs
live in [Recipes](#recipes) and `examples/`.

## Requirements

- Emacs 29+ (for `--init-directory`) and a matching `emacsclient`
- tmux (TTY sessions)
- Python 3.10+
- GUI sessions: a graphical Emacs build (e.g. emacs-plus on macOS); on
  Linux, X11 (optionally Xvfb for headless) and ImageMagick `import` or
  `xwd`+`convert` for screenshots

## Install

elate is on PyPI as [`elate`](https://pypi.org/project/elate/):

```sh
uvx elate --help          # no install — uvx runs it straight from PyPI
pipx run elate --help     # same, via pipx
pip install elate         # or install it like any Python package
```

For development, from a checkout:

```sh
uv sync          # development
uv run elate --help

# or as a tool
uv tool install .
```

## Quick start

The examples below assume `elate` is on PATH (`pip install elate` or
`uv tool install elate`); if you run it without installing, prepend the
runner — `uvx elate start …` / `pipx run elate start …`.

```sh
# Start a sandboxed TTY Emacs (fresh fake $HOME, generated init, own tmux server)
elate start --name demo --load ./my-pkg.el --size 120x36

elate list
elate info demo

# Evaluate elisp — printed value, *Messages* delta, error + backtrace
elate -s demo eval '(+ 1 2)'
elate -s demo eval '(my-pkg-frobnicate 42)' --timeout 5

# Semantic keys (execute-kbd-macro inside Emacs)
elate -s demo keys 'M-x my-pkg-mode RET'
# Queued semantic delivery — for sequences that leave a prompt open
elate -s demo keys 'C-x C-f' --events
# Raw terminal bytes via tmux — works even when Emacs is wedged
elate -s demo keys 'C-g' --raw

# Literal text through the terminal
elate -s demo type 'hello world'

# Observe
elate -s demo state               # one-call scene snapshot (--json for the full tree)
elate -s demo buffer '*scratch*' --from 1 --to 20
elate -s demo messages            # everything since the last call
elate -s demo echo
elate -s demo screenshot -o shot.txt
elate -s demo screenshot --ansi   # with colors
elate -s demo describe key 'C-x C-f'
elate -s demo describe function my-pkg-frobnicate

# Synchronize (no sleep-and-poll)
elate -s demo wait idle
elate -s demo wait text 'Compilation finished' --buffer '*compilation*' --timeout 30
elate -s demo wait prompt

elate stop demo

# Stopped sandboxes stay behind for their transcripts; delete them when
# done (purge never touches a running session)
elate purge demo          # or: elate purge --all
```

Notes:

- `wait text` takes a **Python** regular expression (not elisp syntax) and
  happily polls a buffer that does not exist yet.
- A `messages` delta can start mid-line when Emacs coalesces a repeated
  message (`[2 times]`) at the cursor position.
- Raw `keys` rejects combinations a terminal cannot encode (e.g. `C-%`) —
  use semantic delivery for those.
- Eval results are capped at 64 KiB of printed output; longer values come
  back with `truncated: true` and the full `value-length`.

## Interactive ERT runs

`elate -s N test` runs ERT **inside the live interactive session** — real
redisplay, real window/frame state, working minibuffer — so it catches the
UI bugs `ert-run-tests-batch` cannot see. Results are structural (collected
from ERT's result objects via a listener, never scraped from the `*ert*`
buffer): counts plus per-test name, status, duration, captured `*Messages*`
output, and — for failures/errors — the condition and a trimmed backtrace.

```sh
# Tests must be loaded first: at start (--load), via eval, or --load-file:
elate -s demo test --load-file tests/my-pkg-tests.el        # all tests (t)
elate -s demo test 'my-pkg-'                # name regexp
elate -s demo test my-pkg-test-frobnicate   # one test by name
elate -s demo test '(tag ui)'               # tag selector
elate -s demo test '(not "slow")'           # any compound ERT selector
elate -s demo test :failed --timeout 30     # re-run last failures
```

A selector must be exactly one readable form: trailing tokens and reader
errors (an unbalanced `(tag`) are rejected loudly rather than silently
running a different set of tests. A regexp that does not read as a single
elisp form (e.g. `[0-9]+`) must be quoted: `'"elfix-[0-9]+"'`.

- Exit code `0` when everything behaved as expected, `1` when there are
  unexpected results (failures/errors) or the run timed out. `--json` for
  the full structured result.
- Statuses: `passed` / `failed` (a `should` failed) / `error` (the test
  signalled) / `skipped` (`ert-skip`) / `quit` (the test signalled quit —
  `keyboard-quit` in the body, or a raw `C-g` hitting a running test) /
  `aborted` (the run's timeout interrupted it).
- A quit only ends the *current* test: it is recorded with status `quit`
  (counted in `unexpected`) and the run moves straight on — no hidden
  "Abort testing?" prompt, no hang. Raw `C-g` therefore remains a safe
  way to skip past a stuck test mid-run.
- `--timeout SECS` arms an in-Emacs timeout around the whole run: a test
  stuck in a timer-servicing wait (`sleep-for`, `sit-for`,
  `accept-process-output`) is interrupted and the reply carries
  `timed-out: true`, the interrupted test's name, plus the partial
  results — the session stays usable. A test stuck in a hard elisp loop
  trips the controller's subprocess timeout instead; the session recovers
  as soon as the loop ends.

## Lint

`elate -s N lint FILE...` byte-compiles and checkdocs each file **inside
the session** (so the session's `load-path` — your package under test —
is in effect). Only file paths cross the transport, never contents.

> **Warning — lint executes compile-time code.** Byte-compilation is not
> static analysis: every top-level `eval-when-compile`, `defmacro`, macro
> *expansion*, and `require` in the file is **executed in the live
> session**, and can define/redefine functions and variables there. That
> is inherent to in-session linting (it is also why the session's
> `load-path` resolves your package). Lint untrusted code only in a
> throwaway session you `stop` afterwards. A per-file in-Emacs
> `--timeout` (default 60 s) interrupts compile-time code stuck in a
> timer-servicing wait and reports a clean error, leaving no residue; a
> hard elisp loop falls to the controller's subprocess timeout instead.

```sh
elate -s demo lint my-pkg.el other.el
elate --json -s demo lint my-pkg.el     # [{file, tool, line, col, severity, message}, ...]
```

- Exit `0` when clean, `1` when there are findings.
- The `.elc` is compiled into the session sandbox (`<session>/lint/`) and
  deleted — never written next to your source, so no stale `.elc` can
  shadow newer code in later loads.
- Because compilation happens in the session, lint results can depend on
  session history: functions defined by an earlier load — or by an earlier
  lint's compile-time code (`eval-when-compile`, `require`s) — silence
  undefined-function warnings that a fresh session would emit. (Plain
  `defmacro`/`defun` in a linted file do *not* leak; those definitions
  stay compile-local.) Use a fresh session for an authoritative lint.
- Not included (documented in the JSON `notes`): native-comp warnings
  (native compilation is asynchronous; its warnings would race the run)
  and package-lint (it needs the package archives; the sandbox
  deliberately has no network access).

## Profiling

`elate -s N profile` drives Emacs's **native sampling profiler** and
returns structured reports (never the `profiler-report` UI buffer):

```sh
# One-shot (recommended): start -> eval -> stop -> report in one call
elate -s demo profile run '(my-pkg-heavy-operation)'
elate -s demo profile run '(my-pkg-render)' --mem --timeout 30

# Manual window, e.g. around interactive steps:
elate -s demo profile start --cpu        # or --mem / --both
elate -s demo keys 'M-x my-pkg-mode RET'
elate -s demo profile report --depth 4   # works while profiling, too
elate -s demo profile stop
elate -s demo profile report             # the collected logs are kept
```

- Modes: `--cpu` (default; periodic SIGPROF samples), `--mem` (a sample
  at every allocation — counts are bytes), `--both`.
- The report has, per mode: `total` (samples/bytes), `functions` — a
  top list with `self`/`total` counts and percentages, sorted by self
  time — and `tree`, a depth-limited rendering of profiler.el's own
  unified calltree (`--depth`, default 6, max 20 — JSON nesting caps
  deeper trees; node counts are capped and truncation is flagged per
  node and per tree). `report --cpu`/`--mem` selects which collected
  section to show (a section that was never collected is a loud
  error).
- The cpu sampling interval is Emacs's `profiler-sampling-interval`
  (reported in the `profile start` reply); tune it via the eval escape
  hatch before starting: `elate -s N eval '(setq
  profiler-sampling-interval 200000)'`.
- `profile start` resets earlier logs, so a profile covers exactly one
  start..stop window. Reporting *while* profiling merges samples
  instead of discarding them (unlike stock `profiler-report`).
- `profile run` evaluates the form under the normal eval discipline
  (timeout, error + backtrace under `eval`); the profile up to an
  error is still reported. Exit `1` when the form signalled.

> **Profile in a fresh session.** Profiles are history-dependent:
> everything the session ran — including elate's own request
> servicing — is in the samples. Like lint, use a throwaway session
> for authoritative numbers.

## Benchmarking

`elate -s N bench` is a `benchmark-run-compiled` wrapper:

```sh
elate -s demo bench '(my-pkg-parse big-string)' -n 1000
elate --json -s demo bench '(make-list 1000 t)' --repetitions 200
```

- The form is wrapped in a lambda and **byte-compiled** before timing;
  if compilation fails the interpreted closure is timed instead
  (`compiled: false` + `compile-error` say so).
- Results: `elapsed` (total seconds over `-n` repetitions), `mean`
  (per repetition), `gc-runs` + `gc-elapsed` during the run, and
  `memory-deltas` — the `memory-use-counts` deltas (conses, floats,
  vector-cells, symbols, string-chars, intervals, strings allocated),
  plus `gcs-done`/`gc-elapsed` deltas as context.
- Errors from the form come back with a backtrace, like `eval`
  (exit `1`). `--timeout` follows the eval discipline: it interrupts
  timer-servicing forms; a tight loop falls to the subprocess timeout.
- The fresh-session advice above applies to benchmarks too.

## Faces, text properties, overlays

Verify font-lock, themes, and overlay-based UI structurally instead of
eyeballing screenshots:

```sh
# Run-length-encoded property runs + overlays for a buffer (or line range)
elate -s demo buffer my-buf --props
elate --json -s demo buffer my-buf --from 10 --to 20 --props

# Point query: faces / properties / overlays at LINE:COL (1-based:0-based)
elate -s demo faces-at 3:14 --buffer my-buf
```

- Runs cover contiguous spans with identical "interesting" properties:
  `face` (named faces, face lists, and anonymous `(:weight bold ...)`
  faces), `display`, `invisible`, `field`, plus `button`/`keymap`
  presence. Boundaries caused by uninteresting properties (`fontified`,
  ...) are merged away.
- Overlays report start/end plus `face`, `invisible`, `display`,
  `before-string`, `after-string`, and `priority`.
- `faces-at` also distinguishes the text-property `face` from the
  effective `char-face` (which resolves overlays — what the user sees).
- Font-lock is ensured on the requested range first, so never-displayed
  buffers are still fontified correctly.

## Popup capture

```sh
elate -s demo popups          # {kind, buffer?, text} per visible popup
```

Detects and captures as text: which-key (Emacs 30+), transient (Emacs 31),
hydra's lv hint window, corfu's child frame, company's pseudo tooltip,
completion-preview, and any other visible child frame (posframe & friends).
Mechanisms not installed in the sandbox simply never match. `state` lists
the active popup kinds in its `popups` field, so you know when a capture
is worthwhile.

## GUI sessions

`elate start --ui gui` spawns a windowed Emacs instead of the tmux-hosted
terminal one — same sandbox, same semantic channel, real GUI rendering:

```sh
elate start --name win --ui gui --size 100x35 --load ./my-pkg.el

# PNG screenshot of the Emacs window (default file: ./elate-win-<time>.png)
elate -s win screenshot -o shot.png
elate --json -s win screenshot          # returns path + pixel dimensions

# Mouse — semantic, works for BOTH tty and gui sessions, no OS permissions:
# a real posn is built inside Emacs and a complete click/drag/wheel event
# sequence goes through the command loop, so buttons, follow-link,
# mode-line maps, and mwheel react exactly as for a human click.
elate -s win mouse click --buffer '*my-menu*' --line 3 --col 5
elate -s win mouse click --button 2 --pos 42
elate -s win mouse drag --pos 10 --to-pos 25      # selects a region
elate -s win mouse wheel --direction down --count 3
elate -s win mouse click --mode-line --buffer other-buf   # selects that window

# Live frame resize (works for tty via tmux too)
elate -s win resize 120x40

elate stop win
```

Differences from TTY sessions:

- **No raw channel.** `keys --raw` fails with a pointer to semantic
  delivery; `type` transparently switches to queued
  `unread-command-events` (same typing semantics, but needs a responsive
  Emacs). GUI typing replays every character through the command loop, so
  it is delivered in drained chunks and capped at 10,000 characters --
  use `eval` with `insert` for bulk text. A GUI Emacs that wedges beyond
  the semantic channel can only be stopped, not unblocked.
- **Screenshots are PNG** of the Emacs window, and need a live session
  (no post-mortem capture; check `<session>/log/emacs-gui.log` instead).
- **Liveness** is tracked by pid + semantic ping instead of tmux.

macOS notes:

- Screenshots use `screencapture` against the window resolved via Quartz,
  which requires the **Screen Recording** permission for the application
  that runs elate (your terminal): System Settings → Privacy & Security →
  Screen Recording, then restart the terminal. elate probes the permission
  first and returns an actionable error instead of triggering prompts.
- A tiling window manager (AeroSpace, yabai, Amethyst) will re-tile new
  Emacs windows, overriding `--size`/`resize` — the requested geometry
  still lands in `initial-frame-alist`/`default-frame-alist`.

Linux/CI notes (written for CI, not exercised on macOS dev machines):

- `elate start --ui gui --headless` boots a private Xvfb on a free display
  (via `-displayfd`) and puts Emacs on it; the Xvfb is killed at
  `elate stop`. Screenshots use ImageMagick `import` (or `xwd`+`convert`)
  with the window id taken from the frame's `outer-window-id`.

The `minimal` config sets `mouse-wheel-inhibit-click-time nil`: stock
Emacs silently drops a mouse-2 click arriving within 0.35 s of a wheel
scroll (anti-accidental-paste), which makes scripted runs flaky.

Every command takes `--json` for machine-readable output:

```sh
elate --json -s demo eval '(emacs-version)'
```

Exit codes: `0` success, `1` error (including elisp errors from `eval`),
`2` command-line usage error, `3` `wait` timeout.

## Scenario scripts & `elate run`

A scenario script is a **JSON** file describing a whole interaction —
session config, input/wait steps, and assertions — that `elate run`
executes against a **fresh throwaway session** and turns into an exit
code. That makes a script a regression test and the CI entry point.

JSON, not YAML, deliberately: no new dependency, AI harnesses emit it
natively, and every other elate surface (`--json`, MCP, the JSONL
transcript) already speaks it. What YAML comments would give you is
covered explicitly: every step accepts a `"comment"` key, and
`"skip": true` disables a step without deleting it.

```json
{
  "name": "my-pkg smoke test",
  "session": {"ui": "tty", "size": "100x30", "config": "minimal",
              "load": ["./my-pkg.el"]},
  "steps": [
    {"keys": "M-x my-pkg-mode RET"},
    {"wait": "text", "pattern": "My-Pkg", "buffer": "*scratch*", "timeout": 10},
    {"type": "hello"},
    {"assert": {"buffer_contains": "hello"}},
    {"test": "my-pkg-", "load_files": ["./tests/my-pkg-tests.el"],
     "allow_unexpected": true},
    {"assert": {"tests": {"unexpected": 0, "timed-out": false}}},
    {"lint": ["./my-pkg.el"]},
    {"assert": {"eval": "(featurep 'my-pkg)"}}
  ]
}
```

```sh
elate run scenario.json             # fresh session, steps, assertions, teardown
elate --json run scenario.json      # per-step results, timings, snapshots
elate run scenario.json --keep      # keep the session afterwards
elate run scenario.json --keep-on-failure   # keep it only when it failed
elate run scenario.json --emacs /opt/emacs-29/bin/emacs
elate -s existing run scenario.json # run against an existing session
                                    # (its "session" config is ignored;
                                    #  nothing is torn down)
```

- **Steps** mirror the CLI verbs, exactly one per step: `keys` (options
  `delivery`: semantic/events/raw, `timeout`), `type`, `eval`
  (`timeout`), `wait` (`"idle"`/`"text"`/`"prompt"` + `pattern`,
  `buffer`, `timeout`, `min_idle`), `mouse` (same options as the CLI),
  `test` (ERT selector + `load_files`, `timeout`, `allow_unexpected`),
  `lint` (file list + `timeout`, `allow_findings`), `screenshot`
  (output path, or `null` to embed the text; `ansi`), `resize`
  (`"COLSxROWS"`), and `assert`.
- **Assertions** (one kind per assert step): `buffer_contains` /
  `buffer_matches` (Python regexp; optional `buffer`), `state` (object
  of state-field → expected value, dotted paths like
  `"minibuffer.prompt"` work), `messages_match` (regexp over
  `*Messages*`), `popup` (a popup kind, or `true` for any),
  `tests` (count fields of the last `test` step), `lint_clean`
  (verdict of the last `lint` step), and `eval` (passes when the form
  evaluates without error to non-`nil` — the catch-all).
- A `test` step fails the run on unexpected results (and `lint` on any
  finding) unless `allow_unexpected` / `allow_findings` is set — set
  those when you'd rather assert exact counts.
- The run stops at the **first failure**; the failed step embeds a
  state snapshot (same convention as everywhere else), later steps are
  recorded as `not-run`. Exit `0` when everything passed, `1`
  otherwise.
- Fresh sessions are the default *on purpose*: lint executes
  compile-time code and lint/test results depend on session history, so
  only a throwaway session gives reproducible verdicts.
- A fresh session whose startup `eval`/`load` signalled an error
  **fails the run** before any step executes — the package under test
  may not even be loaded. Set `"allow_init_error": true` in the
  script's `"session"` block for the rare deliberate case.
- Relative paths inside a script (session `load`/`init_file`, test
  `load_files`, `lint` files, `screenshot` output) resolve against the
  **script file's directory**, so scripts can live next to the package
  they test and run from anywhere.
- Step keys *and their value types* are validated strictly up front — a
  typoed option, a wrong-typed number/bool, a bad enum value, an option
  on the wrong step kind, or an empty `"steps"` list is a loud error
  before anything boots, never a silent no-op (or a vacuous PASS).

### Transcript → script export

Every session logs all of its operations to
`<session>/log/transcript.jsonl`. `export-script` converts that into a
scenario file:

```sh
elate -s demo export-script              # to stdout
elate -s demo export-script -o demo.json # then: elate run demo.json
```

This is a **best-effort starting point for editing, not a faithful
recorder**: inputs (keys/type/eval/mouse/wait/test/lint/resize) become
steps in transcript order; observations (state/buffer/messages/echo/
popups/screenshot reads) become assertion stubs with `"skip": true` for
you to edit into real assertions; values the transcript clipped are
exported skipped with a comment. Timing, concurrency, and any
out-of-band changes are not captured, and the emacs binary is not
pinned (use `--emacs`/`matrix` for that). Works on stopped sessions —
the transcript outlives the Emacs.

## Recording a TTY session (asciinema)

```sh
elate -s demo record start               # default: <session>/log/<name>-<time>.cast
elate -s demo record start -o demo.cast
elate -s demo record status
elate -s demo record stop                # reports path, event count, duration
```

Produces an [asciicast v2](https://docs.asciinema.org/manual/asciicast/v2/)
file written directly (no asciinema install needed): a JSONL header plus
timestamped `[time, "o", data]` output events captured via tmux
`pipe-pane`. The first event replays the screen as it looked when the
recording started, so playback begins from the correct picture.

- Play it: `asciinema play demo.cast`. Render a GIF for your README with
  the asciinema ecosystem's [agg](https://github.com/asciinema/agg):
  `agg demo.cast demo.gif` (`brew install agg` / `cargo install agg`).
  There is deliberately no `record gif` — agg does that job better.
- One recording per session at a time. `record stop` closes the pipe,
  waits for the helper to flush and exit, and reports the final event
  count; `record status` shows whether the recording is still live. If
  Emacs **crashes mid-recording**, its pane is kept for post-mortem
  capture with the pipe still attached: `status` then reports the
  recording as stale, and `stop` (or `elate stop`) reaps the helper
  directly and finalizes the cast with everything up to the crash — no
  stray recorder either way.
- TTY sessions only: GUI sessions have no terminal byte stream —
  `record` points you at `snap` instead.

## Snap series (screenshot frames, GUI demo helper)

```sh
elate -s win snap start --interval 0.5            # default dir: <session>/snap-<time>/
elate -s demo snap start --interval 0.2 --ansi -o frames/
elate -s demo snap status
elate -s demo snap stop                           # idempotent
```

Captures a frame every `--interval` seconds into `frame-NNNN.png` (GUI
sessions, reusing the screenshot machinery including the macOS
permission probe) or `frame-NNNN.txt` (TTY sessions; `--ansi` for
colored text), plus a `manifest.json` with per-frame timestamps — raw
material for demo GIFs/videos via external tools.

The snapper is a detached process that only ever *reads* the session: if
it dies the session is unaffected; if the session dies the snapper
finalizes and exits. The manifest is rewritten atomically after every
frame, so it is sane even after a hard kill. `snap stop` is idempotent
and identity-checks the snapper pid before signalling anything.

## Version matrix

```sh
elate matrix --emacs /opt/emacs-29/bin/emacs,/opt/emacs-30/bin/emacs -- scenario.json
elate matrix --emacs-glob '/opt/emacs-*/bin/emacs' scenario.json
elate matrix --emacs emacs scenario.json     # a matrix of one; bare
                                             # names resolve via PATH
```

Runs the scenario once per binary, each in a fresh session, and prints
one summary (`--json` for the structured per-version results, including
the detected `emacs_version` and the first failed step). Exit `0` only
when every version passed. Binaries are checked up front, and one
broken binary does not abort the rest of the matrix.

### CI recipe (GitHub Actions)

`elate run`'s exit code is the contract; the per-version axis maps onto
a CI matrix using a setup-emacs action:

```yaml
jobs:
  scenario:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        emacs_version: ["29.4", "30.1", "snapshot"]
    steps:
      - uses: actions/checkout@v4
      - uses: purcell/setup-emacs@master   # or jcs090218/setup-emacs
        with:
          version: ${{ matrix.emacs_version }}
      - run: sudo apt-get update && sudo apt-get install -y tmux
      - uses: astral-sh/setup-uv@v5
      - run: uvx --from git+https://github.com/you/elate elate --json run scenario.json
```

Notes for CI: TTY sessions need only `tmux`; export a UTF-8 locale
(`LANG=C.UTF-8`) if your steps type non-ASCII text through the raw
channel; on failure the JSON output embeds the failing step's state
snapshot, and `--keep-on-failure` plus
`elate -s <name> screenshot` can capture more before teardown. Within a
single job you can also run several local binaries via `elate matrix`.

## Recipes

Three worked examples. Every command below is also committed as a
scenario script under `examples/` — `elate run examples/<name>.json`
runs the same steps with assertions, and the test suite keeps them
green.

### Test a transient menu

Transient UIs never show up in batch tests — they need a live command
loop and a window. (transient ships with Emacs ≥ 28; this uses the
Emacs 31 one in the sandbox.)

```sh
elate start --name demo --size 100x30

# Define a tiny transient (yours would come from --load ./my-pkg.el):
elate -s demo eval '(progn (require (quote transient))
  (transient-define-prefix demo-transient ()
    ["Demo actions" ("u" "upcase word" upcase-word)])
  t)'

# Fixture text, point at the start of "hello":
elate -s demo eval '(progn (switch-to-buffer "*scratch*") (erase-buffer)
  (insert "hello world") (goto-char (point-min)) t)'

elate -s demo eval '(demo-transient)'   # open the menu
elate -s demo popups                    # == transient ==  u upcase word
elate -s demo keys u                    # press the suffix key
elate -s demo wait idle
elate -s demo buffer '*scratch*'        # -> HELLO world
elate stop demo
```

`state` also flags the open menu (`popups: transient`), so an AI
driving the session knows to call `popups`. Script equivalent:
`elate run examples/transient-menu.json` — the assertion steps are
`{"assert": {"popup": "transient"}}` and
`{"assert": {"buffer_contains": "HELLO world"}}`.

### Verify font-lock / theme faces

Check rendering facts structurally instead of eyeballing screenshots:

```sh
elate start --name demo --size 100x30
elate -s demo eval '(with-current-buffer (get-buffer-create "demo.el")
  (erase-buffer) (emacs-lisp-mode)
  (insert "(defun demo-add (x)\n  \"Add one to X.\"\n  (1+ x))\n")
  t)'

# Run-length encoded face runs for the whole buffer:
elate -s demo buffer demo.el --props
#   2-7  (L1) face=font-lock-keyword-face 'defun'
#   8-16 (L1) face=font-lock-function-name-face 'demo-add'
#   23-38 (L2) face=font-lock-doc-face '"Add one to X."'

# Point query (LINE:COL, 1-based:0-based):
elate -s demo faces-at 1:8 --buffer demo.el
#   face: font-lock-function-name-face
elate stop demo
```

The buffer is never displayed — `--props` ensures font-lock on the
range first, so this works for background buffers too. Overlay-based
UI (hl-line, company) shows up in the same dump under `overlays`.
Script equivalent: `elate run examples/font-lock.json` (asserts the
faces with `{"assert": {"eval": "(eq (get-text-property ...))"}}`).

### Drive a real package interactively

The loop an AI harness should follow — **act → wait → observe**, never
sleep-and-poll — shown against a built-in package (dired) so it works
offline. Driving magit or any other installed package is the same
loop with `--load`/`clean-install` pointing at it.

```sh
elate start --name demo --size 100x30

# Fixture files live in the sandbox's fake $HOME, not yours:
elate -s demo eval '(progn (make-directory "~/demo" t)
  (with-temp-file "~/demo/notes.txt" (insert "remember the milk")) t)'

elate -s demo eval '(dired "~/demo")'          # act
elate -s demo wait text 'notes\.txt'           # wait: listing rendered
elate -s demo state                            # observe: buffer "demo" (dired-mode)

elate -s demo eval '(dired-goto-file (expand-file-name "~/demo/notes.txt"))'
elate -s demo keys RET                         # act: visit, like a user
elate -s demo wait idle
elate -s demo state                            # observe: buffer "notes.txt"
elate -s demo buffer                           # -> remember the milk
elate stop demo
```

Script equivalent: `elate run examples/drive-dired.json`. For the
clean-install variant of this loop (install a package for real, then
drive its autoloaded entry point), see `examples/clean-install.json`
and the [Clean-install sessions](#clean-install-sessions) section.

## Using elate from AI harnesses

elate is built to be driven by AI agents. Pick the integration by harness:

- **Claude Code** → install the [plugin](#claude-code-plugin) (two
  commands; bundles the Agent Skill and the MCP server — everything below
  in one step).
- **Any MCP-capable harness** (Codex CLI, Cursor, Zed, Gemini CLI, Claude
  Desktop, …) → register the [MCP server](#mcp-server): one line, and the
  server command is the same `uvx elate mcp` everywhere.
- **Any harness with shell access** → the plain CLI is the full feature
  set; `AGENTS.md` at the repo root is the ~40-line distillation most
  agents-md-aware tools pick up automatically, and the
  [Agent Skill](#agent-skill) files teach the complete workflow.

### Claude Code plugin

The repo doubles as a Claude Code plugin (and hosts its own marketplace),
bundling the Agent Skill and the MCP server below. Install in two commands:

```sh
claude plugin marketplace add dakra/elate
claude plugin install elate
```

You get:

- the **skill**, namespaced as `/elate:elate` — triggers organically
  whenever the model needs to test-drive or debug Emacs Lisp;
- the **MCP server**, auto-registered from the plugin's `.mcp.json` as
  `uvx elate mcp` (always the latest PyPI release — the plugin clone stays
  a thin config layer with no environment of its own). The first connect
  may take a few seconds while `uvx` resolves elate from PyPI on a cold
  cache;
- the **`emacs-tester` subagent** (`agents/emacs-tester.md`, invokable as
  `elate:emacs-tester`) — a test pilot preloaded with the skill, for
  delegating long interactive test-drives out of the main context; it
  drives elate in its own context window and returns condensed findings
  instead of a wall of transcripts;
- **leftover-session hooks** (`hooks/check-running.sh`): a SessionEnd
  hook warns when you leave a Claude Code session while elate sessions
  are still running (names + the stop command), and a SessionStart hook
  injects the same fact as context at the next session start so the
  model can deal with the leftovers. Both are best-effort and
  fail-silent: they never delay or break a session when elate isn't
  installed or nothing is running — stopped sandboxes are inert and
  don't warrant a warning. One blind spot by design: the hooks look up
  sessions via `elate` on PATH (falling back to the offline uv tool
  cache), so sessions driven exclusively through `uv run` inside a
  checkout are invisible to them.

Two notes on `.mcp.json`:

- **Dual role:** because the repo root is also the plugin root, the same
  `.mcp.json` acts as *project-scope* MCP config for anyone opening this
  repo in Claude Code. That is fine — project-scope servers always require
  per-user approval before they run. Two consequences: the project-scope
  server runs the **latest PyPI release, not your checkout** — when hacking
  on elate, register the checkout command from the
  [MCP server](#mcp-server) section instead. And a maintainer with the
  plugin installed who opens this repo gets **two** elate servers (project
  `elate` plus `plugin:elate:elate`, 44 tools with ambiguous names) —
  approve/enable at most one.
- **Offline / pinned setups:** to run the MCP server from the plugin clone
  itself instead of PyPI, change the server entry to
  `uv run --directory ${CLAUDE_PLUGIN_ROOT} elate mcp`
  (JSON has no comments, so this fallback lives here).

**Pinned (reproducible) installs:** `marketplace add dakra/elate` serves
GitHub HEAD, updating whenever the version label bumps. To pin instead,
add the marketplace at a release tag — releases are tagged
`elate--v{version}` (see [Publishing](#publishing-to-pypi-maintainer)),
and because the plugin's source is the marketplace clone itself
(`"source": "./"`), the installed payload is exactly that tag's tree:

```sh
# X.Y.Z = a released version (git tag -l 'elate--v*' lists them)
claude plugin marketplace add dakra/elate@elate--vX.Y.Z
claude plugin install elate
```

Marketplace sources accept a branch or tag (not a raw commit SHA) two
ways: `@ref` appended to the GitHub shorthand as above, or `#ref` appended
to a full git URL
(`claude plugin marketplace add 'https://github.com/dakra/elate.git#elate--vX.Y.Z'`).
A tag pin is the reproducible form since tags don't move.

Local development: `claude --plugin-dir .` loads the checkout as the
plugin in a session; `claude plugin validate .` checks the marketplace
manifest and `claude plugin validate .claude-plugin/plugin.json` the
plugin manifest (add `--strict` to fail on warnings — CI runs both calls
with it).

### Agent Skill

`skills/elate/` is an Agent Skill that teaches an AI harness to drive the
elate CLI well (the act → wait → observe loop, key-delivery decision tree,
fresh-session rules, scenario scripts) at near-zero ambient token cost:
`SKILL.md` plus progressively-disclosed `REFERENCE.md` (generated from the
CLI — CI fails on drift), `RECIPES.md`, and `SCRIPTING.md`. Install it
standalone by copying or symlinking:

```sh
ln -s "$(pwd)/skills/elate" ~/.claude/skills/elate
```

Harnesses without skill support get the same distillation from `AGENTS.md`
at the repo root.

### MCP server

`elate mcp` serves all of the above as typed MCP tools over stdio — the
right fit for harnesses without shell access, and for GUI screenshots
returned inline as images. The server command is the same in every
harness: `uvx elate mcp`. Registration per harness:

**Claude Code** ([MCP docs](https://code.claude.com/docs/en/mcp)) —
plugin users skip this, the plugin already registers the server:

```sh
claude mcp add elate -- uvx elate mcp
```

**Claude Desktop**
([MCP docs](https://modelcontextprotocol.io/quickstart/user)) — the
canonical no-shell-access case. Settings → Developer → Edit Config opens
`claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`,
Windows: `%APPDATA%\Claude\claude_desktop_config.json`); add the server
and restart the app:

```json
{
  "mcpServers": {
    "elate": { "command": "uvx", "args": ["elate", "mcp"] }
  }
}
```

**Codex CLI** ([MCP docs](https://developers.openai.com/codex/mcp/)):

```sh
codex mcp add elate -- uvx elate mcp
```

or in `~/.codex/config.toml`:

```toml
[mcp_servers.elate]
command = "uvx"
args = ["elate", "mcp"]
```

**Cursor** ([MCP docs](https://cursor.com/docs/context/mcp)) —
`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "elate": { "type": "stdio", "command": "uvx", "args": ["elate", "mcp"] }
  }
}
```

**Zed** ([MCP docs](https://zed.dev/docs/ai/mcp)) — `settings.json`:

```json
{
  "context_servers": {
    "elate": { "command": "uvx", "args": ["elate", "mcp"] }
  }
}
```

**Gemini CLI**
([MCP docs](https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html))
— `gemini mcp add elate uvx elate mcp`, or in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "elate": { "command": "uvx", "args": ["elate", "mcp"] }
  }
}
```

Hacking on elate itself? Serve the checkout instead of the PyPI release —
swap the command for `uv run --directory /path/to/elate elate mcp`
(e.g. `claude mcp add elate -- uv run --directory /path/to/elate elate mcp`;
same split across `command`/`args` in the JSON/TOML forms).

Tools (1:1 with the CLI; every response is structured JSON, and error
responses embed a compact state snapshot so the model sees why):

| tool | purpose |
|---|---|
| `elate_start` / `elate_stop` / `elate_list` / `elate_info` | session lifecycle (`ui`: `tty` or `gui`; `headless` for Xvfb on Linux; `config` includes `clean-install`) |
| `elate_keys` | kbd-notation keys; `delivery`: `semantic`, `events` (holds prompts open), or `raw` (TTY only; works even when Emacs is wedged) |
| `elate_type` | literal text: raw terminal bytes (TTY) or queued events (GUI) |
| `elate_mouse` | semantic mouse for both UIs: click/double/drag/wheel at a buffer position, line/column, or the mode line; fires real bindings (buttons, follow-link, mwheel) |
| `elate_eval` | elisp eval with value, *Messages* delta, error + backtrace |
| `elate_test` | interactive ERT run: selector support, per-test status/duration/messages/condition/backtrace; failures are data (`ok` stays true — check `unexpected`) |
| `elate_lint` | byte-compile + checkdoc by file path: `{file, tool, line, col, severity, message}` items; **executes the file's compile-time code in the session** (see Lint warning above) |
| `elate_profile` | native profiler: one-shot `run` (start → eval → stop → report) or manual `start`/`stop`/`report`; structured top-function list + depth-limited calltree (cpu samples / mem bytes) |
| `elate_bench` | `benchmark-run-compiled` wrapper: elapsed/mean, GC runs + GC time, `memory-use-counts` allocation deltas; interpreted fallback when the form won't compile |
| `elate_state` | one-call scene snapshot: window layout tree with visible text and mode lines, buffer/modes/point/region, minibuffer prompt + input + completion candidates, echo area, visible popup kinds, *Messages* tail |
| `elate_screenshot` | TTY: rendered screen as text (optionally ANSI-colored). GUI: PNG of the Emacs window, returned as MCP image content plus a JSON block with path + dimensions |
| `elate_buffer` / `elate_messages` / `elate_echo` | targeted reads (`elate_messages` is cursor-based: only news since the last call); `elate_buffer` takes `props` for face/text-property runs + overlays |
| `elate_popups` | capture visible popups as text: which-key, transient, hydra, corfu/company, completion-preview, child frames |
| `elate_wait` | wait for `idle` / `text` (Python regexp) / `prompt` |
| `elate_describe` | structured docs + binding resolution for a key/function/variable/mode |
| `elate_run_script` | execute a whole scenario script (by path) in one call: fresh session, steps, assertions, teardown; script failures are data (`ok` stays true — check `success`); `keep_on_failure` keeps the session for inspection |
| `elate_record` | start/stop/status of an asciicast v2 recording of a TTY session (snap series stays CLI-only) |

Sessions live in tmux and survive MCP reconnects; MCP tool calls are
transcript-logged into the session JSONL just like CLI commands.
There is deliberately no `elate_faces_at` tool: `elate_buffer` with
`props: true` and `from_line`/`to_line` covers point queries in one call
(fewer, fatter tools).

## Config modes

- `--config minimal` (default): no startup screen, `debug-on-error`,
  no backups/lockfiles, deterministic test settings; `--load FILE-OR-DIR`
  puts your package on `load-path` (and loads files).
- `--config bare`: `emacs -Q` plus the elate agent only.
- `--init-file PATH`: your own init file, still sandboxed.
- `--config clean-install`: minimal defaults, but the `--load` package
  is **installed for real** instead of load-path injection (next
  section).

## Clean-install sessions

`--config minimal` puts your source tree on `load-path` — fast, but it
cannot tell you whether your *package* works: whether the
`;;;###autoload` cookies generate working autoloads, whether
`Package-Requires` is honest, whether the byte-compiled installed copy
behaves. `--config clean-install` verifies exactly that:

```sh
elate start --name fresh --config clean-install --load ./my-pkg.el
elate -s fresh describe function my-pkg-command   # autoloaded: true, before any require
elate -s fresh keys 'M-x my-pkg-command RET'      # invoking it loads the installed copy
elate info fresh                                  # package_user_dir + installed [{name, version, dir, warnings}]
elate stop fresh
```

- Each `--load` target (an `.el` file, a package **tar**, or a package
  **directory** — the three inputs `package-install-file` accepts) is
  installed via `package-install-file` into a sandbox-local
  `package-user-dir` (`<session>/elpa/`), then activated: autoloads
  generated and loaded, the installed copy byte-compiled.
- **The sandbox has no network** and `package-archives` is nil, so a
  dependency that is not built in (and not installed earlier in the
  same session) cannot be fetched. Dependencies are checked up front
  against `Package-Requires`; missing ones fail the install with one
  clear `init_error` naming each of them (`elate start` prints it,
  `info` keeps it).
- `info` reports `package_user_dir` and `installed` — per package:
  name, version, install dir, and any **byte-compile warnings** the
  install produced. `state` carries the same facts under
  `clean-install`. Both work for dead sessions too.
- In scenario scripts it is just `"session": {"config": "clean-install",
  "load": ["./my-pkg.el"]}` — see `examples/clean-install.json`. A
  failing install fails the run via the normal `init_error` contract.

## How it works

Each session gets a sandbox under `~/.cache/elate/sessions/<name>/`
(`$ELATE_HOME` overrides the base dir) with a fake `home/`, a generated
`init/` used via `--init-directory`, a `server/` dir holding a private
server.el socket, and `log/transcript.jsonl` recording every command.
Stopped sessions keep their sandbox (transcripts outlive the Emacs) until
`elate purge NAME…`/`elate purge --all` deletes them; purge never removes
a running session.

TTY Emacs runs `-nw` inside a dedicated tmux server whose socket lives in
the sandbox (`tmux -S <session_dir>/tmux.sock`), so your own tmux is
untouched and equally-named sessions under different roots can never
collide. A crashed Emacs leaves its dead pane behind
(`remain-on-exit failed`), so `elate -s N screenshot` still captures the
dying screen post-mortem. GUI Emacs is spawned directly (tracked by pid;
stdout/stderr land in `log/emacs-gui.log`).
Errors signalled by `--eval`/`--load`/`--init-file` startup code are
recorded and reported by `elate start` (and shown by `info` as
`init_error`) instead of silently parking the session in the debugger.
Two channels:

- **semantic** — `emacsclient --eval` against the per-session socket; the
  in-Emacs agent (`elate-agent.el`) answers with base64-encoded JSON, which
  sidesteps emacsclient escaping quirks. Hard subprocess timeouts mean a
  blocked Emacs surfaces as "busy" instead of wedging the controller.
- **raw** — `tmux send-keys` / `capture-pane`: real terminal bytes in,
  rendered screen out. The escape hatch when Emacs is blocked (`keys C-g --raw`).

## Development

```sh
uv run pytest                  # integration tests spawn real Emacs+tmux
uv run pytest -k kbd           # unit tests only
```

The integration tests skip automatically when `emacs` or `tmux` is missing.

## Publishing to PyPI (maintainer)

Releases are published manually — CI never uploads.

> **Release rule:** the Agent Skill (`skills/elate/`) teaches `uvx elate`,
> i.e. *latest PyPI* — while its REFERENCE.md is generated from HEAD. A
> merged CLI change is therefore not "done" until the version is bumped
> and published; otherwise every skill consumer reads docs for a CLI that
> `uvx` doesn't serve yet. The Claude Code plugin adds two clauses:
>
> - The version bump is **three files**: `pyproject.toml`,
>   `.claude-plugin/plugin.json`, and `.claude-plugin/marketplace.json`
>   (`test_versions_are_in_sync` fails the suite if they drift).
> - **Publish to PyPI first, push the bump second.** The plugin
>   marketplace serves whatever the cloned GitHub HEAD contains and does
>   no content↔version check at install — the marketplace `version` is a
>   pure label and the *update gate* (installed plugins only refresh when
>   it changes). The label stays honest only if the three-file bump and
>   the PyPI publish both land before the push; anyone installing between
>   a content push and the next bump gets a mislabeled HEAD snapshot and
>   no updates until the version string moves. And note the asymmetry:
>   publishing to PyPI alone updates *nothing* for plugin users — plugin
>   delivery always requires a push.

```sh
# 1. Bump the version in pyproject.toml AND .claude-plugin/plugin.json AND
#    .claude-plugin/marketplace.json (uv run pytest -k versions_are_in_sync
#    checks); update CHANGELOG.md; re-validate the manifests (CI runs the
#    same two calls); commit. Do NOT push yet.
claude plugin validate --strict .
claude plugin validate --strict .claude-plugin/plugin.json

# 2. Tag the release: cross-checks plugin.json against the marketplace
#    entry, then creates the elate--v{version} git tag — the ref consumers
#    pin marketplaces to (see "Pinned installs" in the plugin section).
#    Refuses on a dirty working tree; --dry-run previews. The tag stays
#    local until step 6 — if a later step fails, `git tag -d elate--vX.Y.Z`
#    before retrying (`claude plugin tag` refuses to overwrite an existing
#    tag).
claude plugin tag

# 3. Build sdist + wheel, and check the metadata PyPI will validate:
rm -rf dist && uv build
uvx twine check dist/*

# 4. Optional dress rehearsal against TestPyPI (needs a test.pypi.org token):
uv publish --publish-url https://test.pypi.org/legacy/ --token "$TEST_PYPI_TOKEN"
#    Verify the upload. Two flags are load-bearing: TestPyPI hosts stale
#    copies of most dependencies, so uv's default first-index strategy can
#    never resolve them — `unsafe-best-match` lets dependencies fall back
#    to real PyPI (acceptable here: a manual maintainer action, not a
#    build). And elate itself must be pinned to the new version, which at
#    this point exists ONLY on TestPyPI — that pin is what makes this
#    exercise the upload instead of resolving last release from PyPI.
uvx --isolated --index https://test.pypi.org/simple/ \
  --index-strategy unsafe-best-match \
  --from "elate==$(uv run elate --version | cut -d' ' -f2)" elate --version

# 5. Publish for real (token from pypi.org → Account settings → API tokens;
#    scope it to the elate project after the first upload):
uv publish --token "$PYPI_TOKEN"     # or set UV_PUBLISH_TOKEN

# 6. Only now push the bump, tag included — plugin/skill users update from
#    GitHub HEAD, which must never advertise a version PyPI doesn't serve:
git push origin main "elate--v$(uv run elate --version | cut -d' ' -f2)"
```

### Community marketplace submission (optional)

The self-hosted marketplace above is fully self-sufficient; submitting to
Anthropic's community marketplace is optional and maintainer-initiated.
When desired:

- Submit via the Console form at
  [platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit)
  (the claude.ai form requires a Team/Enterprise org with directory
  management access; the Console form does not).
- Run both `claude plugin validate` calls first — the review pipeline runs
  the same check, plus automated safety screening.
- Approved plugins land **pinned to a commit SHA** in
  [`anthropics/claude-plugins-community`](https://github.com/anthropics/claude-plugins-community)
  (the catalog syncs nightly, so listing lags approval; Anthropic's CI
  bumps the pin as new commits are pushed here). Unlike the official
  marketplace, the community one is not pre-registered — users add it
  once with `claude plugin marketplace add anthropics/claude-plugins-community`,
  then install with `claude plugin install elate@claude-community` — the
  self-hosted marketplace keeps working independently either way.
- PRs against that repo are closed automatically; everything flows through
  the submission form.
