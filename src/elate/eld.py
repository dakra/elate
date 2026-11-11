"""S-expression scenarios (``.eld``): read by Emacs, written by elate.

A ``.eld`` scenario is one plist, read by a batch Emacs running
``elate-scenario.el`` (so the Lisp reader decides what a form is) and
turned into the JSON value a ``.json`` scenario holds. Forms are kept as
their source text. :func:`dumps` writes the reverse: JSON values as an
s-expression, with ``eval``/``pred`` strings written as bare forms when
they read back as themselves.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import ElateError
from .paths import scenario_el_path

SUFFIX = ".eld"
FORM_KEYS = frozenset({"eval", "pred"})
WIDTH = 79
_TIMEOUT = 60.0
_SAFE_KEY = re.compile(r"[A-Za-z0-9_+*/<>=!$%&^~@.{}|-]")

_emacs: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "elate_eld_emacs", default=None)


def is_eld(path: str | Path) -> bool:
    return Path(path).suffix.lower() == SUFFIX


def looks_like_eld(text: str) -> bool:
    """TEXT (e.g. a scenario on stdin) is an s-expression, not JSON."""
    head = text.lstrip()
    return head.startswith(("(", ";"))


@contextlib.contextmanager
def reading_with(emacs: str | None) -> Iterator[None]:
    """Read .eld files with EMACS (default: emacs on PATH) in this block."""
    token = _emacs.set(emacs)
    try:
        yield
    finally:
        _emacs.reset(token)


def _reader() -> str:
    emacs = _emacs.get() or shutil.which("emacs")
    if not emacs:
        raise ElateError(".eld scenarios are read and written with Emacs: "
                         "no emacs on PATH (pass --emacs PATH)")
    return str(Path(emacs).expanduser())


def _batch(fn: str, src: Path, out: Path, name: str) -> Any:
    """Run FN on SRC in a batch Emacs; return the JSON value it wrote to OUT.

    Emacs reports an error as {"error": MESSAGE, "line": N} in OUT, named
    here (not by Emacs: argv and stderr use the locale's encoding).
    """
    cmd = [_reader(), "-Q", "--batch", "-l", str(scenario_el_path()),
           "-f", fn, str(src), str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ElateError(f"reading the scenario took over {_TIMEOUT:.0f}s "
                         "in a batch Emacs") from None
    except OSError as exc:
        raise ElateError(f"cannot run {cmd[0]}: {exc}") from exc
    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
        raise ElateError(f"{name}: " + (lines[-1] if lines else
                         f"{cmd[0]} exited {proc.returncode}")) from None
    if proc.returncode != 0:
        if not isinstance(data, dict) or "error" not in data:
            raise ElateError(f"{name}: {cmd[0]} exited {proc.returncode}")
        line = data.get("line")
        raise ElateError(f"{name}:{line}: {data['error']}" if line
                         else f"{name}: {data['error']}")
    return data


def _write_input(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except UnicodeEncodeError as exc:
        raise ElateError(f"the scenario holds text that is not valid "
                         f"Unicode: {exc.reason} at {exc.object[exc.start:exc.end]!r}"
                         ) from None


def load(path: str | Path, name: str | None = None) -> Any:
    """The .eld scenario at PATH as its JSON value."""
    with tempfile.TemporaryDirectory(prefix="elate-eld-") as tmp:
        return _batch("elate-scenario-to-json", Path(path),
                      Path(tmp) / "out.json", name or str(path))


def loads(text: str, name: str = "<stdin>") -> Any:
    """The .eld scenario TEXT as its JSON value."""
    with tempfile.TemporaryDirectory(prefix="elate-eld-") as tmp:
        src = Path(tmp) / "in.eld"
        _write_input(src, text)
        return load(src, name=name)


# ---------------------------------------------------------------------------
# Writing

def _form_candidates(value: Any, under_form_key: bool = False,
                     found: set[str] | None = None) -> set[str]:
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            _form_candidates(item, key in FORM_KEYS, found)
    elif isinstance(value, list):
        for item in value:
            _form_candidates(item, under_form_key and isinstance(item, str),
                             found)
    elif isinstance(value, str) and under_form_key:
        found.add(value)
    return found


def _bare_forms(candidates: set[str]) -> set[str]:
    """The CANDIDATES that read back as themselves when written bare."""
    ordered = sorted(c for c in candidates if c[:1] in "('`#,")
    if not ordered:
        return set()
    with tempfile.TemporaryDirectory(prefix="elate-eld-") as tmp:
        src = Path(tmp) / "in.json"
        _write_input(src, json.dumps(ordered, ensure_ascii=False))
        verdicts = _batch("elate-scenario-check-forms", src,
                          Path(tmp) / "out.json", "<forms>")
    return {s for s, ok in zip(ordered, verdicts) if ok}


def _key(name: str) -> str:
    return ":" + "".join(c if _SAFE_KEY.fullmatch(c) else "\\" + c
                         for c in name)


def _string(text: str) -> str:
    out = []
    for c in text:
        if c in '"\\':
            out.append("\\" + c)
        elif (ord(c) < 32 and c not in "\n\t") or ord(c) == 127:
            out.append(f"\\{ord(c):03o}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


class _Writer:
    def __init__(self, forms: set[str]) -> None:
        self.forms = forms

    def atom(self, value: Any, form: bool) -> str:
        if value is True:
            return "t"
        if value is False:
            return ":false"
        if value is None:
            return "nil"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ElateError(f"cannot write the number {value}")
            return repr(value)
        if isinstance(value, str):
            return value if form and value in self.forms else _string(value)
        raise ElateError(f"cannot write {value!r} as an s-expression")

    @staticmethod
    def _brackets(items: list[Any]) -> tuple[str, str]:
        # A list headed by t/nil/:false would read as a form or a plist.
        if not items or isinstance(items[0], bool) or items[0] is None:
            return "[", "]"
        return "(", ")"

    def inline(self, value: Any, form: bool = False) -> str:
        if isinstance(value, dict):
            if not value:
                return "#s(hash-table)"
            return "(" + " ".join(
                f"{_key(k)} {self.inline(v, k in FORM_KEYS)}"
                for k, v in value.items()) + ")"
        if isinstance(value, list):
            left, right = self._brackets(value)
            return left + " ".join(self.inline(v, form) for v in value) + right
        return self.atom(value, form)

    def render(self, value: Any, col: int, form: bool = False) -> str:
        flat = self.inline(value, form)
        if (col + len(flat) <= WIDTH and "\n" not in flat) \
                or not isinstance(value, (dict, list)) or not value:
            return flat
        pad = "\n" + " " * (col + 1)
        if isinstance(value, dict):
            parts = []
            for k, v in value.items():
                key = _key(k)
                parts.append(f"{key} "
                             + self.render(v, col + 2 + len(key), k in FORM_KEYS))
            return "(" + pad.join(parts) + ")"
        left, right = self._brackets(value)
        return left + pad.join(self.render(v, col + 1, form)
                               for v in value) + right


def dumps(value: Any, verify: bool = True) -> str:
    """VALUE (a scenario's JSON value) as .eld text.

    With VERIFY, the text is read back and must give VALUE again.
    """
    if not isinstance(value, dict) or not value:
        raise ElateError("a scenario to write as .eld must be a non-empty "
                         "JSON object")
    value = json.loads(json.dumps(value))
    text = _Writer(_bare_forms(_form_candidates(value))).render(value, 0)
    text += "\n"
    if verify:
        back = loads(text, name="<converted>")
        if back != value:
            raise ElateError("the .eld conversion does not read back as the "
                             "same scenario; keep the JSON file")
    return text
