"""Phase 5 integration tests: scenario scripts / `elate run`,
transcript export, asciicast recording, snap series, and the
version-matrix helper.

Same conventions as the other integration suites: a real Emacs in a
real tmux; one module-scoped session for the per-session features
(record/snap/export); `elate run` and `matrix` create their own fresh
sessions by design.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from _gui_probe import GUI_UNAVAILABLE_REASON
from elate import cli
from elate import gui
from elate import record as R
from elate import script as SC
from elate import session as S
from elate.errors import ElateError

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux") and shutil.which("emacsclient")
)

pytestmark = pytest.mark.skipif(
    not HAVE_DEPS, reason="emacs, emacsclient, and tmux are required"
)

NAME = f"rc{os.getpid()}"


@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elrec-")
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


@pytest.fixture(scope="module")
def sess(elate_home: str) -> Iterator[S.Session]:
    session = S.start_session(NAME, config="bare", cols=80, rows=24)
    try:
        yield session
    finally:
        try:
            S.stop_session(NAME)
        except Exception:
            session.raw().kill_server()


def write_script(tmp_path: Path, script: dict, name: str = "scenario.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(script, indent=2), encoding="utf-8")
    return str(path)


def running_run_sessions() -> list[str]:
    return [s["name"] for s in S.list_sessions()
            if s["name"].startswith("run-") and s["status"] == "running"]


PASS_SCRIPT = {
    "name": "phase5 smoke",
    "session": {"config": "bare", "size": "80x24"},
    "steps": [
        {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer))'},
        {"keys": "h i RET"},
        {"type": "typed!"},
        {"wait": "text", "pattern": "typ.d!", "buffer": "*scratch*",
         "timeout": 10},
        {"wait": "until", "pred": '(get-buffer "*scratch*")', "timeout": 10},
        {"assert": {"buffer_contains": "hi", "buffer": "*scratch*"}},
        {"assert": {"buffer_matches": "^typ.d!$", "buffer": "*scratch*"}},
        {"assert": {"state": {"buffer": "*scratch*"}}},
        {"eval": '(message "script-marker-77")'},
        {"assert": {"messages_match": "script-marker-7[0-9]"}},
        {"comment": "a pure comment step is recorded as a comment"},
        {"assert": {"eval": "(= (+ 1 2) 3)"}, "skip": True,
         "comment": "explicitly skipped"},
        {"assert": {"eval": "(= (* 6 7) 42)"}},
    ],
}


# -- script validation (no Emacs needed beyond the module skip) ---------------

def test_load_script_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ElateError, match="does not exist"):
        SC.load_script(tmp_path / "nope.json")


def test_load_script_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ElateError, match="cannot read script"):
        SC.load_script(p)


def test_validate_script_errors() -> None:
    cases = [
        ({"steps": "x"}, "steps"),
        ({"steps": [{"keys": "a", "eval": "b"}]}, "exactly one action"),
        ({"steps": [{"frobnicate": 1}]}, "no action key"),
        ({"steps": [{"keys": "a", "speling": 1}]}, "unknown key"),
        ({"steps": [{"keys": "a", "delivery": "psychic"}]}, "delivery"),
        ({"steps": [{"wait": "text"}]}, "pattern"),
        ({"steps": [{"wait": "forever"}]}, "wait"),
        ({"steps": [{"wait": "until"}]}, "pred"),
        ({"steps": [{"wait": "until", "pred": 7}]}, "pred"),
        ({"steps": [{"wait": "until", "pred": "t", "pattern": "x"}]},
         "not apply"),
        ({"steps": [{"eval": "1", "timeout": -3}]}, "timeout"),
        ({"steps": [{"eval": "1", "timeout": 9999}]}, "timeout"),
        ({"steps": [{"lint": []}]}, "non-empty"),
        ({"steps": [{"resize": "big"}]}, "COLSxROWS"),
        ({"steps": [{"assert": {"buffer_matches": "("}}]}, "invalid regexp"),
        ({"steps": [{"assert": {"state": {}}}]}, "non-empty"),
        ({"steps": [{"assert": {"popup": 3}}]}, "popup"),
        ({"steps": [{"assert": {"buffer_contains": "x", "pos": 1}}]},
         "unknown key"),
        ({"steps": [], "session": {"ui": "vr"}}, "ui"),
        ({"steps": [], "session": {"size": "huge"}}, "COLSxROWS"),
        ({"steps": [], "session": {"config": "weird"}}, "config"),
        ({"steps": [], "session": {"frob": 1}}, "unknown session config"),
        # Wrong-TYPED option values are validation errors up front, not
        # mid-run int()/float() crashes (REVIEW-phase5 bug 1).
        ({"steps": [{"mouse": "click", "button": "left"}]}, "button"),
        ({"steps": [{"mouse": "click", "button": True}]}, "button"),
        ({"steps": [{"mouse": "click", "button": 4}]}, "button"),
        ({"steps": [{"mouse": "wheel", "count": "many"}]}, "count"),
        ({"steps": [{"mouse": "wheel", "count": 0}]}, "count"),
        ({"steps": [{"mouse": "click", "pos": "here"}]}, "pos"),
        ({"steps": [{"mouse": "click", "line": 0}]}, "line"),
        ({"steps": [{"mouse": "click", "col": -1}]}, "col"),
        ({"steps": [{"mouse": "click", "delivery": "psychic"}]}, "delivery"),
        ({"steps": [{"mouse": "click", "part": "fringe"}]}, "part"),
        ({"steps": [{"mouse": "wheel", "direction": "left"}]}, "direction"),
        ({"steps": [{"mouse": "click", "buffer": 7}]}, "buffer"),
        ({"steps": [{"wait": "idle", "min_idle": "fast"}]}, "min_idle"),
        ({"steps": [{"wait": "idle", "min_idle": -1}]}, "min_idle"),
        ({"steps": [{"wait": "text", "pattern": "x", "buffer": 9}]}, "buffer"),
        ({"steps": [{"assert": {"eval": "t", "timeout": "soon"}}]}, "timeout"),
        ({"steps": [{"assert": {"buffer_contains": "x", "buffer": 1}}]},
         "buffer"),
        ({"steps": [{"eval": "1", "skip": "yes"}]}, "skip"),
        ({"steps": [{"eval": "1", "optional": "yes"}]}, "optional"),
        ({"steps": [{"eval": "1", "xfail": "yes"}]}, "xfail"),
        ({"steps": [{"eval": "1", "expect": "maybe"}]}, "expect"),
        ({"steps": [{"eval": "1", "reason": 7}]}, "reason"),
        ({"steps": [{"eval": "1", "group": 7}]}, "group"),
        ({"steps": [{"group": 7}]}, "group"),
        ({"steps": [{"group": ""}]}, "group"),          # empty name
        ({"steps": [{"eval": "1", "group": ""}]}, "group"),
        # A typo'd action verb on a grouped step must not be swallowed as a
        # no-op marker (it has extra keys beyond comment/group).
        ({"steps": [{"evl": "(x)", "group": "cc"}]}, "marker"),
        ({"steps": [{"group": "x", "skip": True}]}, "marker"),
        # optional and xfail/expect are mutually exclusive.
        ({"steps": [{"eval": "1", "optional": True, "xfail": True}]},
         "mutually exclusive"),
        ({"steps": [{"eval": "1", "optional": True, "expect": "fail"}]},
         "mutually exclusive"),
        ({"steps": [{"eval": "1"}], "defaults": "x"}, "defaults"),
        ({"steps": [{"eval": "1"}], "defaults": {"bogus": 1}}, "unknown"),
        ({"steps": [{"eval": "1"}], "defaults": {"timeout": -3}}, "timeout"),
        ({"steps": [{"eval": "1"}], "defaults": {"min_idle": 999}}, "min_idle"),
        ({"steps": [{"eval": "1"}], "session": {"env": "x"}}, "env"),
        ({"steps": [{"eval": "1"}], "session": {"env": {"K": 1}}}, "env"),
        ({"steps": [{"eval": "1"}], "session": {"eval_file": [1]}},
         "list of strings"),
        ({"steps": [{"eval": "1"}], "session": {"home_seed": 5}}, "string path"),
        ({"steps": [{"eval": "1", "buffer": 5}]}, "buffer"),
        ({"steps": [{"assert": {"eval": "t", "buffer": 5}}]}, "buffer"),
        ({"steps": [{"assert": {"state": {"point": {">": "x"}}}}]}, "number"),
        ({"steps": [{"assert": {"state": {"m": {"matches": "("}}}}]},
         "invalid regexp"),
        ({"steps": [{"assert": {"state": {"point": {">": 5, "typo": 1}}}}]},
         "must all be operators"),
        ({"steps": [{"test": "t", "allow_unexpected": 1}]}, "allow_unexpected"),
        ({"steps": [{"lint": ["f.el"], "allow_findings": "no"}]},
         "allow_findings"),
        ({"steps": [{"screenshot": None, "ansi": "yes"}]}, "ansi"),
        # Options on the wrong wait kind are loud, not silently ignored.
        ({"steps": [{"wait": "prompt", "pattern": "x"}]}, "not apply"),
        ({"steps": [{"wait": "idle", "buffer": "*scratch*"}]}, "not apply"),
        ({"steps": [{"wait": "text", "pattern": "x", "min_idle": 1}]},
         "not apply"),
        # An invalid wait regexp fails at load time, like buffer_matches.
        ({"steps": [{"wait": "text", "pattern": "("}]}, "invalid regexp"),
        # Empty scripts cannot pass vacuously (REVIEW-phase5 R3).
        ({"steps": []}, "empty"),
        # Implausible sizes are rejected before tmux sees them (R4).
        ({"steps": [{"eval": "1"}], "session": {"size": "0x0"}}, "implausible"),
        ({"steps": [{"resize": "0x0"}]}, "implausible"),
        ({"steps": [{"eval": "1"}],
          "session": {"allow_init_error": "yes"}}, "allow_init_error"),
    ]
    for script, needle in cases:
        with pytest.raises(ElateError, match=needle):
            SC.validate_script(script)


def test_is_transient_path() -> None:
    assert SC._is_transient('(setenv "HOME" "/tmp/eg-home")')
    assert SC._is_transient('(load "/var/folders/ab/helper.el")')
    assert SC._is_transient('(setenv "X" "$TMPDIR/y")')
    assert not SC._is_transient("(setq x 1)")
    assert not SC._is_transient('(load "/home/me/pkg.el")')
    # Anchored: a durable path that merely contains a temp-looking
    # component, or prose mentioning /tmp/, is NOT transient (no data loss).
    assert not SC._is_transient('(load "~/proj/tmp/fixtures.el")')
    assert not SC._is_transient('(message "scratch is in /tmp/ btw")')
    assert not SC._is_transient('(getenv "$TMPDIRECTORY")')


def test_format_junit_and_tap() -> None:
    # Pure formatter test on a synthetic result (no Emacs): a group becomes
    # one testcase (at its first step), ungrouped non-comment steps their own.
    import xml.etree.ElementTree as ET
    result = {
        "name": "demo", "success": False, "duration": 1.25,
        "groups": [
            {"name": "dw", "status": "PASS", "steps": [1, 2]},
            {"name": "u", "status": "XFAIL", "steps": [3]},
            {"name": "cc", "status": "FAIL", "steps": [4]},
        ],
        "steps": [
            {"index": 1, "status": "comment", "group": "dw",
             "summary": "# group: dw"},
            {"index": 2, "status": "ok", "group": "dw", "summary": "assert ok"},
            {"index": 3, "status": "xfail", "group": "u",
             "summary": "assert x", "reason": "known"},
            {"index": 4, "status": "failed", "group": "cc",
             "summary": "assert y", "error": "boom"},
            {"index": 5, "status": "failed", "summary": "eval z",
             "error": "ungrouped boom"},
            {"index": 6, "status": "comment", "summary": "# note"},
        ],
    }
    xml = cli._format_junit(result)
    root = ET.fromstring(xml)                       # well-formed
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "4"              # dw, u, cc, step 5
    assert root.attrib["failures"] == "2"           # cc + ungrouped step 5
    assert root.attrib["skipped"] == "1"            # u (xfail)
    names = [tc.attrib["name"] for tc in root.findall("testcase")]
    assert names == ["group: dw", "group: u", "group: cc", "step 5: eval z"]
    u_case = root.findall("testcase")[1]
    assert u_case.find("skipped") is not None       # xfail -> skipped
    cc_case = root.findall("testcase")[2]
    assert cc_case.find("failure") is not None

    tap = cli._format_tap(result)
    lines = tap.splitlines()
    assert lines[0] == "TAP version 13"
    assert lines[1] == "1..4"
    assert lines[2] == "ok 1 - group: dw"
    assert lines[3] == "ok 2 - group: u # TODO xfail: known failure"
    assert lines[4] == "not ok 3 - group: cc"
    assert lines[5].startswith("not ok 4 - step 5: eval z")


def test_format_junit_tap_edge_cases() -> None:
    import xml.etree.ElementTree as ET
    result = {
        "name": "e\x1bdge", "success": False, "duration": 0.5, "groups": [],
        "steps": [
            {"index": 1, "status": "failed", "summary": "assert x",
             "error": "boom \x1b[31m\x00 red"},         # raw control chars
            {"index": 2, "status": "failed", "optional": True,
             "summary": "opt", "error": "tolerated"},   # optional -> tolerated
            {"index": 3, "status": "xfail", "summary": "kf",
             "reason": "line1\nline2"},                 # newline in reason
        ],
    }
    xml = cli._format_junit(result)
    root = ET.fromstring(xml)                           # well-formed despite ctrl chars
    assert "\x1b" not in xml and "\x00" not in xml
    assert root.attrib["name"] == "edge"                # stripped from suite name
    assert root.attrib["failures"] == "1"               # only the non-optional fail
    assert root.attrib["skipped"] == "2"                # optfail + xfail

    tap = cli._format_tap(result)
    lines = tap.splitlines()
    assert lines[1] == "1..3"
    assert len(lines) == 2 + 3                           # no phantom line from the reason newline
    assert lines[3] == "not ok 2 - step 2: opt # TODO optional failure"
    assert lines[4] == "ok 3 - step 3: kf # TODO xfail: line1 line2"


def test_outcome_unknown_status_surfaces_as_failure() -> None:
    # A future/unknown status must never be silently rendered as a pass.
    assert cli._step_outcome({"status": "weird"})[0] == "fail"
    assert cli._group_outcome({"status": "WEIRD"})[0] == "fail"


def test_sandbox_environment_merges_user_env(tmp_path: Path) -> None:
    from elate import sandbox
    env = sandbox.environment(tmp_path, {"FOO": "bar", "HOME": "/evil"})
    assert env["FOO"] == "bar"                        # user var added
    assert env["HOME"] == str(tmp_path / "home")      # isolation var still wins


def test_sandbox_validate_env_rejects_unsafe_keys() -> None:
    from elate import sandbox
    sandbox.validate_env({"OK_NAME_1": "v", "_x": ""})           # valid POSIX names
    for bad in ({"$(touch /tmp/x)": "v"}, {"FOO BAR": "v"}, {"": "v"},
                {"A=B": "v"}, {"1FOO": "v"}, {"a;b": "v"}):
        with pytest.raises(ElateError, match="env var name"):
            sandbox.validate_env(bad)
    with pytest.raises(ElateError, match="isolation"):
        sandbox.validate_env({"HOME": "/evil"})                  # reserved
    with pytest.raises(ElateError, match="isolation"):
        sandbox.validate_env({"ELATE_SCRATCH": "/elsewhere"})    # reserved


def test_state_match_operators() -> None:
    m = SC._state_match
    assert m(100, {">": 50}) is True
    assert m(100, {">": 500}) is False
    assert m(5, {">=": 1, "<=": 5}) is True            # all operators must hold
    assert m(6, {">=": 1, "<=": 5}) is False
    assert m("fundamental-mode", {"matches": "mode$"}) is True
    assert m("dired", {"matches": "^x"}) is False
    assert m(3, {"!=": 4}) is True
    assert m(3, {"equals": 3}) is True
    assert m("dired-mode", "dired-mode") is True        # bare equality unchanged
    assert m({"a": 1}, {"a": 1}) is True                # non-operator dict = equality
    assert m("x", {">": 1}) is False                    # non-number vs numeric op
    # A missing/null field (actual None) fails EVERY operator -- no silent
    # pass on a typo'd path; assert null with a bare value instead.
    assert m(None, {"matches": "y"}) is False
    assert m(None, {"!=": 1}) is False
    assert m(None, {"equals": None}) is False
    assert m(None, None) is True                         # bare equality asserts null


def test_render_script_templating() -> None:
    raw = {"name": "d", "params": {"a": "1", "b": "B"},
           "session": {"env": {"X": "{{a}}"}},
           "steps": [{"eval": "{{b}}-{{a}}"}]}
    r = SC.render_script(raw, {"a": "override"})
    assert "params" not in r                          # consumed, not in result
    assert r["session"]["env"]["X"] == "override"     # --set overrides the default
    assert r["steps"][0]["eval"] == "B-override"      # default b + overridden a
    assert raw["session"]["env"]["X"] == "{{a}}"      # did not mutate the input
    # An unknown var is a loud error.
    with pytest.raises(ElateError, match="unknown template variable"):
        SC.render_script({"steps": [{"eval": "{{missing}}"}]}, {})
    # params must be string -> string.
    with pytest.raises(ElateError, match="params"):
        SC.render_script({"params": {"a": 1}, "steps": []}, {})
    # A non-{{word}} brace sequence is left literal (no escape needed).
    out = SC.render_script({"steps": [{"eval": "{{a}} {{ not a var }}"}]},
                           {"a": "x"})
    assert out["steps"][0]["eval"] == "x {{ not a var }}"
    # A kebab-case/dotted var IS a reference: substituted when bound...
    assert SC.render_script({"steps": [{"eval": "{{my-var}}"}]},
                            {"my-var": "ok"})["steps"][0]["eval"] == "ok"
    # ...and a loud error when not (never a silent literal passthrough).
    with pytest.raises(ElateError, match="unknown template variable"):
        SC.render_script({"steps": [{"eval": "{{my-var}}"}]}, {})


def test_declared_variants_validation() -> None:
    ok = {"variants": {"a": {"x": "1"}, "b.2-c": {"x": "2", "y": ""}},
          "steps": []}
    assert SC.declared_variants(ok) == ok["variants"]
    assert SC.declared_variants({"steps": []}) == {}      # absent -> {}
    assert SC.declared_variants("not a dict") == {}       # render errors later
    cases = [
        ({"variants": []}, "non-empty object"),           # wrong type
        ({"variants": {}}, "non-empty object"),           # empty block = typo
        ({"variants": {"has space": {}}}, "alphanumeric"),
        ({"variants": {"-lead": {}}}, "alphanumeric"),
        ({"variants": {"z" * 33: {}}}, "max 32"),
        ({"variants": {"a": "x"}}, "string -> string"),
        ({"variants": {"a": {"x": 1}}}, "string -> string"),
        ({"variants": {"a": {"variant": "x"}}}, "reserved"),
    ]
    for raw, msg in cases:
        with pytest.raises(ElateError, match=msg):
            SC.declared_variants(raw)


def test_render_script_variant_bindings() -> None:
    raw = {"name": "on-{{variant}}",
           "params": {"x": "default", "y": "Y"},
           "variants": {"a": {"x": "ax"}, "b": {"x": "bx", "y": "by"}},
           "steps": [{"eval": "{{x}}/{{y}}"}]}
    # Precedence: params default < variant binding < override.
    r = SC.render_script(raw, {}, variant="a")
    assert r["steps"][0]["eval"] == "ax/Y"
    assert r["name"] == "on-a"                        # implicit {{variant}}
    assert "variants" not in r and "params" not in r  # both consumed
    r = SC.render_script(raw, {"x": "over"}, variant="b")
    assert r["steps"][0]["eval"] == "over/by"         # --set wins per variable
    # No selection: params defaults only, {{variant}} binds "".
    r = SC.render_script(raw, {})
    assert r["steps"][0]["eval"] == "default/Y" and r["name"] == "on-"
    assert raw["name"] == "on-{{variant}}"            # did not mutate the input
    # {{variant}} without a variants block is an unknown var like any other.
    with pytest.raises(ElateError, match="unknown template variable"):
        SC.render_script({"steps": [{"eval": "{{variant}}"}]}, {})
    # Loud errors: unknown name (listing the declared ones), a selection
    # without a block, and any attempt to bind the reserved name directly.
    with pytest.raises(ElateError, match=r"unknown variant 'c'.*'a', 'b'"):
        SC.render_script(raw, {}, variant="c")
    with pytest.raises(ElateError, match='no "variants" block'):
        SC.render_script({"steps": []}, {}, variant="a")
    with pytest.raises(ElateError, match="reserved"):
        SC.render_script(raw, {"variant": "x"})
    with pytest.raises(ElateError, match="reserved"):
        SC.render_script({"params": {"variant": "x"}, "steps": []}, {})
    # The missing-var hint mentions variants when a block is declared.
    with pytest.raises(ElateError, match='"params"/"variants" block'):
        SC.render_script({"variants": {"a": {"x": "1"}},
                          "steps": [{"eval": "{{nope}}"}]}, {})


def test_validate_xfail_map() -> None:
    def step(extra: dict) -> dict:
        return {"eval": "t", **extra}

    # Bool form (with or without a reason) stays valid, as does a map.
    SC.validate_script({"steps": [step({"xfail": True, "reason": "why"})]})
    SC.validate_script({"steps": [step({"xfail": {"nu": "no C-_"}})]})
    cases = [
        ({"xfail": "yes"}, "true/false or an object"),
        ({"xfail": {}}, "non-empty object"),
        ({"xfail": {"nu": ""}}, "non-empty reason"),
        ({"xfail": {"nu": 1}}, "non-empty reason"),
        # expect:"fail" would make the step xfail on EVERY variant,
        # silently defeating the map -- loud, never silent.
        ({"xfail": {"nu": "r"}, "expect": "fail"}, "defeating"),
        # The map values ARE the reasons; a sibling "reason" is ambiguous.
        ({"xfail": {"nu": "r"}, "reason": "other"}, "ambiguous"),
        # The existing optional/xfail exclusivity fires for the map too.
        ({"xfail": {"nu": "r"}, "optional": True}, "mutually exclusive"),
    ]
    for extra, msg in cases:
        with pytest.raises(ElateError, match=msg):
            SC.validate_script({"steps": [step(extra)]})


def test_render_script_xfail_map_unknown_variant() -> None:
    # A typo'd map key would silently gate everywhere -- render_script
    # cross-checks every key against the declared variants.
    raw = {"variants": {"a": {"x": "1"}, "b": {"x": "2"}},
           "steps": [{"eval": "{{x}}", "xfail": {"a": "r", "c": "r"}}]}
    with pytest.raises(ElateError, match=r"\['c'\] are not declared"):
        SC.render_script(raw, {}, variant="a")
    # A map-form xfail in a scenario without variants can never match.
    with pytest.raises(ElateError, match='needs a "variants" block'):
        SC.render_script({"steps": [{"eval": "t", "xfail": {"a": "r"}}]}, {})


def test_step_timeout_resolution() -> None:
    # step timeout > scenario default > per-verb builtin.
    assert SC._step_timeout({}, "keys", {}) == SC._DEFAULT_TIMEOUTS["keys"]
    assert SC._step_timeout({}, "eval", {"timeout": 8}) == 8.0
    assert SC._step_timeout({"timeout": 3}, "eval", {"timeout": 8}) == 3.0


def test_screen_tail_retries_past_a_blank_frame(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # A capture that lands mid-redraw is all-blank; _screen_tail retries and
    # returns the repainted content rather than an empty list.
    monkeypatch.setattr(S.time, "sleep", lambda *_a: None)

    class FakeRaw:
        def __init__(self) -> None:
            self.n = 0

        def capture_pane(self, ansi: bool = False,
                         start: int | None = None) -> str:
            self.n += 1
            return "\n\n\n" if self.n < 3 else "top line\nPROMPT>\n"

    class FakeSess:
        ui = "tty"

        def __init__(self) -> None:
            self._raw = FakeRaw()

        def raw(self) -> Any:
            return self._raw

    assert S._screen_tail(FakeSess()) == ["top line", "PROMPT>"]


def test_screen_tail_falls_back_to_scrollback(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # If the live frame stays blank, fall back to scrollback history.
    monkeypatch.setattr(S.time, "sleep", lambda *_a: None)
    starts: list[int | None] = []

    class FakeRaw:
        def capture_pane(self, ansi: bool = False,
                         start: int | None = None) -> str:
            starts.append(start)
            return "hist one\nhist two\n" if start is not None else "\n\n"

    class FakeSess:
        ui = "tty"

        def raw(self) -> Any:
            return FakeRaw()

    assert S._screen_tail(FakeSess()) == ["hist one", "hist two"]
    assert any(s is not None and s < 0 for s in starts)   # reached into history


def test_run_script_bad_types_clean_cli_error(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """REVIEW-phase5 bug 1 repros: a wrong-typed option value must be a
    structured validation error (valid --json, exit 1), never a raw
    int()/float() traceback -- and no session is booted for it."""
    for step in ({"mouse": "click", "button": "left"},
                 {"wait": "idle", "min_idle": "fast"},
                 {"assert": {"eval": "t", "timeout": "soon"}}):
        path = write_script(tmp_path, {"steps": [step]})
        code = cli.main(["--json", "run", path])
        out = json.loads(capsys.readouterr().out)
        assert code == 1, step
        assert out["ok"] is False and "must be" in out["error"]
    assert running_run_sessions() == []


def test_event_step_wait_shapes() -> None:
    """Pin the exported wait-step fields for both producer shapes
    (REVIEW-phase5 bug 3: buffer/min_idle must survive the round trip)."""
    # CLI shape: positional args list + the (now logged) buffer field.
    assert SC._event_step({"event": "wait", "condition": "text",
                           "args": ["pat"], "buffer": "*Messages*",
                           "timeout": 5.0}) == {
        "wait": "text", "pattern": "pat", "buffer": "*Messages*",
        "timeout": 5.0}
    assert SC._event_step({"event": "wait", "condition": "idle",
                           "args": ["1.5"], "timeout": 10.0}) == {
        "wait": "idle", "min_idle": 1.5}
    # MCP/script shape: named min_idle field.
    assert SC._event_step({"event": "wait", "condition": "idle",
                           "min_idle": 1.0, "timeout": 10.0,
                           "via": "mcp"}) == {"wait": "idle", "min_idle": 1.0}
    # Defaults are omitted so exported scripts stay minimal.
    assert SC._event_step({"event": "wait", "condition": "idle",
                           "min_idle": 0.2, "timeout": 10.0}) == {"wait": "idle"}
    # buffer never leaks onto non-text waits (it would not validate).
    assert SC._event_step({"event": "wait", "condition": "prompt",
                           "buffer": "*scratch*", "timeout": 10.0}) == {
        "wait": "prompt"}
    # until: CLI shape (pred in args[0]) and MCP/script shape (pred field).
    assert SC._event_step({"event": "wait", "condition": "until",
                           "args": ["(featurep 'x)"], "timeout": 10.0}) == {
        "wait": "until", "pred": "(featurep 'x)"}
    assert SC._event_step({"event": "wait", "condition": "until",
                           "pred": "t", "buffer": "b", "timeout": 10.0,
                           "via": "mcp"}) == {
        "wait": "until", "pred": "t", "buffer": "b"}


# -- elate run ----------------------------------------------------------------

def test_run_script_passes_and_tears_down(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, PASS_SCRIPT)
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True and out["success"] is True
    assert out["fresh_session"] is True and out["kept"] is False
    assert out["emacs_version"]
    assert out["passed"] == 11 and out["failed"] == 0
    # The pure-comment step is now a "comment" annotation, not a skipped
    # step; only the explicit "skip": true step counts as skipped.
    assert out["skipped"] == 1 and out["comment"] == 1 and out["not_run"] == 0
    statuses = [s["status"] for s in out["steps"]]
    assert statuses.count("skipped") == 1
    assert statuses.count("comment") == 1
    assert all(s["status"] in ("ok", "skipped", "comment") for s in out["steps"])
    # Fresh session torn down: nothing left running.
    assert running_run_sessions() == []
    # A successful run purges its throwaway sandbox by default (no pile-up).
    assert out["purged"] is True
    assert not os.path.isdir(out["session_dir"])


def test_run_with_field_prints_only_the_field(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # --field must suppress per-step progress streaming: the bare value is
    # the WHOLE stdout, so $(elate --field success run x.json) is clean.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 2)"}],
    })
    code = cli.main(["--field", "success", "run", path])
    assert code == 0
    assert capsys.readouterr().out == "true\n"


def test_run_keep_names_from_scenario(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # --keep names the kept session from the scenario "name" (sanitized), not
    # an opaque run-<hex>.
    path = write_script(tmp_path, {
        "name": "My Kept Run!",
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)"}],
    }, "named.json")
    code = cli.main(["--json", "run", path, "--keep"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["kept"] is True
    assert out["session"] == "My-Kept-Run"
    assert S.load_session(out["session"]).is_alive()
    assert cli.main(["--json", "stop", out["session"]]) == 0


def test_run_keep_name_guards(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)"}],
    }, "g.json")
    # --name requires --keep.
    code = cli.main(["--json", "run", path, "--name", "foo"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "requires --keep" in out["error"]
    # A kept name cannot squat the throwaway run- namespace.
    code = cli.main(["--json", "run", path, "--keep", "--name", "run-foo"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "run-" in out["error"]
    # A very long name is capped, so it can't crash mkdir with a raw
    # OSError -- the run completes cleanly (parseable JSON, an int code),
    # whether or not the capped name then fits the socket path.
    code = cli.main(["--json", "run", path, "--keep", "--name", "z" * 300])
    out = json.loads(capsys.readouterr().out)   # clean JSON => no traceback
    assert code in (0, 1)
    if out.get("session"):
        assert len(out["session"]) <= 64
        cli.main(["--json", "stop", out["session"]])
        capsys.readouterr()


def test_run_no_purge_and_failure_keeps_sandbox(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # --no-purge keeps a successful run's sandbox on disk...
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)"}],
    }, "np.json")
    code = cli.main(["--json", "run", path, "--no-purge"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and not out.get("purged")
    assert os.path.isdir(out["session_dir"])
    # ...and a FAILED run is always kept (never purged), for post-mortem.
    fpath = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "nil"}}],
    }, "fail.json")
    code = cli.main(["--json", "run", fpath])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and not out.get("purged")
    assert os.path.isdir(out["session_dir"])
    assert running_run_sessions() == []


def test_purge_glob_and_name_prefix(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # Failed runs leave stopped run-<hex> sandboxes; purge --glob sweeps them.
    # (elate_home is module-scoped, so track our own sessions, not a count.)
    mine = []
    for i in range(2):
        p = write_script(tmp_path, {
            "session": {"config": "bare", "size": "80x24"},
            "steps": [{"assert": {"eval": "nil"}}],
        }, f"f{i}.json")
        cli.main(["--json", "run", p])
        out = json.loads(capsys.readouterr().out)
        assert out["session"].startswith("run-") and not out.get("purged")
        mine.append(out["session"])
    code = cli.main(["--json", "purge", "--glob", "run-*"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(mine) <= {p["name"] for p in out["purged"]}      # ours were swept
    # A follow-up prefix sweep no longer finds them.
    cli.main(["--json", "purge", "--name-prefix", "run-"])
    out = json.loads(capsys.readouterr().out)
    assert not (set(mine) & {p["name"] for p in out["purged"]})
    # Glob/prefix cannot combine with explicit names.
    code = cli.main(["--json", "purge", "--glob", "run-*", "somename"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "cannot be combined" in out["error"]


def test_run_script_failing_assertion(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    script = {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"type": "present"},
            {"wait": "text", "pattern": "present", "buffer": "*scratch*"},
            {"assert": {"buffer_contains": "absent-xyzzy"}},
            {"eval": "(never-runs)"},
        ],
    }
    path = write_script(tmp_path, script)
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is True  # the run executed; the script failed
    assert out["success"] is False
    failed = out["steps"][2]
    assert failed["status"] == "failed"
    assert "absent-xyzzy" in failed["error"]
    # The failed step embeds the state snapshot (existing convention).
    assert "state" in failed or "screen_tail" in failed
    assert failed["detail"]["buffer_tail"]
    assert out["steps"][3]["status"] == "not-run"
    assert running_run_sessions() == []


def test_run_script_keep_going_runs_every_step(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # With --keep-going a failure does not stop the run: every later step
    # still executes, and every failure is reported (not just the first).
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"assert": {"eval": "(= 1 2)"}},   # fails
            {"eval": "(+ 2 2)"},               # would be not-run without --keep-going
            {"assert": {"eval": "(= 3 4)"}},   # also fails -- still reported
        ],
    })
    code = cli.main(["--json", "run", path, "--keep-going"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False   # a failed run still exits 1
    assert out["steps"][0]["status"] == "failed"
    assert out["steps"][1]["status"] == "ok"       # ran despite the earlier failure
    assert out["steps"][2]["status"] == "failed"
    assert out["failed"] == 2 and out["not_run"] == 0
    assert running_run_sessions() == []


def test_run_script_optional_step_does_not_gate_or_stop(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A step marked "optional" may fail without failing the run and without
    # stopping it -- even in the default fail-fast mode (no --keep-going).
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"assert": {"eval": "(= 1 2)"}, "optional": True},  # fails, but optional
            {"eval": "(+ 2 2)"},                                # still runs
        ],
    })
    code = cli.main(["--json", "run", path])   # default: fail-fast
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True
    assert out["steps"][0]["status"] == "failed"
    assert out["steps"][0]["optional"] is True
    assert out["steps"][1]["status"] == "ok"
    # Raw "failed" count includes the optional failure; it is broken out so
    # a passing run never reads as failed.
    assert out["failed"] == 1 and out["optional_failed"] == 1
    assert running_run_sessions() == []


def test_run_script_xfail_known_failure_does_not_gate(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A step marked expect:"fail" that DOES fail is a known/expected
    # failure: it is reported as xfail, does not fail the run, and does
    # not stop it (later steps still run) -- even without --keep-going.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"assert": {"eval": "(= 1 2)"}, "expect": "fail",
             "reason": "known-broken"},
            {"eval": "(+ 1 1)"},
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True
    assert out["steps"][0]["status"] == "xfail"
    assert out["steps"][0]["reason"] == "known-broken"
    assert out["steps"][1]["status"] == "ok"
    assert out["xfail"] == 1 and out["failed"] == 0
    assert running_run_sessions() == []


def test_run_script_xpass_fails_the_run(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A step marked xfail that unexpectedly PASSES is an xpass: it fails
    # the run (drop the stale marker) but does not stop it.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"assert": {"eval": "(= 1 1)"}, "xfail": True,
             "reason": "should still be broken"},
            {"eval": "(+ 1 1)"},
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert out["steps"][0]["status"] == "xpass"
    assert out["steps"][1]["status"] == "ok"   # xpass does not stop the run
    assert out["xpass"] == 1
    assert running_run_sessions() == []


def test_run_script_xfail_does_not_mask_internal_error(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    # A controller-side bug (the except-Exception safety net) is an elate
    # fault, not a test outcome: it must gate the run (exit 1) even on an
    # xfail-marked step -- otherwise the "always surfaces" guarantee breaks.
    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("controller boom")
    monkeypatch.setattr(SC, "_exec_step", boom)
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)", "xfail": True}],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert out["steps"][0]["status"] == "failed"   # NOT reclassified to xfail
    assert "internal error" in out["steps"][0]["error"]
    assert out["xfail"] == 0
    assert running_run_sessions() == []


def test_run_script_named_groups(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A "group" is sticky: a {"group": ...} boundary (or an inline "group"
    # on a step) names the following steps, and the run reports one verdict
    # per group.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"group": "dw"},                      # boundary marker
            {"assert": {"eval": "(= 1 1)"}},      # dw: ok
            {"assert": {"eval": "(= 2 2)"}},      # dw: ok
            {"group": "u", "assert": {"eval": "(= 1 2)"},
             "expect": "fail"},                   # u: xfail (inline group)
            {"group": "cc"},
            {"assert": {"eval": "(= 3 4)"}},      # cc: fail
        ],
    })
    code = cli.main(["--json", "run", path, "--keep-going"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert [g["name"] for g in out["groups"]] == ["dw", "u", "cc"]
    groups = {g["name"]: g for g in out["groups"]}
    assert groups["dw"]["status"] == "PASS" and groups["dw"]["passed"] == 2
    assert groups["dw"]["steps"] == [1, 2, 3]   # boundary marker + 2 asserts
    assert groups["u"]["status"] == "XFAIL" and groups["u"]["xfail"] == 1
    assert groups["cc"]["status"] == "FAIL" and groups["cc"]["failed"] == 1
    # The human summary renders the per-group verdict line.
    summary = cli._run_summary(out)
    assert "dw: PASS" in summary
    assert "u: XFAIL" in summary
    assert "cc: FAIL" in summary
    assert running_run_sessions() == []


def test_run_script_group_merge_clear_and_empty(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A recurring group name merges into one entry; {"group": null} ends the
    # current group; a group that never holds a runnable step is dropped.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"group": "empty"},                # 1: no real step -> dropped
            {"group": "dw"},                   # 2
            {"assert": {"eval": "(= 1 1)"}},   # 3: dw ok
            {"group": "cc"},                   # 4
            {"assert": {"eval": "(= 2 2)"}},   # 5: cc ok
            {"group": "dw"},                   # 6: dw recurs -> same entry
            {"assert": {"eval": "(= 3 3)"}},   # 7: dw ok
            {"group": None},                   # 8: end the group
            {"eval": "(+ 1 1)"},               # 9: ungrouped
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True
    assert [g["name"] for g in out["groups"]] == ["dw", "cc"]  # "empty" dropped
    groups = {g["name"]: g for g in out["groups"]}
    assert groups["dw"]["passed"] == 2                # both dw asserts merged
    assert groups["dw"]["steps"] == [2, 3, 6, 7]
    # The trailing step after {"group": null} belongs to no group.
    assert not any(9 in g["steps"] for g in out["groups"])
    assert running_run_sessions() == []


def test_run_format_junit_plumbing(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # --format junit overrides the global output mode, emits a valid XML
    # document, exits 1 on failure, and suppresses per-step streaming.
    import xml.etree.ElementTree as ET
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"group": "ok"},
            {"assert": {"eval": "(= 1 1)"}},
            {"group": "bad"},
            {"assert": {"eval": "(= 1 2)"}},
        ],
    })
    code = cli.main(["run", path, "--format", "junit", "--keep-going"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("<?xml")               # no streaming leaked ahead of it
    root = ET.fromstring(out)                     # the CLI emitted valid XML
    assert root.attrib["failures"] == "1"
    names = {tc.attrib["name"] for tc in root.findall("testcase")}
    assert names == {"group: ok", "group: bad"}
    assert running_run_sessions() == []


def test_run_script_defaults_timeout_applies(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A scenario "defaults.timeout" replaces the built-in per-verb timeout:
    # an eval that would pass under the 15s eval default times out under 1s.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "defaults": {"timeout": 1.0},
        "steps": [{"eval": "(sleep-for 3)"}],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert out["steps"][0]["status"] == "failed"
    assert out["steps"][0]["duration"] < 2.5        # timed out ~1s, not ~3s
    assert running_run_sessions() == []


def test_run_script_tty_failure_embeds_screen_tail(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A failed step in a tty session embeds a non-empty pane snapshot.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                     '(insert "eg-screen-marker"))'},
            {"assert": {"buffer_contains": "absent-nope-qqq"}},
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    failed = out["steps"][1]
    assert failed["status"] == "failed"
    tail = failed.get("screen_tail")
    assert tail and any(ln.strip() for ln in tail)   # populated, non-blank
    assert running_run_sessions() == []


def test_run_script_session_parity(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # eval_file, home_seed, and env all reach a run scenario's session (the
    # parity `start` already had).
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / ".myrc").write_text("hello-rc", encoding="utf-8")
    evalfile = tmp_path / "setup.el"
    evalfile.write_text("(setq eg-parity-loaded t)", encoding="utf-8")
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24",
                    "eval_file": [str(evalfile)],
                    "home_seed": str(seed),
                    "env": {"EG_PARITY": "yes-parity"}},
        "steps": [
            {"assert": {"eval": "(bound-and-true-p eg-parity-loaded)"}},
            {"assert": {"eval": '(equal (getenv "EG_PARITY") "yes-parity")'}},
            {"assert": {"eval": '(file-exists-p "~/.myrc")'}},
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True and out["passed"] == 3
    assert running_run_sessions() == []


def test_run_script_env_cannot_override_isolation(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # env must not clobber the sandbox $HOME isolation -- the run fails to
    # start with a loud error rather than silently escaping the sandbox.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24",
                    "env": {"HOME": "/tmp/evil"}},
        "steps": [{"eval": "1"}],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert "isolation" in out["error"] or "HOME" in out["error"]


def test_run_script_env_key_injection_is_rejected(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # An env KEY containing a shell command-substitution must be rejected up
    # front and must NEVER execute on the host (it used to inject via the
    # tmux `sh -c` env prefix).
    marker = tmp_path / "pwned"
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24",
                    "env": {f"$(touch {marker})": "x"}},
        "steps": [{"eval": "1"}],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert "env var name" in out["error"]
    assert not marker.exists()          # the injected command never ran


def test_run_script_eval_buffer_context(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # eval/assert-eval default to the SELECTED window's buffer (so a
    # current-line read sees content, not ""), and "buffer" targets another.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                     '(insert "visible-line"))'},
            # Default buffer = the visible one: the current line is readable.
            {"assert": {"eval": '(equal (buffer-substring-no-properties '
                                '(line-beginning-position) (line-end-position)) '
                                '"visible-line")'}},
            {"eval": '(with-current-buffer (get-buffer-create "eg-other") '
                     '(insert "other-content"))'},
            # An eval STEP with "buffer" runs in that buffer.
            {"eval": '(insert " more")', "buffer": "eg-other"},
            # An assert-eval with "buffer" reads that buffer.
            {"assert": {"eval": '(equal (buffer-string) "other-content more")',
                        "buffer": "eg-other"}},
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True, out
    assert running_run_sessions() == []


def test_run_script_templating_set_overrides(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A scenario `params` default renders and runs; --set substitutes a
    # different value (observably changing the outcome).
    path = write_script(tmp_path, {
        "params": {"n": "1"},
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "(= {{n}} 1)"}}],
    })
    assert cli.main(["--json", "run", path]) == 0          # default n=1 -> (= 1 1)
    capsys.readouterr()
    code = cli.main(["--json", "run", path, "--set", "n=2"])  # -> (= 2 1)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert running_run_sessions() == []


def test_run_script_assert_state_operators(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # assert state values may be comparison/regex operators, not just equality.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                     '(insert "abcdef") (goto-char 4))'},
            {"assert": {"state": {"point": {">": 1, "<": 100},
                                  "column": {">=": 0}}}},
            {"assert": {"state": {"major-mode": {"matches": "mode"}}}},
            # A failing operator (kept optional so the run stays green) proves
            # a negative actually rejects.
            {"assert": {"state": {"point": {">": 99999}}}, "optional": True},
        ],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True, out
    assert out["steps"][3]["status"] == "failed"   # the >99999 operator rejected
    assert running_run_sessions() == []


def test_run_script_failing_step_elisp_error(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": '(error "step boom")'}],
    })
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    failed = out["steps"][0]
    assert "step boom" in failed["error"]
    assert failed["detail"]["backtrace"]


def test_run_script_keep_and_keep_on_failure(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # --keep on success.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)"}],
    }, "keep.json")
    code = cli.main(["--json", "run", path, "--keep"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["kept"] is True
    name = out["session"]
    assert S.load_session(name).is_alive()
    assert cli.main(["--json", "stop", name]) == 0
    capsys.readouterr()
    # --keep-on-failure keeps only failing runs.
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "nil"}}],
    }, "keepfail.json")
    code = cli.main(["--json", "run", path, "--keep-on-failure"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["kept"] is True
    name = out["session"]
    assert S.load_session(name).is_alive()
    assert cli.main(["--json", "stop", name]) == 0
    capsys.readouterr()


def test_run_script_against_existing_session(
        sess: S.Session, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, {
        # The session config is ignored when -s targets an existing session.
        "session": {"config": "minimal", "size": "100x44"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer))'},
            {"type": "in-existing"},
            {"wait": "text", "pattern": "in-existing", "buffer": "*scratch*"},
        ],
    }, "existing.json")
    code = cli.main(["--json", "-s", NAME, "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True
    assert out["fresh_session"] is False and out["session"] == NAME
    # Nothing torn down: the session is still ours and alive.
    assert sess.is_alive()
    assert [sess.cols, sess.rows] == [80, 24]  # config block was ignored


def test_run_script_test_and_lint_steps(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    fixture = tmp_path / "rcfix-tests.el"
    fixture.write_text(
        ";;; rcfix-tests.el --- fixture -*- lexical-binding: t; -*-\n"
        "(require 'ert)\n"
        "(ert-deftest rcfix-pass () (should t))\n"
        "(ert-deftest rcfix-fail () (should (= 1 2)))\n"
        "(provide 'rcfix-tests)\n;;; rcfix-tests.el ends here\n",
        encoding="utf-8")
    lint_dirty = tmp_path / "rcdirty.el"
    lint_dirty.write_text(
        ";;; rcdirty.el --- fixture -*- lexical-binding: t; -*-\n"
        "(defun rcdirty-f () (setq rcdirty-free 1))\n",
        encoding="utf-8")
    # Relative paths resolve against the script's directory.
    script = {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"test": "rcfix-", "load_files": ["rcfix-tests.el"],
             "allow_unexpected": True},
            {"assert": {"tests": {"total": 2, "passed": 1, "unexpected": 1,
                                  "timed-out": False}}},
            {"lint": ["rcdirty.el"], "allow_findings": True},
            {"assert": {"lint_clean": False}},
        ],
    }
    path = write_script(tmp_path, script, "quality.json")
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["success"] is True
    # Without allow_unexpected, the failing test fails the step.
    script["steps"] = [{"test": "rcfix-", "load_files": ["rcfix-tests.el"]}]
    path = write_script(tmp_path, script, "quality-fail.json")
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "unexpected" in out["steps"][0]["error"]
    assert out["steps"][0]["detail"]["tests"]["total"] == 2


def test_run_script_setup_eval_error_fails_run(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """REVIEW-phase5 bug 5: a failed session.eval setup form fails the
    run (exit 1) -- the package under test may not even be loaded."""
    script = {
        "session": {"config": "bare", "size": "80x24",
                    "eval": ['(error "setup exploded")']},
        "steps": [{"eval": "(+ 1 1)"}],
    }
    path = write_script(tmp_path, script, "initerr.json")
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is True  # the run executed; the script failed
    assert out["success"] is False
    assert "setup exploded" in out["error"]
    assert "setup exploded" in out["init_error"]
    assert out["steps"][0]["status"] == "not-run"
    assert running_run_sessions() == []
    # allow_init_error is the documented escape hatch.
    script["session"]["allow_init_error"] = True
    path = write_script(tmp_path, script, "initerr-allowed.json")
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["success"] is True
    assert "setup exploded" in out["init_error"]
    assert running_run_sessions() == []


def test_run_script_internal_error_keeps_records(
        sess: S.Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net behind the typed validation: an unexpected controller
    exception mid-run becomes a failed step (records kept, failures are
    data) instead of a raw traceback that discards the run."""
    def boom(*args: object, **kwargs: object) -> None:
        raise ValueError("synthetic controller bug")

    monkeypatch.setattr(SC.S, "wait_idle", boom)
    result = SC.run_script(
        {"steps": [{"eval": "(+ 1 1)"}, {"wait": "idle"},
                   {"eval": "(+ 2 2)"}]},
        session=sess)
    assert result["success"] is False
    assert result["steps"][0]["status"] == "ok"
    assert result["steps"][1]["status"] == "failed"
    assert "internal error" in result["steps"][1]["error"]
    assert "synthetic controller bug" in result["steps"][1]["error"]
    assert result["steps"][2]["status"] == "not-run"


