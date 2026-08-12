# elate — agent notes

elate spawns disposable, sandboxed Emacs sessions (terminal or GUI) and
drives them with keys, mouse, and elisp — for interactively testing Emacs
packages where `emacs --batch`/ERT can't see (keybindings, prompts,
popups, redisplay). Run it as `uvx elate …` (PyPI) or `uv run elate …` in
this checkout. Full guidance: `skills/elate/SKILL.md` (+ REFERENCE.md,
RECIPES.md, SCRIPTING.md); README has the human-oriented tour.

## Driving elate

- Lifecycle: `elate start --name s --load ./pkg.el` … `elate stop s`.
  Always name sessions; **always stop every session you started**
  (`elate list` shows leftovers). Most commands take `-s NAME` before the
  subcommand. Stopped sessions keep an inert sandbox (and a `stopped`
  list entry) at `~/.cache/elate/sessions/<name>` for their transcripts —
  `elate purge NAME…`/`elate purge --all` deletes them (never running
  sessions; `prune` is an alias); `elate purge --all --stopped-older-than
  1h` GCs only stale ones (`elate list --older-than 1h` previews them).
  `start` auto-names when `--name` is omitted; `start --name X --replace`
  recreates a live `X`; `stop` is idempotent (missing session = no-op) and
  `stop --all` stops every running session.
- The loop is **act → wait → observe**: `keys`/`type`/`mouse`/`eval`/
  `send-process` (and `focus`/`send-events` for window-system focus events,
  ordered against clicks/keys), then `wait stable --buffer B --quiet-ms N`
  (subprocess/REPL output settled) / `wait idle` (command-loop idle) /
  `wait text REGEXP` / `wait prompt` / `wait until '<elisp-pred>'` (any
  other condition — never sleep-and-poll or eval-poll), then `state`
  (one-call scene snapshot — run it first when confused) or
  `buffer`/`messages`/`faces-at`/`popups`/`screenshot`.
- Key delivery: semantic `keys 'M-x foo RET'` by default. Semantic keys run
  **through the command loop**, so they obey the active keymaps (e.g. in evil
  *normal* state `type "abc"` sends commands, not text), and a command that
  rings the bell aborts the whole macro — use `keys … --no-abort-on-bell`
  (or `--events`) to deliver past a bell. Because they obey the buffer's
  keymaps, a buffer that intercepts keys (a terminal emulator in char mode,
  many special-mode buffers) can **swallow** one and your global command
  never runs; the result's `command` field is what the keys resolved to in
  the focused buffer (`null` for an unbound key or a multi-command sequence)
  — check it, and `eval` a command directly when it must run regardless of
  bindings. A sequence that opens a minibuffer
  prompt and leaves it open needs `keys … --events` (queued); unwedging a
  stuck Emacs (`info` shows `busy: true`) needs `interrupt` (raw C-g on TTY;
  on GUI it breaks Emacs into the Lisp debugger and unwinds it back to top
  level — `--signal usr2` to stay in the debugger and read `debug show`).
  A session parked in the Lisp debugger (`in_debugger` in `info`,
  `in-debugger` in `state`/the idle probe) answers the channel but eats
  every key: `debug abort` unwinds it.
- Drive a **subprocess** (shell/REPL/terminal) with `send-process`: it writes
  straight to the buffer's process (`send-process --char C-c` interrupts,
  `send-process 'cmd\n'` feeds input) — `keys`/`type` drive Emacs, this drives
  the process. A buffer with several live processes is an error naming them
  (pick one with `--process NAME`); the result echoes the target's name +
  command line — check it when input vanishes, and `eval` the package's own
  send function when it writes through a raw fd no process fronts. To see
  which internal functions ran (args, order), use `trace on FN…` / `trace
  read` instead of advice spies — `read`'s JSON carries structured
  `records` (`{fn, depth, args, ret, error}` per call, completion order;
  assert on those, never regex the raw text). `faces-at --pos N` /
  `--run K` reads cells by position / a run at once.
