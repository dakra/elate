---
# Keep this frontmatter to plain `key: value` scalars (indented
# continuation lines are fine): tests/test_skill.py parses it without YAML.
# `version` is the skill CONTENT version: bump it only in releases whose
# skill files change (tests/skill_content_version.txt pins this); `elate
# start` compares it against installed copies to nudge about staleness.
name: elate
version: 0.15.0
description: Spawns sandboxed Emacs sessions (terminal or GUI) and drives them
  with keys, mouse, and elisp to test Emacs Lisp interactively - run ERT tests
  in a live session, inspect faces/overlays/popups structurally, lint, profile,
  benchmark, capture screenshots, and turn sessions into replayable regression
  scripts. Use when developing or debugging an Emacs package or config, when
  asked to test-drive, reproduce a bug in, or verify behavior of Emacs Lisp
  code, or when batch `emacs --batch`/ERT alone cannot show interactive
  behavior (keybindings, minibuffer prompts, transient menus, redisplay).
---

# elate — drive a real Emacs from the shell

elate spawns disposable, sandboxed Emacs sessions (fresh fake `$HOME`,
generated init, private tmux server) and gives you structured control and
observation over them. Run it with `uvx elate …` (PyPI; no install step),
or `uv tool install elate` once to put `elate` on `PATH` (handy when many
sub-agents each shell out). In a checkout of the elate repo itself, use
`uv run elate …` instead. After `claude plugin update`, the CLI (`uvx`/`uv
run`) is already on the new version while a registered MCP server (if you
enabled one) stays on the old one until the client restarts — so
mid-session the CLI is the live path.

Supporting files (read on demand):
- [REFERENCE.md](REFERENCE.md) — every command, option, default (generated from the CLI)
- [RECIPES.md](RECIPES.md) — worked examples: transient menus, font-lock, dired, clean-install
- [SCRIPTING.md](SCRIPTING.md) — scenario JSON format, assertions, `run`/`matrix`/`export-script`, CI

## Quickstart

```sh
uvx elate start --name s --load ./my-pkg.el   # sandboxed TTY Emacs, pkg on load-path
uvx elate -s s eval '(my-pkg-mode 1)'         # act
uvx elate -s s wait idle                      # synchronize
uvx elate -s s state                          # observe
uvx elate stop s                              # ALWAYS stop your sessions when done
```

Rules that prevent the most common mistakes:
- **Always name sessions** (`--name`), always `elate stop NAME` when done.
  `elate list` shows leftovers; stop them. `start` without `--name`
  auto-generates an `elate-<hex>` name (returned in the result); `start
  --name X --replace` stops and recreates a live `X`. `stop` is idempotent
  (stopping a missing session is a no-op success), and `stop --all` stops
  every running session at once.
- Most commands need `-s NAME` **before** the subcommand: `elate -s s eval …`.
- The sandbox `$HOME` is fake: create fixture files via `eval`
  (`(with-temp-file "~/f" …)`) so nothing touches the real home. There is
  **no network** inside the sandbox. For files a **subprocess** needs at
  spawn (e.g. shell rc files for shell-integration tests), use
  `start --home-seed DIR` — it copies a fixture tree into the fake `$HOME`
  *before* Emacs launches, keeping isolation (don't point `HOME` at a real
  dir).
- Every session has a **private scratch directory** for your setup files
  and artifacts: `elate -s NAME path` prints it (bare path — substitutes
  into shell commands), and in-Emacs code sees it as `$ELATE_SCRATCH`
  (`(getenv "ELATE_SCRATCH")`). Use it instead of a shared temp dir —
  concurrent agents sharing one scratchpad overwrite each other's files;
  the scratch dir is purged with the sandbox.
- Startup forms run **before `emacs-startup-hook`** (set vars an auto-launch
  hook reads): inline `--eval FORM`, or — to reuse the same setup across
  sessions instead of re-pasting it — `--eval-file PATH` (a forms file, no
  `load-path` side effects) and `--profile NAME`
  (`$XDG_CONFIG_HOME/elate/profiles/NAME.el`). Order:
  `--load` → `--eval-file` → `--profile` → `--eval` (so `--eval` overrides a
  profile).
- `--load PATH`: a **file** is loaded (and its directory put on
  `load-path`); a **directory** is only added to `load-path` — nothing in
  it is loaded, so `require`/load the feature yourself or pass the `.el`
  files. Use `--config clean-install --load pkg.el` to instead **install**
  the package for real (autoloads, Package-Requires, byte-compilation
  verified; see RECIPES.md).

