"""Replayable scenario scripts: validate, run, export (Phase 5).

Format: JSON, deliberately not YAML. Rationale: zero new runtime
dependency; AI harnesses and CI both emit/parse JSON natively; and every
other elate surface (--json, the MCP contract, the JSONL transcript)
already speaks it, so one grammar serves the whole tool and the
transcript->script exporter is a near-identity mapping. The affordances
YAML would have added are replaced explicitly: a "comment" key is allowed
on every step (a step with only a comment is recorded as a "comment"
annotation, not a skipped step), "skip": true disables a step without
deleting it (the exporter marks its assertion stubs this way), and
"optional": true lets a step fail without failing or stopping the run;
unknown *top-level* keys are ignored so
scripts can carry metadata ("name", "exported_at", ...). Step keys AND
their value types are validated strictly up front -- a typoed option, a
wrong-typed number/bool, a bad enum value, or an option on the wrong
step kind is a loud error before anything boots, never a silent no-op
or a mid-run crash.

A script:

    {
      "name": "demo",
      "params": {"shell": "/bin/zsh"},
      "session": {"ui": "tty", "size": "100x30", "config": "minimal",
                  "load": ["./my-pkg.el"], "eval": ["(my-setup)"],
                  "env": {"SHELL": "{{shell}}"}},
      "defaults": {"timeout": 8, "min_idle": 0.3},
      "steps": [
        {"keys": "M-x my-mode RET"},
        {"wait": "text", "pattern": "ready", "buffer": "*scratch*"},
        {"assert": {"buffer_contains": "ready"}},
        {"test": "my-", "load_files": ["./tests/my-tests.el"]},
        {"assert": {"tests": {"unexpected": 0}}}
      ]
    }

A "params" block declares {{var}} template defaults; every {{var}} in a
string value is substituted before validation, and `--set NAME=VALUE`
(matrix: `--param`) overrides a default -- so one scenario drives N shells
(an unknown {{var}} is a loud error).

Relative paths in a script (session load/init_file, test load_files,
lint files, screenshot output) resolve against the script file's
directory, so a script can live next to the package it tests and run
from any working directory.

`run_script` runs the steps in order against a fresh throwaway session
(the default -- lint executes compile-time code and lint/test results
depend on session history, so a shared session gives non-reproducible
verdicts) and, by default, stops at the first failure; a failed step
embeds a state snapshot, matching the error convention everywhere else
in elate. `keep_going` runs every step regardless (for a regression
matrix that must report every check, not just the first to break).
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import session as S
from .errors import ElateError, WaitTimeout

MAX_STEP_TIMEOUT = 600.0

_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")

VERBS = ("keys", "type", "eval", "wait", "mouse", "focus", "send_events",
         "test", "lint", "screenshot", "resize", "assert")

# Keys allowed on every step besides the verb itself.
#   "optional": a failing step does not fail the run and does not stop it
#   (later steps still run) -- for checks that are allowed to fail.
#   "expect": "fail" (a.k.a. "xfail": true) marks a KNOWN failure: if the
#   step fails it is reported as xfail (non-gating, run continues); if it
#   unexpectedly passes it is an xpass (fails the run -- drop the marker).
#   "reason" annotates why a step is optional/xfail.
#   "group" (sticky) names a test group: the step and every following step
#   belong to it until another "group" appears, so a run reports named
#   verdicts ("dw: PASS, u: XFAIL, cc: FAIL") instead of bare indices. A
#   verb-less {"group": "dw"} step is a pure boundary marker;
#   {"group": null} ends the current group (following steps are ungrouped).
_COMMON_KEYS = {"comment", "skip", "optional", "expect", "xfail", "reason",
                "group"}

# Option keys allowed per verb (mirroring the CLI flags).
_STEP_OPTIONS: dict[str, set[str]] = {
    "keys": {"delivery", "timeout"},
    "type": set(),
    "eval": {"timeout", "buffer"},
    "wait": {"pattern", "buffer", "timeout", "min_idle"},
    "mouse": {"button", "buffer", "pos", "line", "col", "part", "to_pos",
              "to_line", "to_col", "direction", "count", "delivery",
              "timeout"},
    "focus": {"frame", "set_focus_state", "timeout"},
    "send_events": {"buffer", "frame", "set_focus_state", "timeout"},
    "test": {"load_files", "timeout", "allow_unexpected"},
    "lint": {"timeout", "allow_findings"},
    "screenshot": {"ansi"},
    "resize": set(),
    "assert": set(),
}

# Option keys that only apply to a particular wait kind: an option on
# the wrong kind would be silently ignored at run time, which is exactly
# the quiet failure strict validation exists to prevent.
_WAIT_OPTIONS: dict[str, set[str]] = {
    "idle": {"min_idle"},
    "text": {"pattern", "buffer"},
    "prompt": set(),
}

# Assertion kinds -> extra option keys each kind accepts.
_ASSERT_KINDS: dict[str, set[str]] = {
    "buffer_contains": {"buffer"},
    "buffer_matches": {"buffer"},
    "state": set(),
    "messages_match": set(),
    "popup": set(),
    "tests": set(),
    "lint_clean": set(),
    "eval": {"timeout", "buffer"},
    "snapshot": set(),  # options live inside the value object, not as siblings
}

_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SNAPSHOT_OF = ("screen", "faces", "state")
_SNAPSHOT_KEYS = {"name", "of", "buffer", "from", "to", "ansi"}

_SESSION_KEYS = {"ui", "size", "config", "init_file", "load", "eval",
                 "eval_file", "profile", "home_seed", "env",
                 "emacs", "headless", "allow_init_error"}

_DEFAULT_TIMEOUTS = {"keys": 15.0, "eval": 15.0, "wait": 10.0,
                     "mouse": 15.0, "focus": 15.0, "send_events": 15.0,
                     "test": 60.0, "lint": 60.0}


# ---------------------------------------------------------------------------
# Loading & validation

# A {{var}} reference: any single non-space, non-brace token (so kebab-case
# and dotted names like {{my-var}} / {{a.b}} are real references, substituted
# or reported-missing -- never silently left literal). A brace run with
# whitespace inside ({{ not a var }}) is not a reference and stays literal.
_TEMPLATE_RE = re.compile(r"\{\{\s*([^\s{}]+)\s*\}\}")


def _substitute(node: Any, bindings: dict[str, str], missing: set[str]) -> Any:
    """Replace every {{var}} in string values of NODE using BINDINGS.

    Recurses through lists and dict VALUES (keys are never templated); a
    {{var}} with no binding is collected into MISSING (reported at once).
    """
    if isinstance(node, str):
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1)
            if name in bindings:
                return bindings[name]
            missing.add(name)
            return m.group(0)
        return _TEMPLATE_RE.sub(repl, node)
    if isinstance(node, list):
        return [_substitute(x, bindings, missing) for x in node]
    if isinstance(node, dict):
        return {k: _substitute(v, bindings, missing) for k, v in node.items()}
    return node


def template_vars(node: Any) -> set[str]:
    """Every {{var}} name referenced in NODE's string values (recursively)."""
    names: set[str] = set()
    if isinstance(node, str):
        names.update(_TEMPLATE_RE.findall(node))
    elif isinstance(node, list):
        for x in node:
            names |= template_vars(x)
    elif isinstance(node, dict):
        for v in node.values():
            names |= template_vars(v)
    return names


def render_script(raw: Any, overrides: dict[str, str]) -> dict[str, Any]:
    """Apply {{var}} templating to a raw scenario dict; return the result.

    Bindings are the scenario's own "params" block (defaults) overlaid with
    OVERRIDES (CLI --set, matrix --param). The "params" block is consumed
    (not templated, and dropped from the result). An unknown {{var}} is a
    loud error before anything boots. Called once per matrix combo, so it
    must not mutate RAW.
    """
    if not isinstance(raw, dict):
        raise ElateError('a script must be a JSON object with a "steps" list')
    raw = dict(raw)
    params = raw.pop("params", None)
    bindings: dict[str, str] = {}
    if params is not None:
        if not (isinstance(params, dict) and all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in params.items())):
            raise ElateError(
                'script "params" must be an object of string -> string')
        bindings.update(params)
    bindings.update(overrides)
    missing: set[str] = set()
    rendered = _substitute(raw, bindings, missing)
    if missing:
        raise ElateError(
            f"unknown template variable(s) {sorted(missing)}: define them in a "
            '"params" block, or pass --set NAME=VALUE (matrix: --param)')
    return rendered