def test_run_emacs_with_existing_session_is_loud(
        sess: S.Session, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, {"steps": [{"eval": "(+ 1 1)"}]},
                        "emacs-vs-s.json")
    code = cli.main(["--json", "-s", NAME, "run", path,
                     "--emacs", "/some/emacs"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "--emacs" in out["error"] and "existing session" in out["error"]


def test_run_human_output_streams_steps(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)"},
                  {"assert": {"eval": "nil"}}],
    }, "human.json")
    code = cli.main(["--human", "run", path])
    out = capsys.readouterr().out
    assert code == 1
    assert "[1/2] eval" in out and "... ok" in out
    assert "[2/2] assert eval" in out and "FAIL" in out
    assert "FAIL: 1 passed, 1 failed" in out


def test_run_script_deadline(elate_home: str, tmp_path: Path) -> None:
    script = {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "(+ 1 1)"}],
    }
    result = SC.run_script(script, base_dir=tmp_path,
                           deadline=time.monotonic() - 1.0)
    assert result["success"] is False
    assert "deadline" in result["steps"][0]["error"]
    assert running_run_sessions() == []


# -- export-script --------------------------------------------------------------

def test_export_script_roundtrip(elate_home: str, tmp_path: Path,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    name = f"{NAME}exp"
    assert cli.main(["--json", "start", "--name", name, "--config", "bare",
                     "--size", "80x24"]) == 0
    capsys.readouterr()
    try:
        for argv in (
            ["-s", name, "eval",
             '(progn (switch-to-buffer "*scratch*") (erase-buffer))'],
            ["-s", name, "keys", "r t RET"],
            ["-s", name, "type", "exported"],
            # Switch the current buffer away before the buffer-targeted
            # wait: on replay, the wait passes only if its --buffer arg
            # survived the export (REVIEW-phase5 bug 3 -- no more
            # passing by current-buffer coincidence).
            ["-s", name, "eval",
             '(switch-to-buffer (get-buffer-create "elsewhere"))'],
            ["-s", name, "wait", "text", "exported", "--buffer", "*scratch*"],
            ["-s", name, "state"],          # observation -> skipped stub
            ["-s", name, "messages"],       # observation -> skipped stub
        ):
            assert cli.main(argv) == 0, argv
            capsys.readouterr()
    finally:
        assert cli.main(["--json", "stop", name]) == 0
        capsys.readouterr()

    exported = tmp_path / "exported.json"
    assert cli.main(["--human", "-s", name, "export-script", "-o", str(exported)]) == 0
    human = capsys.readouterr().out
    assert "Best-effort" in human

    script = json.loads(exported.read_text(encoding="utf-8"))
    assert script["session"]["config"] == "bare"
    assert script["session"]["size"] == "80x24"
    verbs = [next((v for v in SC.VERBS if v in s), None) for s in script["steps"]]
    assert verbs[:5] == ["eval", "keys", "type", "eval", "wait"]
    # The wait step's fields survived the export verbatim (bug 3).
    wait_step = script["steps"][4]
    assert wait_step["pattern"] == "exported"
    assert wait_step["buffer"] == "*scratch*"
    # Observations became skipped assertion stubs.
    stubs = [s for s in script["steps"] if s.get("skip")]
    assert len(stubs) == 2
    assert all(s.get("comment") for s in stubs)
    assert {"state"} <= set(stubs[0]["assert"])
    # The exported script replays cleanly in a fresh session.
    code = cli.main(["--json", "run", str(exported)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["success"] is True
    assert out["skipped"] == 2
    assert running_run_sessions() == []


def test_export_script_clean_strips_transients(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    name = "cleanexp"
    assert cli.main(["--json", "start", "--name", name, "--config", "bare",
                     "--eval", '(setenv "EG_T" "/tmp/eg-thing")',
                     "--eval", "(setq eg-durable t)"]) == 0
    capsys.readouterr()
    # A recorded eval step that references a transient temp path.
    cli.main(["--json", "-s", name, "eval", '(message "/tmp/eg-marker")'])
    capsys.readouterr()

    plain_p = tmp_path / "plain.json"
    assert cli.main(["-s", name, "export-script", "-o", str(plain_p)]) == 0
    capsys.readouterr()
    plain = json.loads(plain_p.read_text(encoding="utf-8"))
    assert any("/tmp/eg-thing" in e for e in plain["session"]["eval"])  # kept
    m = [s for s in plain["steps"] if "/tmp/eg-marker" in (s.get("eval") or "")]
    assert m and not m[0].get("skip")                                   # not skipped

    clean_p = tmp_path / "clean.json"
    assert cli.main(["-s", name, "export-script", "--clean",
                     "-o", str(clean_p)]) == 0
    capsys.readouterr()
    clean = json.loads(clean_p.read_text(encoding="utf-8"))
    evals = clean["session"].get("eval", [])
    assert all("/tmp/eg-thing" not in e for e in evals)                 # transient dropped
    assert any("eg-durable" in e for e in evals)                        # durable kept
    m = [s for s in clean["steps"] if "/tmp/eg-marker" in (s.get("eval") or "")]
    assert m and m[0].get("skip") is True                              # now skipped
    assert cli.main(["--json", "stop", name]) == 0


def test_export_script_stdout_and_no_transcript(
        elate_home: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "export-script"])  # needs -s NAME
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "session" in out["error"]
    code = cli.main(["--json", "-s", "no-such-rec-session", "export-script"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False


# -- asciicast recording ----------------------------------------------------------

def test_record_asciicast_end_to_end(sess: S.Session, tmp_path: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    cast = tmp_path / "demo.cast"
    code = cli.main(["--json", "-s", NAME, "record", "start",
                     "-o", str(cast)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["recording"] is True
    assert Path(out["path"]) == cast.resolve()
    cast = Path(out["path"])
    assert out["width"] == 80 and out["height"] == 24

    # Double start is rejected while the pipe is open.
    code = cli.main(["--json", "-s", NAME, "record", "start"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "already active" in out["error"]

    # Drive some output through the pane.
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (erase-buffer))')
    sess.raw().type_text("cast-marker-123")
    S.wait_text(sess, "cast-marker-123", buffer="*scratch*", timeout=10.0)

    code = cli.main(["--json", "-s", NAME, "record", "status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["recording"] is True

    # Let redisplay output drain through the pipe before stopping.
    S.wait_idle(sess, timeout=5.0)
    code = cli.main(["--json", "-s", NAME, "record", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["recording"] is False
    assert out["events"] >= 2  # initial screen + at least one output chunk

    # Validate the asciicast v2 file.
    lines = cast.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["width"] == 80 and header["height"] == 24
    assert isinstance(header["timestamp"], int)
    events = [json.loads(ln) for ln in lines[1:]]
    assert events, "no events recorded"
    times = [e[0] for e in events]
    assert all(isinstance(t, (int, float)) for t in times)
    assert times == sorted(times), "timestamps must be monotonic"
    assert all(e[1] == "o" and isinstance(e[2], str) for e in events)
    # The first event replays the initial screen (escape-coded).
    assert events[0][0] == 0.0
    assert "\x1b[" in events[0][2]
    # The typed marker crossed the pane and was captured.
    assert "cast-marker-123" in "".join(e[2] for e in events)

    # Stop again: no active recording.
    code = cli.main(["--json", "-s", NAME, "record", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "no recording" in out["error"]
    # No stray state in the sandbox.
    assert not (sess.dir / "record.json").exists()


def test_record_rejects_gui_session(tmp_path: Path) -> None:
    fake = S.Session(
        name="recgui", session_dir=str(tmp_path / "recgui"), emacs="emacs",
        emacsclient="emacsclient", config="bare", cols=80, rows=24,
        created_at=0.0, ui="gui",
    )
    with pytest.raises(ElateError, match="snap"):
        R.start_recording(fake)


def test_record_output_flag_only_for_start(sess: S.Session,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "record", "stop", "-o", "x.cast"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "start" in out["error"]


def test_record_unicode_output(sess: S.Session, tmp_path: Path,
                               capsys: pytest.CaptureFixture[str]) -> None:
    """Multibyte output through the pipe helper: the incremental decoder
    must never produce U+FFFD even when redisplay floods the pipe."""
    cast = tmp_path / "uni.cast"
    assert cli.main(["--json", "-s", NAME, "record", "start",
                     "-o", str(cast)]) == 0
    capsys.readouterr()
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (erase-buffer)'
        ' (dotimes (_ 20) (insert "\U0001f680 ünïcödé '
        '☪︎ 你好\n")))')
    S.wait_text(sess, "ünïcödé", buffer="*scratch*", timeout=10.0)
    S.wait_idle(sess, timeout=5.0)
    code = cli.main(["--json", "-s", NAME, "record", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["events"] >= 2
    lines = cast.read_text(encoding="utf-8").splitlines()
    events = [json.loads(ln) for ln in lines[1:]]  # strict parse
    payload = "".join(e[2] for e in events)
    assert "�" not in payload, "replacement char: decoder split a rune"
    assert "\U0001f680" in payload  # a single codepoint cannot be split
    times = [e[0] for e in events]
    assert times == sorted(times)


def test_record_stop_with_vanished_cast(sess: S.Session, tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    """REVIEW-phase5 bug 4: a deleted cast file must not wedge stop with
    the state file stuck -- stop degrades to events=0 plus a note."""
    cast = tmp_path / "vanish.cast"
    assert cli.main(["--json", "-s", NAME, "record", "start",
                     "-o", str(cast)]) == 0
    capsys.readouterr()
    cast.unlink()
    code = cli.main(["--json", "-s", NAME, "record", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["recording"] is False
    assert out["events"] == 0
    assert "cannot read cast file" in out["note"]
    assert not (sess.dir / "record.json").exists()  # state cleared
    # The session is fully recoverable: stop again says "no recording",
    # and a fresh start/stop cycle works.
    code = cli.main(["--json", "-s", NAME, "record", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "no recording" in out["error"]
    assert cli.main(["--json", "-s", NAME, "record", "start"]) == 0
    capsys.readouterr()
    code = cli.main(["--json", "-s", NAME, "record", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and "note" not in out


def _wait_pane_dead(sess: S.Session, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and sess.raw().is_pane_alive():
        time.sleep(0.1)
    assert not sess.raw().is_pane_alive()


def _wait_helper_gone(pid: int, identity: str | None,
                      timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and gui.pid_alive(pid, identity, "python"):
        time.sleep(0.1)
    assert not gui.pid_alive(pid, identity, "python"), \
        f"recorder helper {pid} is still running"


def test_record_survives_emacs_crash(elate_home: str, tmp_path: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    """REVIEW-phase5 bug 2: a SIGKILLed Emacs leaves a dead-but-kept
    pane with the pipe attached. status must report the recording as
    over (stale), stop must reap the helper and finalize the cast."""
    name = f"{NAME}crash"
    crash = S.start_session(name, config="bare", cols=80, rows=24)
    try:
        cast = tmp_path / "crash.cast"
        assert cli.main(["--json", "-s", name, "record", "start",
                         "-o", str(cast)]) == 0
        capsys.readouterr()
        st = json.loads((crash.dir / "record.json").read_text())
        assert st["helper_pid"], "helper pid must be recorded at start"
        assert gui.pid_alive(st["helper_pid"], st["helper_identity"], "python")
        crash.semantic().eval_form(
            '(progn (switch-to-buffer "*scratch*") (erase-buffer))')
        crash.raw().type_text("pre-crash-marker")
        S.wait_text(crash, "pre-crash-marker", buffer="*scratch*", timeout=10.0)
        S.wait_idle(crash, timeout=5.0)

        os.kill(crash.emacs_pid, signal.SIGKILL)
        _wait_pane_dead(crash)

        # status: the dead pane means the recording is over, not active.
        code = cli.main(["--json", "-s", name, "record", "status"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["recording"] is False and out["stale"] is True
        assert "died mid-recording" in out["note"]

        # stop: finalizes the cast, clears the state, reaps the helper.
        code = cli.main(["--json", "-s", name, "record", "stop"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["recording"] is False
        assert out["events"] >= 1
        assert not (crash.dir / "record.json").exists()
        _wait_helper_gone(st["helper_pid"], st["helper_identity"])
        # The cast holds everything up to the crash and stays valid.
        text = cast.read_text(encoding="utf-8")
        assert "pre-crash-marker" in text
        assert json.loads(text.splitlines()[0])["version"] == 2
    finally:
        S.stop_session(name)


def test_stop_session_reaps_orphan_recorder(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`elate stop` must uphold the no-stray-recorder guarantee even for
    a recording orphaned by a crashed Emacs (REVIEW-phase5 bug 2)."""
    name = f"{NAME}orph"
    sess2 = S.start_session(name, config="bare", cols=80, rows=24)
    try:
        assert cli.main(["--json", "-s", name, "record", "start",
                         "-o", str(tmp_path / "orphan.cast")]) == 0
        capsys.readouterr()
        st = json.loads((sess2.dir / "record.json").read_text())
        assert st["helper_pid"]
        os.kill(sess2.emacs_pid, signal.SIGKILL)
        _wait_pane_dead(sess2)
    finally:
        S.stop_session(name)
    _wait_helper_gone(st["helper_pid"], st["helper_identity"])


# -- snap series ---------------------------------------------------------------

def test_snap_series_tty(sess: S.Session,
                         capsys: pytest.CaptureFixture[str]) -> None:
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (erase-buffer)'
        ' (insert "snap-marker"))')
    code = cli.main(["--json", "-s", NAME, "snap", "start",
                     "--interval", "0.2"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["snapping"] is True
    snap_dir = Path(out["dir"])
    assert out["format"] == "txt"

    # Second start is rejected while the snapper lives.
    code = cli.main(["--json", "-s", NAME, "snap", "start"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "already running" in out["error"]

    time.sleep(1.5)
    code = cli.main(["--json", "-s", NAME, "snap", "status"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["snapping"] is True
    assert out["frames"] >= 2

    code = cli.main(["--json", "-s", NAME, "snap", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["stopped"] is True
    frames = out["frames"]
    assert frames >= 2

    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert manifest["session"] == NAME and manifest["format"] == "txt"
    assert manifest["frames_total"] == len(manifest["frames"]) == frames
    elapsed = [f["elapsed"] for f in manifest["frames"]]
    assert elapsed == sorted(elapsed)
    files = sorted(p.name for p in snap_dir.glob("frame-*.txt"))
    assert files == [f["file"] for f in manifest["frames"]]
    assert "snap-marker" in (snap_dir / files[0]).read_text(encoding="utf-8")

    # Stop is idempotent (exit 0, structured "nothing to do").
    code = cli.main(["--json", "-s", NAME, "snap", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["stopped"] is False
    # No snapper process is left behind.
    assert not (sess.dir / "snap.json").exists()


def test_snap_ansi_frames(sess: S.Session, tmp_path: Path,
                          capsys: pytest.CaptureFixture[str]) -> None:
    out_dir = tmp_path / "ansi-frames"
    code = cli.main(["--json", "-s", NAME, "snap", "start",
                     "--interval", "0.2", "--ansi", "-o", str(out_dir)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["format"] == "ansi"
    time.sleep(0.7)
    code = cli.main(["--json", "-s", NAME, "snap", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["frames"] >= 1
    first = next(iter(sorted(out_dir.glob("frame-*.txt"))))
    assert "\x1b[" in first.read_text(encoding="utf-8")


def test_snap_interval_bounds(sess: S.Session,
                              capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "snap", "start",
                     "--interval", "0.001"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "interval" in out["error"]


def test_snap_write_failure_ends_series_cleanly(
        sess: S.Session, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """REVIEW-phase5 R2: an OSError on a frame write (outdir made
    unwritable mid-series) ends the series cleanly -- no traceback, no
    crash-loop -- and stop stays idempotent."""
    out_dir = tmp_path / "rofail"
    code = cli.main(["--json", "-s", NAME, "snap", "start",
                     "--interval", "0.2", "-o", str(out_dir)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    deadline = time.time() + 10.0
    while time.time() < deadline and not list(out_dir.glob("frame-*.txt")):
        time.sleep(0.1)
    assert list(out_dir.glob("frame-*.txt")), "no frame ever appeared"
    os.chmod(out_dir, 0o555)  # next frame write fails with EACCES
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            code = cli.main(["--json", "-s", NAME, "snap", "status"])
            status = json.loads(capsys.readouterr().out)
            if not status["snapping"]:
                break
            time.sleep(0.2)
        assert status["snapping"] is False and status["stale"] is True
    finally:
        os.chmod(out_dir, 0o755)
    log = (out_dir / "snapper.log").read_text(encoding="utf-8")
    assert "failed" in log
    assert "Traceback" not in log, "snapper died with an unhandled OSError"
    code = cli.main(["--json", "-s", NAME, "snap", "stop"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["stopped"] is True


@pytest.mark.skipif(
    GUI_UNAVAILABLE_REASON is not None,
    reason=f"GUI snap: {GUI_UNAVAILABLE_REASON}",
)
def test_snap_series_gui(elate_home: str,
                         capsys: pytest.CaptureFixture[str]) -> None:
    from elate import screenshot as shot

    if shot.screen_recording_allowed() is False:
        pytest.skip("Screen Recording permission not granted")
    name = f"{NAME}g"
    started = S.start_session(name, config="bare", cols=80, rows=24, ui="gui")
    try:
        code = cli.main(["--json", "-s", name, "snap", "start",
                         "--interval", "0.3"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["format"] == "png"
        snap_dir = Path(out["dir"])
        time.sleep(1.5)
        code = cli.main(["--json", "-s", name, "snap", "stop"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["frames"] >= 1
        pngs = sorted(snap_dir.glob("frame-*.png"))
        assert pngs
        assert pngs[0].read_bytes()[:8] == shot.PNG_MAGIC
    finally:
        del started
        S.stop_session(name)


# -- matrix --------------------------------------------------------------------

def test_matrix_of_one(elate_home: str, tmp_path: Path,
                       capsys: pytest.CaptureFixture[str]) -> None:
    emacs = shutil.which("emacs")
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": "(emacs-version)"},
            {"assert": {"eval": "(>= emacs-major-version 27)"}},
        ],
    }, "matrix.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs, "--", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["success"] is True
    assert len(out["results"]) == 1
    entry = out["results"][0]
    assert entry["success"] is True
    assert entry["version"]  # e.g. "31.0.90"
    assert entry["passed"] == 2 and entry["failed"] == 0
    assert running_run_sessions() == []
    # Human output renders a summary table.
    code = cli.main(["--human", "matrix", "--emacs", emacs, path])
    human = capsys.readouterr().out
    assert code == 0
    assert "1/1 combo(s) passed" in human


def test_matrix_param_axis(elate_home: str, tmp_path: Path,
                           capsys: pytest.CaptureFixture[str]) -> None:
    # --param crosses with the Emacs axis: one Emacs x two n values = two
    # combos, each rendering {{n}} differently.
    emacs = shutil.which("emacs")
    path = write_script(tmp_path, {
        "params": {"n": "1"},
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "(= {{n}} 1)"}}],
    }, "matrix-param.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "n=1,2", "--", path])
    out = json.loads(capsys.readouterr().out)
    assert out["axes"] == ["n"]
    assert len(out["results"]) == 2                 # 1 emacs x 2 values of n
    by_n = {r["axes"]["n"]: r for r in out["results"]}
    assert by_n["1"]["success"] is True             # (= 1 1)
    assert by_n["2"]["success"] is False            # (= 2 1)
    assert by_n["1"]["axes"]["emacs"] == emacs
    assert code == 1 and out["success"] is False    # not all combos passed
    # A duplicate axis is a loud error.
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "n=1", "--param", "n=2", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "more than once" in out["error"]
    assert running_run_sessions() == []


def test_matrix_param_guards(elate_home: str, tmp_path: Path,
                             capsys: pytest.CaptureFixture[str]) -> None:
    # These fail before any session boots (no Emacs is spawned).
    emacs = shutil.which("emacs")
    path = write_script(tmp_path, {
        "session": {"config": "bare"},
        "steps": [{"assert": {"eval": "(= {{n}} 1)"}}],
    }, "guard.json")
    # An axis the scenario never references is a loud error.
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "n=1", "--param", "unused=9", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "never referenced" in out["error"]
    # 'emacs' is a reserved axis name.
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "emacs=x", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "reserved" in out["error"]


def test_matrix_param_snapshot_stem_collision(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # Two param values that sanitize to the same stem ("a/b" and "a-b" both
    # -> "a-b") must NOT share a golden directory.
    emacs = shutil.which("emacs")
    snapdir = tmp_path / "snaps"
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                     '(insert "{{p}}"))'},
            {"assert": {"snapshot": "buf"}},
        ],
    }, "coll.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs, "--param", "p=a/b,a-b",
                     "--snapshot-dir", str(snapdir), "--update-snapshots",
                     "--", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    stems = sorted(d.name for d in snapdir.iterdir() if d.is_dir())
    assert len(stems) == 2, stems      # collision disambiguated into 2 goldens
    assert running_run_sessions() == []


def test_matrix_failure_and_bad_binary(elate_home: str, tmp_path: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    emacs = shutil.which("emacs")
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "nil"}}],
    }, "matrix-fail.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs, path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert out["results"][0]["failed_step"]
    assert running_run_sessions() == []
    # A non-executable --emacs fails fast, before any session boots.
    code = cli.main(["--json", "matrix", "--emacs", "/no/such/emacs", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "not an executable" in out["error"]
    # No binaries at all is a usage-style error.
    code = cli.main(["--json", "matrix", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "--emacs" in out["error"]


def test_matrix_bare_name_and_dedup(elate_home: str, tmp_path: Path,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    """REVIEW-phase5 bug 6: bare PATH names ('emacs') must work like on
    every other --emacs surface, and spellings of the same binary
    dedup to one run."""
    real = shutil.which("emacs")
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "(stringp (emacs-version))"}}],
    }, "bare-name.json")
    code = cli.main(["--json", "matrix", "--emacs", f"emacs,{real}", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["success"] is True
    assert len(out["results"]) == 1  # bare name and abs path dedup'd
    assert out["results"][0]["emacs"] == real
    assert out["results"][0]["version"]
    assert running_run_sessions() == []


def test_matrix_wrapper_binary_and_broken_binary(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Pin two claims that had no coverage: a wrapper binary actually
    reaches the spawned session (override plumbing), and one broken
    binary records a failed entry without aborting the rest."""
    real = shutil.which("emacs")
    wrapper = tmp_path / "emacs-wrapper"
    wrapper.write_text(f'#!/bin/sh\nexec {real} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    broken = tmp_path / "emacs-broken"
    broken.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    broken.chmod(0o755)
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "(>= emacs-major-version 27)"}}],
    }, "wrapper.json")
    code = cli.main(["--json", "matrix",
                     "--emacs", f"{broken},{wrapper}", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["success"] is False
    assert len(out["results"]) == 2
    broken_entry, wrapper_entry = out["results"]
    assert broken_entry["emacs"] == str(broken)
    assert broken_entry["success"] is False and broken_entry["error"]
    # The broken binary did not abort the rest: the wrapper ran and the
    # override reached the session (version reported through it).
    assert wrapper_entry["emacs"] == str(wrapper)
    assert wrapper_entry["success"] is True
    assert wrapper_entry["version"]
    assert running_run_sessions() == []


VARIANT_SCRIPT = {
    "variants": {"good": {"expr": "(= 1 1)"}, "bad": {"expr": "(= 1 2)"}},
    "session": {"config": "bare", "size": "80x24"},
    "steps": [{"assert": {"eval": "{{expr}}"}}],
}


def test_run_variant_cli(elate_home: str, tmp_path: Path,
                         capsys: pytest.CaptureFixture[str]) -> None:
    path = write_script(tmp_path, {
        "params": {"expr": "(= 2 2)"}, **VARIANT_SCRIPT}, "var.json")
    code = cli.main(["--json", "run", "--variant", "good", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["variant"] == "good"        # the result names its variant
    # No selection: params defaults only, and no "variant" result key.
    code = cli.main(["--json", "run", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and "variant" not in out
    # The "bad" variant's binding actually reaches the step.
    code = cli.main(["--json", "run", "--variant", "bad", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["variant"] == "bad" and out["failed"] == 1
    # An unknown name is loud and lists the declared ones (no boot).
    code = cli.main(["--json", "run", "--variant", "nope", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "unknown variant" in out["error"]
    assert "good" in out["error"] and "bad" in out["error"]
    assert running_run_sessions() == []


def test_matrix_variant_axis(elate_home: str, tmp_path: Path,
                             capsys: pytest.CaptureFixture[str]) -> None:
    emacs = shutil.which("emacs")
    path = write_script(tmp_path, dict(VARIANT_SCRIPT), "vmatrix.json")
    # Every declared variant runs by default: 1 emacs x 2 variants.
    code = cli.main(["--json", "matrix", "--emacs", emacs, "--", path])
    out = json.loads(capsys.readouterr().out)
    assert out["axes"] == ["variant"]
    assert len(out["results"]) == 2
    by_v = {r["axes"]["variant"]: r for r in out["results"]}
    assert by_v["good"]["success"] is True
    assert by_v["bad"]["success"] is False          # (= 1 2)
    assert code == 1 and out["success"] is False
    # --variant filters the axis.
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--variant", "good", "--", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert [r["axes"]["variant"] for r in out["results"]] == ["good"]
    assert running_run_sessions() == []
    # Guards, all firing before any session boots:
    for argv, msg in [
        (["--variant", "nope"], "unknown --variant"),
        (["--variant", "good,good"], "more than once"),
        (["--param", "variant=x"], "reserved"),
        # A --param axis crossing a variant-bound variable is loud.
        (["--param", "expr=1,2"], "co-vary"),
    ]:
        code = cli.main(["--json", "matrix", "--emacs", emacs, *argv, path])
        out = json.loads(capsys.readouterr().out)
        assert code == 1 and msg in out["error"], (argv, out)
    # --variant against a scenario with no variants block is loud too.
    plain = write_script(tmp_path, {
        "session": {"config": "bare"},
        "steps": [{"comment": "hi"}],
    }, "novar.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--variant", "good", plain])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and 'no "variants"' in out["error"]


def test_matrix_variant_unused_binding_guard(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # A variant binding a variable the scenario never references is a loud
    # error naming both (mirrors the unused --param axis check); no boot.
    emacs = shutil.which("emacs")
    path = write_script(tmp_path, {
        "variants": {"a": {"expr": "1", "dead": "x"}},
        "session": {"config": "bare"},
        "steps": [{"assert": {"eval": "{{expr}}"}}],
    }, "vdead.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs, path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "'a'" in out["error"] and "dead" in out["error"]
    assert "never referenced" in out["error"]


def test_matrix_variant_snapshot_stem(elate_home: str, tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    # Each variant gets its own golden directory (+variant-<name>), so
    # per-variant snapshots never collide -- and `run --variant` writes to
    # the same stem matrix uses for that combo. The "b-" name pins that
    # the suffix takes the name VERBATIM (not through _safe_param, whose
    # edge-stripping would yield "b" and break run/matrix golden sharing).
    emacs = shutil.which("emacs")
    snapdir = tmp_path / "snaps"
    path = write_script(tmp_path, {
        "variants": {"a": {"text": "aa"}, "b-": {"text": "bb"}},
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                     '(insert "{{text}}"))'},
            {"assert": {"snapshot": "buf"}},
        ],
    }, "vsnap.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--snapshot-dir", str(snapdir), "--update-snapshots",
                     "--", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    stems = sorted(d.name for d in snapdir.iterdir() if d.is_dir())
    assert stems == ["vsnap+variant-a", "vsnap+variant-b-"]
    # A plain run of one variant COMPARES against the matrix-written golden
    # (same stem), proving the two surfaces share goldens per variant.
    for vname in ("a", "b-"):
        code = cli.main(["--json", "run", "--variant", vname,
                         "--snapshot-dir", str(snapdir), path])
        out = json.loads(capsys.readouterr().out)
        assert code == 0, out
    assert running_run_sessions() == []


def test_matrix_variant_param_cross(elate_home: str, tmp_path: Path,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    # The variant axis crosses with --param axes: 2 variants x 2 param
    # values = 4 combos, each carrying both in its axes, and the snapshot
    # stem folds both in (sorted key order: +p-...+variant-...).
    emacs = shutil.which("emacs")
    snapdir = tmp_path / "snaps"
    path = write_script(tmp_path, {
        "variants": {"a": {"text": "aa"}, "b": {"text": "bb"}},
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                     '(insert "{{text}}-{{p}}"))'},
            {"assert": {"snapshot": "buf"}},
        ],
    }, "vcross.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "p=x,y",
                     "--snapshot-dir", str(snapdir), "--update-snapshots",
                     "--", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["axes"] == ["variant", "p"]      # variant leads the axis list
    assert len(out["results"]) == 4             # full Cartesian product
    seen = {(r["axes"]["variant"], r["axes"]["p"]) for r in out["results"]}
    assert seen == {("a", "x"), ("a", "y"), ("b", "x"), ("b", "y")}
    stems = sorted(d.name for d in snapdir.iterdir() if d.is_dir())
    assert stems == ["vcross+p-x+variant-a", "vcross+p-x+variant-b",
                     "vcross+p-y+variant-a", "vcross+p-y+variant-b"]
    assert running_run_sessions() == []


def test_run_script_xfail_map_classification(
        elate_home: str, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    # One scenario, four runs: a map-form xfail is a known failure ONLY
    # under its named variants and a normal gating step everywhere else.
    path = write_script(tmp_path, {
        "params": {"expr": "(= 1 2)"},
        "variants": {"nu": {"expr": "(= 1 2)"},
                     "fixed": {"expr": "(= 1 1)"},
                     "bash": {"expr": "(= 1 2)"}},
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "{{expr}}"},
                   "xfail": {"nu": "reedline has no C-_",
                             "fixed": "stale marker"}}],
    }, "xmap.json")

    def run(*argv: str) -> tuple[int, dict]:
        code = cli.main(["--json", "run", *argv, path])
        return code, json.loads(capsys.readouterr().out)

    # Matching variant + failing step: xfail with the map value as reason.
    code, out = run("--variant", "nu")
    assert code == 0 and out["success"] is True, out
    assert out["steps"][0]["status"] == "xfail"
    assert out["steps"][0]["reason"] == "reedline has no C-_"
    # Matching variant + passing step: xpass gates (stale marker noticed),
    # and the map value still lands as the reason.
    code, out = run("--variant", "fixed")
    assert code == 1 and out["steps"][0]["status"] == "xpass"
    assert out["steps"][0]["reason"] == "stale marker"
    # Non-matching variant: a plain failure that gates.
    code, out = run("--variant", "bash")
    assert code == 1 and out["steps"][0]["status"] == "failed"
    assert "reason" not in out["steps"][0]
    # No variant selected: the map can never match -- gates too.
    code, out = run()
    assert code == 1 and out["steps"][0]["status"] == "failed"
    assert running_run_sessions() == []


SET_FILE_SCRIPT = {
    "session": {"config": "bare", "size": "80x24"},
    "steps": [
        {"eval": '(progn (switch-to-buffer "*scratch*") (erase-buffer) '
                 '(insert "{{v}}"))'},
        {"assert": {"eval": '(string= (buffer-string) "{{want}}")',
                    "buffer": "*scratch*"}},
    ],
}


def test_run_set_file(elate_home: str, tmp_path: Path,
                      capsys: pytest.CaptureFixture[str]) -> None:
    # The file's contents bind verbatim minus exactly ONE trailing newline
    # (editors append one; a second survives), alongside a plain --set.
    path = write_script(tmp_path, SET_FILE_SCRIPT, "sf.json")
    multi = tmp_path / "multi.txt"
    multi.write_text("line1\nline2\n", encoding="utf-8")
    code = cli.main(["--json", "run", "--set-file", f"v={multi}",
                     "--set", "want=line1\\nline2", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    blank = tmp_path / "blank.txt"
    blank.write_text("x\n\n", encoding="utf-8")
    code = cli.main(["--json", "run", "--set-file", f"v={blank}",
                     "--set", "want=x\\n", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert running_run_sessions() == []


def test_run_set_file_guards(elate_home: str, tmp_path: Path,
                             capsys: pytest.CaptureFixture[str]) -> None:
    # All loud, all before anything boots.
    path = write_script(tmp_path, SET_FILE_SCRIPT, "sfg.json")
    val = tmp_path / "val.txt"
    val.write_text("x", encoding="utf-8")
    for argv, msg in [
        (["--set-file", f"v={tmp_path}/nope.txt"], "cannot read --set-file"),
        (["--set", "v=a", "--set-file", f"v={val}"], "both --set and"),
        (["--set-file", f"v={val}", "--set-file", f"v={val}"],
         "more than once"),
        # The reserved name is rejected via render_script like any override.
        (["--set-file", f"variant={val}"], "reserved"),
    ]:
        code = cli.main(["--json", "run", *argv, path])
        out = json.loads(capsys.readouterr().out)
        assert code == 1 and msg in out["error"], (argv, out)
    assert running_run_sessions() == []


def test_matrix_set_file(elate_home: str, tmp_path: Path,
                         capsys: pytest.CaptureFixture[str]) -> None:
    emacs = shutil.which("emacs")
    val = tmp_path / "val.txt"
    val.write_text("constant\n", encoding="utf-8")
    path = write_script(tmp_path, {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": '(string= "{{v}}" "constant")'},
                   "comment": "n={{n}}"}],
    }, "msf.json")
    # A constant binding crosses with a --param axis: both combos see it.
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "n=1,2", "--set-file", f"v={val}",
                     "--", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert len(out["results"]) == 2
    assert all(r["success"] for r in out["results"])
    # Guards: a constant cannot also be an axis, and a dead constant is loud.
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "v=1,2", "--set-file", f"v={val}", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "cannot also be an axis" in out["error"]
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--param", "n=1", "--set-file", f"dead={val}", path])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "never referenced" in out["error"]
    assert "dead" in out["error"]
    # Documented precedence: a --set-file constant MAY override a
    # variant-bound variable (params < variant < overrides), unlike a
    # --param axis, which errors.
    vpath = write_script(tmp_path, {
        "variants": {"a": {"v": "from-variant"}},
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": '(string= "{{v}}" "constant")'}}],
    }, "msfv.json")
    code = cli.main(["--json", "matrix", "--emacs", emacs,
                     "--set-file", f"v={val}", "--", vpath])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out                    # the file value won
    assert out["results"][0]["axes"]["variant"] == "a"
    assert running_run_sessions() == []


def test_run_session_name_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    import re as _re
    # A short sessions root leaves the full 40-char hint budget.
    monkeypatch.setenv("ELATE_HOME", "/tmp/el")
    # A throwaway with a scenario hint is identifiable, still in the swept
    # run-* namespace.
    assert _re.fullmatch(r"run-my-scen-[0-9a-f]{6}",
                         SC._run_session_name(None, "my-scen"))
    # Matrix stems carry '+': sanitized into the session-name charset.
    assert _re.fullmatch(r"run-groups-variant-nu-[0-9a-f]{6}",
                         SC._run_session_name(None, "groups+variant-nu"))
    # An over-long hint is capped (the name becomes a directory).
    long = SC._run_session_name(None, "x" * 100)
    assert _re.fullmatch(r"run-x{40}-[0-9a-f]{6}", long)
    # No hint (or one that sanitizes away): the legacy opaque name.
    assert _re.fullmatch(r"run-[0-9a-f]{10}", SC._run_session_name(None))
    assert _re.fullmatch(r"run-[0-9a-f]{10}", SC._run_session_name(None, "++"))
    # The name rides in the Emacs server SOCKET path (~104-byte cap): a
    # deep sessions root shrinks the hint, then drops it entirely, keeping
    # the whole path at/below the legacy worst case -- never a session
    # that dies with "Service name too long".
    monkeypatch.setenv("ELATE_HOME", "/x" * 30)   # root >= 69 chars
    shrunk = SC._run_session_name(None, "my-scen")
    assert _re.fullmatch(r"run-my[a-z-]*-[0-9a-f]{6}", shrunk)  # "run-my-sce-…"
    assert (len(str(SC.paths.sessions_root())) + 1 + len(shrunk)
            + len("/server/elate")) <= 100
    monkeypatch.setenv("ELATE_HOME", "/x" * 60)   # hopeless: legacy name
    assert _re.fullmatch(r"run-[0-9a-f]{10}",
                         SC._run_session_name(None, "my-scen"))
    # The PREFER branch is untouched: kept names win over the hint and the
    # reserved-namespace rejection still fires.
    assert SC._run_session_name("kept-name", "my-scen") == "kept-name"
    with pytest.raises(ElateError, match="reserved"):
        SC._run_session_name("run-squat", "my-scen")


def test_failed_run_session_named_after_scenario(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    # An auto-kept FAILURE carries the scenario stem, so a post-mortem in a
    # pile of sandboxes is findable -- and a targeted glob sweeps the whole
    # campaign. A SHORT sessions root (not the deep pytest tmpdir the
    # module fixture uses) so the full stem survives the socket budget.
    home = tempfile.mkdtemp(prefix="el-", dir="/tmp")
    monkeypatch.setenv("ELATE_HOME", home)
    try:
        path = write_script(tmp_path, {
            "session": {"config": "bare", "size": "80x24"},
            "steps": [{"assert": {"eval": "nil"}}],
        }, "my-scen.json")
        code = cli.main(["--json", "run", path])
        out = json.loads(capsys.readouterr().out)
        assert code == 1 and out["success"] is False
        assert out["session"].startswith("run-my-scen-")
        assert out["kept"] is False                   # stopped, not running
        assert os.path.isdir(out["session_dir"])      # but kept on disk
        code = cli.main(["--json", "purge", "--glob", "run-my-scen-*"])
        purged = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["session"] in [p["name"] for p in purged["purged"]]
        assert not os.path.isdir(out["session_dir"])
        assert running_run_sessions() == []
    finally:
        shutil.rmtree(home, ignore_errors=True)


# -- session "require" key: validation + export round-trip -------------------

def test_session_require_key_validation() -> None:
    ok = {"session": {"require": ["mypkg"]}, "steps": [{"eval": "t"}]}
    SC.validate_script(ok)
    with pytest.raises(ElateError, match='"require" must be a list of strings'):
        SC.validate_script({"session": {"require": "mypkg"},
                            "steps": [{"eval": "t"}]})


def test_export_script_emits_require(tmp_path: Path) -> None:
    sd = tmp_path / "sess"
    (sd / "log").mkdir(parents=True)
    (sd / "log" / "transcript.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00+00:00", "event": "eval",
                    "form": "(+ 1 2)", "timeout": 15.0, "buffer": None})
        + "\n", encoding="utf-8")
    sess = S.Session(name="exp", session_dir=str(sd), emacs="emacs",
                     emacsclient="emacsclient", config="minimal",
                     cols=100, rows=30, created_at=0.0,
                     requires=["mypkg"])
    script = SC.export_script(sess)
    assert script["session"]["require"] == ["mypkg"]
    # The exported script validates, i.e. the key round-trips.
    SC.validate_script(script)
