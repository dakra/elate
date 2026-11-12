---
# Keep this frontmatter to plain `key: value` scalars (indented
# continuation lines are fine): tests/test_skill.py parses it without YAML.
# `version` is the skill CONTENT version: bump it only in releases whose
# skill files change (tests/skill_content_version.txt pins this); `elate
# start` compares it against installed copies to nudge about staleness.
name: elate
version: 0.17.0
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

This file is the core: the loop, the traps that bite before you would
think to look anything up, and a map of where everything else is. **Before
you build anything nontrivial, scan "Find the right tool" below** — most
"elate can't do X" moments are a feature listed there. Topic files (read
on demand):

- [INPUT.md](INPUT.md) — key delivery modes, mouse, `send-process`, focus, wedged Emacs / debugger
- [INSPECT.md](INSPECT.md) — eval details, JSON output, structural rendering checks, `trace`, ERT/lint/profile/bench
- [GUI.md](GUI.md) — GUI sessions: capture, placement, region/pixel checks, native menus, pointer, drag-and-drop
- [SESSIONS.md](SESSIONS.md) — startup options, fixtures, sandboxes and disk, several agents on one machine
- [SCRIPTING.md](SCRIPTING.md) — scenario files (JSON or `.eld`), assertions, templates, `run`/`matrix`, CI
- [RECIPES.md](RECIPES.md) — worked examples: transient menus, font-lock, dired, clean-install, focus-vs-click
- [REFERENCE.md](REFERENCE.md) — every command, option, default (generated from the CLI)

## Quickstart

```sh
uvx elate start --name s --load ./my-pkg.el   # sandboxed TTY Emacs, pkg on load-path
uvx elate -s s eval '(my-pkg-mode 1)'         # act
uvx elate -s s wait idle                      # synchronize
uvx elate -s s state                          # observe
uvx elate stop s                              # ALWAYS stop your sessions when done
```

- **Always name sessions** (`--name`) and `elate stop NAME` when done;
  `elate list` shows leftovers.
- Most commands need `-s NAME` **before** the subcommand: `elate -s s eval …`.
- The sandbox `$HOME` is fake and there is **no network**: create fixture
  files via `eval` (`(with-temp-file "~/f" …)`) or `start --home-seed DIR`.
  Put your own setup files in the session's private scratch dir (`elate -s
  NAME path`; `$ELATE_SCRATCH` inside Emacs), not a shared temp dir.
- `--load FILE` loads it (directory on `load-path`); `--load DIR` only
  extends `load-path`. Startup forms (`--eval`, `--eval-file`, `--profile`)
  run before `emacs-startup-hook`; if startup code signals, `start` exits 1
  with `init_error` (the session stays up — stop it). Details: SESSIONS.md.

## The loop: act → wait → state

Never sleep-and-poll. Never assume an effect happened — observe it.

1. **Act**: `keys` / `type` / `mouse` / `eval` / `send-process`.
2. **Wait** for the effect (exit 3 = timed out, the condition never held):
   ```sh
   uvx elate -s s wait stable --buffer '*shell*' --quiet-ms 300  # output settled
   uvx elate -s s wait idle                  # command loop went quiet
   uvx elate -s s wait text 'Compiled OK' --buffer '*compilation*' --timeout 30
   uvx elate -s s wait prompt                # a minibuffer prompt opened
   uvx elate -s s wait until '(eq major-mode (quote my-mode))' --timeout 5
   ```
   `wait text` takes a **Python** regexp (not elisp syntax!) and happily
   polls a buffer that does not exist yet. For **subprocess / REPL /
   terminal** output, `wait stable` is the "did the output stop?" question;
   `wait idle` is *command-loop* idle and says nothing about output. Any
   other condition: `wait until '<pred>'` — never write an eval-poll loop.
   An elisp *error* from the predicate fails the wait at once; wrap it in
   `ignore-errors` if an error just means "not yet".
3. **Observe**: `state` is the one-call scene snapshot (buffer, mode, point,
   window layout, minibuffer prompt + completions, echo area, popup kinds,
   *Messages* tail, windows that appeared since the last call). When
   confused, run `state` first — it almost always explains what happened.
   Targeted reads: `buffer`, `messages` (delta since last call), `echo`,
   `popups`, `screenshot`, `logs` (Emacs stderr, also after a crash).

## Traps that bite first

| situation | use |
|---|---|
| normal key sequence that completes | `keys 'M-x foo RET'` (semantic, default) |
| sequence that **opens a prompt and leaves it open** | `keys 'C-x C-f' --events` |
| answering an already-open prompt | `type 'filename'` then `keys RET --events` |
| Emacs is wedged/busy, nothing responds | `interrupt` (raw C-g on TTY; signals a GUI Emacs) |
| literal text into a buffer | `type 'hello'` (or `eval '(insert …)'` for bulk) |
| drive a **subprocess** (shell/REPL): ^C, feed input | `send-process --char C-c` / `send-process 'cmd\n'` |

- Semantic `keys` run as a keyboard macro **through the command loop**:
  they obey the active keymaps (evil *normal* state turns `type "abc"`
  into commands), exit an open minibuffer prompt with empty input, and a
  command that **rings the bell aborts the rest** (the error names it;
  `--no-abort-on-bell` delivers past it). Code that reads keys itself
  (`read-key`) needs `--events`.
- A buffer that intercepts keys (a terminal emulator in char mode, many
  special modes) can **swallow** one: check the result's `command` field
  (what the keys resolved to), and `eval` a command that must run anyway.
- The frame **never has real window-system focus**; inject it with `focus
  in` (`--set-focus-state` for `frame-focus-state`) instead of fighting
  window managers.
- A session in the **Lisp debugger** answers but eats every key: `info`
  shows `in_debugger`; `debug show` / `debug abort`. `eval` forms run in
  the **selected window's buffer**; quote-heavy elisp goes through `eval
  --file forms.el` (shell quoting mangles `#'fn`).
