# Changelog

## 0.11.0

A scenario run can now report *what does not work* — every check, not just the
first to break — and one scenario file can drive a matrix of shells, configs,
and Emacs versions.

- **Continue-on-failure & optional steps** (`run --keep-going`, step
  `"optional": true`): `--keep-going` runs every step even after a failure (a
  failed run still exits non-zero), so a matrix surfaces every check, not just
  the first. An `"optional"` step may fail without failing or stopping the run.
  A pure-comment step is now a `comment` annotation, not a `skipped` step, so it
  no longer inflates the counts.

- **Known failures — xfail / xpass** (`"expect": "fail"`, a.k.a. `"xfail":
  true`, + optional `"reason"`): a step you know is broken is reported `xfail`
  (non-gating), so you flag it instead of deleting the check to stay green. If it
  unexpectedly passes it becomes `xpass` and fails the run — telling you to drop
  the stale marker.

- **Named groups** (a `{"group": "dw"}` marker or a `"group"` key on a step): a
  run reports one verdict per group (`dw: PASS · u: XFAIL · cc: FAIL`) instead of
  bare step indices, and groups map 1:1 to JUnit test-cases.

- **Machine-readable output** (`run --format {json,human,junit,tap}`): emit a
  JUnit XML testsuite or a TAP 13 stream for CI, alongside the default human
  summary and full JSON.

- **Parameter templating & matrix** (`{{var}}`, a top-level `params` block, `run
  --set NAME=VALUE`, `matrix --param NAME=v1,v2`): fill `{{var}}` holes from a
  scenario default or the CLI, and cross the Emacs-binary axis with any
  parameter axes — one scenario, N shells × M Emacs versions, aggregated into a
  grid that exits 0 only when every combo passes (xfail honored).

- **Scenario `defaults`** (top-level `{"defaults": {"timeout": 8, "min_idle":
  0.3}}`): set per-verb fallbacks once instead of repeating them on every step.

- **Session-config parity for scenarios**: the `session` block now accepts
  `eval_file`, `profile`, and `home_seed` (like `elate start`), plus a new `env`
  (extra process environment variables, e.g. `{"SHELL": "/bin/zsh"}`), also
  exposed as `start --env` and on MCP. `env` cannot override the sandbox
  HOME/XDG_* isolation, and each key must be a POSIX variable name.

- **`eval` runs in the buffer you see** (`eval --buffer`, and the new default):
  an `eval` / assert-`eval` now runs in the selected window's buffer instead of
  an arbitrary RPC-time one, so `current-buffer` / point / line read what is on
  screen; `--buffer NAME` targets another buffer.

- **Assert comparison/regex operators**: a `state` matcher value may be an
  operator object — `{">": n}` / `{">=":}` / `{"<":}` / `{"<=":}` / `{"!=":}` /
  `{"equals":}` / `{"matches": "regexp"}` — not only a bare equality check.

- **Lifecycle ergonomics**: a successful `run` purges its throwaway sandbox by
  default (`--no-purge` to keep; failed runs are always kept for post-mortem);
  `run --keep` names the kept session from the scenario `"name"` (or `--name`);
  `purge`/`prune` gain `--glob 'run-*'` and `--name-prefix` bulk selectors.

- **Robust TTY failure snapshots**: a failed step's `screen_tail` no longer
  comes back empty when the capture lands mid-redraw (retry + scrollback
  fallback); the fix also covers wait-timeout snapshots.

- **`export-script --clean`**: prune transient temp-path references (a recorded
  `(setenv "HOME" "/tmp/…")` / `(load "/tmp/…")`) so an export replays on another
  machine; transient session `load`/`eval` are dropped (and named in the header
  comment) and a transient `eval` step is marked `skip`.

## 0.10.0

`keys` now tells you whether a keystroke actually reached the command you meant.