def load_raw(path: str | Path) -> tuple[Any, Path]:
    """Read a scenario file to a raw value + base_dir (no templating/validation).

    For callers that render per parameter combo (matrix): read the file
    once, then `render_script` + `validate_script` per combo.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ElateError(f"script file does not exist: {path}")
    try:
        return json.loads(p.read_text(encoding="utf-8")), p.parent.resolve()
    except (OSError, ValueError) as exc:
        raise ElateError(f"cannot read script {path}: {exc}") from exc


def load_script(path: str | Path,
                overrides: dict[str, str] | None = None
                ) -> tuple[dict[str, Any], Path]:
    """Read, template, and validate a scenario file; return (script, base_dir).

    OVERRIDES bind {{var}} templates (over any scenario "params" defaults).
    BASE_DIR is the script file's directory: relative paths inside the
    script resolve against it.
    """
    raw, base = load_raw(path)
    script = render_script(raw, overrides or {})
    validate_script(script)
    return script, base


def validate_script(script: Any) -> None:
    """Validate the script shape; raise ElateError with a precise message."""
    if not isinstance(script, dict):
        raise ElateError('a script must be a JSON object with a "steps" list')
    steps = script.get("steps")
    if not isinstance(steps, list):
        raise ElateError('script needs a "steps" list')
    cfg = script.get("session")
    if cfg is not None:
        _validate_session_config(cfg)
    defaults = script.get("defaults")
    if defaults is not None:
        _validate_defaults(defaults)
    if not steps:
        # An empty CI script "passing" vacuously is far more likely a
        # typo (or a truncated export) than intent: be loud.
        raise ElateError(
            'script "steps" is empty -- a script with nothing to run '
            "cannot pass; add steps (or assertions)")
    for index, step in enumerate(steps, 1):
        _validate_step(step, index)


def _check_bool(obj: dict[str, Any], key: str, where: str) -> None:
    val = obj.get(key)
    if val is not None and not isinstance(val, bool):
        raise ElateError(f'{where}: "{key}" must be true or false, got {val!r}')


def _check_str(obj: dict[str, Any], key: str, where: str) -> None:
    val = obj.get(key)
    if val is not None and not isinstance(val, str):
        raise ElateError(f'{where}: "{key}" must be a string, got {val!r}')


def _check_group(step: dict[str, Any], where: str) -> None:
    # null is allowed and means "leave the current group"; a present name
    # must be a non-empty string (it labels a test group).
    g = step.get("group")
    if g is not None and not (isinstance(g, str) and g):
        raise ElateError(
            f'{where}: "group" must be a non-empty string, or null to clear '
            f"the current group, got {g!r}")


def _check_int(obj: dict[str, Any], key: str, where: str,
               minimum: int | None = None, maximum: int | None = None) -> None:
    val = obj.get(key)
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, int):
        raise ElateError(f'{where}: "{key}" must be an integer, got {val!r}')
    if (minimum is not None and val < minimum) or (
            maximum is not None and val > maximum):
        bounds = (f">= {minimum}" if maximum is None
                  else f"between {minimum} and {maximum}")
        raise ElateError(f'{where}: "{key}" must be {bounds}, got {val}')


def _check_number(obj: dict[str, Any], key: str, where: str,
                  minimum: float | None = None,
                  maximum: float | None = None) -> None:
    val = obj.get(key)
    if val is None:
        return
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ElateError(f'{where}: "{key}" must be a number, got {val!r}')
    if (minimum is not None and val < minimum) or (
            maximum is not None and val > maximum):
        bounds = (f">= {minimum:g}" if maximum is None
                  else f"between {minimum:g} and {maximum:g}")
        raise ElateError(f'{where}: "{key}" must be {bounds}, got {val:g}')


def _check_timeout(obj: dict[str, Any], where: str) -> None:
    timeout = obj.get("timeout")
    if timeout is not None and not (
            isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
            and 0 < timeout <= MAX_STEP_TIMEOUT):
        raise ElateError(
            f"{where}: timeout must be a number in (0, {MAX_STEP_TIMEOUT:g}]")


def _check_size(size: Any, what: str) -> None:
    m = _SIZE_RE.match(size) if isinstance(size, str) else None
    if not m:
        raise ElateError(f'{what} must be "COLSxROWS", got {size!r}')
    if int(m.group(1)) < 10 or int(m.group(2)) < 4:
        # Same bounds resize_session enforces; without this, "0x0"
        # reaches tmux and dies with a cryptic boot error.
        raise ElateError(f"{what} {size} is implausible (minimum 10x4)")


_DEFAULTS_KEYS = {"timeout", "min_idle"}


def _validate_defaults(defaults: Any) -> None:
    if not isinstance(defaults, dict):
        raise ElateError('script "defaults" must be an object')
    unknown = set(defaults) - _DEFAULTS_KEYS
    if unknown:
        raise ElateError(
            f'unknown "defaults" key(s) {sorted(unknown)}; '
            f"known: {sorted(_DEFAULTS_KEYS)}")
    _check_timeout(defaults, 'script "defaults"')
    _check_number(defaults, "min_idle", 'script "defaults"',
                  minimum=0, maximum=60)


def _validate_session_config(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        raise ElateError('script "session" must be an object')
    unknown = set(cfg) - _SESSION_KEYS
    if unknown:
        raise ElateError(
            f"unknown session config key(s) {sorted(unknown)}; "
            f"known: {sorted(_SESSION_KEYS)}")
    if cfg.get("ui", "tty") not in ("tty", "gui"):
        raise ElateError(f"session ui must be 'tty' or 'gui', got {cfg.get('ui')!r}")
    _check_size(cfg.get("size", "120x36"), "session size")
    for key in ("headless", "allow_init_error"):
        _check_bool(cfg, key, "session config")
    if cfg.get("config", "minimal") not in S.sandbox.CONFIG_MODES:
        raise ElateError(
            f"unknown session config mode {cfg.get('config')!r} "
            f"(use {'/'.join(S.sandbox.CONFIG_MODES)})")
    if cfg.get("init_file") is not None and cfg.get("config", "minimal") \
            not in ("minimal", "init-file"):
        # Same rule as `elate start`: an init file implies config
        # 'init-file' and must never silently downgrade an explicitly
        # conflicting bare/clean-install mode.
        raise ElateError(
            f'session "init_file" conflicts with config '
            f"{cfg.get('config')!r}; drop one of the two")
    for key in ("load", "eval", "eval_file", "profile"):
        val = cfg.get(key)
        if val is not None and not (
                isinstance(val, list) and all(isinstance(x, str) for x in val)):
            raise ElateError(f'session "{key}" must be a list of strings')
    for key in ("init_file", "emacs", "home_seed"):
        if cfg.get(key) is not None and not isinstance(cfg[key], str):
            raise ElateError(f'session "{key}" must be a string path')
    env = cfg.get("env")
    if env is not None and not (
            isinstance(env, dict)
            and all(isinstance(k, str) and isinstance(v, str)
                    for k, v in env.items())):
        raise ElateError('session "env" must be an object of string -> string')


def _validate_step(step: Any, index: int) -> None:
    where = f"step {index}"
    if not isinstance(step, dict):
        raise ElateError(f"{where}: each step must be a JSON object")
    verbs = [v for v in VERBS if v in step]
    if len(verbs) > 1:
        raise ElateError(f"{where}: a step takes exactly one action, got {verbs}")
    if not verbs:
        if "comment" in step or "group" in step:
            # A pure comment and/or group-boundary marker: an annotation,
            # recorded with status "comment" (it never runs). It runs
            # nothing, so it takes only "comment"/"group" -- any other key
            # is almost certainly a mistyped action verb (e.g. "evl" for
            # "eval") that would silently degrade a real step into a no-op
            # marker, so reject it like every other typo.
            unknown = set(step) - {"comment", "group"}
            if unknown:
                raise ElateError(
                    f'{where}: a comment/group marker runs nothing and takes '
                    f'only "comment"/"group"; unknown key(s) {sorted(unknown)} '
                    "-- did you mistype an action verb?")
            _check_str(step, "comment", where)
            _check_group(step, where)
            return
        raise ElateError(
            f"{where}: no action key (one of: {', '.join(VERBS)}) "
            'and no "comment"/"group"')
    verb = verbs[0]
    allowed = _STEP_OPTIONS[verb] | _COMMON_KEYS | {verb}
    unknown = set(step) - allowed
    if unknown:
        raise ElateError(
            f"{where} ({verb}): unknown key(s) {sorted(unknown)}; "
            f"allowed: {sorted(allowed)}")
    _check_timeout(step, where)
    _check_bool(step, "skip", where)
    _check_bool(step, "optional", where)
    _check_bool(step, "xfail", where)
    _check_str(step, "reason", where)
    _check_group(step, where)
    if step.get("expect") is not None and step.get("expect") not in (
            "pass", "fail"):
        raise ElateError(
            f'{where}: "expect" must be "pass" or "fail", '
            f'got {step.get("expect")!r}')
    if step.get("optional") and (
            step.get("xfail") or step.get("expect") == "fail"):
        # Two different failure-handling modes: "allowed to fail" vs
        # "expected to fail". Combining them is a contradiction (and would
        # make an optional xpass -- a gating-looking label on a green run).
        raise ElateError(
            f'{where}: "optional" and "expect"/"xfail" are mutually exclusive '
            "-- a step is either allowed to fail (optional) or expected to "
            "fail (xfail), not both")
    val = step[verb]
    if verb in ("keys", "type", "eval") and not isinstance(val, str):
        raise ElateError(f'{where}: "{verb}" takes a string')
    if verb == "eval":
        _check_str(step, "buffer", where)
    if verb == "keys" and step.get("delivery", "semantic") not in (
            "semantic", "events", "raw"):
        raise ElateError(
            f"{where}: keys delivery must be semantic/events/raw, "
            f"got {step.get('delivery')!r}")
    if verb == "wait":
        if val not in ("idle", "text", "prompt"):
            raise ElateError(
                f'{where}: wait must be "idle", "text", or "prompt", got {val!r}')
        stray = (set(step) & {"pattern", "buffer", "min_idle"}) - _WAIT_OPTIONS[val]
        if stray:
            raise ElateError(
                f'{where}: option(s) {sorted(stray)} do not apply to wait '
                f'"{val}" (allowed: {sorted(_WAIT_OPTIONS[val]) or "none"})')
        if val == "text":
            pattern = step.get("pattern")
            if not isinstance(pattern, str):
                raise ElateError(
                    f'{where}: wait "text" needs a "pattern" (Python regexp)')
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ElateError(
                    f"{where}: invalid regexp {pattern!r}: {exc}") from exc
            _check_str(step, "buffer", where)
        if val == "idle":
            _check_number(step, "min_idle", where, minimum=0, maximum=60)
    if verb == "mouse":
        if val not in S.MOUSE_ACTIONS:
            raise ElateError(
                f"{where}: mouse action must be one of "
                f"{'/'.join(S.MOUSE_ACTIONS)}, got {val!r}")
        if step.get("delivery", "macro") not in ("macro", "events"):
            raise ElateError(
                f"{where}: mouse delivery must be macro/events, "
                f"got {step.get('delivery')!r}")
        if step.get("part", "text") not in ("text", "mode-line"):
            raise ElateError(
                f"{where}: mouse part must be text/mode-line, "
                f"got {step.get('part')!r}")
        if step.get("direction", "down") not in ("up", "down"):
            raise ElateError(
                f"{where}: mouse direction must be up/down, "
                f"got {step.get('direction')!r}")
        _check_str(step, "buffer", where)
        _check_int(step, "button", where, 1, 3)
        _check_int(step, "count", where, 1, 50)
        for key in ("pos", "line", "to_pos", "to_line"):
            _check_int(step, key, where, minimum=1)
        for key in ("col", "to_col"):
            _check_int(step, key, where, minimum=0)
    if verb == "focus":
        if val not in ("in", "out"):
            raise ElateError(f'{where}: focus must be "in" or "out", got {val!r}')
        _check_str(step, "frame", where)
        _check_bool(step, "set_focus_state", where)
    if verb == "send_events":
        if not (isinstance(val, list) and val
                and all(isinstance(x, str) for x in val)):
            raise ElateError(
                f'{where}: "send_events" takes a non-empty list of event tokens')
        for tok in val:
            try:
                S.parse_event_token(tok)
            except ElateError as exc:
                raise ElateError(f"{where}: {exc}") from exc
        _check_str(step, "buffer", where)
        _check_str(step, "frame", where)
        _check_bool(step, "set_focus_state", where)
    if verb == "test" and not isinstance(val, str):
        raise ElateError(f'{where}: "test" takes an ERT selector string')
    if verb == "test":
        lf = step.get("load_files")
        if lf is not None and not (
                isinstance(lf, list) and all(isinstance(x, str) for x in lf)):
            raise ElateError(f'{where}: "load_files" must be a list of paths')
        _check_bool(step, "allow_unexpected", where)
    if verb == "lint":
        if not (isinstance(val, list) and val
                and all(isinstance(x, str) for x in val)):
            raise ElateError(
                f'{where}: "lint" takes a non-empty list of file paths')
        _check_bool(step, "allow_findings", where)
    if verb == "screenshot":
        if not (val is None or isinstance(val, str)):
            raise ElateError(
                f'{where}: "screenshot" takes an output path or null (inline text)')
        _check_bool(step, "ansi", where)
    if verb == "resize":
        _check_size(val, f'{where}: "resize" size')
    if verb == "assert":
        _validate_assert(val, where)


def _validate_assert(spec: Any, where: str) -> None:
    if not isinstance(spec, dict):
        raise ElateError(f'{where}: "assert" takes an object')
    kinds = [k for k in _ASSERT_KINDS if k in spec]
    if len(kinds) != 1:
        raise ElateError(
            f"{where}: an assertion takes exactly one of "
            f"{', '.join(_ASSERT_KINDS)}; got {kinds or 'none of them'}")
    kind = kinds[0]
    unknown = set(spec) - (_ASSERT_KINDS[kind] | {kind})
    if unknown:
        raise ElateError(
            f"{where} (assert {kind}): unknown key(s) {sorted(unknown)}")
    val = spec[kind]
    if kind in ("buffer_contains", "buffer_matches",
                "messages_match", "eval") and not isinstance(val, str):
        raise ElateError(f'{where}: assert "{kind}" takes a string')
    if kind in ("buffer_contains", "buffer_matches"):
        _check_str(spec, "buffer", f"{where} (assert {kind})")
    if kind == "eval":
        _check_timeout(spec, f"{where} (assert eval)")
        _check_str(spec, "buffer", f"{where} (assert eval)")
    if kind in ("buffer_matches", "messages_match"):
        try:
            re.compile(val)
        except re.error as exc:
            raise ElateError(f"{where}: invalid regexp {val!r}: {exc}") from exc
    if kind == "state":
        if not (isinstance(val, dict) and val):
            raise ElateError(
                f'{where}: assert "state" takes a non-empty object of '
                'state-field (dotted paths ok) -> expected value')
        for path, expected in val.items():
            if _is_operator_spec(expected):
                _validate_state_ops(expected, f"{where} (assert state {path})")
            elif isinstance(expected, dict) and (set(expected) & _STATE_OPS):
                # A dict mixing operators with other keys is almost certainly
                # a typo (it would otherwise silently become an always-failing
                # equality check against the dict).
                raise ElateError(
                    f"{where} (assert state {path}): an operator object's keys "
                    "must all be operators; got non-operator key(s) "
                    f"{sorted(set(expected) - _STATE_OPS)}")
    if kind == "popup" and not (val is True or isinstance(val, str)):
        raise ElateError(
            f'{where}: assert "popup" takes a popup kind string, '
            "or true for any popup")
    if kind == "tests" and not (isinstance(val, dict) and val):
        raise ElateError(
            f'{where}: assert "tests" takes a non-empty object of '
            'count-field -> expected value, e.g. {{"unexpected": 0}}')
    if kind == "lint_clean" and not isinstance(val, bool):
        raise ElateError(f'{where}: assert "lint_clean" takes true or false')
    if kind == "snapshot":
        _validate_snapshot(val, where)


def _validate_snapshot(val: Any, where: str) -> None:
    """Validate a snapshot assert value (a name string, or an options object)."""
    if isinstance(val, str):
        name, obj = val, {}
    elif isinstance(val, dict):
        name, obj = val.get("name"), val
    else:
        raise ElateError(
            f'{where}: assert "snapshot" takes a name string or an object '
            'with at least "name"')
    if not (isinstance(name, str) and _SNAPSHOT_NAME_RE.match(name)):
        raise ElateError(
            f'{where}: snapshot "name" must be a string matching '
            "[A-Za-z0-9._-]+ (it becomes a filename)")
    if not obj:
        return
    unknown = set(obj) - _SNAPSHOT_KEYS
    if unknown:
        raise ElateError(
            f"{where} (assert snapshot): unknown key(s) {sorted(unknown)}")
    of = obj.get("of", "screen")
    if of not in _SNAPSHOT_OF:
        raise ElateError(
            f'{where}: snapshot "of" must be one of {list(_SNAPSHOT_OF)}')
    if of != "faces" and ({"buffer", "from", "to"} & set(obj)):
        raise ElateError(
            f'{where}: snapshot "buffer"/"from"/"to" apply only to of="faces"')
    if of != "screen" and "ansi" in obj:
        raise ElateError(
            f'{where}: snapshot "ansi" applies only to of="screen"')
    if "buffer" in obj and not isinstance(obj["buffer"], str):
        raise ElateError(f'{where}: snapshot "buffer" must be a string')
    if "ansi" in obj and not isinstance(obj["ansi"], bool):
        raise ElateError(f'{where}: snapshot "ansi" must be true or false')
    for k in ("from", "to"):
        if k in obj and not (isinstance(obj[k], int)
                             and not isinstance(obj[k], bool) and obj[k] >= 1):
            raise ElateError(f'{where}: snapshot "{k}" must be an integer >= 1')
    if "from" in obj and "to" in obj and obj["from"] > obj["to"]:
        raise ElateError(f'{where}: snapshot "from" must be <= "to"')


# ---------------------------------------------------------------------------
# Running

class _StepFailure(ElateError):
    """A step ran but did not pass (assertion failed, elisp error, ...)."""

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def run_script(
    script: dict[str, Any],
    *,
    base_dir: Path | None = None,
    session: S.Session | None = None,
    emacs: str | None = None,
    keep: bool = False,
    keep_on_failure: bool = False,
    keep_going: bool = False,
    keep_as_name: str | None = None,
    purge: bool = False,
    deadline: float | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    origin: str | None = None,
    update_snapshots: bool = False,
    snapshot_dir: Path | None = None,
    snapshot_stem: str | None = None,
) -> dict[str, Any]:
    """Execute SCRIPT; return the structured run result.

    With SESSION the steps run against that existing session (its config
    block is ignored, nothing is ever torn down); otherwise a fresh
    throwaway session is created from the script's "session" config
    (EMACS overrides its binary) and stopped afterwards unless KEEP, or
    KEEP_ON_FAILURE and the run failed. A fresh session whose startup
    code signalled an error FAILS the run without executing any step
    (the package under test may not even be loaded) unless the script's
    session config sets "allow_init_error": true. Steps run in order;
    by default the first failure stops the run (later steps are recorded
    as not-run) and the failed step record embeds a state snapshot. With
    KEEP_GOING every step runs regardless of failures (a failed run still
    exits non-zero); a step whose own action fails under "optional": true
    never gates or stops the run, in either mode.
    DEADLINE is a time.monotonic() instant: it is a hard wall -- a step
    not started by then fails, gates, and stops the run regardless of
    KEEP_GOING or "optional" (a blown global budget is not a step
    outcome). ON_STEP is called with each step record as it completes (for
    streaming output). ORIGIN tags the transcript's run-script events
    (e.g. "mcp") for forensics.
    """
    validate_script(script)
    if session is not None and emacs:
        # Loud, never silent: an existing session already runs its own
        # binary; an override cannot retroactively apply to it.
        raise ElateError(
            "an emacs override cannot apply to an existing session; "
            "run the script in a fresh session instead")
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    t_start = time.monotonic()
    fresh = session is None
    if fresh:
        sess = _start_for(script, base, emacs, keep_as_name)
    else:
        sess = session
        sess.require_alive()

    result: dict[str, Any] = {
        "name": script.get("name"),
        "session": sess.name,
        "session_dir": sess.session_dir,
        "fresh_session": fresh,
        "emacs": sess.emacs,
        "emacs_version": sess.emacs_version,
    }
    gate_failed = False  # a real, non-optional failure occurred (fails the run)
    stopped = False      # halt: remaining steps recorded as not-run
    if fresh:
        init_error = sess.init_error()
        if init_error:
            result["init_error"] = init_error
            if not (script.get("session") or {}).get("allow_init_error"):
                # CI contract: a run whose declared setup failed must not
                # pass -- the package under test may not even be loaded.
                result["error"] = (
                    "session startup code signalled an error (no step was "
                    f"run): {init_error} -- fix the session config, or set "
                    '"allow_init_error": true in the script\'s "session" '
                    "block to run regardless")
                _embed_state(result, sess)
                gate_failed = True
                stopped = True
    steps = script.get("steps") or []
    sess.log("run-script", name=script.get("name"), steps=len(steps),
             **({"origin": origin} if origin else {}))

    ctx: dict[str, Any] = {"_defaults": script.get("defaults") or {}}
    snap_root = (Path(snapshot_dir) if snapshot_dir is not None
                 else base / "__snapshots__")
    ctx["_snapshot"] = {
        "dir": snap_root,
        "stem": snapshot_stem or _safe_stem(script.get("name")) or "scenario",
        "update": update_snapshots,
        "emacs_version": sess.emacs_version,
    }
    records: list[dict[str, Any]] = []
    current_group: str | None = None  # sticky: set by any step's "group"
    try:
        for index, step in enumerate(steps, 1):
            verb = next((v for v in VERBS if v in step), None)
            if "group" in step:
                current_group = step["group"]
            rec: dict[str, Any] = {"index": index, "verb": verb,
                                   "summary": _summary(step, verb)}
            if current_group is not None:
                rec["group"] = current_group
            if stopped:
                rec["status"] = "not-run"
            elif verb is None:
                # A pure comment/annotation: it never ran and never can
                # fail, so it is not a "skipped" step (which would inflate
                # the skipped count and the pass/fail math) -- record it
                # distinctly.
                rec["status"] = "comment"
            elif step.get("skip"):
                rec["status"] = "skipped"
            elif deadline is not None and time.monotonic() > deadline:
                rec["status"] = "failed"
                rec["error"] = ("script deadline exceeded before this step; "
                                "raise the run timeout or split the script")
                gate_failed = True
                stopped = True  # every later step would also be past the wall
            else:
                t0 = time.monotonic()
                internal = False  # controller bug: never waived (see below)
                try:
                    rec["result"] = _exec_step(sess, step, verb, base, ctx)
                    rec["status"] = "ok"
                except _StepFailure as exc:
                    rec["status"] = "failed"
                    rec["error"] = str(exc)
                    if exc.detail:
                        rec["detail"] = exc.detail
                    _embed_state(rec, sess)
                except WaitTimeout as exc:
                    rec["status"] = "failed"
                    rec["error"] = str(exc)
                    rec.update(exc.state)  # state/screen_tail as siblings
                except ElateError as exc:
                    rec["status"] = "failed"
                    rec["error"] = str(exc)
                    _embed_state(rec, sess)
                except Exception as exc:
                    # Safety net: a controller-side bug must surface as a
                    # failed step (records kept, structured output, exit 1)
                    # -- never as a raw traceback that discards the run, and
                    # never masked by "optional"/xfail (it is an elate bug,
                    # not a test outcome).
                    traceback.print_exc(file=sys.stderr)
                    rec["status"] = "failed"
                    rec["error"] = f"internal error: {type(exc).__name__}: {exc}"
                    _embed_state(rec, sess)
                    internal = True
                xfail = bool(step.get("xfail")) or step.get("expect") == "fail"
                if internal:
                    gate_failed = True
                    if not keep_going:
                        stopped = True
                elif xfail and rec["status"] in ("ok", "failed"):
                    # A known failure: a failed xfail step is expected
                    # (non-gating, never stops the run -- that is the whole
                    # point of marking it); a passing one is an xpass, which
                    # fails the run so the stale marker gets noticed.
                    rec["status"] = "xfail" if rec["status"] == "failed" \
                        else "xpass"
                    if step.get("reason"):
                        rec["reason"] = step["reason"]
                    if rec["status"] == "xpass" and not step.get("optional"):
                        gate_failed = True
                elif rec["status"] == "failed":
                    if step.get("optional"):
                        rec["optional"] = True  # reported, but never gates
                    else:
                        gate_failed = True
                    if not (keep_going or step.get("optional")):
                        stopped = True
                rec["duration"] = round(time.monotonic() - t0, 3)
            records.append(rec)
            if on_step is not None:
                try:
                    on_step(rec)
                except Exception:
                    pass  # a broken progress printer must not fail the run
    finally:
        success = not gate_failed
        kept = True if not fresh else (keep or (not success and keep_on_failure))
        if fresh and not kept:
            try:
                S.stop_session(sess.name)
            except ElateError as exc:
                result["teardown_error"] = str(exc)

    counts = {status: sum(1 for r in records if r["status"] == status)
              for status in ("ok", "failed", "xfail", "xpass", "skipped",
                             "not-run", "comment")}
    # An optional step that fails is still "failed" status, but it did not
    # gate the run; break it out so the summary can read "PASS ... 1
    # optional-failed" without contradiction.
    optional_failed = sum(1 for r in records
                          if r["status"] == "failed" and r.get("optional"))
    result.update({
        "success": success,
        "kept": kept,
        "passed": counts["ok"],
        "failed": counts["failed"],
        "optional_failed": optional_failed,
        "xfail": counts["xfail"],
        "xpass": counts["xpass"],
        "skipped": counts["skipped"],
        "not_run": counts["not-run"],
        "comment": counts["comment"],
        "duration": round(time.monotonic() - t_start, 3),
        "groups": _aggregate_groups(records),
        "steps": records,
    })
    sess.log("run-script-result", success=success, **counts)
    # Purge the throwaway sandbox LAST -- after the final transcript write,
    # which would otherwise recreate the directory we just removed. Only on a
    # clean successful teardown; a FAILED run is left on disk for post-mortem,
    # and `purge --glob 'run-*'` can sweep it later.
    if (fresh and not kept and purge and success
            and "teardown_error" not in result):
        try:
            S.purge_sessions([sess.name])
            result["purged"] = True
        except ElateError as exc:
            result["teardown_error"] = str(exc)
    return result


_SANE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run_session_name(prefer: str | None) -> str:
    """A kept run's session name: PREFER (sanitized) if usable, else run-<hex>.

    So `run --keep` on a scenario named "evil-ghostel" keeps a session you
    can find by name, while an unnamed throwaway stays run-<hex>. The name
    is capped (it becomes a directory) and cannot squat the reserved
    "run-" throwaway namespace that `purge --glob 'run-*'` sweeps.
    """
    if prefer:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(prefer)).strip("-._")[:64]
        if cleaned.startswith("run-"):
            raise ElateError(
                f"a kept session name cannot start with 'run-' (reserved for "
                f"throwaway run sessions, swept by purge --glob 'run-*'): "
                f"{cleaned!r}")
        if cleaned and _SANE_NAME_RE.match(cleaned):
            return cleaned
    return f"run-{uuid.uuid4().hex[:10]}"


def _start_for(script: dict[str, Any], base: Path,
               emacs_override: str | None,
               prefer_name: str | None = None) -> S.Session:
    cfg = script.get("session") or {}
    m = _SIZE_RE.match(cfg.get("size") or "120x36")
    assert m is not None  # validated
    return S.start_session(
        _run_session_name(prefer_name),
        emacs=emacs_override or cfg.get("emacs"),
        config=cfg.get("config", "minimal"),
        init_file=_resolve(cfg.get("init_file"), base),
        loads=[_resolve(p, base) for p in cfg.get("load") or []],
        evals=list(cfg.get("eval") or []),
        eval_files=[_resolve(p, base) for p in cfg.get("eval_file") or []],
        profiles=list(cfg.get("profile") or []),
        home_seed=_resolve(cfg.get("home_seed"), base),
        env=cfg.get("env") or None,
        cols=int(m.group(1)),
        rows=int(m.group(2)),
        ui=cfg.get("ui", "tty"),
        headless=bool(cfg.get("headless")),
    )


def _resolve(path: str | None, base: Path) -> str | None:
    """Resolve PATH against the script's directory (absolute stays as-is)."""
    if path is None:
        return None
    p = Path(path).expanduser()
    return str(p) if p.is_absolute() else str((base / p).resolve())


