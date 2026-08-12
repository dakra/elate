"""Unit tests for sandbox startup wiring: --eval-file, --profile, --home-seed.

These exercise ``build_sandbox`` directly -- it only creates the sandbox
directory tree, seeds the fake $HOME, and writes the generated init.el / Emacs
argv; no Emacs or tmux is spawned -- so they are fast and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elate import sandbox
from elate.errors import ElateError


def _init_el(session_dir: Path) -> str:
    return (session_dir / "init" / "init.el").read_text(encoding="utf-8")


def test_eval_file_and_profile_load_before_inline_eval(tmp_path, monkeypatch):
    setup = tmp_path / "setup.el"
    setup.write_text("(setq from-eval-file t)\n", encoding="utf-8")
    prof_dir = tmp_path / "cfg" / "elate" / "profiles"
    prof_dir.mkdir(parents=True)
    (prof_dir / "demo.el").write_text("(setq from-profile t)\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    sd = tmp_path / "sess"
    sandbox.build_sandbox(sd, eval_files=[str(setup)], profiles=["demo"],
                          evals=["(setq inline-marker t)"])
    init = _init_el(sd)
    # Ordering: --eval-file -> --profile -> inline --eval (so inline wins).
    i_file = init.index(str(setup))
    i_prof = init.index(str(prof_dir / "demo.el"))
    i_inline = init.index("(setq inline-marker t)")
    assert i_file < i_prof < i_inline
    # All run inside the single elate-guard, i.e. before emacs-startup-hook.
    assert "(elate-guard" in init


def test_eval_file_loads_without_loadpath_mutation(tmp_path):
    setup = tmp_path / "s.el"
    setup.write_text("(setq x 1)\n", encoding="utf-8")
    sd = tmp_path / "sess"
    sandbox.build_sandbox(sd, eval_files=[str(setup)])
    init = _init_el(sd)
    assert f'(load "{setup}" nil t)' in init
    # Unlike --load, a forms file never touches load-path.
    assert "add-to-list 'load-path" not in init


def test_missing_eval_file_errors_before_launch(tmp_path):
    sd = tmp_path / "sess"
    with pytest.raises(ElateError, match="--eval-file does not exist"):
        sandbox.build_sandbox(sd, eval_files=[str(tmp_path / "nope.el")])


def test_missing_profile_names_the_search_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    sd = tmp_path / "sess"
    with pytest.raises(ElateError) as exc:
        sandbox.build_sandbox(sd, profiles=["ghost"])
    assert str(Path("elate") / "profiles" / "ghost.el") in str(exc.value)


def test_profile_accepts_a_literal_path(tmp_path):
    prof = tmp_path / "myprof.el"
    prof.write_text("(setq y 1)\n", encoding="utf-8")
    sd = tmp_path / "sess"
    sandbox.build_sandbox(sd, profiles=[str(prof)])
    assert f'(load "{prof}" nil t)' in _init_el(sd)


def test_home_seed_merges_into_fake_home(tmp_path):
    seed = tmp_path / "seedhome"
    (seed / ".config" / "sub").mkdir(parents=True)
    (seed / ".config" / "sub" / "marker.txt").write_text("SEEDED", encoding="utf-8")
    (seed / ".bashrc").write_text("export FOO=1\n", encoding="utf-8")
    sd = tmp_path / "sess"
    sandbox.build_sandbox(sd, home_seed=str(seed))
    home = sd / "home"
    assert (home / ".config" / "sub" / "marker.txt").read_text() == "SEEDED"
    assert (home / ".bashrc").exists()
    # The pre-created XDG dirs survive the merge (fixture files just add to them).
    assert (home / ".local" / "share").is_dir()
    assert (home / ".cache").is_dir()


def test_home_seed_must_be_a_directory(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    sd = tmp_path / "sess"
    with pytest.raises(ElateError,
                       match="--home-seed must be an existing directory"):
        sandbox.build_sandbox(sd, home_seed=str(f))


def test_require_forms_after_load_before_evals(tmp_path):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    sd = tmp_path / "sess"
    sandbox.build_sandbox(sd, loads=[str(pkg)], requires=["mypkg"],
                          evals=["(setq inline-marker t)"])
    init = _init_el(sd)
    i_load = init.index("add-to-list 'load-path")
    i_req = init.index("(require 'mypkg)")
    i_inline = init.index("(setq inline-marker t)")
    assert i_load < i_req < i_inline
    assert "(elate-guard" in init


def test_require_rejects_non_symbol_feature(tmp_path):
    with pytest.raises(ElateError, match="feature name"):
        sandbox.build_sandbox(tmp_path / "sess",
                              requires=["(malicious-form)"])
