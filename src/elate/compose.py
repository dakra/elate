"""Scenario composition: project config, includes, and step templates.

Applied to a raw scenario before {{var}} templating and validation:

- **Project config** -- the nearest ``.elate.json`` in the scenario's
  directory or an ancestor supplies defaults shared by every scenario of
  a project. The search stops at a repository root (a directory holding
  ``.git``), at $HOME, and before any directory not owned by the current
  user or writable by others; a config file owned by someone else is
  ignored. $ELATE_NO_PROJECT_CONFIG disables the lookup. The config holds
  ``session`` (merged under the scenario's own: scalars and ``env`` keys
  from the scenario win, list options such as ``eval_file`` are
  concatenated config-first), ``defaults``, ``requires``,
  ``timeout_scale``, ``include`` and ``templates``. Relative session
  paths, include paths and ``requires`` executables in it resolve against
  the config file's directory, and its ``requires`` commands run there.
- **Includes** -- ``"include": ["lib/steps.json"]`` reads template
  definitions from other files (relative to the including file; an
  included file holds ``templates`` and may ``include`` further files;
  a ``.eld`` include is an s-expression file).
- **Templates** -- ``"templates": {"open-hints": {"params": {"key":
  "f"}, "steps": [...]}}`` defines a parameterized step group, used as
  ``{"use": "open-hints", "with": {"key": "g"}}``. ``params`` maps each
  parameter to its default (``null`` = required) or is a list of required
  names; ``{{name}}`` in the template's steps is replaced by the bound
  value, and any other ``{{var}}`` is left for scenario templating. A
  ``use`` step may also carry ``comment`` and ``group`` (the group starts
  at the first expanded step). Paths inside template steps resolve
  against the scenario, like the scenario's own steps. Precedence, lowest
  first: config templates, config includes, scenario includes, scenario
  templates; within one layer a name defined twice differently is an
  error.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any

from . import eld
from .errors import ElateError

CONFIG_NAME = ".elate.json"
_CONFIG_KEYS = {"session", "defaults", "requires", "timeout_scale", "include",
                "templates", "comment"}
_INCLUDE_KEYS = {"templates", "include", "comment"}
_TEMPLATE_KEYS = {"params", "steps", "comment"}
_USE_KEYS = {"use", "with", "comment", "group"}
_SESSION_LIST_KEYS = ("load", "require", "eval", "eval_file", "profile")
_SESSION_PATH_KEYS = ("init_file", "home_seed")
_SESSION_PATH_LIST_KEYS = ("load", "eval_file")
_MAX_DEPTH = 10
_VAR_RE = re.compile(r"\{\{\s*([^\s{}]+)\s*\}\}")


def _trusted(path: Path) -> bool:
    """PATH is owned by this user and not writable by group or others."""
    try:
        st = path.stat()
    except OSError:
        return False
    return (st.st_uid == os.getuid()
            and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def find_config(start: Path) -> Path | None:
    """The nearest trusted .elate.json at or above START, or None.

    The walk stops at a repository root (a directory with .git), at
    $HOME, and before any directory another user could plant a config in
    (not owned by us, or group/world-writable -- /tmp and friends): a
    config's session code and requires commands run as the caller.
    """
    start = start.resolve()
    home = Path.home().resolve()
    for directory in (start, *start.parents):
        if not _trusted(directory):
            break
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate if _trusted(candidate) else None
        if directory == home or (directory / ".git").exists():
            break
    return None


def _read_json(path: Path, what: str) -> dict[str, Any]:
    if eld.is_eld(path) and path.is_file():
        data = eld.load(path)
        if not isinstance(data, dict):
            raise ElateError(f"{what} {path} must be a plist")
        return data
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ElateError(f"cannot read {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ElateError(f"{what} {path} must be a JSON object")
    return data


def _abs(path: Any, base: Path, where: str) -> str:
    if not isinstance(path, str):
        raise ElateError(f"{where}: expected a path string, got {path!r}")
    p = Path(path).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())


def _absolute_session(cfg: dict[str, Any], base: Path,
                      where: str) -> dict[str, Any]:
    """CFG's session block with its relative paths anchored at BASE."""
    out = dict(cfg)
    for key in _SESSION_PATH_KEYS:
        if out.get(key) is not None:
            out[key] = _abs(out[key], base, f"{where} session.{key}")
    for key in _SESSION_PATH_LIST_KEYS:
        if isinstance(out.get(key), list):
            out[key] = [_abs(p, base, f"{where} session.{key}")
                        for p in out[key]]
    if isinstance(out.get("profile"), list):
        out["profile"] = [
            _abs(p, base, f"{where} session.profile")
            if isinstance(p, str) and ("/" in p or p.endswith(".el")) else p
            for p in out["profile"]]
    emacs = out.get("emacs")
    if isinstance(emacs, str) and "/" in emacs:
        out["emacs"] = _abs(emacs, base, f"{where} session.emacs")
    return out


