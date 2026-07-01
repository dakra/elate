# elate scenario scripts

A scenario script is a **JSON** file (deliberately not YAML: zero extra
dependency, every other elate surface already speaks JSON) describing a
whole interaction — session config, steps, assertions — that `elate run`
executes against a fresh throwaway session and turns into an exit code.
That makes a script a regression test and the CI entry point.

```json
{
  "name": "my-pkg smoke test",
  "session": {"ui": "tty", "size": "100x30", "config": "minimal",
              "load": ["./my-pkg.el"]},
  "steps": [
    {"keys": "M-x my-pkg-mode RET"},
    {"wait": "text", "pattern": "My-Pkg", "buffer": "*scratch*", "timeout": 10},
    {"type": "hello"},
    {"assert": {"buffer_contains": "hello"}},
    {"test": "my-pkg-", "load_files": ["./tests/my-pkg-tests.el"],
     "allow_unexpected": true},
    {"assert": {"tests": {"unexpected": 0, "timed-out": false}}},
    {"lint": ["./my-pkg.el"]},
    {"assert": {"eval": "(featurep 'my-pkg)"}}
  ]
}
```

Validation is strict and up front: unknown step/assert/session keys, wrong
value types, bad enum values, out-of-bounds numbers, an option on the wrong
step kind, or an empty `"steps"` list are loud errors **before anything
boots**. Unknown *top-level* keys are ignored (metadata like `"name"`,
`"exported_at"`). Every step additionally accepts `"comment"` (string) and
`"skip": true` (record it but do not run it); a step with only a comment is
recorded as a `comment` annotation (not a skipped or failed step). Three
keys govern failure handling: `"optional": true` lets a step fail without
failing or stopping the run; `"expect": "fail"` (a.k.a. `"xfail": true`)
marks a **known** failure — a failing xfail step is reported `xfail` and
does not gate the run, while one that unexpectedly passes is an `xpass` and
**does** fail the run (drop the stale marker); `"reason"` (string) annotates
why. `"xfail"` may instead be a map `{"nu": "reedline has no C-_"}` keyed by
**variant name** (see Variants below): the step is expected to fail only
under those variants — the matching value lands in the record as the
`reason` — and gates normally everywhere else, keeping the known break and
its why next to the check instead of at the call site. (`optional` and
`expect`/`xfail` are mutually exclusive.) Pass
`--keep-going` to `elate run` to execute every step even after a failure (a
failed run still exits non-zero). A `"group"` (string) names a test group:
it and every following step belong to it (**sticky**) until another
`"group"` — or `{"group": null}`, which ends the current group — so the run
reports one verdict per group (`dw: PASS · u: XFAIL · cc: FAIL`). A
verb-less `{"group": "dw"}` is a boundary marker; a comment/group marker
runs nothing and takes no other keys.

A top-level `"defaults"` block sets fallbacks for options every step would
otherwise repeat: `"timeout"` (seconds, the fallback for any verb's step
timeout) and `"min_idle"` (the fallback for `wait: "idle"`). A step's own
`timeout`/`min_idle` still wins; the defaults just replace the built-in
per-verb baseline.

Relative paths (session `load`/`init_file`, test `load_files`, `lint`
files, `screenshot` output) resolve against the **script file's
directory**, so scripts can live next to the package they test and run
from any cwd.

## The `session` block (all keys optional)

| key | type | default | notes |
|---|---|---|---|
| `ui` | `"tty"` \| `"gui"` | `"tty"` | |
| `size` | `"COLSxROWS"` | `"120x36"` | minimum `10x4` |
| `config` | `"minimal"` \| `"bare"` \| `"init-file"` \| `"clean-install"` | `"minimal"` | |
| `init_file` | string path | — | only with config `minimal`/`init-file` (implies `init-file`); conflicts loudly with `bare`/`clean-install` |
| `load` | list of strings | — | files/dirs for load-path; with `clean-install`: packages to install |
| `eval` | list of strings | — | startup forms |
| `eval_file` | list of strings | — | elisp files loaded at startup (like `eval`, no load-path effect) |
| `profile` | list of strings | — | named snippets from `$XDG_CONFIG_HOME/elate/profiles/NAME.el` (or a `.el` path) |
| `home_seed` | string path | — | fixture tree copied into the sandbox `$HOME` before launch (rc files for shell tests) |
| `env` | object (string→string) | — | extra process env vars (e.g. `{"SHELL": "/bin/zsh"}`); cannot override `HOME`/`XDG_*` |
| `emacs` | string path | — | binary override (also: `run --emacs`, `matrix`) |
| `headless` | bool | false | GUI under a private Xvfb (Linux) |
| `allow_init_error` | bool | false | see init_error contract below |

