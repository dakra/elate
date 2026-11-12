# Observe and verify: eval, output, rendering, trace, tests

Reading results reliably: waits, eval and its values, JSON output and exit codes,
structural rendering checks, the function tracer, and ERT/lint/profile/
bench. Every flag: REFERENCE.md.

Contents: waiting for an effect · eval · JSON output and exit codes ·
verify rendering structurally · trace internal functions · tests, lint,
profile, bench

## Waiting for an effect

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

## Eval

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
- A/B one definition: `eval --file pkg.el --defun my-fn --rev main`
  evaluates only `my-fn` as it is at that git revision (nothing else in
  the file loads); `eval --file pkg.el --defun my-fn` restores the
  working-tree version. `--rev` alone evaluates the whole file at REV.
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
- `state`'s `windows-appeared` names buffers newly shown in a window since
  the previous `state` call on this session (a `*Warnings*` split from a
  stray warning, a `*Backtrace*`, a popup; a split from a `--load` file at
  startup is news to the first call), and `alerts` lists visible `*Warnings*`/
  `*Backtrace*` windows as long as they are up — a layout that changed
  under you is named, not silently measured.
- TTY `screenshot` prints the rendered screen as text (works post-mortem on
  a crashed Emacs); GUI `screenshot -o x.png` writes a PNG you can Read.
- GUI captures (PNG, regions, pixel colors) and their macOS requirements:
  GUI.md.

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