- **`command` field on `keys` results** (CLI, `elate_keys`, scenario scripts):
  every semantic `keys` reply now reports what the sequence resolves to in the
  focused buffer (`key-binding`), so you can tell an intended command from one a
  buffer keymap swallowed. Keys run through the command loop and obey the focused
  buffer's keymaps, so a buffer that intercepts keys — a terminal emulator in
  char mode (vterm/eat/term), many special-mode buffers — can forward a
  globally-bound key and the command you meant never runs; `command` makes that
  visible instead of leaving you to infer it from `delivered`. It is `null` for
  an unbound key or a multi-command sequence (no single binding). The CLI's
  human one-liner shows it too (`sent '<f8>' (semantic) -> term-send-raw`). When
  a command must run regardless of buffer bindings, call it directly with
  `eval`. Docs (SKILL/RECIPES/AGENTS) note the swallow case.

## 0.9.0

Crash, hang, and lifecycle handling for heavy parallel / fuzz-testing runs,
plus detection of a tiling window manager resizing a GUI frame.

- **Crash & death reporting**: when a session's Emacs dies, `eval` returns
  `session_died: true` with the fatal `signal` and the OS `crash_report` path
  instead of an opaque transport error; `wait dead` blocks until the session
  exits and returns the same; and `info`/`list` show a dead session's signal
  (rendered `dead (SIGABRT)`). The signal is grepped from the Emacs stderr log
  (so it appears even before the OS writes a report), and the macOS `.ips`
  report is attributed by matching the recorded pid — correct even when several
  sandboxed Emacsen crash in parallel.

- **`eval --on-timeout sample`** (CLI + `elate_eval` `on_timeout`): on a timeout
  with Emacs still busy, capture a thread backtrace of the wedged process
  (macOS `sample`; Linux `eu-stack`/`gdb`) and attach it to the error as
  `sample` — the fastest way to see *where* a hang is stuck.

- **`elate logs`** (alias `stderr`; CLI + `elate_logs`): tail the driven
  Emacs's stderr — native-module panics, GC/native-comp warnings, and the
  fatal-signal line on a crash. TTY sessions now capture Emacs's stderr to a
  file (off the tmux pane, so screenshots stay clean); GUI sessions log
  stdout+stderr. Works on dead and stopped sessions too.

- **GUI orphan reaping**: a GUI session's Emacs leads its own process group, so
  a subprocess it backgrounds is now killed at `stop`/`purge` (filtered by
  start time, so a recycled process group is never touched). `list`/`info` flag
  a live session's leaked descendants as `orphans`.

- **Lifecycle ergonomics for parallel runs**: `stop` is idempotent (stopping a
  missing session is a no-op success) and gains `--all` to stop every running
  session; `start` auto-generates a name when `--name` is omitted and gains
  `--replace` to recreate a live session in place; `list --older-than DUR`
  previews stale sandboxes; `prune` is an alias for `purge`.

- **Window-manager resize detection**: a GUI frame resized out from under the
  requested size (a tiling WM such as AeroSpace/yabai/Amethyst) is reported as
  a `wm_warning` from `start`/`resize`; the frame is titled `elate:<session>`
  so the WM can be configured to float just elate's windows (see the README).

## 0.8.0

- **`elate interrupt`** (CLI + `elate_interrupt`): unblock a wedged-but-alive
  session without killing it — the recovery for a session whose `info` reports
  `busy` (e.g. Emacs blocked on a slow synchronous `call-process`), short of
  stopping and restarting it. A TTY session gets a raw `C-g` over tmux, which
  works even when the semantic channel is blocked. A GUI session — which has
  no raw channel — is signalled instead: `--signal int` (default) sends a
  `C-g`-like SIGINT that unwinds a stuck call back to top level, and
  `--signal usr2` drops Emacs into the Lisp debugger so a follow-up
  observation shows where it was stuck.

- **Screenshot failure reasons**: a failed GUI capture carries a
  machine-readable `reason` in the error JSON — `locked`, `display_asleep`,
  `window_gone`, or `permission`. On macOS a locked screen or asleep display
  is diagnosed by probing the window-server session, so it is reported
  distinctly from a missing Screen Recording grant.

- **`elate list` filtering**: `list [NAME]` shows a single session and
  `--status {running,stopped,all}` filters by liveness, so a busy parallel run
  can query just what it needs rather than every session at once. The table
  footer points at `purge` once inert sessions accumulate.

- **Ergonomics**: a global flag placed after the subcommand (`elate stop -s
  NAME`) suggests the correct order (`elate -s NAME stop`) rather than a bare
  argparse error. The eval-timeout hints point at `interrupt` and `--timeout`,
  and the skill documents driving a program inside a terminal buffer with
  `send-process`.