- Tests, lint, profile, bench: use a **fresh session** per verdict (results
  depend on session history), and `lint` **executes compile-time code**.

Details for all of these: INPUT.md and INSPECT.md.

## Find the right tool

| you want to… / you hit… | use | read |
|---|---|---|
| keys vanish, a prompt closes, a bell aborts | `keys --events`, result `command`, `--no-abort-on-bell` | INPUT.md |
| click a fringe/mode line, drag, scroll | `mouse --area`, `drag --steps`, `lines` | INPUT.md |
| drive a shell, REPL, or terminal buffer | `send-process [--buffer B] [--process P]` | INPUT.md |
| focus-dependent behavior | `focus in --set-focus-state`, `send-events focus-in` | INPUT.md |
| Emacs hangs or sits in the debugger | `interrupt`, `debug show/abort`, `eval --on-timeout sample` | INPUT.md |
| assert faces, text properties, overlays | `buffer --props`, `faces-at --pos N --run K` | INSPECT.md |
| check a transient/which-key/corfu popup | `popups`, `state` | INSPECT.md |
| see which internal functions ran, with args | `trace on FN…` / `trace read` | INSPECT.md |
| a structured value instead of a printed sexp | `eval --json-result`, `--raw`, `--field NAME` | INSPECT.md |
| A/B one function against a git revision | `eval --file f.el --defun NAME --rev REV` | INSPECT.md |
| run ERT inside a live session; lint; profile | `test`, `lint`, `profile run`, `bench` | INSPECT.md |
| Emacs crashed | `session_died` in the result, `logs`, `wait dead` | INSPECT.md |
| images, fringes, fonts, child frames: real pixels | `--ui gui`, `screenshot --window/--region`, `pixel` | GUI.md |
| code opens a context menu or dialog | `menu`, `menu choose ITEM`, `menu dismiss` | GUI.md |
| GUI frames get in the user's way | `start --monitor N --position X,Y --background` | GUI.md |
| drag-and-drop from outside, the real pointer | `dnd drop`, `pointer warp/query`, `window-info` | GUI.md |
| reusable startup code, shell rc fixtures | `--eval-file`, `--profile`, `--home-seed` | SESSIONS.md |
| other agents' sessions on this machine, leftovers | `list --mine`, `creator_state`, `ps`, `--ttl`, `purge` | SESSIONS.md |
| turn a session into a regression test | `export-script`, `run`, `--keep-going` | SCRIPTING.md |
| elisp escaping inside JSON is painful | `.eld` scenarios, `elate convert` | SCRIPTING.md |
| the same steps / setup in many scenarios | `"templates"` + `{"use": …}`, `"include"`, `.elate.json` | SCRIPTING.md |
| a scenario needs a tool or env var that may be missing | `"requires"` → SKIP | SCRIPTING.md |
| known breakage, flaky or slow machines | `"expect": "fail"`, `--retries N`, `--timeout-scale F` | SCRIPTING.md |
| many scenarios × Emacs versions × variants | `matrix -j N --emacs … --format junit` | SCRIPTING.md |
| several steps against a live session in one call | `elate -s NAME run -` | SCRIPTING.md |
| install the package for real (autoloads, deps) | `start --config clean-install --load pkg.el` | RECIPES.md |
| any flag's exact spelling and default | — | REFERENCE.md |

