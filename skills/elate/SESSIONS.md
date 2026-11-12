# Sessions: setup, sandboxes, cleanup, several agents

Starting sessions the way the code under test needs, where their files
live, and keeping a shared machine clean. Every flag: REFERENCE.md.

Contents: starting a session · stopping, sandboxes, and disk · several
agents on one machine

## Starting a session

- **Always name sessions** (`--name`), always `elate stop NAME` when done.
  `elate list` shows leftovers; stop them. `start` without `--name`
  auto-generates an `elate-<hex>` name (returned in the result); `start
  --name X --replace` stops and recreates a live `X`. `stop` is idempotent
  (stopping a missing session is a no-op success), and `stop --all` stops
  every running session at once — on a machine other agents share, stop
  only your own instead (see "Several agents" below).
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
  profile). Relative paths resolve against your working directory. If any
  startup code signals, `start` **exits 1** with the error in `init_error`;
  the session stays up for inspection (stop it), and `--allow-init-error`
  accepts a failing startup. `init_error` holds the error message only;
  for a backtrace, repeat the failing step in the still-running session —
  `elate -s NAME eval '(load (expand-file-name "my-pkg.el"))'` — since
  eval errors come back with `backtrace` and the *Messages* delta.
- `--load PATH`: a **file** is loaded (and its directory put on
  `load-path`); a **directory** is only added to `load-path` — nothing in
  it is loaded, so `require`/load the feature yourself or pass the `.el`
  files. Use `--config clean-install --load pkg.el` to instead **install**
  the package for real (autoloads, Package-Requires, byte-compilation
  verified; see RECIPES.md).

## Stopping, sandboxes, and disk

```sh
uvx elate list            # anything still running?
uvx elate stop NAME       # stop every session you started
```
`elate stop` ends Emacs and whatever it started that outlives it
(reported under `killed`); `elate ps NAME` lists that tree — a process it
does not list is not the session's, even if `pgrep` matches its name. A
crashed TTY Emacs keeps a dead pane for post-mortem `screenshot`. A stopped session's
sandbox dir — and its `stopped` entry in `elate list` (which shows each
one's idle age) — stays behind on purpose (transcripts outlive the Emacs).
Stopped sandboxes are inert; when the transcripts are no longer needed,
`elate purge NAME…` (or `elate purge --all`; `prune` is the same command)
deletes them — purge never touches a running session; `elate stop NAME
--purge` does both for a session nobody will look at again. `elate run` purges
its own throwaway sandbox on success, so only failed runs pile up — named
`run-<scenario>-<hex>` after the scenario file, so a post-mortem is
findable: sweep them by pattern with `elate purge --glob 'run-*'` (or a
targeted `--glob 'run-my-scen-*'`, or `--name-prefix run-`). During a
long parallel run, GC only the stale ones with `elate
purge --stopped-older-than 1h`, and preview which they are with
`elate list --older-than 1h`. Stopped sandboxes older than 7 days are
purged automatically (`ELATE_PURGE_AFTER=12h` tunes it, `0` disables;
crashed sessions are kept).
Sandboxes live under `~/.cache/elate/sessions/<name>`
(`$ELATE_HOME` overrides the base).

## Several agents on one machine

Tag sessions with an owner and manage only your own. `--owner NAME` sets
it; without it, sessions get `$ELATE_OWNER`, else (under Claude Code) a
tag derived from the current conversation, which `--mine` selects:

```sh
uvx elate start --name rx-a --ttl 30m
uvx elate list --mine                 # just mine (or --owner NAME)
uvx elate stop --mine                 # stop all of mine (also: --glob/--name-prefix)
uvx elate purge --mine                # delete my stopped sandboxes
```

`list` marks each running session's `creator_state`: `self` (yours),
`other` (another live agent's — never stop it), `gone`/`unknown` (leftover).
The Claude Code tag is per conversation, so sessions leaked by an earlier
conversation are not `--mine`: they show `creator_state: gone`, and you
stop those by name. For cleanup that must work across conversations (a CI
wrapper, a crash-prone loop), set a stable `$ELATE_OWNER` or `--owner`.

`--ttl DUR` is the crash insurance: a session idle (no commands) past its
TTL is stopped *and* purged by an opportunistic sweep that any later elate
command runs — so sessions leaked by a crashed agent reap themselves. Only
TTL'd sessions are ever swept; the session a command targets is exempt.