**init_error contract**: a fresh session whose startup `load`/`eval`
signalled an error **fails the run before any step executes** (the package
under test may not even be loaded) — unless `"allow_init_error": true`.

## Templating: `params` + `{{var}}`

A top-level `"params"` block declares template variables with defaults;
every `{{var}}` in a string value (anywhere in the scenario) is substituted
**before** validation. `elate run --set NAME=VALUE` (repeatable) overrides a
default, and `matrix --param NAME=v1,v2` turns one into an axis — so a
single scenario drives many shells/configs. For multi-line or quote-heavy
values (a shell setup snippet full of `;`, `$`, and quotes), `--set-file
NAME=PATH` (run and matrix) binds the file's contents verbatim, minus
exactly one trailing newline — no shell quoting in the way; binding the
same NAME with both `--set` and `--set-file` is an error. (The MCP
`elate_run_script` tool needs no file indirection: its `params` object
carries multi-line values directly.) An unknown `{{var}}` (no
default, no `--set`) is a loud error before anything boots. The `params`
block is consumed (it is not itself templated and never reaches the run).
Only **string** values are templated, so numeric fields (`timeout`,
`min_idle`) can't be parameterized — a substituted `"{{t}}"` stays a
string and fails number validation.

```json
{"params": {"shell": "/bin/zsh"},
 "session": {"env": {"SHELL": "{{shell}}"}},
 "steps": [{"send_process": "{{shell}} --version\n"}]}
```

### Variants: named sets of co-varying bindings

Independent `--param` axes cross every value with every other — wrong when
several variables must move **together** (nushell needs its shell path *and*
its echo alias *and* a setup snippet as one unit). A top-level `"variants"`
block declares named binding sets:

```json
{"params": {"setup": ""},
 "variants": {
   "bash": {"shell": "/bin/bash", "echo": "echo"},
   "nu":   {"shell": "/usr/bin/nu", "echo": "e",
            "setup": "def e [...rest] { print ($rest | str join ' ') }"}},
 "steps": [{"send_process": "{{setup}}\n"}]}
```

- `elate run scenario.json --variant nu` overlays one set on the `params`
  defaults. Precedence per variable: `params` default < variant binding <
  `--set`.
- `elate matrix scenario.json` runs **every declared variant** (crossed with
  the Emacs axis and any `--param` axes); `--variant nu,fish` filters. The
  per-combo `axes` carry `"variant": "nu"`, and snapshot stems get
  `+variant-nu`, so per-variant goldens never collide.
- `{{variant}}` implicitly binds the active variant name (`""` when the
  scenario declares variants but none is selected). The name `variant` is
  reserved — it cannot be bound by `params`, a variant, `--set`, or
  `--param`.
- A step's `"xfail"` may be a map keyed by variant name (see the failure
  handling section above): `{"assert": {...}, "xfail": {"nu": "reedline has
  no C-_"}}` is a known break scoped to the `nu` variant and a normal
  gating check everywhere else.
- Loud, never silent: an unknown `--variant` name is always an error, a
  map-form `"xfail"` key must name a declared variant, and `matrix`
  additionally rejects a variant binding a variable no template references
  and a `--param` axis colliding with a variant-bound variable — all before
  anything boots.

## Steps — exactly one verb per step

Timeouts are numbers in `(0, 600]` seconds.