def _merge_session(config: dict[str, Any],
                   script: dict[str, Any]) -> dict[str, Any]:
    merged = {**config, **script}
    for key in _SESSION_LIST_KEYS:
        if isinstance(config.get(key), list) and isinstance(script.get(key), list):
            merged[key] = config[key] + script[key]
    if isinstance(config.get("env"), dict) and isinstance(script.get("env"), dict):
        merged["env"] = {**config["env"], **script["env"]}
    return merged


def merge_requires(*blocks: Any) -> dict[str, Any] | None:
    """Combine "requires" blocks: list entries concatenate, commands too."""
    out: dict[str, list[Any]] = {}
    for block in blocks:
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ElateError(
                '"requires" must be an object of env/executable/command')
        for key, val in block.items():
            items = val if isinstance(val, list) else [val]
            out.setdefault(key, []).extend(items)
    return out or None


def apply_config(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    """RAW with the project config at PATH merged underneath it."""
    config = _read_json(path, "project config")
    unknown = sorted(set(config) - _CONFIG_KEYS)
    if unknown:
        raise ElateError(
            f"project config {path}: unknown key(s) {unknown}; known: "
            f"{sorted(_CONFIG_KEYS - {'comment'})}")
    base = path.parent
    where = f"project config {path}"
    out = dict(raw)
    if config.get("session") is not None:
        if not isinstance(config["session"], dict):
            raise ElateError(f'{where}: "session" must be an object')
        script_session = raw.get("session")
        if script_session is not None and not isinstance(script_session, dict):
            return out  # validate_script reports the scenario's own shape error
        out["session"] = _merge_session(
            _absolute_session(config["session"], base, where),
            script_session or {})
    if config.get("defaults") is not None:
        if not isinstance(config["defaults"], dict):
            raise ElateError(f'{where}: "defaults" must be an object')
        own = raw.get("defaults")
        if own is None or isinstance(own, dict):
            out["defaults"] = {**config["defaults"], **(own or {})}
    if config.get("requires") is not None:
        out["requires"] = merge_requires(
            _anchored_requires(config["requires"], base, where),
            raw.get("requires"))
    if config.get("timeout_scale") is not None and "timeout_scale" not in raw:
        out["timeout_scale"] = config["timeout_scale"]
    includes = _include_list(config.get("include"), where)
    if includes:
        out["_config_include"] = [_abs(p, base, where) for p in includes]
    if config.get("templates") is not None:
        out["_config_templates"] = _check_templates(config["templates"], where)
    out["_config_path"] = str(path)
    return out


def _anchored_requires(block: Any, base: Path, where: str) -> Any:
    """A config "requires" whose paths and commands act from BASE."""
    if not isinstance(block, dict):
        raise ElateError(f'{where}: "requires" must be an object of '
                         "env/executable/command")
    out = dict(block)
    as_list = lambda v: v if isinstance(v, list) else [v]  # noqa: E731
    if "executable" in out:
        out["executable"] = [
            _abs(e, base, where) if isinstance(e, str) and "/" in e else e
            for e in as_list(out["executable"])]
    if "command" in out:
        out["command"] = [
            f"cd {shlex.quote(str(base))} && {c}" if isinstance(c, str) else c
            for c in as_list(out["command"])]
    return out


def _include_list(val: Any, where: str) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list) and all(isinstance(v, str) for v in val):
        return list(val)
    raise ElateError(f'{where}: "include" must be a path or a list of paths')


def _check_templates(block: Any, where: str) -> dict[str, Any]:
    if not isinstance(block, dict):
        raise ElateError(f'{where}: "templates" must be an object of name -> '
                         "{params, steps}")
    for name, tpl in block.items():
        tw = f"{where}: template {name!r}"
        if not isinstance(tpl, dict):
            raise ElateError(f"{tw} must be an object with \"steps\"")
        unknown = sorted(set(tpl) - _TEMPLATE_KEYS)
        if unknown:
            raise ElateError(f"{tw}: unknown key(s) {unknown}; known: "
                             f"{sorted(_TEMPLATE_KEYS)}")
        steps = tpl.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ElateError(f'{tw} needs a non-empty "steps" list')
        params = tpl.get("params")
        if params is not None and not (
                (isinstance(params, list)
                 and all(isinstance(p, str) for p in params))
                or (isinstance(params, dict) and all(
                    isinstance(v, str) or v is None for v in params.values()))):
            raise ElateError(
                f'{tw}: "params" must be a list of names or an object of '
                "name -> default string (null = required)")
    return block