## 0.7.0

- **`elate install`**: wire elate into AI coding harnesses with one command.
  It copies elate's Agent Skill (the SKILL.md that teaches the CLI) into each
  harness's skills directory — Claude Code (`~/.claude/skills`), Codex CLI
  (`~/.agents/skills`), opencode (`~/.config/opencode/skills`), pi
  (`~/.pi/agent/skills`), and Antigravity (`~/.gemini/skills`), which all read
  the same SKILL.md format. With no argument it auto-detects installed
  harnesses; name them explicitly or pass `all`. `--project` installs into the
  current repo's skills dir; `--dry-run` previews; `--mcp` additionally
  registers the optional MCP server where supported (shells out to
  `claude`/`codex mcp add`; prints a paste-ready snippet for opencode and
  Antigravity; pi has no MCP). The skill now ships **inside the wheel**, so a
  plain `pip install elate` / `uvx` can materialize it without a checkout.

- **`focus in` / `focus out`** (CLI + `elate_focus`): inject a `focus-in` /
  `focus-out` event. It runs `handle-focus-in` / `handle-focus-out` through
  `special-event-map` exactly as a real window-system focus change would --
  firing `after-focus-change-function` and setting the `last-focus-update`
  frame parameter. Works in TTY and GUI sessions. `--frame NAME` targets a
  named frame.
- **`send-events`** (CLI + `elate_send_events`): inject an ordered stream of
  focus / mouse / key tokens that drains through the command loop in order,
  so a focus event's hooks run before a following click's command. Tokens:
  `focus-in`/`focus-out`, `down-mouse-N`/`mouse-N`/`up-mouse-N`/
  `double-mouse-N`/`wheel-up`/`wheel-down` (N=1..3, optional `@LINE,COL` or
  `#POS`), and `key:KBD`. A focus event only fires at the head of a
  command-loop turn, so focus tokens are auto-split into separate, drained
  batches -- making any ordering faithful, including a mouse-down dispatched
  *before* a focus-in. Lower-level than `mouse`: each mouse token is exactly
  one event (no implicit down+click pair).
- **`--set-focus-state`** (on both): also make `(frame-focus-state)` report
  the injected focus. A non-native shim (advice on `frame-focus-state`
  deriving from `last-focus-update`), since an injected event cannot move the
  C-owned focus state. Enable only when the code under test reads
  `(frame-focus-state)`.
- **Scenario scripts** gain `focus` and `send_events` verbs; see
  `examples/focus-ordering.json` for a self-contained, runnable demo of the
  focus-vs-click ordering case.

## 0.6.1

- **Packaging fix:** the sdist now ships a default-deny `only-include`
  allowlist (`[tool.hatch.build.targets.sdist]`). Hatchling's default sdist
  includes everything not matched by `.gitignore` and does not read
  `.git/info/exclude`, so untracked-but-uncommitted files were being swept into
  the published tarball. The 0.6.0 sdist is withdrawn; install 0.6.1.

## 0.6.0

- **The plugin ships no `.mcp.json`.** Drive elate through the CLI -- the
  full feature set, and what the skill teaches. Register the MCP server
  explicitly when you want typed tools for a shell-less harness or inline
  GUI screenshots: `claude mcp add elate -- uvx elate mcp`. Keeping it
  opt-in keeps the plugin's tools out of every turn's context until you ask
  for them, and avoids a second project-scope `elate` server when you open
  the repo in Claude Code.
- **`attach`** (CLI-only): hand a TTY session off to a human -- `elate attach
  NAME` exec's into the session's tmux so a person can drive Emacs directly,
  then detach with `C-b d` (the session keeps running). `--read-only` watches
  without sending input; `--print-command` prints the tmux command without
  attaching. GUI sessions point at their visible window; a non-interactive
  stdio is a usage error (exit 2) rather than an exec into nothing.