def _group_verdict(members: list[dict[str, Any]]) -> str:
    """One label for a group of step records (annotations excluded).

    Precedence is severity-first so the label names the worst thing that
    happened: a real failure, then an xpass (also gating), then a known
    xfail, then an optional-only failure, then pass/skip.
    """
    statuses = [m["status"] for m in members]
    if any(m["status"] == "failed" and not m.get("optional") for m in members):
        return "FAIL"
    if "xpass" in statuses:
        return "XPASS"
    if "xfail" in statuses:
        return "XFAIL"
    if "failed" in statuses:          # optional-only (non-optional caught above)
        return "OPT-FAIL"
    if "ok" in statuses:
        return "PASS"
    if "not-run" in statuses:
        return "not-run"
    return "SKIP"                     # only skipped/comment members


def _aggregate_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll step records into named group verdicts (one entry per name).

    All records sharing a "group" name aggregate into a single entry, in
    first-seen order -- so names are unique (a name that recurs after an
    intervening group still yields one entry, which maps cleanly to a
    single JUnit test-case). A group with only comment/marker members (no
    real step) is dropped; comment records count toward membership but not
    the verdict.
    """
    order: list[str] = []
    by_name: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        name = rec.get("group")
        if name is None:
            continue
        if name not in by_name:
            by_name[name] = []
            order.append(name)
        by_name[name].append(rec)
    groups: list[dict[str, Any]] = []
    for name in order:
        members = by_name[name]
        real = [m for m in members if m["status"] != "comment"]
        if not real:
            continue  # a group that never held a runnable step is not reported
        groups.append({
            "name": name,
            "status": _group_verdict(real),
            "steps": [m["index"] for m in members],
            "passed": sum(1 for m in real if m["status"] == "ok"),
            "failed": sum(1 for m in real if m["status"] == "failed"),
            # optional failures are counted but broken out (they do not gate),
            # mirroring the top-level result so JUnit can tell them apart.
            "optional_failed": sum(1 for m in real
                                   if m["status"] == "failed" and m.get("optional")),
            "xfail": sum(1 for m in real if m["status"] == "xfail"),
            "xpass": sum(1 for m in real if m["status"] == "xpass"),
            "skipped": sum(1 for m in real if m["status"] == "skipped"),
            "not_run": sum(1 for m in real if m["status"] == "not-run"),
        })
    return groups


def _embed_state(rec: dict[str, Any], sess: S.Session) -> None:
    try:
        rec.update(S.state_dump(sess))
    except Exception:
        pass  # diagnostics must never mask the step's own error


def _clip(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary(step: dict[str, Any], verb: str | None) -> str:
    if verb is None:
        if step.get("comment"):
            return f"# {_clip(str(step['comment']))}"
        if step.get("group"):
            return f"# group: {step['group']}"
        if "group" in step:                 # {"group": null}: end the group
            return "# end group"
        return "#"
    val = step[verb]
    if verb == "assert" and isinstance(val, dict):
        kind = next((k for k in _ASSERT_KINDS if k in val), "?")
        return f"assert {kind} {_clip(json.dumps(val.get(kind), ensure_ascii=False))}"
    if verb == "wait":
        extra = f" /{step.get('pattern')}/" if step.get("pattern") else ""
        return f"wait {val}{extra}"
    if verb == "lint" and isinstance(val, list):
        return f"lint {', '.join(Path(f).name for f in val)}"
    if verb == "screenshot":
        return f"screenshot {val or '(inline)'}"
    return f"{verb} {_clip(json.dumps(val, ensure_ascii=False))}"


def _step_timeout(step: dict[str, Any], verb: str,
                  defaults: dict[str, Any]) -> float:
    """A step's timeout: its own, else the scenario default, else the builtin."""
    return float(step.get("timeout",
                          defaults.get("timeout", _DEFAULT_TIMEOUTS[verb])))


