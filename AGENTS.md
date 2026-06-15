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
  sessions); `elate purge --all --stopped-older-than 1h` GCs only stale
  ones (`elate list` shows each stopped session's idle age).
- The loop is **act → wait → observe**: `keys`/`type`/`mouse`/`eval`/
  `send-process` (and `focus`/`send-events` for window-system focus events,
  ordered against clicks/keys), then `wait stable --buffer B --quiet-ms N`
  (subprocess/REPL output settled) / `wait idle` (command-loop idle) /
  `wait text REGEXP` / `wait prompt` (never sleep-and-poll), then `state`
  (one-call scene snapshot — run it first when confused) or
  `buffer`/`messages`/`faces-at`/`popups`/`screenshot`.
- Key delivery: semantic `keys 'M-x foo RET'` by default. Semantic keys run
  **through the command loop**, so they obey the active keymaps (e.g. in evil
  *normal* state `type "abc"` sends commands, not text), and a command that
  rings the bell aborts the whole macro — use `keys … --no-abort-on-bell`
  (or `--events`) to deliver past a bell. A sequence that opens a minibuffer
  prompt and leaves it open needs `keys … --events` (queued); unwedging a
  stuck Emacs needs `keys C-g --raw` (TTY only).
- Drive a **subprocess** (shell/REPL/terminal) with `send-process`: it writes
  straight to the buffer's process (`send-process --char C-c` interrupts,
  `send-process 'cmd\n'` feeds input) — `keys`/`type` drive Emacs, this drives
  the process. `faces-at --pos N` / `--run K` reads cells by position / a run
  at once.
- Eval forms don't run in the selected window's buffer — wrap
  buffer-mutating forms in `(with-current-buffer …)`. Output truncates at
  64 KiB. `wait text` patterns are **Python** regexps, not elisp.
- Sandbox `$HOME` is fake. For files a **subprocess** needs at spawn (shell
  rc files), `start --home-seed DIR` copies a fixture tree in before launch.
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