def _load_includes(paths: list[str], base: Path, seen: tuple[Path, ...],
                   ) -> dict[str, Any]:
    templates: dict[str, Any] = {}
    origin: dict[str, Path] = {}
    for entry in paths:
        path = Path(_abs(entry, base, "include"))
        if path in seen:
            chain = " -> ".join(str(p) for p in (*seen, path))
            raise ElateError(f"include cycle: {chain}")
        if len(seen) >= _MAX_DEPTH:
            raise ElateError(f"includes nested deeper than {_MAX_DEPTH}")
        data = _read_json(path, "include file")
        unknown = sorted(set(data) - _INCLUDE_KEYS)
        if unknown:
            raise ElateError(
                f"include file {path}: unknown key(s) {unknown}; an include "
                'holds "templates" (and may "include" more files)')
        found = _load_includes(_include_list(data.get("include"), str(path)),
                               path.parent, (*seen, path))
        found.update(_check_templates(data.get("templates") or {},
                                      f"include file {path}"))
        for name, tpl in found.items():
            if name in templates and templates[name] != tpl:
                raise ElateError(
                    f"template {name!r} is defined differently in "
                    f"{origin[name]} and {path}")
            templates[name] = tpl
            origin[name] = path
    return templates


def _bind(node: Any, bindings: dict[str, str]) -> Any:
    """Substitute only the {{names}} BINDINGS knows; leave the rest."""
    if isinstance(node, str):
        return _VAR_RE.sub(
            lambda m: bindings.get(m.group(1), m.group(0)), node)
    if isinstance(node, list):
        return [_bind(x, bindings) for x in node]
    if isinstance(node, dict):
        return {k: _bind(v, bindings) for k, v in node.items()}
    return node


def _expand(steps: list[Any], templates: dict[str, Any],
            stack: tuple[str, ...] = ()) -> list[Any]:
    out: list[Any] = []
    for step in steps:
        if not (isinstance(step, dict) and "use" in step):
            out.append(step)
            continue
        name = step["use"]
        where = f'"use": {name!r}' + (f" (inside {' -> '.join(stack)})"
                                       if stack else "")
        if not isinstance(name, str) or name not in templates:
            known = sorted(templates) or "none"
            raise ElateError(f"{where}: unknown template (defined: {known})")
        if name in stack:
            raise ElateError(f"{where}: template uses itself recursively")
        if len(stack) >= _MAX_DEPTH:
            raise ElateError(f"{where}: templates nested deeper than "
                             f"{_MAX_DEPTH}")
        unknown = sorted(set(step) - _USE_KEYS)
        if unknown:
            raise ElateError(f"{where}: unknown key(s) {unknown} on a use "
                             f"step; known: {sorted(_USE_KEYS)}")
        given = step.get("with") or {}
        if not (isinstance(given, dict) and all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in given.items())):
            raise ElateError(f'{where}: "with" must be an object of '
                             "string -> string")
        tpl = templates[name]
        params = tpl.get("params") or {}
        defaults = ({p: None for p in params} if isinstance(params, list)
                    else dict(params))
        extra = sorted(set(given) - set(defaults))
        if extra:
            raise ElateError(f"{where}: unknown parameter(s) {extra}; the "
                             f"template takes {sorted(defaults) or 'none'}")
        bindings = {k: v for k, v in defaults.items() if v is not None}
        bindings.update(given)
        missing = sorted(k for k in defaults if k not in bindings)
        if missing:
            raise ElateError(f"{where}: missing required parameter(s) "
                             f"{missing}")
        body = _expand(_bind(tpl["steps"], bindings), templates,
                       (*stack, name))
        if "group" in step:
            # A group marker of its own, so the template's first step keeps
            # its shape (and any group it sets itself).
            body = [{"group": step["group"]}, *body]
        out.extend(body)
    return out


def prepare(raw: Any, base: Path, use_config: bool = True) -> Any:
    """Apply project config, includes, and templates to a raw scenario."""
    if not isinstance(raw, dict):
        return raw
    if use_config and not os.environ.get("ELATE_NO_PROJECT_CONFIG"):
        config = find_config(base)
        if config is not None:
            raw = apply_config(raw, config)
    raw = dict(raw)
    # Lowest precedence first: each layer overrides the ones before it.
    templates: dict[str, Any] = dict(raw.pop("_config_templates", None) or {})
    templates.update(_load_includes(raw.pop("_config_include", None) or [],
                                    base, ()))
    templates.update(_load_includes(
        _include_list(raw.pop("include", None), "scenario"), base, ()))
    if raw.get("templates") is not None:
        templates.update(_check_templates(raw.pop("templates"), "scenario"))
    steps = raw.get("steps")
    if isinstance(steps, list) and (templates or any(
            isinstance(s, dict) and "use" in s for s in steps)):
        raw["steps"] = _expand(steps, templates)
    return raw