def _exec_step(sess: S.Session, step: dict[str, Any], verb: str,
               base: Path, ctx: dict[str, Any]) -> dict[str, Any]:
    defaults = ctx.get("_defaults") or {}
    if verb == "keys":
        delivery = step.get("delivery", "semantic")
        timeout = _step_timeout(step, verb, defaults)
        sess.log("keys", keys=step["keys"],
                 channel="raw" if delivery == "raw" else "semantic",
                 method="events" if delivery == "events" else "macro",
                 via="script")
        if delivery == "raw":
            sess.raw().send_kbd(step["keys"])
            return {"keys": step["keys"], "channel": "raw"}
        method = "events" if delivery == "events" else "macro"
        data = sess.semantic().rpc("keys", step["keys"], method, timeout=timeout)
        return {"keys": step["keys"], "channel": "semantic", **data}

    if verb == "type":
        sess.log("type", text=step["type"],
                 channel="raw" if sess.ui == "tty" else "events", via="script")
        return S.deliver_type(sess, step["type"])

    if verb == "eval":
        timeout = _step_timeout(step, verb, defaults)
        sess.log("eval", form=step["eval"], timeout=timeout,
                 buffer=step.get("buffer"), via="script")
        data = sess.semantic().eval_form(step["eval"], timeout=timeout,
                                         buffer=step.get("buffer"))
        sess.log("eval-result", **data)
        if data.get("error"):
            raise _StepFailure(
                f"elisp error: {data['error']}",
                {k: data.get(k) for k in ("backtrace", "messages") if data.get(k)})
        return data

    if verb == "wait":
        cond = step["wait"]
        timeout = _step_timeout(step, verb, defaults)
        sess.log("wait", condition=cond, pattern=step.get("pattern"),
                 buffer=step.get("buffer"), min_idle=step.get("min_idle"),
                 timeout=timeout, via="script")
        if cond == "idle":
            return S.wait_idle(
                sess,
                min_idle=float(step.get("min_idle",
                                        defaults.get("min_idle", 0.2))),
                timeout=timeout)
        if cond == "text":
            return S.wait_text(sess, step["pattern"],
                               buffer=step.get("buffer"), timeout=timeout)
        return S.wait_prompt(sess, timeout=timeout)

    if verb == "mouse":
        kwargs = dict(
            action=step["mouse"], button=int(step.get("button", 1)),
            buffer=step.get("buffer"), pos=step.get("pos"),
            line=step.get("line"), col=step.get("col"),
            part=step.get("part", "text"), to_pos=step.get("to_pos"),
            to_line=step.get("to_line"), to_col=step.get("to_col"),
            direction=step.get("direction", "down"),
            count=int(step.get("count", 1)),
            delivery=step.get("delivery", "macro"),
        )
        sess.log("mouse", via="script", **kwargs)
        return S.mouse_event(
            sess, timeout=_step_timeout(step, verb, defaults), **kwargs)

    if verb == "focus":
        timeout = _step_timeout(step, verb, defaults)
        set_state = bool(step.get("set_focus_state"))
        sess.log("focus", direction=step["focus"], frame=step.get("frame"),
                 set_focus_state=set_state, via="script")
        return S.focus_event(sess, step["focus"], frame=step.get("frame"),
                             set_focus_state=set_state, timeout=timeout)

    if verb == "send_events":
        timeout = _step_timeout(step, verb, defaults)
        set_state = bool(step.get("set_focus_state"))
        sess.log("send-events", events=step["send_events"],
                 buffer=step.get("buffer"), frame=step.get("frame"),
                 set_focus_state=set_state, via="script")
        return S.send_events(sess, step["send_events"], buffer=step.get("buffer"),
                             frame=step.get("frame"), set_focus_state=set_state,
                             timeout=timeout)

    if verb == "test":
        timeout = _step_timeout(step, verb, defaults)
        load_files = [_resolve(p, base) for p in step.get("load_files") or []]
        sess.log("test", selector=step["test"], load_files=load_files,
                 timeout=timeout, via="script")
        data = S.run_ert(sess, selector=step["test"] or "t",
                         load_files=load_files, timeout=timeout)
        sess.log("test-result", total=data.get("total"),
                 unexpected=data.get("unexpected"),
                 timed_out=data.get("timed-out"))
        ctx["last_test"] = data
        if ((data.get("unexpected") or 0) or data.get("timed-out")) \
                and not step.get("allow_unexpected"):
            raise _StepFailure(
                f"{data.get('unexpected', 0)} unexpected test result(s)"
                + (", run timed out" if data.get("timed-out") else "")
                + ' (set "allow_unexpected": true to assert on counts instead)',
                {"tests": data})
        return data

    if verb == "lint":
        timeout = _step_timeout(step, verb, defaults)
        files = [_resolve(p, base) for p in step["lint"]]
        sess.log("lint", files=files, timeout=timeout, via="script")
        data = S.lint_files(sess, files, timeout=timeout)
        sess.log("lint-result", files=len(data["files"]),
                 items=len(data["items"]))
        ctx["last_lint"] = data
        if not data["clean"] and not step.get("allow_findings"):
            raise _StepFailure(
                f"{len(data['items'])} lint finding(s) "
                '(set "allow_findings": true to assert with lint_clean instead)',
                {"lint": data})
        return data

    if verb == "screenshot":
        return _screenshot_step(sess, step, base)

    if verb == "resize":
        m = _SIZE_RE.match(step["resize"])
        assert m is not None  # validated
        return S.resize_session(sess, int(m.group(1)), int(m.group(2)))

    # assert
    return _eval_assert(sess, step["assert"], ctx)


