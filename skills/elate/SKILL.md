---
# Keep this frontmatter to plain `key: value` scalars (indented
# continuation lines are fine): tests/test_skill.py parses it without YAML.
name: elate
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
observation over them. Run it with `uvx elate …` (PyPI; no install step).
In a checkout of the elate repo itself, use `uv run elate …` instead.

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
  `elate list` shows leftovers; stop them.
- Most commands need `-s NAME` **before** the subcommand: `elate -s s eval …`.
- The sandbox `$HOME` is fake: create fixture files via `eval`
  (`(with-temp-file "~/f" …)`) so nothing touches the real home. There is
  **no network** inside the sandbox.
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
   uvx elate -s s wait idle                  # command loop went quiet
   uvx elate -s s wait text 'Compiled OK' --buffer '*compilation*' --timeout 30
   uvx elate -s s wait prompt                # a minibuffer prompt opened
   ```
   `wait text` takes a **Python** regexp (not elisp syntax!) and happily
   polls a buffer that does not exist yet.
3. **Observe**: `state` is the one-call scene snapshot (buffer, mode, point,
   window layout, minibuffer prompt + completions, echo area, active popup
   kinds, *Messages* tail). When confused, run `state` first — it almost
   always explains what happened. Targeted reads: `buffer`, `messages`
   (delta since last call), `echo`, `popups`, `screenshot`.

## Key delivery: which mode when

`keys` takes Emacs kbd notation (`'C-x C-f'`, `'M-x foo RET'`, `'TAB'`).

| situation | use |
|---|---|
| normal key sequence that completes | `keys 'M-x foo RET'` (semantic, default) |
| sequence that **opens a prompt and leaves it open** | `keys 'C-x C-f' --events` |
| answering an already-open prompt | `type 'filename'` then `keys RET --events` |
| Emacs is wedged/busy, nothing responds | `keys C-g --raw` (TTY only) |
| literal text into a buffer | `type 'hello'` (or `eval '(insert …)'` for bulk) |

Why: semantic delivery runs `execute-kbd-macro`, which does **not** block on
an open minibuffer prompt — it exits the prompt with empty input (bare
`M-x` errors with "'' is not a valid command name"). `--events` queues on
`unread-command-events` instead, so the prompt stays open for you to
inspect (`state` shows prompt + candidates) and answer.

- `--raw` sends real terminal bytes via tmux: works even when Emacs is
  stuck (the unwedging tool), but rejects chords a terminal cannot encode
  (e.g. `C-%`) and does not exist for GUI sessions.
- A semantic `keys` error "Keyboard macro terminated by a command ringing
  the bell" means the sequence hit an **undefined key** or a command
  signalled. Diagnose with `describe key 'C-c g'` (bound? to what?) and
  `messages`.
- If a semantic `keys` call times out, the sequence probably left Emacs
  reading input: retry with `--events`, or recover with `C-g --raw`.
- After an eval/keys timeout where Emacs stays busy: `keys C-g --raw`
  usually unblocks it. A GUI session has no raw channel — stop it instead.

## Eval gotchas

```sh
uvx elate -s s eval '(my-fn 42)' --timeout 5
```
- Forms do **not** run in the selected window's buffer (they run in the
  server's context). Anything buffer-sensitive must wrap itself:
  `eval '(with-current-buffer "*scratch*" (insert "hi"))'`.
- Errors come back structured: `error` + `backtrace` + the *Messages*
  delta; exit code 1.
- Printed values are truncated at 64 KiB (`truncated: true` +
  `value-length` in `--json`). Don't pass huge strings as arguments
  either (~1 MiB argv limit) — write a temp file and `load` it.
- A form stuck in `sleep-for`/`sit-for`/process waits is interrupted by
  `--timeout`; a hard elisp loop only ends via `C-g --raw`.

## Verify rendering structurally, not by eyeballing

Screenshots are for humans; assertions should read structure:

```sh
uvx elate -s s buffer demo.el --props        # RLE face/property runs + overlays
uvx elate -s s faces-at 3:14 --buffer demo.el  # LINE:COL (1-based:0-based)
uvx elate -s s popups                        # transient/which-key/corfu/childframes as text
```
- `--props` runs `font-lock-ensure` first, so never-displayed buffers
  fontify correctly.
- `state`'s `popups` field tells you when a `popups` capture is worthwhile.
- TTY `screenshot` prints the rendered screen as text (works post-mortem on
  a crashed Emacs); GUI `screenshot -o x.png` writes a PNG you can Read.

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
uvx elate -s s export-script -o scenario.json   # works on stopped sessions too
# edit: observations were exported as skipped assertion stubs — fill them in
uvx elate run scenario.json                     # fresh session, steps, exit 0/1
uvx elate matrix --emacs-glob '/opt/emacs-*/bin/emacs' scenario.json
```
`elate run` boots a fresh session per run, stops at the first failure
(embedding a state snapshot), and exits 0/1 — the CI entry point.
`--keep-on-failure` keeps the session for inspection. Scenario format,
every step and assertion kind: see [SCRIPTING.md](SCRIPTING.md).

## JSON output and exit codes

Every command takes a global `--json` (before the subcommand):

```sh
uvx elate --json -s s eval '(emacs-version)'   # {"ok": true, "value": …}
```
Errors embed a state snapshot so you see *why*. Exit codes: **0** success,
**1** error (elisp errors, test failures, lint findings), **2** CLI usage
error, **3** `wait` timeout. Branch on them in shell loops.

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

## When the MCP server fits better

This skill drives the CLI via shell — the full feature set with near-zero
ambient cost. Prefer the MCP server only when there is **no shell access**
(e.g. desktop apps), or when you want GUI screenshots returned **inline as
images** instead of PNG files to read. If `elate_*` MCP tools are already
available in your session (the Claude Code plugin registers the server
automatically), use them directly — do **not** register a duplicate;
otherwise the server can be registered with
`claude mcp add elate -- uvx elate mcp`. The 22 `elate_*` tools cover the core surface (`purge`,
`resize`, `faces-at`, `export-script`, `snap`, and `matrix` stay
CLI-only); sessions are shared between both (same names, same sandboxes),
so you can mix.

## Cleanup checklist (always)

```sh
uvx elate list            # anything still running?
uvx elate stop NAME       # stop every session you started
```
`elate stop` ends the session's processes; a crashed TTY Emacs keeps a
dead pane for post-mortem `screenshot` until stopped. A stopped session's
sandbox dir — and its `stopped` entry in `elate list` — stays behind on
purpose (transcripts outlive the Emacs). Stopped sandboxes are inert;
when the transcripts are no longer needed, `elate purge NAME…` (or
`elate purge --all`) deletes them — purge never touches a running
session. Sandboxes live under `~/.cache/elate/sessions/<name>`
(`$ELATE_HOME` overrides the base).