- **`state --since TOKEN`** (CLI and `elate_state`): a delta observation. Every
  `state` result now carries an opaque `token`; pass it back as `--since` to get
  only what changed -- buffers added/removed/modified, point and selected-buffer
  movement, new *Messages* lines, minibuffer open/close/prompt -- much cheaper
  than a full snapshot, with `"changed": false` meaning your last action did
  nothing observable. The token is self-describing (no server-side state); an
  unknown or since-restarted-session token degrades to a full snapshot.
- **`trace`** (CLI `trace`; MCP `elate_trace`): `trace on FUNC...` wraps
  `trace-function` around functions; drive the session, then `trace read`
  returns the call/args/return log and clears it (so each read sees only new
  calls), `trace off [FUNC...]` untraces. Surfaces internals you cannot see on
  screen -- why an advice fires twice, what args a hook receives.
- **`eval --backtrace`** (and `elate_eval(backtrace=true)`): on error, return
  structured `frames` (each frame's function + printed args) alongside the
  rendered backtrace string -- off by default to keep replies small.
- **Golden snapshots** in the scenario layer: a new `snapshot` assertion
  compares the current render against a committed golden and fails with a diff
  on mismatch. `of: screen` (text/PNG), `of: faces` (the deterministic choice:
  text + face/property runs + overlays), or `of: state`. Goldens are
  version-keyed under `<scenario-dir>/__snapshots__/<stem>/<name>@<major>.<ext>`,
  so `matrix` gets one per Emacs ("renders identically on 29/30/31, diff when
  not"). Create/refresh with `elate run --update-snapshots` (also on `matrix`
  and `elate_run_script(update_snapshots=true)`); a missing golden in compare
  mode is a hard failure (CI never passes on an absent golden).

## 0.5.0

- **JSON by default for non-interactive use.** The CLI now emits JSON
  automatically when stdout is not a TTY (a pipe or an agent), and the human
  table on a terminal -- so programmatic callers get structured output
  without remembering `--json`. Force either mode with the global `--json` /
  `--human`.
- **The declarative layer is now surfaced where agents work.** The MCP server
  instructions point at `elate_run_script` (and the CLI's `export-script` /
  `matrix`), the `elate --help` epilog points at `run`/`export-script`/
  `matrix`, and `AGENTS.md` covers `matrix` -- the scenario workflow was easy
  to miss from the imperative loop.
- **`faces-at` addressing:** `--pos N` (an absolute buffer position, natural
  from elisp) in addition to `LINE:COL`, and `--run K` to dump K adjacent
  cells in one call (e.g. compare a typed cell against the dimmed suggestion
  beside it). Mirrored on `elate_faces_at` (`pos` / `run`).
- **`send-process`** (CLI `send-process`; MCP `elate_send_process`): write raw
  input straight to a buffer's subprocess (`process-send-string`), bypassing
  the command loop -- `--char C-c` interrupts a job, text/`--file` feeds a
  shell/REPL. `keys`/`type` drive Emacs; this drives the process.
- **keys through the command loop, documented and tamed.** Semantic `keys`
  obey the active keymaps (in evil normal state plain letters are commands,
  not text); a command that rings the bell aborts a `--semantic` macro -- the
  error now names the culprit command/key/point instead of the opaque
  "terminated by a command ringing the bell". New `keys --no-abort-on-bell`
  delivers past a spurious bell via the events path (asynchronous; the
  bell-tolerant `delivery="events"` was already on `elate_keys`).
- **Stopped-session GC:** `purge --stopped-older-than DUR` (MCP `elate_purge`
  `stopped_older_than`) GCs only sessions inert long enough, and `elate list`
  now shows each stopped session's idle age -- so heavy parallel runs can
  clean up stale sandboxes without touching just-stopped ones.
- **Invocation ergonomics (docs):** `uv tool install elate` to put `elate` on
  `PATH`, and a note that after `claude plugin update` the CLI is the live
  path while the MCP server stays on the old version until the client
  restarts.

## 0.4.0

- `wait stable` (CLI `wait stable --buffer B --quiet-ms N`; MCP `elate_wait`
  `condition="stable"`, `quiet_ms`): wait until a buffer's text has not
  changed for `quiet_ms` ms. Tracks `buffer-chars-modified-tick`, so it
  settles on comint/REPL, compilation, terminal (vterm & friends), and
  async-LSP output -- the "did the output stop?" question that `wait idle`
  (command-loop idle) cannot answer.
- `elate_faces_at` MCP tool (the CLI `faces-at` had no MCP counterpart), and
  both now return `property-values`: every text property at the point paired
  with its clipped printed value, so a package's own props (a flag `t` vs a
  number) are checkable without repeated `get-text-property` evals.
