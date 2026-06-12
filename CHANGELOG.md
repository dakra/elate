# Changelog

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

Everything below shipped in phases on the way to 0.1; one section per phase.

### Phase 1 — TTY MVP

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

### Phase 2 — MCP server

- `elate mcp`: stdio server, tools 1:1 with the CLI, every response
  structured JSON with an `ok` flag; errors embed a compact state
  snapshot so the model sees why.
- `elate_state`: the one-call scene snapshot (window layout tree with
  visible text, buffer/modes/point/region, minibuffer prompt + input +
  completion candidates, echo area, pending input, *Messages* tail).
- Non-Unicode payloads sanitized centrally (`elate--encode`); tool
  bodies run off the event loop (`_threaded`); timeouts schema-bounded.

### Phase 3 — GUI sessions

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

### Phase 4 — Testing & quality tooling

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

### Phase 5 — Recording & CI

- JSON scenario scripts: strict up-front validation, steps mirroring
  the CLI verbs plus assertions; `elate run` executes them in a fresh
  throwaway session (exit 0/1, first failure stops the run, failed
  steps embed state); `elate_run_script` over MCP.
- `export-script`: transcript → editable scenario; `record`: asciicast
  v2 via tmux pipe-pane; `snap`: detached periodic frame series;
  `matrix`: one script across several Emacs binaries.

### Phase 6 — Polish

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