| verb | value | options (type, default) |
|---|---|---|
| `keys` | kbd string | `delivery`: `"semantic"`(default)/`"events"`/`"raw"`; `timeout` (15) |
| `type` | literal string | — |
| `eval` | elisp form string | `timeout` (15); `buffer` (name; default: the **selected window's buffer**, so `current-buffer`/point/line see what is on screen) |
| `wait` | `"idle"` \| `"text"` \| `"prompt"` | idle: `min_idle` (number 0–60, default 0.2); text: `pattern` (required, **Python** regexp), `buffer` (string); all: `timeout` (10). Options on the wrong wait kind are rejected. |
| `mouse` | `"click"` \| `"double"` \| `"drag"` \| `"wheel"` | `button` (int 1–3, 1); `buffer`; `pos`/`line`/`to_pos`/`to_line` (int >= 1); `col`/`to_col` (int >= 0); `part`: `"text"`(default)/`"mode-line"`; `direction`: `"down"`(default)/`"up"`; `count` (int 1–50, 1); `delivery`: `"macro"`(default)/`"events"`; `timeout` (15) |
| `focus` | `"in"` \| `"out"` | `frame` (string); `set_focus_state` (bool); `timeout` (15) |
| `send_events` | non-empty list of event tokens | `buffer`; `frame` (string); `set_focus_state` (bool); `timeout` (15). Tokens: `focus-in`/`focus-out`, `down-mouse-N`/`mouse-N`/`up-mouse-N`/`double-mouse-N`/`wheel-up`/`wheel-down` (N=1–3, optional `@LINE,COL` [1-based line, 0-based col] or `#POS`), `key:KBD`. Focus tokens are auto-split into separate drained batches (a focus event only fires at the head of a command-loop turn). |
| `test` | ERT selector string (`"t"` = all) | `load_files` (list of paths); `timeout` (60); `allow_unexpected` (bool) |
| `lint` | non-empty list of file paths | `timeout` (60); `allow_findings` (bool) |
| `screenshot` | output path, or `null` to embed text | `ansi` (bool, TTY only) |
| `resize` | `"COLSxROWS"` (min `10x4`) | — |
| `assert` | assertion object (below) | — |

Failure semantics:
- An `eval` step fails on an elisp error (backtrace in the step record).
- A `test` step fails on unexpected results or timeout **unless**
  `allow_unexpected` — set it when you'd rather assert exact counts.
- A `lint` step fails on any finding **unless** `allow_findings`.
- The run stops at the **first failure**; the failed step embeds a state
  snapshot; later steps are recorded `"not-run"`.

## Assertions — exactly one kind per `assert` step

| kind | value | extra options |
|---|---|---|
| `buffer_contains` | substring | `buffer` (default: current) |
| `buffer_matches` | Python regexp (multiline) | `buffer` |
| `state` | non-empty object of state-field → expected; dotted paths work (`"region.size"`). A value is a bare equality check, **or** an operator object — `{">": n}`/`{">=":}`/`{"<":}`/`{"<=":}` (numeric), `{"!=":}`/`{"equals":}`, `{"matches": "regexp"}` (Python regexp on the stringified value); all operators in the object must hold | — |
| `messages_match` | Python regexp over `*Messages*` | — |
| `popup` | popup kind string, or `true` for any | — |
| `tests` | non-empty object of count-field → expected (`{"unexpected": 0, "timed-out": false}`), checked against the **last `test` step** | — |
| `lint_clean` | `true`/`false`, checked against the **last `lint` step** | — |
| `eval` | elisp form; passes when it evaluates without error to non-`nil` (the catch-all) | `timeout` (10); `buffer` (name; default: the selected window's buffer) |
| `snapshot` | a name string, or `{"name", "of"}` — compare the current render against a committed golden (see below) | — |

`tests`/`lint_clean` need a preceding `test`/`lint` step in the same run.

To assert a **buffer-local or package variable** that is not in the state
snapshot (e.g. `evil-state`), use the `eval` matcher — it runs in the
selected window's buffer, so buffer-local values read correctly:
`{"assert": {"eval": "(eq evil-state 'insert)"}}`. Note the regexp split:
`eval` runs **elisp**, while `buffer_matches`/`messages_match` and the
`state` `matches` operator take **Python** regexps (as does `wait text`).

## Golden snapshots

A `snapshot` assert compares the current render against a stored golden and
fails with a diff on mismatch — regression-testing for rendering, especially
across Emacs versions with `matrix`.

```json
{"assert": {"snapshot": "font-lock-render"}}
{"assert": {"snapshot": {"name": "fl", "of": "faces", "buffer": "demo.el"}}}
```

- A bare string is sugar for `{"name": <s>, "of": "screen"}`. `name` must match
  `[A-Za-z0-9._-]+` (it becomes a filename).
- **`of`** picks what is captured:
  - `screen` (default) — the text screenshot (TTY) or a PNG (GUI). Pin
    `session.size`; precede with `wait idle` so redisplay has settled. `ansi:
    true` (TTY only) includes colour escapes.
  - `faces` — buffer text + run-length face/property runs + overlays over
    `buffer`/`from`/`to`. **The deterministic choice** (geometry- and
    clock-independent; font-lock is ensured) — prefer it for theme/font-lock
    regressions.
  - `state` — a normalized scene snapshot (volatile fields, the absolute
    buffer file path, and per-window visible text stripped). Use over
    in-memory buffers; for file-visiting buffers prefer `faces`.
- Goldens live at
  `<scenario-dir>/__snapshots__/<scenario-stem>/<name>@<major-emacs-version>.<ext>`
  — version-keyed, so `matrix` gets one golden per Emacs and "renders
  identically on 29/30/31, diff when not" just works. Commit them.
- **Create/update** goldens with `elate run --update-snapshots scenario.json`
  (or `elate matrix --update-snapshots ...`); the run still executes every
  step. Review the diff and commit deliberately.
- In compare mode a **missing golden is a hard failure** (CI never passes on an
  absent golden) — mint it with `--update-snapshots` first. `--snapshot-dir
  DIR` overrides the base location.
- GUI `of: screen` PNG goldens are brittle (fonts/AA/HiDPI/Xvfb) — prefer
  `faces`/`state` for GUI sessions.

## Running

```sh
elate run scenario.json                  # fresh session, teardown, exit 0/1
elate --json run scenario.json           # per-step records, timings, snapshots
                                         # (--json is global: BEFORE the subcommand)
elate run scenario.json --keep           # keep the session afterwards
elate run scenario.json --keep-on-failure
elate run scenario.json --emacs /opt/emacs-29/bin/emacs
elate -s existing run scenario.json      # against an existing session:
                                         # session block ignored, no teardown
```

Fresh sessions per run are the default **on purpose**: lint executes
compile-time code and lint/test results depend on session history, so only
a throwaway session gives reproducible verdicts. `--emacs` with `-s` is a
loud error (an existing session already runs its own binary).

## Version matrix

```sh
elate matrix --emacs /opt/e29/bin/emacs,/opt/e30/bin/emacs scenario.json
elate matrix --emacs-glob '/opt/emacs-*/bin/emacs' scenario.json
elate matrix --emacs emacs scenario.json   # bare names resolve via PATH
```

One fresh session per binary; duplicates (symlink/relative/PATH spellings)
run once; one broken binary records a failure but does not abort the rest.
Exit 0 only when every version passed.

## Transcript export

```sh
elate -s NAME export-script -o scenario.json   # stopped sessions work too
```

Best-effort starting point, not a faithful recorder: inputs become steps in
transcript order; observations (`state`/`buffer`/`messages`/`popups`/…)
become assertion stubs with `"skip": true` for you to edit into real
assertions; transcript-clipped values export skipped with a comment.
Timing and out-of-band changes are not captured; the emacs binary is
deliberately not pinned (that is `--emacs`/`matrix`'s job).

## CI pattern (GitHub Actions)

```yaml
jobs:
  scenario:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        emacs_version: ["29.4", "30.1", "snapshot"]
    steps:
      # In production CI, pin third-party actions to commit SHAs.
      - uses: actions/checkout@v6
      - uses: purcell/setup-emacs@master
        with:
          version: ${{ matrix.emacs_version }}
      - run: sudo apt-get update && sudo apt-get install -y tmux
      - uses: astral-sh/setup-uv@v8
      - run: uvx elate --json run scenario.json
```

TTY sessions need only tmux. Export a UTF-8 locale (`LANG=C.UTF-8`) if
steps type non-ASCII text. On failure the `--json` output embeds the
failing step's state snapshot; `--keep-on-failure` plus
`elate -s NAME screenshot` captures more before teardown.