- `start --home-seed DIR` (MCP `elate_start` `home_seed`): copy a fixture
  tree into the sandbox's fake `$HOME` before Emacs launches, so rc files
  are in place before any subprocess the session spawns -- shell-integration
  testing that keeps the sandbox isolated.
- `start --eval-file PATH` and `start --profile NAME` (MCP `eval_files` /
  `profiles`): reusable startup snippets that run before `emacs-startup-hook`
  (a forms file with no `load-path` side effects, or a named file from
  `$XDG_CONFIG_HOME/elate/profiles/`), instead of re-pasting the same forms
  into every session. Startup order is `--load` -> `--eval-file` ->
  `--profile` -> `--eval`, and that ordering guarantee is now documented.
- `elate_purge` MCP tool: the CLI-only `purge` now has its MCP counterpart,
  so an MCP agent can clean up its own stopped sessions (same safety: a
  running session is never purged).
- `wait idle` now reads as "idle Ns since last activity" and its docs note a
  large idle value is healthy (Emacs waiting for input), not a hang.

## 0.3.0

- `elate lint --package-lint [--archive-dir DIR]` (MCP: `elate_lint`'s
  `package_lint` / `archive_dir`): opt-in package-lint pass, additive on
  top of the default byte-compile + checkdoc. Items are tagged
  `tool: "package-lint"`. package-lint is installed into a sandbox-local
  `elpa/`; `--archive-dir` points at a local package archive directory
  (a plain path containing `archive-contents`, not a `file://` URL) for
  an offline, reproducible run -- without it the standard archives are
  refreshed live (network, non-deterministic). A missing/empty/offline
  archive fails with a clear structured error; the session and the
  semantic channel survive. The default lint stays offline, deterministic,
  and residue-free; the package-lint path may leave sandbox-contained
  install/native-comp artifacts.
- `elate --version` now derives from the installed distribution metadata.
  The published 0.2.0 wheel mistakenly reported `elate 0.1.0` (a hardcoded
  `__version__` the release bump missed); functionality was unaffected. The
  version-sync test now covers the runtime string.

## 0.2.0

- `elate purge`: delete the sandboxes of stopped/dead sessions (the
  transcripts included) — by name or `--all`; a running session is never
  purged. Leftover processes of dead sessions are cleaned up first.
  Containment-first: only entries directly under the sessions root are
  removed, and a hand-made symlinked session dir is unlinked without
  following (the target survives; the report says so).
- Plugin: `emacs-tester` subagent (`elate:emacs-tester`) — preloaded
  with the skill, for delegating long interactive Emacs test-drives out
  of the main context; returns findings, not transcripts.
- Plugin: leftover-session hooks — SessionEnd warns about elate
  sessions still running when a Claude Code session ends, SessionStart
  injects the same fact as context at the next session start (silent
  and instant for everyone else).
- CI on GitHub Actions: Linux Emacs matrix (29.4 → snapshot), a
  dedicated Xvfb GUI job, macOS full suite, build + wheel smoke test
  (publishing stays manual).
- `skills/elate/`: an Agent Skill teaching harnesses to drive the elate
  CLI (act → wait → observe loop, key delivery, scenario scripts), with
  generated-from-the-CLI `REFERENCE.md` (CI fails on drift), `RECIPES.md`,
  and `SCRIPTING.md`; `AGENTS.md` for harnesses without skill support.
- Claude Code plugin + self-hosted marketplace (`.claude-plugin/`,
  `.mcp.json`): two-command install delivering the skill as
  `/elate:elate` plus the MCP server (`uvx elate mcp`) auto-registered.
- Harness docs: README "Using elate from AI harnesses" with per-harness
  MCP registration (Claude Code, Claude Desktop, Codex CLI, Cursor, Zed,
  Gemini CLI — the same `uvx elate mcp` everywhere) and pinned-install
  guidance (`elate--v{version}` tags, `@ref`/`#ref` forms); release flow
  tags via `claude plugin tag`; CI runs `claude plugin validate --strict`
  over both manifests plus an end-to-end local marketplace-install check.