def _screenshot_step(sess: S.Session, step: dict[str, Any],
                     base: Path) -> dict[str, Any]:
    out = step.get("screenshot")
    ansi = bool(step.get("ansi"))
    if sess.ui == "gui":
        from . import screenshot as shot

        if ansi:
            raise ElateError('"ansi" applies to TTY text screenshots only')
        if out:
            path = Path(_resolve(out, base))
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = sess.dir / "log" / f"screenshot-{stamp}.png"
        result = shot.capture_gui(sess, path)
        sess.log("screenshot", output=result["path"], via="script")
        return result
    if sess.raw().pane_info() is None:
        raise ElateError("no tmux pane left to capture")
    screen = sess.raw().capture_pane(ansi=ansi)
    sess.log("screenshot", ansi=ansi, output=out, via="script")
    if out:
        path = Path(_resolve(out, base))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(screen, encoding="utf-8")
        return {"written": str(path), "ansi": ansi}
    return {"screen": screen, "ansi": ansi}


# Comparison/regex operators for `assert state` values. A value that is a
# dict whose keys are ALL operators is an operator spec (all must hold);
# any other value -- including a plain dict -- is a bare equality check, so
# existing scenarios and dict-valued fields keep working.
_STATE_OPS = {">", ">=", "<", "<=", "!=", "equals", "matches"}
_STATE_NUM_OPS = {">", ">=", "<", "<="}


