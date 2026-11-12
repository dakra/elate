# elate — agent notes

elate spawns disposable, sandboxed Emacs sessions (terminal or GUI) and
drives them with keys, mouse, and elisp — for interactively testing Emacs
packages where `emacs --batch`/ERT can't see (keybindings, prompts,
popups, redisplay). Run it as `uvx elate …` (PyPI) or `uv run elate …` in
this checkout. Full guidance: `skills/elate/SKILL.md` — its "Find the
right tool" table maps situations to features and to the topic file that
covers them; README has the human-oriented tour.

## Driving elate

- Lifecycle: `elate start --name s --load ./pkg.el` … `elate stop s`.
  Always name sessions; **always stop every session you started**
  (`elate list` shows leftovers). Most commands take `-s NAME` before the
  subcommand. Never stop a session whose `creator_state` is `other`: it
  belongs to another live agent (`--mine` selects only yours).
- The loop is **act → wait → observe**: `keys`/`type`/`mouse`/`eval`/
  `send-process`, then `wait stable|idle|text|prompt|until '<pred>'`
  (never sleep-and-poll), then `state` (run it first when confused).
- Semantic `keys` run as a macro through the command loop: they obey the
  active keymaps, close an open prompt (use `--events` to leave it open),
  and a bell aborts the rest. Check the result's `command` field when a
  key seems swallowed. A stuck Emacs: `interrupt`; one in the Lisp
  debugger: `debug show` / `debug abort`.
- Eval runs in the selected window's buffer (`--buffer NAME` for
  another); `eval --file` for quote-heavy forms; `--json-result`/`--raw`
  for structured values. Output is JSON when piped; exit codes 0 ok, 1
  error, 2 usage, 3 wait-timeout.
- Tests/lint/profile/bench want a **fresh throwaway session**, and `lint`
  executes compile-time code.

Where to look (all under `skills/elate/`):

| need | file |
|---|---|
| key delivery modes, mouse, `send-process`, focus, recovery | `INPUT.md` |
| waits, eval details, JSON output, faces/props, `trace`, ERT/lint/profile | `INSPECT.md` |
| GUI sessions: capture, placement, region/pixel checks, native menus, drag-and-drop | `GUI.md` |
| startup options, fixtures, sandboxes, owners/TTL, cleanup | `SESSIONS.md` |
| scenario files (JSON or `.eld`), templates, `.elate.json`, `requires`, retries, `matrix`, CI | `SCRIPTING.md` |
| worked examples | `RECIPES.md` |
| every command and flag (generated) | `REFERENCE.md` |

No shell available? Use the MCP server instead: `uvx elate mcp` (stdio;
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