## 0.1.0

- Sandboxed sessions: fresh fake `$HOME` + XDG dirs, generated init
  (`--init-directory`), per-session server.el socket, dedicated tmux
  server with its socket inside the sandbox.
- Config modes `minimal` (deterministic test defaults), `bare` (`-Q`),
  `init-file`; `--load` / `--eval` startup hooks with errors captured
  to `init_error` instead of wedging the session.
- Two channels: semantic (`emacsclient --eval` against the in-Emacs
  agent; base64-JSON replies) and raw (tmux keys/capture-pane — works
  even when Emacs is wedged; crashed panes stay for post-mortem
  screenshots).
- CLI with `--json` everywhere: `start/stop/list/info`, `keys` (kbd
  notation; macro, queued-events, or raw delivery), `type`, `eval`
  (printed value, *Messages* delta, error + full backtrace, hard
  timeouts), `buffer`, `messages` (cursor-based delta), `echo`,
  `screenshot` (text/ANSI), `describe`, `wait idle|text|prompt`
  (timeouts embed a state snapshot; exit 3).
- `elate mcp`: stdio server, tools 1:1 with the CLI, every response
  structured JSON with an `ok` flag; errors embed a compact state
  snapshot so the model sees why.
- `elate_state`: the one-call scene snapshot (window layout tree with
  visible text, buffer/modes/point/region, minibuffer prompt + input +
  completion candidates, echo area, pending input, *Messages* tail).
- Non-Unicode payloads sanitized centrally (`elate--encode`); tool
  bodies run off the event loop (`_threaded`); timeouts schema-bounded.
- `--ui gui`: windowed Emacs (macOS native; Linux X11 with `--headless`
  Xvfb), PNG screenshots (`screencapture` / `import`/`xwd`), live
  `resize`, frame geometry pinning.
- Semantic mouse for both UIs (`mouse` / `elate_mouse`): synthesized
  posn-based click/double/drag/wheel through the command loop — real
  bindings fire, no OS permissions needed.
- Robustness: server.el request handling shielded from the debugger
  (dead-client replies can no longer wedge the channel), E2BIG argv
  errors converted to actionable messages, identity-checked pid
  handling against reuse, GUI `type` chunked + capped.
- `test` / `elate_test`: interactive ERT runs (selector support) with
  structural per-test results (status, duration, captured messages,
  condition, trimmed backtrace); in-Emacs run timeout with partial
  results; quits are recorded, never prompt.
- `lint` / `elate_lint`: byte-compile + checkdoc as structured items;
  in-session by design (and loudly documented as executing
  compile-time code — lint untrusted code in a throwaway session).
- `buffer --props`: run-length-encoded face/text-property runs +
  overlay dumps; `faces-at` point queries; `popups` capture
  (which-key, transient, hydra/lv, corfu, company,
  completion-preview, child frames).
- JSON scenario scripts: strict up-front validation, steps mirroring
  the CLI verbs plus assertions; `elate run` executes them in a fresh
  throwaway session (exit 0/1, first failure stops the run, failed
  steps embed state); `elate_run_script` over MCP.
- `export-script`: transcript → editable scenario; `record`: asciicast
  v2 via tmux pipe-pane; `snap`: detached periodic frame series;
  `matrix`: one script across several Emacs binaries.
- `profile` / `elate_profile`: Emacs's native profiler with structured
  reports — top functions (self/total + percentages) and a
  depth-limited unified calltree; one-shot `profile run FORM`.
- `bench` / `elate_bench`: `benchmark-run-compiled` wrapper with
  interpreted fallback; elapsed/mean, GC stats, `memory-use-counts`
  allocation deltas.
- `--config clean-install`: install the package under test for real
  (`package-install-file` into a sandbox-local elpa/) — verifies
  autoload cookies, `Package-Requires` (missing deps fail with a clear
  offline error), and byte-compilation of the installed copy; install
  results exposed via `info`/`state`.
- Recipes in the README backed by tested scripts in `examples/`
  (transient menu, font-lock verification, driving dired,
  clean-install).