def _is_operator_spec(expected: Any) -> bool:
    return (isinstance(expected, dict) and bool(expected)
            and all(k in _STATE_OPS for k in expected))


def _validate_state_ops(spec: dict[str, Any], where: str) -> None:
    for op, operand in spec.items():
        if op == "matches":
            if not isinstance(operand, str):
                raise ElateError(f'{where}: "matches" takes a Python regexp string')
            try:
                re.compile(operand)
            except re.error as exc:
                raise ElateError(
                    f"{where}: invalid regexp {operand!r}: {exc}") from exc
        elif op in _STATE_NUM_OPS and (
                isinstance(operand, bool) or not isinstance(operand, (int, float))):
            raise ElateError(f'{where}: "{op}" takes a number, got {operand!r}')


def _apply_state_op(actual: Any, op: str, operand: Any) -> bool:
    # Operators require a present, non-null value: a missing/typo'd path
    # (_dig -> None) or a genuinely null field fails EVERY operator (to
    # assert null, use a bare value: {"mark": null}). Without this, a
    # typo'd field would silently PASS `!=` (None != anything is true).
    if actual is None:
        return False
    if op == "equals":
        return actual == operand
    if op == "!=":
        return actual != operand
    if op == "matches":
        return re.search(str(operand), str(actual)) is not None
    # Numeric comparisons: a non-number actual simply fails (never crashes).
    if (isinstance(actual, bool) or isinstance(operand, bool)
            or not isinstance(actual, (int, float))
            or not isinstance(operand, (int, float))):
        return False
    if op == ">":
        return actual > operand
    if op == ">=":
        return actual >= operand
    if op == "<":
        return actual < operand
    return actual <= operand  # op == "<="


