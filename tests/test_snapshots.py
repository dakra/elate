"""Golden snapshot assertions in the scenario layer.

Self-contained: each test mints its golden with --update-snapshots into a
tmp tree, then compares -- so it is version-agnostic (no committed goldens
that would need one per Emacs in the CI matrix).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from elate import script as SC
from elate.errors import ElateError

HAVE = bool(shutil.which("emacs") and shutil.which("tmux")
            and shutil.which("emacsclient"))
pytestmark = pytest.mark.skipif(not HAVE, reason="emacs, emacsclient, tmux required")

FACES = {"assert": {"snapshot": {"name": "demo-faces", "of": "faces",
                                 "buffer": "demo"}}}
SCREEN = {"assert": {"snapshot": "demo-screen"}}


@pytest.fixture
def elate_home() -> Iterator[str]:
    # Short prefix: the fresh run-session's emacsclient socket lives under
    # this dir and macOS caps unix-socket paths at ~104 bytes.
    tmp = tempfile.mkdtemp(prefix="esn-")
    old = os.environ.get("ELATE_HOME")
    os.environ["ELATE_HOME"] = tmp
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop("ELATE_HOME", None)
        else:
            os.environ["ELATE_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp: Path, asserts: list, **kw: object) -> dict:
    scn = {
        "name": "snaptest",
        "session": {"ui": "tty", "size": "80x24", "config": "minimal"},
        "steps": [
            {"eval": '(progn (switch-to-buffer (get-buffer-create "demo")) '
                     '(erase-buffer) (insert "hello world") '
                     '(emacs-lisp-mode))'},
            {"wait": "idle"},
            *asserts,
        ],
    }
    p = tmp / "snaptest.json"
    p.write_text(json.dumps(scn))
    sc, base = SC.load_script(str(p))
    return SC.run_script(sc, base_dir=base, snapshot_stem="snaptest", **kw)


def _golden_dir(tmp: Path) -> Path:
    return tmp / "__snapshots__" / "snaptest"


def test_faces_update_then_match(elate_home: str, tmp_path: Path) -> None:
    r1 = _run(tmp_path, [FACES], update_snapshots=True)
    assert r1["success"]
    assert list(_golden_dir(tmp_path).glob("demo-faces@*.faces.json"))
    r2 = _run(tmp_path, [FACES])
    assert r2["success"]
    assert any((s.get("result") or {}).get("status") == "match"
               for s in r2["steps"])


def test_screen_snapshot_round_trip(elate_home: str, tmp_path: Path) -> None:
    assert _run(tmp_path, [SCREEN], update_snapshots=True)["success"]
    assert list(_golden_dir(tmp_path).glob("demo-screen@*.txt"))
    assert _run(tmp_path, [SCREEN])["success"]


def test_missing_golden_fails_in_compare(elate_home: str, tmp_path: Path) -> None:
    r = _run(tmp_path, [FACES])  # no --update-snapshots first
    assert r["success"] is False
    fs = next(s for s in r["steps"] if s["status"] == "failed")
    assert "update-snapshots" in fs["error"]


def test_mismatch_reports_diff(elate_home: str, tmp_path: Path) -> None:
    _run(tmp_path, [FACES], update_snapshots=True)
    g = next(_golden_dir(tmp_path).glob("demo-faces@*.faces.json"))
    g.write_text(g.read_text().replace("hello", "HELLO"))
    r = _run(tmp_path, [FACES])
    assert r["success"] is False
    fs = next(s for s in r["steps"] if s["status"] == "failed")
    assert "diff" in (fs.get("detail") or {})


def test_state_snapshot_round_trip(elate_home: str, tmp_path: Path) -> None:
    st = {"assert": {"snapshot": {"name": "demo-state", "of": "state"}}}
    assert _run(tmp_path, [st], update_snapshots=True)["success"]
    assert _run(tmp_path, [st])["success"]


def test_step_line_renders_snapshot_diff() -> None:
    """A snapshot mismatch's diff must reach the human (not just --json)."""
    from elate import cli
    rec = {"index": 3, "total": 5, "verb": "assert",
           "summary": 'assert snapshot "x"', "status": "failed",
           "duration": 0.1,
           "error": "snapshot 'x' (of faces) differs from golden",
           "detail": {"snapshot": "x",
                      "diff": "--- golden\n+++ actual\n-hello\n+HELLO"}}
    line = cli._step_line(rec, 5)
    assert "differs from golden" in line
    assert "-hello" in line and "+HELLO" in line


def test_validation_rejects_bad_snapshots() -> None:
    bad = [
        {"snapshot": {"name": "../escape"}},          # path-unsafe name
        {"snapshot": {"name": "x", "of": "bogus"}},   # bad of
        {"snapshot": {"name": "x", "buffer": "b"}},   # buffer with of=screen
        {"snapshot": 5},                              # not a name/object
        {"snapshot": {"of": "faces"}},                # no name
        {"snapshot": {"name": "x", "of": "faces", "from": 5, "to": 1}},
    ]
    for spec in bad:
        with pytest.raises(ElateError):
            SC.validate_script({"steps": [{"assert": spec}]})
    # valid forms pass
    SC.validate_script({"steps": [{"assert": {"snapshot": "ok-name"}}]})
    SC.validate_script({"steps": [{"assert": {"snapshot": {
        "name": "ok", "of": "faces", "buffer": "b", "from": 1, "to": 9}}}]})
