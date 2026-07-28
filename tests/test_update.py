"""`elate update` tests -- pure unit, no Emacs, tmux, or real subprocesses.

They pin the install-channel classification from sys.prefix, the step plan
per channel (upgrade first, then a skill refresh per copy location; uvx
collapses both into `uvx --refresh`), --dry-run inertness, the non-tty
--yes requirement, execution order with first-failure abort, and the
running-sessions hygiene line.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from elate import cli, install
from elate.cli import main


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point install's $HOME/XDG resolution at a throwaway tree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install, "_home", lambda: home)
    monkeypatch.setattr(install, "_xdg_config_home", lambda: home / ".config")
    return home


@pytest.fixture
def no_sessions(monkeypatch):
    monkeypatch.setattr(cli.S, "list_sessions", lambda: [])


@pytest.fixture
def copies(fake_home, tmp_path, monkeypatch):
    """One project copy (cwd inside the project) and one global copy."""
    proj = tmp_path / "proj"
    proj_md = proj / ".claude/skills/elate/SKILL.md"
    proj_md.parent.mkdir(parents=True)
    proj_md.write_text("version: 0.1.0\n")
    global_md = fake_home / ".claude/skills/elate/SKILL.md"
    global_md.parent.mkdir(parents=True)
    global_md.write_text("version: 0.1.0\n")
    monkeypatch.chdir(proj)
    return proj


@pytest.mark.parametrize("prefix,channel", [
    ("/opt/homebrew/Cellar/elate/0.13.0/libexec", "homebrew"),
    ("/home/x/.local/share/uv/tools/elate", "uv-tool"),
    ("/Users/x/Library/Caches/uv/archive-v0/AbCd12", "uvx"),
    ("/home/x/.cache/uv/archive-v0/AbCd12", "uvx"),
    ("/home/x/.cache/uv/environments-v2/elate-1a2b", "uvx"),
    ("/home/x/.local/pipx/venvs/elate", "pipx"),
    ("/home/x/src/proj/.venv", "pip"),
    ("/usr", "pip"),
])
def test_install_channel_classification(monkeypatch, prefix, channel):
    monkeypatch.setattr(sys, "prefix", prefix)
    assert install.install_channel() == channel


def test_update_steps_upgrade_then_refresh(copies, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda n: f"/fake/bin/{n}")
    steps = install.update_steps("uv-tool")
    # Refreshes name the found copies' harnesses explicitly: the refresh
    # must not depend on auto-detection re-finding them.
    assert [s["cmd"] for s in steps] == [
        ["uv", "tool", "upgrade", "elate"],
        ["/fake/bin/elate", "install", "claude"],
        ["/fake/bin/elate", "install", "claude", "--global"],
    ]
    assert steps[0]["cwd"] is None
    assert steps[1]["cwd"] == str(copies)
    assert steps[2]["cwd"] is None


def test_update_steps_uvx_collapses_upgrade_into_refresh(copies):
    steps = install.update_steps("uvx")
    assert [s["cmd"] for s in steps] == [
        ["uvx", "--refresh", "elate", "install", "claude"],
        ["uvx", "--refresh", "elate", "install", "claude", "--global"],
    ]
    assert steps[0]["cwd"] == str(copies)


def test_update_steps_pip_targets_this_interpreter(copies, monkeypatch):
    """A bare `pip` on PATH may belong to another environment (or the dev
    checkout's venv) -- the executed upgrade must go through the running
    interpreter."""
    monkeypatch.setattr(install.shutil, "which", lambda n: f"/fake/bin/{n}")
    steps = install.update_steps("pip")
    assert steps[0]["cmd"] == [
        sys.executable, "-m", "pip", "install", "-U", "elate"]


def test_update_steps_skip_symlinked_copies(fake_home, tmp_path, monkeypatch):
    """A symlinked skill dir tracks its checkout (the README-suggested
    setup); update must not replace it with a static copy."""
    checkout = tmp_path / "checkout-skill"
    checkout.mkdir()
    (checkout / "SKILL.md").write_text("version: 99.0.0\n")
    root = fake_home / ".claude" / "skills"
    root.mkdir(parents=True)
    (root / "elate").symlink_to(checkout, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    steps = install.update_steps("pipx")
    assert [s["cmd"] for s in steps] == [["pipx", "upgrade", "elate"]]


def test_update_steps_uvx_without_copies_still_refreshes(fake_home, tmp_path,
                                                         monkeypatch):
    monkeypatch.chdir(tmp_path)
    steps = install.update_steps("uvx")
    assert steps == [
        {"cmd": ["uvx", "--refresh", "elate", "--version"], "cwd": None}]


def test_update_steps_without_copies_only_upgrades(fake_home, tmp_path,
                                                   monkeypatch):
    monkeypatch.chdir(tmp_path)
    steps = install.update_steps("pipx")
    assert [s["cmd"] for s in steps] == [["pipx", "upgrade", "elate"]]


def test_update_dry_run_prints_plan_and_executes_nothing(
        copies, monkeypatch, capsys, no_sessions):
    monkeypatch.setattr(sys, "prefix", "/home/x/.local/share/uv/tools/elate")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: ran.append(a))
    assert main(["--human", "update", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "uv tool upgrade elate" in out
    assert "uv-tool" in out
    assert ran == []


def test_update_dry_run_json_separates_cmd_and_cwd(copies, monkeypatch,
                                                   capsys, no_sessions):
    """steps[].cmd is the clean command; the working dir is its own key,
    never a display annotation baked into the string."""
    import json

    monkeypatch.setattr(sys, "prefix", "/home/x/.local/share/uv/tools/elate")
    monkeypatch.setattr(install.shutil, "which", lambda n: f"/fake/bin/{n}")
    assert main(["update", "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    install_step = result["steps"][1]
    assert install_step["cmd"] == "/fake/bin/elate install claude"
    assert install_step["cwd"] == str(copies)
    assert "(in " not in install_step["cmd"]


def test_update_non_tty_without_yes_is_usage_error(copies, monkeypatch,
                                                   capsys, no_sessions):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: ran.append(a))
    assert main(["--human", "update"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert ran == []


def test_update_interactive_decline_aborts(copies, monkeypatch, capsys,
                                           no_sessions):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: ran.append(a))
    assert main(["--human", "update"]) == 1
    assert "aborted" in capsys.readouterr().out
    assert ran == []


def test_update_decline_json_says_not_ok(copies, monkeypatch, capsys,
                                         no_sessions):
    """A declined run exits 1 and must carry ok:false in the JSON result
    (overriding main()'s {"ok": True, **result} spread)."""
    import json

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert main(["update"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["aborted"] is True


def test_update_prompt_eof_declines(copies, monkeypatch, capsys, no_sessions):
    def raise_eof():
        raise EOFError

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", raise_eof)
    ran = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda *a, **k: ran.append(a))
    assert main(["--human", "update"]) == 1
    assert "aborted" in capsys.readouterr().out
    assert ran == []


def test_update_missing_step_binary_is_flat_error(copies, monkeypatch,
                                                  capsys, no_sessions):
    """An absent upgrade binary (e.g. pipx not on PATH) must surface as
    the flat `elate:` error with exit 1, not a traceback."""
    def fake_run(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert main(["--human", "update", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "update step failed" in err
    assert "Traceback" not in err


def test_update_yes_runs_steps_in_order_with_hygiene(copies, monkeypatch,
                                                     capsys):
    monkeypatch.setattr(sys, "prefix", "/home/x/.local/share/uv/tools/elate")
    monkeypatch.setattr(install.shutil, "which", lambda n: f"/fake/bin/{n}")
    monkeypatch.setattr(
        cli.S, "list_sessions",
        lambda: [{"name": "a", "status": "running"},
                 {"name": "b", "status": "stopped"}])
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("cwd")))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert main(["--human", "update", "--yes"]) == 0
    assert calls == [
        (["uv", "tool", "upgrade", "elate"], None),
        (["/fake/bin/elate", "install", "claude"], str(copies)),
        (["/fake/bin/elate", "install", "claude", "--global"], None),
    ]
    captured = capsys.readouterr()
    assert "1 running session(s)" in captured.out
    assert "restart the MCP client" in captured.out
    assert "update plan" in captured.err  # plan shown under --yes too


def test_update_aborts_on_first_failure(copies, monkeypatch, capsys,
                                        no_sessions):
    monkeypatch.setattr(sys, "prefix", "/home/x/.local/share/uv/tools/elate")
    monkeypatch.setattr(install.shutil, "which", lambda n: f"/fake/bin/{n}")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="",
                                           stderr="boom: no network")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert main(["--human", "update", "--yes"]) == 1
    assert len(calls) == 1  # first failure stops the run
    err = capsys.readouterr().err
    assert "update step failed" in err
    assert "boom: no network" in err
