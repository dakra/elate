"""Scenario composition and run policy.

Templates/includes/project config are pure transformations (no Emacs);
requires/SKIP, retries/FLAKY, timeout scaling, stdin scripts, delivery
defaults and the multi-scenario parallel matrix run real sessions.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

import pytest

from elate import cli, compose
from elate import script as SC
from elate import session as S
from elate.errors import ElateError

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux")
    and shutil.which("emacsclient")
)
needs_emacs = pytest.mark.skipif(not HAVE_DEPS, reason="needs emacs + tmux")


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Short root: run-<scenario>-<hex> names must fit the unix-socket limit.
    tmp = Path(tempfile.mkdtemp(prefix="el-", dir="/tmp"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def work(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private scenario directory outside $HOME (its world-writable
    parent ends the project-config walk), with the lookup enabled."""
    monkeypatch.delenv("ELATE_NO_PROJECT_CONFIG", raising=False)
    tmp = Path(tempfile.mkdtemp(prefix="elw-", dir="/tmp"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run_json(argv: list[str], capsys: pytest.CaptureFixture[str]
              ) -> tuple[int, dict[str, Any]]:
    code = cli.main(["--json", *argv])
    return code, json.loads(capsys.readouterr().out)


# -- templates & includes ----------------------------------------------------

def test_use_expands_template_with_params_and_defaults(work: Path):
    raw = {
        "templates": {"press": {
            "params": {"key": None, "pred": "t"},
            "steps": [{"keys": "{{key}}"},
                      {"wait": "until", "pred": "{{pred}}"},
                      {"eval": "{{outer}}"}]}},
        "steps": [{"use": "press", "with": {"key": "C-c a"}, "group": "g1"},
                  {"use": "press", "with": {"key": "b", "pred": "(foo)"}}],
    }
    out = compose.prepare(raw, work)
    steps = out["steps"]
    # A group on the use step becomes a marker ahead of the expansion.
    assert steps[0] == {"group": "g1"}
    assert [s.get("keys") for s in (steps[1], steps[4])] == ["C-c a", "b"]
    assert steps[2]["pred"] == "t" and steps[5]["pred"] == "(foo)"
    # Non-parameter {{vars}} are left for scenario templating.
    assert steps[3]["eval"] == "{{outer}}"
    assert "templates" not in out


@pytest.mark.parametrize("step,match", [
    ({"use": "nope"}, "unknown template"),
    ({"use": "t", "with": {"zz": "1"}}, "unknown parameter"),
    ({"use": "t"}, "missing required parameter"),
    ({"use": "t", "with": {"k": "1"}, "optional": True}, "unknown key"),
])
def test_use_step_errors(work: Path, step: dict[str, Any], match: str):
    raw = {"templates": {"t": {"params": ["k"], "steps": [{"keys": "{{k}}"}]}},
           "steps": [step]}
    with pytest.raises(ElateError, match=match):
        compose.prepare(raw, work)


def test_recursive_template_is_rejected(work: Path):
    raw = {"templates": {"a": {"steps": [{"use": "b"}]},
                         "b": {"steps": [{"use": "a"}]}},
           "steps": [{"use": "a"}]}
    with pytest.raises(ElateError, match="recursively"):
        compose.prepare(raw, work)


def test_include_files_and_local_override(work: Path):
    _write(work / "lib" / "base.json",
           {"templates": {"x": {"steps": [{"eval": "1"}]}}})
    _write(work / "lib" / "common.json", {
        "include": ["base.json"],
        "templates": {"y": {"steps": [{"eval": "2"}]}}})
    raw = {"include": "lib/common.json",
           "templates": {"y": {"steps": [{"eval": "local"}]}},
           "steps": [{"use": "x"}, {"use": "y"}]}
    out = compose.prepare(raw, work)
    assert [s["eval"] for s in out["steps"]] == ["1", "local"]


def test_include_cycle_is_an_error(work: Path):
    _write(work / "a.json", {"include": ["b.json"]})
    _write(work / "b.json", {"include": ["a.json"]})
    with pytest.raises(ElateError, match="include cycle"):
        compose.prepare({"include": ["a.json"], "steps": []}, work)


# -- project config ----------------------------------------------------------

def test_project_config_merges_under_the_scenario(work: Path):
    (work / "lib").mkdir()
    (work / "lib" / "helpers.el").write_text("(setq h 1)\n")
    _write(work / ".elate.json", {
        "session": {"ui": "tty", "eval_file": ["lib/helpers.el"],
                    "env": {"A": "1", "B": "cfg"}},
        "defaults": {"timeout": 20, "keys_delivery": "events"},
        "timeout_scale": 2,
        "templates": {"t": {"steps": [{"eval": "cfg"}]}},
    })
    scen = _write(work / "tests" / "s.json", {
        "session": {"eval_file": ["own.el"], "env": {"B": "own"}},
        "defaults": {"timeout": 5},
        "steps": [{"use": "t"}]})
    raw, base = SC.load_raw(scen)
    session = raw["session"]
    assert session["eval_file"] == [str((work / "lib" / "helpers.el").resolve()),
                                     "own.el"]
    assert session["env"] == {"A": "1", "B": "own"}
    assert raw["defaults"] == {"timeout": 5, "keys_delivery": "events"}
    assert raw["timeout_scale"] == 2
    assert raw["steps"] == [{"eval": "cfg"}]
    assert raw["_config_path"] == str((work / ".elate.json").resolve())
    # --no-config ignores it (so the config's template is undefined).
    with pytest.raises(ElateError, match="unknown template"):
        SC.load_raw(scen, use_config=False)


def test_project_config_unknown_key(work: Path):
    _write(work / ".elate.json", {"sesion": {}})
    scen = _write(work / "s.json", {"steps": [{"eval": "t"}]})
    with pytest.raises(ElateError, match="unknown key"):
        SC.load_raw(scen)


# -- timeout scale ------------------------------------------------------------

def test_timeout_scale_precedence(monkeypatch: pytest.MonkeyPatch):
    assert SC.timeout_scale() == 1.0
    assert SC.timeout_scale(config={"timeout_scale": 3}) == 3.0
    monkeypatch.setenv("ELATE_TIMEOUT_SCALE", "2.5")
    assert SC.timeout_scale(config={"timeout_scale": 3}) == 2.5
    assert SC.timeout_scale(1.5, {"timeout_scale": 3}) == 1.5
    with pytest.raises(ElateError, match="between"):
        SC.timeout_scale(50)
    monkeypatch.setenv("ELATE_TIMEOUT_SCALE", "fast")
    with pytest.raises(ElateError, match="must be a number"):
        SC.timeout_scale()
    assert SC._step_timeout({}, "wait", {"_scale": 3}) == 30.0
    assert SC._step_timeout({"timeout": 2}, "wait", {"_scale": 3}) == 6.0


def test_bad_delivery_default_is_rejected():
    with pytest.raises(ElateError, match="keys_delivery"):
        SC.validate_script({"defaults": {"keys_delivery": "telepathy"},
                            "steps": [{"keys": "a"}]})


# -- requires / SKIP ----------------------------------------------------------

def test_unmet_requirement_reasons(work: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ELATE_TEST_UNSET", raising=False)
    check = lambda req: SC.unmet_requirement({"requires": req}, work)  # noqa: E731
    assert "ELATE_TEST_UNSET" in check({"env": "ELATE_TEST_UNSET"})
    assert "not found" in check({"executable": ["sh", "no-such-tool-xyz"]})
    assert "exited 3" in check({"command": "echo nope >&2; exit 3"})
    assert check({"executable": "sh", "command": ["true"]}) is None
    with pytest.raises(ElateError, match="unknown key"):
        SC.validate_script({"requires": {"os": "linux"},
                            "steps": [{"eval": "t"}]})


def test_variant_requires_skips_without_booting(
        work: Path, elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ELATE_TEST_REMOTE", raising=False)
    scen = _write(work / "v.json", {
        "variants": {"remote": {"host": "h",
                                "requires": {"env": "ELATE_TEST_REMOTE"}}},
        "steps": [{"eval": "{{host}}"}]})
    code, out = _run_json(["run", str(scen), "--variant", "remote"], capsys)
    assert code == 0 and out["status"] == "skip"
    assert "ELATE_TEST_REMOTE" in out["skip_reason"]
    assert S.list_sessions() == []  # nothing booted
    code = cli.main(["run", str(scen), "--variant", "remote",
                     "--format", "junit"])
    xml = capsys.readouterr().out
    assert code == 0 and "<skipped" in xml and "ELATE_TEST_REMOTE" in xml


# -- real runs ----------------------------------------------------------------

@needs_emacs
def test_retries_report_flaky_and_keep_failed_attempts(
        work: Path, elate_home: Path, capsys: pytest.CaptureFixture[str]):
    flag = work / "ran-once"
    form = (f'(if (file-exists-p "{flag}") t '
            f'(write-region "" nil "{flag}") nil)')
    scen = _write(work / "flaky.json", {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": form}}]})
    code, out = _run_json(["run", str(scen)], capsys)
    assert code == 1 and out["status"] == "fail"
    flag.unlink()
    code, out = _run_json(["run", str(scen), "--retries", "2"], capsys)
    assert code == 0 and out["status"] == "flaky" and out["success"] is True
    first, second = out["attempts"]
    assert not first["success"] and second["success"]
    assert Path(first["session_dir"]).is_dir()  # failed attempt kept


@needs_emacs
def test_stdin_script_and_delivery_default(
        work: Path, elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(work)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session": {"config": "bare", "size": "80x24"},
        "defaults": {"keys_delivery": "events"},
        "steps": [{"keys": "a"}, {"eval": "(+ 1 2)"}]})))
    code, out = _run_json(["run", "-"], capsys)
    assert code == 0, out
    assert out["steps"][0]["result"]["delivered"] == "events"
    assert out["steps"][1]["result"]["value"] == "3"


@needs_emacs
def test_matrix_over_scenarios_in_parallel(
        work: Path, elate_home: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ELATE_TEST_REMOTE", raising=False)
    ok = _write(work / "ok.json", {
        "session": {"config": "bare", "size": "80x24"},
        "variants": {"a": {"v": "1"}, "b": {"v": "2"},
                     "far": {"v": "3", "requires": {"env": "ELATE_TEST_REMOTE"}}},
        "steps": [{"assert": {"eval": "(= (+ {{v}} 0) {{v}})"}}]})
    bad = _write(work / "bad.json", {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "t"}, {"assert": {"eval": "nil"}}]})
    code, out = _run_json(["matrix", "--jobs", "3", str(ok), str(bad)], capsys)
    assert code == 1
    assert out["totals"] == {"pass": 2, "fail": 1, "skip": 1, "flaky": 0}
    assert out["axes"][0] == "scenario"
    failed = next(r for r in out["results"] if r["status"] == "fail")
    assert failed["script"] == str(bad)
    assert failed["failed_steps"][0]["index"] == 2
    assert Path(failed["session_dir"]).is_dir()  # the log is kept
    assert not [s for s in S.list_sessions() if s["status"] == "running"]
    code = cli.main(["matrix", str(ok), str(bad), "--variant", "a",
                     "--format", "junit"])
    xml = capsys.readouterr().out
    assert code == 1
    assert xml.count("<testsuite ") == 2 and "<failure" in xml


def test_template_layers_and_config_anchoring(work: Path):
    _write(work / "cfg-lib.json", {"templates": {
        "a": {"steps": [{"eval": "cfg-include"}]},
        "b": {"steps": [{"eval": "cfg-include"}]}}})
    (work / "bin").mkdir()
    _write(work / ".elate.json", {
        "include": ["cfg-lib.json"],
        "templates": {"a": {"steps": [{"eval": "cfg-inline"}]},
                      "c": {"steps": [{"eval": "cfg-inline"}]}},
        "requires": {"executable": "./bin/tool", "command": "test -d bin"}})
    _write(work / "t" / "lib.json",
           {"templates": {"b": {"steps": [{"eval": "scen-include"}]}}})
    scen = _write(work / "t" / "s.json", {
        "include": ["lib.json"],
        "steps": [{"use": "a"}, {"use": "b"}, {"use": "c"}]})
    raw, base = SC.load_raw(scen)
    # config inline < config includes < scenario includes < scenario inline
    assert [st["eval"] for st in raw["steps"]] == [
        "cfg-include", "scen-include", "cfg-inline"]
    # Config requires act from the config's directory, not the scenario's.
    assert "./bin/tool" not in str(raw["requires"]["executable"])
    assert SC.unmet_requirement(raw, base) == "executable '%s' not found" % (
        work.resolve() / "bin" / "tool")
    tool = work / "bin" / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    assert SC.unmet_requirement(raw, base) is None


def test_untrusted_directory_ends_the_config_walk(work: Path):
    _write(work / ".elate.json", {"defaults": {"timeout": 3}})
    inner = work / "shared"
    inner.mkdir()
    inner.chmod(0o777)  # writable by others: a config above it is not trusted
    scen = _write(inner / "s.json", {"steps": [{"eval": "t"}]})
    raw, _ = SC.load_raw(scen)
    assert "defaults" not in raw
    inner.chmod(0o755)
    raw, _ = SC.load_raw(scen)
    assert raw["defaults"] == {"timeout": 3}
    (work / ".elate.json").chmod(0o666)  # a config others can rewrite
    raw, _ = SC.load_raw(scen)
    assert "defaults" not in raw


def test_non_dict_scenario_defaults_reach_validation(work: Path):
    _write(work / ".elate.json", {"defaults": {"timeout": 3}})
    scen = _write(work / "s.json", {"defaults": [1], "steps": [{"eval": "t"}]})
    with pytest.raises(ElateError, match='"defaults" must be an object'):
        SC.load_script(scen)


def test_retries_reject_keep_and_bad_scale_fails_fast(
        work: Path, elate_home: Path, capsys: pytest.CaptureFixture[str]):
    scen = _write(work / "s.json", {"steps": [{"eval": "t"}]})
    code, out = _run_json(["run", str(scen), "--keep", "--retries", "1"], capsys)
    assert code == 1 and "--keep" in out["error"]
    code, out = _run_json(["matrix", str(scen), "--timeout-scale", "50"], capsys)
    assert code == 1 and "between" in out["error"]
    assert S.list_sessions() == []


@needs_emacs
def test_matrix_axes_are_uniform_across_scenarios(
        work: Path, elate_home: Path, capsys: pytest.CaptureFixture[str]):
    a = _write(work / "a" / "t.json", {
        "session": {"config": "bare", "size": "80x24"},
        "variants": {"x": {"v": "1"}},
        "steps": [{"eval": "(list {{v}} {{n}})"}]})
    b = _write(work / "b" / "t.json", {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"eval": "t"}]})
    code, out = _run_json(["matrix", "--param", "n=1,2", str(a), str(b)], capsys)
    assert code == 0, out
    keys = {tuple(sorted(r["axes"])) for r in out["results"]}
    assert keys == {("emacs", "n", "scenario", "variant")}
    plain = [r for r in out["results"] if r["axes"]["n"] is None]
    assert len(plain) == 1 and plain[0]["axes"]["variant"] is None
    # Same file stem in two directories: labelled by path.
    assert {r["axes"]["scenario"] for r in out["results"]} == {str(a), str(b)}


@needs_emacs
def test_interrupting_a_parallel_matrix_is_prompt(
        work: Path, elate_home: Path):
    import os
    import signal
    import subprocess
    import sys
    import time

    scen = _write(work / "slow.json", {
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"comment": "{{rep}}"},
                  {"wait": "until", "pred": "nil", "timeout": 60}]})
    proc = subprocess.Popen(
        [sys.executable, "-m", "elate.cli", "--json", "matrix", "-j", "2",
         "--param", "rep=1,2,3", str(scen)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "ELATE_HOME": str(elate_home)})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if sum(s["status"] == "running" for s in S.list_sessions()) >= 2:
            break
        time.sleep(0.2)
    time.sleep(1.0)
    t0 = time.monotonic()
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=40)
    assert time.monotonic() - t0 < 15, "interrupt waited out the steps"
    assert proc.returncode == 130
    assert not [s for s in S.list_sessions() if s["status"] == "running"]