## The loop: act → wait → state

Never sleep-and-poll. Never assume an effect happened — observe it.

1. **Act**: `keys` / `type` / `mouse` / `eval`.
2. **Wait** for the effect (exit 3 = timed out, the condition never held):
   ```sh
   uvx elate -s s wait stable --buffer '*shell*' --quiet-ms 300  # output settled
   uvx elate -s s wait idle                  # command loop went quiet
   uvx elate -s s wait text 'Compiled OK' --buffer '*compilation*' --timeout 30
   uvx elate -s s wait prompt                # a minibuffer prompt opened
   uvx elate -s s wait until '(eq major-mode (quote my-mode))' --timeout 5
   ```
   `wait text` takes a **Python** regexp (not elisp syntax!) and happily
   polls a buffer that does not exist yet.
   For **subprocess / REPL / terminal** output (comint, compilation, vterm,
   async LSP), reach for `wait stable` — it returns once the buffer's text
   has not changed for `--quiet-ms` ms, which is the "did the output stop?"
   question. `wait idle` is *command-loop* idle (its `idle` number is just
   seconds since the last activity — a big value is healthy, not a hang) and
   says nothing about whether output finished.
   For any condition that is neither text nor quiescence (a mode change, a
   marker position, process state), `wait until '<pred>'` polls an elisp
   predicate until non-nil and returns its value — never write an eval-poll
   loop. An elisp *error* from the predicate fails the wait immediately (so
   a typo can't hide until the deadline); wrap the form in `ignore-errors`
   if an error just means "not yet". `--buffer B` evaluates it in a buffer.
3. **Observe**: `state` is the one-call scene snapshot (buffer, mode, point,
   window layout, minibuffer prompt + completions, echo area, active popup
   kinds, *Messages* tail). When confused, run `state` first — it almost
   always explains what happened. Targeted reads: `buffer`, `messages`
   (delta since last call), `echo`, `popups`, `screenshot`, and `logs`
   (the Emacs stderr tail — module panics, GC/native-comp warnings, the
   fatal-signal line on a crash; works on dead/stopped sessions too).

## Key delivery: which mode when

`keys` takes Emacs kbd notation (`'C-x C-f'`, `'M-x foo RET'`, `'TAB'`).

| situation | use |
|---|---|
| normal key sequence that completes | `keys 'M-x foo RET'` (semantic, default) |
| sequence that **opens a prompt and leaves it open** | `keys 'C-x C-f' --events` |
| answering an already-open prompt | `type 'filename'` then `keys RET --events` |
| Emacs is wedged/busy, nothing responds | `interrupt` (raw C-g on TTY; signals a GUI Emacs) |
| literal text into a buffer | `type 'hello'` (or `eval '(insert …)'` for bulk) |
| drive a **subprocess** (shell/REPL): ^C, feed input | `send-process --char C-c` / `send-process 'cmd\n'` |

Why: semantic delivery runs `execute-kbd-macro`, which runs the keys
**through the command loop** — so they obey whatever keymaps are active.
In an evil buffer in *normal* state, `type "abc"` sends the commands `a`,
`b`, `c`, not the text; enter insert state (or use `--raw`) first. And it
does **not** block on an open minibuffer prompt — it exits the prompt with
empty input (bare `M-x` errors with "'' is not a valid command name").
`--events` queues on `unread-command-events` instead, so the prompt stays
open for you to inspect (`state` shows prompt + candidates) and answer.

- Because keys obey the focused buffer's keymaps, a buffer that intercepts
  keys can **swallow** one: a terminal emulator in char mode (vterm/eat,
  and the like) forwards most keystrokes to its PTY, and many special-mode
  buffers rebind keys, so a globally-bound key never runs as the command
  you meant. The result's `command` field is what the sequence resolves to
  in the focused buffer (`null` for an unbound key or a multi-command
  sequence) — assert on it instead of inferring from `delivered`:
  `keys '<f8>'` returning `"command": "term-send-raw"` tells you the
  terminal ate the key. When a command **must** run regardless of buffer
  bindings, call it directly with `eval`.
- `--raw` sends real terminal bytes via tmux: works even when Emacs is
  stuck, but rejects chords a terminal cannot encode (e.g. `C-%`) and does
  not exist for GUI sessions. To simply unwedge a busy Emacs, prefer
  `interrupt` (below) over raw key plumbing.
- A command that **rings the bell** aborts the whole semantic macro. elate
  reports the culprit — `key delivery aborted -- COMMAND rang the bell in
  BUFFER at point N` — so diagnose with `describe key …` / `messages`. To
  deliver *past* a spurious bell (e.g. evil insert off the prompt row),
  use `keys … --no-abort-on-bell` (queued via events, so asynchronous —
  follow with a `wait`).
- `send-process` writes straight to a buffer's subprocess
  (`process-send-string`), bypassing the command loop: `--char C-c`
  interrupts a job, `send-process 'cmd\n'` feeds a shell/REPL, `--file`
  seeds a large payload. `keys`/`type` drive Emacs; this drives the process.
  `--buffer NAME` targets *any* buffer with a live subprocess — a `shell`,
  `comint` REPL, or a terminal buffer (`term`/`vterm`/`eat`). To drive a
  program running inside a terminal buffer, send its bytes there directly,
  e.g. `send-process --buffer '*ghostel*' --char 'C-c'` then
  `send-process --buffer '*ghostel*' 'git status\n'`.
  Targeting rule: the buffer must have exactly **one** live process — none
  or several is an error naming the candidates, never a silent pick; pick
  one with `--process NAME` (by process name, buffer optional). The result
  echoes the chosen process's name and command line — check it when input
  seems to vanish: a package can also write through its **own** channel (a
  raw fd its Emacs process object doesn't front), in which case no process
  is the write path and you call the package's send function via `eval`
  instead.
- The sandboxed frame **never has real window-system focus** while an agent
  drives it, so code gated on focus (paste-on-focus, focus-dimming, a
  click-to-refocus mode) sees an unfocused frame and behaves differently.
  Don't fight it with window managers: inject focus with `focus in` /
  `send-events 'focus-in' …` (ordered with clicks/keys), and add
  `--set-focus-state` when the code reads `(frame-focus-state)` — that
  C-owned state cannot be moved from elisp, so the flag shims it. Worked
  example: the focus-vs-click recipe in RECIPES.md.
- If a semantic `keys` call times out, the sequence probably left Emacs
  reading input: retry with `--events`, or recover with `interrupt`.
- After an eval/keys timeout where Emacs stays busy (`info` shows
  `busy: true`): `interrupt` unblocks it — raw C-g on TTY; on GUI it
  breaks Emacs into the Lisp debugger with SIGUSR2 and unwinds it back to
  top level (`--signal usr2` instead stays in the debugger so `debug show`
  reveals where it was stuck). Never use `--signal int` to recover: SIGINT
  *terminates* a GUI-only Emacs. Stop the session only if it stays wedged
  after an interrupt.
- A session sitting in the Lisp debugger (code under test signalled and
  `*Backtrace*` popped up) is the opposite failure mode: it answers the
  channel and looks idle, but every key/eval lands inside the debugger.
  `info` reports `in_debugger`/`recursion_depth` and `state` reports
  `in-debugger`/`recursion-depth` (note the spelling difference), `wait
  idle` fails fast pointing there; `debug show` prints the backtrace,
  `debug abort` unwinds to top level with the session intact.

## Eval gotchas

```sh
uvx elate -s s eval '(my-fn 42)' --timeout 5
```
- Forms run in the **selected window's buffer** by default (so
  `current-buffer`/point see what's on screen); target another buffer
  with `--buffer NAME` or wrap the form:
  `eval '(with-current-buffer "*scratch*" (insert "hi"))'`.
- Shell single-quoting cannot contain `'` — so `#'fn` and `'symbol`
  break as argv. Don't rewrite the elisp: put the forms in a file and
  `eval --file forms.el` (or `--file -` for stdin) to run them verbatim.
- Errors come back structured: `error` + `backtrace` + the *Messages*
  delta; exit code 1.
- Printed values are truncated at 64 KiB (`truncated: true` +
  `value-length` in `--json`). Don't pass huge strings as arguments
  either (~1 MiB argv limit) — write a temp file and `load` it.
- `--timeout` (default 15s) bounds the eval both in-Emacs and at the
  client; **raise it** for a legitimately slow form (spawn, compile,
  package install) rather than letting it abort. A form stuck in
  `sleep-for`/`sit-for`/process waits is interrupted by `--timeout`; a
  synchronous `call-process` or a hard elisp loop ignores it — recover with
  `interrupt`.
- Predicates often return a truthy *value*, not `t`: `(process-live-p p)`
  yields the status tail `(run open listen connect stop)`, not `t`. Wrap
  with `(and … t)` (or `(if … t nil)`) when you want a clean boolean back.
- The default `value` is a **printed sexp string**. For structured probes
  add `--json-result` (real JSON in `value`) or `--raw` (bare value, no
  envelope) — see "JSON output and exit codes" below; never regex a
  printed plist.
- If a still-busy timeout needs *where* it is stuck, add `--on-timeout
  sample`: it attaches a thread backtrace of the wedged Emacs (macOS
  `sample`; Linux eu-stack/gdb) to the timeout error as `sample`.
- If a form **crashes** Emacs, `eval` returns `session_died: true` with the
  fatal `signal` and the OS `crash_report` path (instead of an opaque
  transport error). `wait dead` blocks until the session exits and returns
  the same; `info`/`list` show a dead session's signal (e.g. `dead
  (SIGABRT)`). Read the stderr with `logs`.

## Verify rendering structurally, not by eyeballing

Screenshots are for humans; assertions should read structure:

```sh
uvx elate -s s buffer demo.el --props        # RLE face/property runs + overlays
uvx elate -s s faces-at 3:14 --buffer demo.el  # LINE:COL (1-based:0-based)
uvx elate -s s faces-at --pos 420 --run 3    # by position; 3 adjacent cells at once
uvx elate -s s popups                        # transient/which-key/corfu/childframes as text
```
- `--props` runs `font-lock-ensure` first, so never-displayed buffers
  fontify correctly.
- `faces-at` reports every text property at the point, with **values**
  (`property-values`: e.g. your own `my-prompt=t` vs `my-count=42`) — use it
  to assert a package's custom text properties instead of repeated
  `eval (get-text-property …)`. Address by `LINE:COL` or `--pos N` (a buffer
  position, handy from elisp); `--run K` dumps K adjacent cells in one call
  (compare a typed cell against the dimmed suggestion beside it). Over MCP it
  is `elate_faces_at` (`pos` / `run`).
- `state`'s `popups` field tells you when a `popups` capture is worthwhile.
- TTY `screenshot` prints the rendered screen as text (works post-mortem on
  a crashed Emacs); GUI `screenshot -o x.png` writes a PNG you can Read.
- GUI capture on **macOS needs an awake, unlocked display** (`--headless`/Xvfb
  is Linux-only). A failed capture reports a `reason` in the error JSON —
  `locked` / `display_asleep` / `window_gone` / `permission` — so a locked or
  asleep Mac is distinguishable from a missing Screen Recording grant. For an
  unattended/CI Mac, keep a real GUI login awake and unlocked (auto-login +
  disable screen-lock + `caffeinate -dimsu`); a backgrounded `launchd` runner
  has no GUI session and always captures black.

## Trace internal functions — don't hand-roll advice spies

To see which internal functions ran, in what order, with what args (what
bytes hit the PTY? why did the hook fire twice?), use the built-in tracer
instead of writing `advice-add` wrappers that record calls:

```sh
uvx elate -s s trace on my-pkg--send my-pkg--filter   # start recording
uvx elate -s s keys 'x'                               # drive the session
uvx elate -s s trace read                             # calls+args+returns, in order; clears
uvx elate -s s trace off                              # untrace all
```

Each call records nesting, arguments, and return value. `read` clears the
log, so each read sees only new calls (`--keep` to accumulate). Trace a
handful of named functions, not a whole package — tracing is per-function.
With `--json`, `read` also carries structured `records` — `{fn, depth,
args, ret, error}` per call, in completion order (a nested call precedes
its caller; depth 1 = outermost) — assert on those, never regex the raw
`output` text.

## Tests, lint, profile, bench — fresh sessions only

Results depend on session history (earlier loads/lints/evals skew them).
For an authoritative verdict, use a **fresh throwaway session** per run.

```sh
uvx elate -s s test --load-file tests/my-tests.el          # all tests
uvx elate -s s test 'my-pkg-' --timeout 30                 # name regexp
uvx elate -s s test '(tag ui)'                             # any ERT selector
```
ERT runs **inside the live session** — real redisplay, real minibuffer —
and returns structured per-test results (status, duration, messages,
condition, backtrace). Exit 0 = all expected, 1 = unexpected/timed out.
A timed-out run returns partial results and names the interrupted test.

```sh
uvx elate -s s lint my-pkg.el
```
**Lint executes the files' compile-time code** (`eval-when-compile`, macro
expansion, top-level `require`) in the session — that is inherent to
in-session linting. Lint untrusted files only in a throwaway session you
stop afterwards. Exit 1 on any finding.

```sh
uvx elate -s s profile run '(my-pkg-heavy)' --timeout 30   # start→eval→stop→report
uvx elate -s s bench '(my-pkg-parse s)' -n 100
```

## From interactive session to regression test

Everything you did in a session is in its transcript. Turn it into a
replayable script:

```sh
uvx elate -s s export-script --clean -o scenario.json  # --clean strips transient temp paths
# edit: observations were exported as skipped assertion stubs — fill them in
uvx elate run scenario.json                     # fresh session, steps, exit 0/1
uvx elate run scenario.json --keep-going --format junit   # every check + a CI report
uvx elate matrix scenario.json --emacs emacs30,emacs31 --param shell=/bin/bash,/bin/zsh
```
`elate run` boots a fresh session per run and exits 0/1 — the CI entry
point; by default it stops at the first failure (embedding a state
snapshot) and purges its sandbox on success (failed runs are kept —
`purge --glob 'run-*'` sweeps them). For a regression matrix that reports
what does **not** work: `--keep-going` runs every step; a step marked
`"expect": "fail"` reports `xfail` (a known break that never gates, and
flips to a run-failing `xpass` if it starts passing); a `{"group": "dw"}`
marker names verdicts (`dw: PASS · u: XFAIL`); `--format junit`/`tap`
emits CI-ready output; and `{{var}}` + `--set` / `matrix --param` drive
one scenario across many shells/configs. When several variables must move
together (a shell + its aliases + its setup snippet), declare a
`"variants"` block of named binding sets: `matrix` runs every variant by
default, `run --variant NAME` picks one. `--keep`/`--keep-on-failure`
keep the session for inspection. Scenario format, every step and
assertion kind: see [SCRIPTING.md](SCRIPTING.md).

## JSON output and exit codes

Output is the human table on a terminal and **JSON when stdout is not a TTY**
— i.e. you get clean JSON automatically when piping or running headless, no
flag needed. Force it either way with the global `--json` / `--human` (before
the subcommand):

```sh
uvx elate -s s eval '(emacs-version)' | cat     # piped → {"ok": true, "value": …}
uvx elate --json -s s eval '(emacs-version)'    # force JSON even on a terminal
uvx elate --human -s s list | less              # force the table even when piped
```
Parse the JSON — don't scrape the human table (`eval --json` gives `value`,
`value-length`, `truncated`, `error`, `backtrace`, `messages`). Errors embed a
state snapshot so you see *why*. Exit codes: **0** success, **1** error (elisp
errors, test failures, lint findings), **2** CLI usage error, **3** `wait`
timeout. Branch on them in shell loops — `elate -s s eval '(my-check)' &&
next-step` needs no output parsing at all.

**Don't double-parse eval results.** By default eval's `value` is the
*printed sexp as a string* — JSON tooling can't take it apart, and regex/
string surgery on it is a bug farm. Instead:

```sh
# Real JSON inside the envelope: jq works on the value itself.
uvx elate --json -s s eval --json-result \
  '(list :mode major-mode :ro buffer-read-only :point (point))'
# → "value": {"mode":"lisp-mode","ro":false,"point":316}, "value-encoding":"json"

# No envelope at all: the bare value, straight into shell tests.
[ "$(uvx elate -s s eval --raw 'major-mode')" = lisp-mode ]
uvx elate -s s eval --raw --json-result '(list :a 1)'   # bare {"a":1}

# Any command: print one envelope field bare (global flag).
uvx elate --field name -s s info
```

`--json-result` converts in-session: `nil` → `null` (by rule — elisp can't
tell nil/false/empty-list apart), `t` → `true`, symbols → their names,
keyword plists/alists/hash-tables → objects, other lists and vectors →
arrays. A value with no faithful JSON shape (buffers, markers, circular
structures) falls back to the printed string — **check `value-encoding`**
(`"json"` vs `"printed"`), never guess. With `--raw`/`--field`, a failed
command prints *nothing* to stdout (error on stderr, normal exit code), so
`$(…)` substitutions compare against emptiness, not an error blob. And
prefer the structured commands (`state`, `faces-at`, `info`) over eval
probes — they already return real JSON.

## GUI sessions (when TTY isn't enough)

```sh
uvx elate start --name g --ui gui --size 100x35 --load ./my-pkg.el
uvx elate -s g screenshot -o shot.png          # real PNG; Read it
uvx elate -s g mouse click --buffer '*menu*' --line 3 --col 5
```
Mouse is semantic (a real event sequence through the command loop — no OS
permissions) and works on TTY too. GUI differences: no `--raw` channel;
`type` is capped at 10,000 chars; macOS PNG capture needs the Screen
Recording permission (elate preflights and reports instead of prompting).
On Linux/CI add `--headless` for a private Xvfb.

Three GUI-level commands reach *below* the command loop, where
`special-event-map` code (drag-and-drop in particular) lives:

```sh
uvx elate -s g window-info                                # X11 ids as ints, pixel edges per window
uvx elate -s g pointer warp --buffer '*scratch*' --line 3 --col 5
uvx elate -s g dnd drop --uris file:///tmp/a,file:///tmp/b --buffer dired
```

`pointer` moves/reads the REAL pointer (`mouse` synthesizes command-loop
events; dnd code reads the live pointer instead, so only `pointer` can
target it). `dnd drop` runs a full XDND exchange from an external X
client — the drop travels Emacs's C event dispatch and x-dnd.el exactly
like a user drag; X11 sessions only (`--ui gui --headless` on Linux),
needs `pip install 'elate[dnd]'`. A refused drop returns
`status: "rejected"` with exit 0; every result carries `in-debugger` —
true with `finished: false` means the package's drop handler errored
(inspect with `debug show`). `--hover` asserts drag feedback without
dropping. After a drop, `wait stable --buffer B` is the settle
primitive.

## When the MCP server fits better

This skill drives the CLI via shell — the full feature set with near-zero
ambient cost. Prefer the MCP server only when there is **no shell access**
(e.g. desktop apps), or when you want GUI screenshots returned **inline as
images** instead of PNG files to read. If `elate_*` MCP tools are already
available in your session (someone registered the server — the plugin is
CLI-first and does not register it for you), use them directly — do **not**
register a duplicate; otherwise register it with
`claude mcp add elate -- uvx elate mcp`. The 35 `elate_*` tools cover the core surface (`attach`, `resize`,
`prune`, `stderr`, `export-script`, `snap`, `matrix`, `install`, and `update` stay CLI-only); `prune`
aliases `purge` and `stderr` aliases `logs`. Sessions are shared
between both (same names, same sandboxes), so you can mix.

## Cleanup checklist (always)

```sh
uvx elate list            # anything still running?
uvx elate stop NAME       # stop every session you started
```
`elate stop` ends the session's processes; a crashed TTY Emacs keeps a
dead pane for post-mortem `screenshot` until stopped. A stopped session's
sandbox dir — and its `stopped` entry in `elate list` (which shows each
one's idle age) — stays behind on purpose (transcripts outlive the Emacs).
Stopped sandboxes are inert; when the transcripts are no longer needed,
`elate purge NAME…` (or `elate purge --all`; `prune` is the same command)
deletes them — purge never touches a running session. `elate run` purges
its own throwaway sandbox on success, so only failed runs pile up — named
`run-<scenario>-<hex>` after the scenario file, so a post-mortem is
findable: sweep them by pattern with `elate purge --glob 'run-*'` (or a
targeted `--glob 'run-my-scen-*'`, or `--name-prefix run-`). During a
long parallel run, GC only the stale ones with `elate
purge --all --stopped-older-than 1h`, and preview which they are with
`elate list --older-than 1h`. Sandboxes live under `~/.cache/elate/sessions/<name>`
(`$ELATE_HOME` overrides the base).

**Several agents on one machine:** tag your sessions at start and manage
only your own —

```sh
uvx elate start --name rx-a --owner agent3 --ttl 30m
uvx elate list --owner agent3         # just mine
uvx elate stop --owner agent3         # stop all of mine (also: --glob/--name-prefix)
uvx elate purge --owner agent3        # delete my stopped sandboxes
```

`--ttl DUR` is the crash insurance: a session idle (no commands) past its
TTL is stopped *and* purged by an opportunistic sweep that any later elate
command runs — so sessions leaked by a crashed agent reap themselves. Only
TTL'd sessions are ever swept; the session a command targets is exempt.