def _state_match(actual: Any, expected: Any) -> bool:
    """True if ACTUAL satisfies EXPECTED (a bare value, or an operator spec)."""
    if _is_operator_spec(expected):
        return all(_apply_state_op(actual, op, v) for op, v in expected.items())
    return actual == expected


def _dig(data: Any, path: str) -> Any:
    """Look up a dotted PATH ("minibuffer.prompt") in nested dicts."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _eval_assert(sess: S.Session, spec: dict[str, Any],
                 ctx: dict[str, Any]) -> dict[str, Any]:
    kind = next(k for k in _ASSERT_KINDS if k in spec)
    val = spec[kind]

    if kind in ("buffer_contains", "buffer_matches"):
        data = sess.semantic().rpc("buffer", spec.get("buffer"))
        text = data.get("text") or ""
        if kind == "buffer_contains":
            ok = val in text
        else:
            ok = re.search(val, text, re.MULTILINE) is not None
        if not ok:
            raise _StepFailure(
                f"buffer {data.get('name')!r} does not "
                + ("contain" if kind == "buffer_contains" else "match")
                + f" {val!r}",
                {"buffer": data.get("name"), "buffer_tail": text[-400:]})
        return {"buffer": data.get("name"), "matched": val}

    if kind == "state":
        state = sess.semantic().rpc("state")
        mismatches = {}
        for path, expected in val.items():
            actual = _dig(state, path)
            if not _state_match(actual, expected):
                mismatches[path] = {"expected": expected, "actual": actual}
        if mismatches:
            raise _StepFailure(f"state mismatch on {sorted(mismatches)}",
                               {"mismatches": mismatches})
        return {"checked": sorted(val)}

    if kind == "messages_match":
        data = sess.semantic().rpc("buffer", "*Messages*")
        text = data.get("text") or ""
        m = re.search(val, text, re.MULTILINE)
        if not m:
            raise _StepFailure(f"*Messages* does not match {val!r}",
                               {"messages_tail": text[-400:]})
        return {"matched": m.group(0)}

    if kind == "popup":
        data = sess.semantic().rpc("popups")
        kinds = [p.get("kind") for p in data.get("popups") or []]
        ok = bool(kinds) if val is True else val in kinds
        if not ok:
            raise _StepFailure(
                "expected popup "
                + ("(any)" if val is True else repr(val))
                + f", visible: {kinds or 'none'}",
                {"popups": kinds})
        return {"popups": kinds}

    if kind == "tests":
        last = ctx.get("last_test")
        if last is None:
            raise _StepFailure('assert "tests" needs a preceding test step')
        mismatches = {k: {"expected": v, "actual": last.get(k)}
                      for k, v in val.items() if last.get(k) != v}
        if mismatches:
            raise _StepFailure(f"test counts mismatch on {sorted(mismatches)}",
                               {"mismatches": mismatches})
        return {"checked": sorted(val)}

    if kind == "lint_clean":
        last = ctx.get("last_lint")
        if last is None:
            raise _StepFailure('assert "lint_clean" needs a preceding lint step')
        if bool(last.get("clean")) != val:
            raise _StepFailure(
                f"lint clean={last.get('clean')}, expected clean={val}",
                {"items": last.get("items")})
        return {"clean": last.get("clean")}

    if kind == "snapshot":
        return _eval_snapshot(sess, val, ctx)

    # kind == "eval": passes when the form evaluates without error to non-nil
    timeout = float(spec.get("timeout",
                             (ctx.get("_defaults") or {}).get("timeout", 10.0)))
    data = sess.semantic().eval_form(val, timeout=timeout,
                                     buffer=spec.get("buffer"))
    if data.get("error"):
        raise _StepFailure(f"assertion form signalled: {data['error']}",
                           {"backtrace": data.get("backtrace")})
    if data.get("value") == "nil":
        raise _StepFailure(f"assertion form returned nil: {val}")
    return {"value": data.get("value")}


# ---------------------------------------------------------------------------
# Golden snapshots

def _safe_stem(name: Any) -> str | None:
    """A scenario name reduced to the golden-filename charset, or None."""
    if not isinstance(name, str) or not name.strip():
        return None
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or None


_STATE_VOLATILE = {"idle", "last-command", "echo", "input-pending", "unread",
                   "messages-tail", "popups", "token", "mode", "since-status",
                   "minibuffer-depth",
                   # absolute path inside the per-run sandbox $HOME -- machine-
                   # and run-specific, so it would make an of:state golden
                   # non-portable for any file-visiting buffer.
                   "file"}


def _canon_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _normalize_faces(data: dict[str, Any]) -> dict[str, Any]:
    """Deterministic projection of a buffer --props result for a golden."""
    return {k: data[k] for k in ("text", "props", "overlays") if k in data}


def _strip_window_text(node: Any) -> Any:
    """Drop geometry/scroll-dependent fields from a window-layout tree."""
    drop = {"text", "text-truncated", "mode-line", "start-line", "end-line",
            "width", "height"}
    if isinstance(node, dict):
        return {k: _strip_window_text(v) for k, v in node.items()
                if k not in drop}
    if isinstance(node, list):
        return [_strip_window_text(n) for n in node]
    return node


def _normalize_state(data: dict[str, Any]) -> dict[str, Any]:
    """Deterministic projection of a state snapshot for a golden."""
    out = {k: v for k, v in data.items() if k not in _STATE_VOLATILE}
    if "windows" in out:
        out["windows"] = _strip_window_text(out["windows"])
    return out


def _capture_snapshot(sess: S.Session, of: str,
                      spec: dict[str, Any]) -> tuple[Any, str]:
    """Capture the current render for OF; return (payload, file-extension).

    Payload is str for text goldens (screen TTY, faces/state JSON) and
    bytes for a GUI PNG.
    """
    if of == "screen":
        if sess.ui == "gui":
            from . import screenshot as shot
            path = sess.dir / "log" / "snapshot-capture.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            shot.capture_gui(sess, path)
            return path.read_bytes(), ".png"
        if sess.raw().pane_info() is None:
            raise _StepFailure("no tmux pane left to snapshot")
        screen = sess.raw().capture_pane(ansi=bool(spec.get("ansi")))
        return screen.rstrip("\n") + "\n", ".txt"
    if of == "faces":
        data = sess.semantic().rpc("buffer", spec.get("buffer"),
                                   spec.get("from"), spec.get("to"), True)
        return _canon_json(_normalize_faces(data)), ".faces.json"
    data = sess.semantic().rpc("state")
    return _canon_json(_normalize_state(data)), ".state.json"


def _eval_snapshot(sess: S.Session, val: Any,
                   ctx: dict[str, Any]) -> dict[str, Any]:
    sopts = ctx.get("_snapshot") or {}
    snap_dir = sopts.get("dir")
    if snap_dir is None:
        raise _StepFailure(
            "snapshot assertions need a snapshot directory (run via "
            "`elate run`/`elate matrix`)")
    spec = {"name": val} if isinstance(val, str) else dict(val)
    name = spec["name"]
    of = spec.get("of", "screen")
    verkey = str(sopts.get("emacs_version") or "0").split(".")[0]
    payload, ext = _capture_snapshot(sess, of, spec)
    path = Path(snap_dir) / sopts.get("stem", "scenario") / f"{name}@{verkey}{ext}"

    if sopts.get("update"):
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            path.write_bytes(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        return {"snapshot": name, "of": of, "path": str(path), "updated": True}

    if not path.exists():
        raise _StepFailure(
            f"no golden snapshot {name!r} for emacs {verkey} at {path}; "
            "create it with `elate run --update-snapshots`",
            {"snapshot": name, "of": of, "path": str(path)})

    if path.suffix == ".png":
        golden = path.read_bytes()
        if golden == payload:
            return {"snapshot": name, "of": of, "status": "match"}
        actual_path = path.with_name(path.stem + ".actual.png")
        try:
            actual_path.write_bytes(payload)
        except OSError:
            actual_path = None
        raise _StepFailure(
            f"snapshot {name!r} (of {of}) differs from golden",
            {"snapshot": name, "golden_bytes": len(golden),
             "actual_bytes": len(payload),
             "actual_written": str(actual_path) if actual_path else None})

    golden = path.read_text(encoding="utf-8")
    if golden == payload:
        return {"snapshot": name, "of": of, "status": "match"}
    diff = list(difflib.unified_diff(
        golden.splitlines(), payload.splitlines(),
        fromfile="golden", tofile="actual", lineterm=""))
    clipped = diff[:200]
    text = "\n".join(clipped)
    if len(diff) > 200:
        text += f"\n... ({len(diff) - 200} more diff lines)"
    raise _StepFailure(f"snapshot {name!r} (of {of}) differs from golden",
                       {"snapshot": name, "diff": text})


# ---------------------------------------------------------------------------
# Transcript -> script export

def export_script(sess: S.Session) -> dict[str, Any]:
    """Convert the session's JSONL transcript into a scenario script.

    Best-effort, not a faithful recorder: inputs (keys/type/eval/mouse/
    wait/test/lint/resize) become steps in transcript order; observations
    (state/buffer/messages/echo/popups/screenshot) become *skipped*
    assertion stubs to be edited into real assertions. Values the
    transcript clipped are exported skipped too, with a comment. The
    emacs binary is deliberately not pinned (use `elate run --emacs` or
    `elate matrix`).
    """
    transcript = sess.dir / "log" / "transcript.jsonl"
    if not transcript.is_file():
        raise ElateError(
            f"session {sess.name!r} has no transcript at {transcript}")
    steps: list[dict[str, Any]] = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        step = _event_step(event)
        if step is None:
            continue
        if _was_clipped(step):
            step["skip"] = True
            note = "value was clipped in the transcript -- restore it by hand"
            step["comment"] = (f"{step['comment']}; {note}"
                               if step.get("comment") else note)
        steps.append(step)
    return {
        "name": f"exported from session {sess.name}",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "comment": ("Best-effort export, not a faithful recording: inputs "
                    "became steps in transcript order; observations became "
                    "skipped assertion stubs. Edit the stubs into real "
                    'assertions and drop their "skip": true.'),
        "session": _session_config(sess),
        "steps": steps,
    }


def _session_config(sess: S.Session) -> dict[str, Any]:
    cfg: dict[str, Any] = {"ui": sess.ui, "size": f"{sess.cols}x{sess.rows}",
                           "config": sess.config}
    if sess.init_file:
        cfg["init_file"] = sess.init_file
    if sess.loads:
        cfg["load"] = list(sess.loads)
    if sess.evals:
        cfg["eval"] = list(sess.evals)
    if sess.headless:
        cfg["headless"] = True
    return cfg


def _was_clipped(obj: Any) -> bool:
    # Substring heuristic: a legitimate payload containing the clip
    # marker exports as a skipped step too. Accepted -- a false
    # "skipped, restore by hand" beats replaying truncated garbage.
    if isinstance(obj, str):
        return "...[clipped, " in obj
    if isinstance(obj, dict):
        return any(_was_clipped(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_was_clipped(v) for v in obj)
    return False


def _stub(spec: dict[str, Any], what: str) -> dict[str, Any]:
    return {"assert": spec, "skip": True,
            "comment": f"observation stub: {what} -- edit, then drop \"skip\""}


def _event_step(e: dict[str, Any]) -> dict[str, Any] | None:  # noqa: C901
    ev = e.get("event")
    if ev == "keys" and isinstance(e.get("keys"), str):
        step: dict[str, Any] = {"keys": e["keys"]}
        if e.get("channel") == "raw":
            step["delivery"] = "raw"
        elif e.get("method") == "events":
            step["delivery"] = "events"
        return step
    if ev == "type" and isinstance(e.get("text"), str):
        return {"type": e["text"]}
    if ev == "eval" and isinstance(e.get("form"), str):
        step = {"eval": e["form"]}
        t = e.get("timeout")
        if isinstance(t, (int, float)) and t != _DEFAULT_TIMEOUTS["eval"]:
            step["timeout"] = t
        return step
    if ev == "wait" and isinstance(e.get("condition"), str):
        cond = e["condition"]
        if cond not in ("idle", "text", "prompt"):
            return None
        step = {"wait": cond}
        args = e.get("args") or []  # CLI logs positionals; MCP logs fields
        if cond == "text":
            pattern = e.get("pattern")
            if pattern is None and args:
                pattern = args[0]
            if not isinstance(pattern, str):
                return None
            step["pattern"] = pattern
            # buffer applies to text waits only (validation rejects it
            # elsewhere); dropping it would silently retarget the wait
            # at replay's current buffer.
            if e.get("buffer"):
                step["buffer"] = e["buffer"]
        elif cond == "idle":
            min_idle = e.get("min_idle")  # MCP/script shape
            if min_idle is None and args:  # CLI shape: positional
                try:
                    min_idle = float(args[0])
                except (TypeError, ValueError):
                    min_idle = None
            if isinstance(min_idle, (int, float)) and min_idle != 0.2:
                step["min_idle"] = min_idle
        t = e.get("timeout")
        if isinstance(t, (int, float)) and t != _DEFAULT_TIMEOUTS["wait"]:
            step["timeout"] = t
        return step
    if ev == "mouse" and isinstance(e.get("action"), str) \
            and e["action"] in S.MOUSE_ACTIONS:
        step = {"mouse": e["action"]}
        for key in ("buffer", "pos", "line", "col",
                    "to_pos", "to_line", "to_col"):
            if e.get(key) is not None:
                step[key] = e[key]
        if e.get("button") not in (None, 1):
            step["button"] = e["button"]
        if e.get("part") == "mode-line":
            step["part"] = "mode-line"
        if e["action"] == "wheel":
            if e.get("direction"):
                step["direction"] = e["direction"]
            if e.get("count") not in (None, 1):
                step["count"] = e["count"]
        if e.get("delivery") == "events":
            step["delivery"] = "events"
        return step
    if ev == "test":
        step = {"test": e.get("selector") or "t"}
        if e.get("load_files"):
            step["load_files"] = list(e["load_files"])
        t = e.get("timeout")
        if isinstance(t, (int, float)) and t != _DEFAULT_TIMEOUTS["test"]:
            step["timeout"] = t
        return step
    if ev == "lint" and isinstance(e.get("files"), list) and e["files"]:
        step = {"lint": list(e["files"])}
        t = e.get("timeout")
        if isinstance(t, (int, float)) and t != _DEFAULT_TIMEOUTS["lint"]:
            step["timeout"] = t
        return step
    if ev == "resize" and e.get("cols") and e.get("rows"):
        return {"resize": f"{e['cols']}x{e['rows']}"}
    if ev == "screenshot":
        return {"screenshot": e.get("output"), "ansi": bool(e.get("ansi")),
                "skip": True,
                "comment": "observation: a screenshot was taken here"}
    # Observations -> skipped assertion stubs.
    if ev == "state":
        return _stub({"state": {"buffer": e.get("buffer") or ""}},
                     "state was inspected here")
    if ev == "buffer":
        spec: dict[str, Any] = {"buffer_contains": ""}
        if e.get("name"):
            spec["buffer"] = e["name"]
        return _stub(spec, "buffer contents were read here")
    if ev == "messages":
        return _stub({"messages_match": ""},
                     "the *Messages* delta was read here")
    if ev == "echo":
        return _stub({"state": {"echo": e.get("echo")}},
                     "the echo area was read here")
    if ev == "popups":
        kinds = e.get("kinds") or []
        return _stub({"popup": kinds[0] if kinds else True},
                     "popups were captured here")
    return None