- Below the command loop (GUI only): `window-info` returns per-frame X11
  window ids **as ints** + absolute pixel edges per window; `pointer
  warp/query` drives the REAL pointer (`mouse` synthesizes command-loop
  events — dnd code reads the live pointer instead); `dnd drop --uris
  file:///a,file:///b --buffer B` runs a full XDND exchange from an
  external X client through C dispatch + x-dnd.el (X11 sessions only,
  needs the `elate[dnd]` extra; rejected drops return `status:
  "rejected"` with exit 0; `in-debugger: true` + `finished: false` means
  the drop handler errored — `debug show`).
- Eval forms run in the **selected window's buffer** (or `--buffer NAME`),
  so `current-buffer`/point probes see what is on screen. Output truncates
  at 64 KiB. `wait text` patterns are **Python** regexps, not elisp.
  The default `value` is a printed sexp string — for structured probes use
  `eval --json-result` (real JSON in `value`; fallback flagged by
  `value-encoding`), `eval --raw` (bare value, no envelope, stdout empty on
  error), or the global `--field NAME` (one envelope field bare) — never
  regex a printed plist. Quote-heavy source (`#'fn`, `'sym`) that shell
  single-quoting would mangle: `eval --file forms.el` (or `--file -` for
  stdin) runs it verbatim.
- Crashes/hangs: a form that crashes Emacs comes back as `session_died`
  with the fatal `signal` + OS `crash_report` path (also via `wait dead` /
  `info` / `list`, which renders `dead (SIGABRT)`); `eval --on-timeout
  sample` attaches a thread backtrace of a still-wedged Emacs; `logs`
  (alias `stderr`) tails the Emacs stderr, including the fatal-signal line.
- Sandbox `$HOME` is fake. For files a **subprocess** needs at spawn (shell
  rc files), `start --home-seed DIR` copies a fixture tree in before launch.
  Put your own setup files/artifacts in the session's private scratch dir:
  `elate -s NAME path` prints it; in-Emacs code sees `$ELATE_SCRATCH`.
  Several agents on one machine: `start --owner ME --ttl 30m`, then
  `list/stop/purge --owner ME` (a session idle past its `--ttl` is reaped
  automatically).
  Startup forms run before `emacs-startup-hook`: inline `--eval`, or reuse
  `--eval-file PATH` / `--profile NAME` (`$XDG_CONFIG_HOME/elate/profiles/`).
- Tests/lint/profile/bench want a **fresh throwaway session** (results
  depend on session history), and `lint` **executes compile-time code** —
  never lint untrusted files in a session you keep.
- Output is the human table on a terminal and **JSON when piped** (i.e. for
  you); force either with `--json` / `--human`. Exit codes: 0 ok, 1 error,
  2 usage, 3 wait-timeout.
- Regression flow: `export-script` a session → edit assertions →
  `elate run scenario.json`, exit 0/1 (format: `skills/elate/SCRIPTING.md`).
  Run it across Emacs versions with `elate matrix --emacs-glob '…' s.json`.
  Prefer this declarative path for anything repeatable; it's the artifact
  you commit to a project's CI.
- No shell available? Use the MCP server instead: `uvx elate mcp` (stdio;
  per-harness registration: README "Using elate from AI harnesses").

## Contributing to elate

- `uv run pytest` (integration tests spawn real Emacs+tmux; they skip if
  either is missing). `uv run pytest -k kbd` for units only.
- Before changing agent elisp or session code, read the existing patterns
  carefully: agent RPC replies are sanitized/encoded centrally
  (`elate--encode`), MCP tools run threaded with schema-bounded timeouts,
  and errors use one flat shape with an embedded state snapshot.
- `skills/elate/REFERENCE.md` is generated: edit the CLI, then run
  `uv run python scripts/gen-skill-ref.py` (CI fails on drift).
- Never `git commit`/`push` on the maintainer's behalf.