## Output and exit codes

Output is the human table on a terminal and **JSON when stdout is not a
TTY** — you get clean JSON automatically; `--json` / `--human` (before the
subcommand) force either. Parse the JSON, don't scrape the table. Exit
codes: **0** success, **1** error (elisp errors, test failures, lint
findings), **2** usage error, **3** `wait` timeout — `elate -s s eval
'(my-check)' && next-step` needs no parsing. Eval's default `value` is a
*printed sexp string*: for structured probes use `eval --json-result` (real
JSON; check `value-encoding`) or `eval --raw` (bare value, stdout empty on
failure) — never regex a printed plist. More: INSPECT.md.

## GUI sessions

```sh
uvx elate start --name g --ui gui --size 100x35 --load ./my-pkg.el
uvx elate -s g screenshot -o shot.png          # real PNG; Read it
```

Use GUI only when TTY can't show it (images, fringes, child frames, real
fonts). No `--raw`; `type` caps at 10,000 chars; macOS capture needs Screen
Recording and an unlocked display; Linux/CI `--headless`. Native menus,
placement, region/pixel checks, drag-and-drop: GUI.md.

## When the MCP server fits better

This skill drives the CLI via shell — the full feature set with near-zero
ambient cost. Prefer the MCP server only when there is **no shell access**
(e.g. desktop apps), or when you want GUI screenshots returned **inline as
images** instead of PNG files to read. If `elate_*` MCP tools are already
available in your session (someone registered the server — the plugin is
CLI-first and does not register it for you), use them directly — do **not**
register a duplicate; otherwise register it with
`claude mcp add elate -- uvx elate mcp`. The 39 `elate_*` tools cover the core surface (`attach`, `resize`,
`prune`, `stderr`, `export-script`, `convert`, `snap`, `matrix`, `install`, and `update` stay CLI-only); `prune`
aliases `purge` and `stderr` aliases `logs`. Sessions are shared
between both (same names, same sandboxes), so you can mix.

## Cleanup checklist (always)

```sh
uvx elate list            # anything still running?
uvx elate stop NAME       # stop every session you started
```

- `stop` ends Emacs and whatever it started (`killed` in the result);
  `elate ps NAME` lists a session's processes.
- Other agents may share the machine: `list` marks each running session's
  `creator_state` — `self` (yours), `other` (another live agent's —
  **never stop it**), `gone`/`unknown` (leftovers). `--mine` on
  `list`/`stop`/`purge` selects only yours; `start --ttl 30m` reaps a
  session you leak.
- Stopped sandboxes keep their transcripts; `elate purge NAME` (or
  `--glob 'run-*'` for failed scenario runs) deletes them. Details:
  SESSIONS.md.
