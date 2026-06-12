# elate — Emacs Lisp Automation Tool

Spawn disposable, sandboxed Emacs sessions — terminal (tmux-hosted TTY) or
windowed GUI — drive them with keys, mouse, and elisp, and observe the
result as text, structured data, or PNG screenshots. Built for test-driving
Emacs packages interactively — the things `emacs --batch` + ERT can't see.
See `PLAN.md` for the design and `CHANGELOG.md` for what each phase added;
ready-to-run walkthroughs live in [Recipes](#recipes) and `examples/`.

## Requirements

- Emacs 29+ (for `--init-directory`) and a matching `emacsclient`
- tmux (TTY sessions)
- Python 3.10+
- GUI sessions: a graphical Emacs build (e.g. emacs-plus on macOS); on
  Linux, X11 (optionally Xvfb for headless) and ImageMagick `import` or
  `xwd`+`convert` for screenshots

## Install

```sh
uv sync          # development
uv run elate --help

# or as a tool
uv tool install .
```

## Quick start

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
elate run scenario.json --json      # per-step results, timings, snapshots
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
      - run: uvx --from git+https://github.com/you/elate elate run scenario.json --json
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

## MCP server

`elate mcp` serves all of the above as MCP tools over stdio, for AI
harnesses like Claude Code. Register it:

```sh
# from a checkout
claude mcp add elate -- uv run --directory /path/to/elate elate mcp

# or, after `uv tool install .`
claude mcp add elate -- elate mcp
```

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
