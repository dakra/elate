"""Plugin hook script behavior (Phase I5).

hooks/check-running.sh serves two events: `end` (SessionEnd -- warning
on stderr + exit 1; SessionEnd stdout is debug-log-only and stderr
surfaces on a non-zero exit per the docs) and `start` (SessionStart --
plain stdout is injected as model context, exit 0). It runs at every
session start/end for EVERY user of the plugin, so its contract is
asymmetric: it may only ever speak when an elate session is still
RUNNING, and it must be silent exit 0 in every other circumstance:
elate/uv missing from PATH, `elate list` failing, no sessions, only
stopped/dead/corrupt sessions.

Four test tiers: no elate at all (PATH stripped to bare POSIX tools),
a fake `elate` with canned output (parsing without booting Emacs), fake
`uvx`/`uv` binaries pinning the offline fallback argv (hermetic -- no
real uv, no network), and a real session (gated on emacs/tmux being
installed).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "hooks" / "check-running.sh"

# Everything the script itself needs; deliberately NOT uv/uvx/elate.
POSIX_TOOLS = ("sh", "tr", "sed", "wc", "cat", "env")

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux")
    and shutil.which("emacsclient")
)


@pytest.fixture()
def toolbox(tmp_path: Path) -> Path:
    """A PATH directory holding only bare POSIX tools."""
    d = tmp_path / "bin"
    d.mkdir()
    for tool in POSIX_TOOLS:
        real = shutil.which(tool)
        assert real, f"test machine lacks {tool}"
        (d / tool).symlink_to(real)
    return d


def _run(path_dir: Path | str, mode: str = "end",
         **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": str(path_dir), **env_extra}
    return subprocess.run(
        ["/bin/sh", str(SCRIPT), mode],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _fake_tool(toolbox: Path, name: str, body: str) -> None:
    fake = toolbox / name
    fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    fake.chmod(0o755)


def _fake_elate(toolbox: Path, body: str) -> None:
    _fake_tool(toolbox, "elate", body)


RUNNING_PAYLOAD = (
    '{"ok": true, "sessions": [{"name": "alpha", "ui": "tty", '
    '"status": "running", "emacs_version": "31.0.90", "uptime": 5.0, '
    '"session_dir": "/x"}]}'
)


@pytest.mark.parametrize("mode", ["end", "start"])
def test_silent_exit_zero_without_elate(toolbox: Path, mode: str):
    proc = _run(toolbox, mode)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""


def test_silent_exit_zero_when_list_fails(toolbox: Path):
    _fake_elate(toolbox, "exit 1")
    proc = _run(toolbox)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""


@pytest.mark.parametrize("mode", ["end", "start"])
def test_silent_exit_zero_with_no_sessions(toolbox: Path, mode: str):
    _fake_elate(toolbox, """printf '%s\\n' '{"ok": true, "sessions": []}'""")
    proc = _run(toolbox, mode)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""


def test_warns_only_about_running_sessions(toolbox: Path):
    payload = (
        '{"ok": true, "sessions": ['
        '{"name": "alpha", "ui": "tty", "status": "running", '
        '"emacs_version": "31.0.90", "uptime": 5.0, "session_dir": "/x"}, '
        '{"name": "beta", "ui": "tty", "status": "stopped", '
        '"emacs_version": "31.0.90", "uptime": null, "session_dir": "/y"}, '
        '{"name": "dee", "ui": "gui", "status": "dead", '
        '"emacs_version": null, "uptime": null, "session_dir": "/z"}, '
        '{"name": "gamma", "status": "corrupt", "error": "boom"}]}'
    )
    _fake_elate(toolbox, f"printf '%s\\n' '{payload}'")
    proc = _run(toolbox)
    assert proc.returncode == 1
    assert proc.stdout == ""  # debug-log-only channel stays clean
    assert "alpha" in proc.stderr
    for quiet in ("beta", "dee", "gamma"):
        assert quiet not in proc.stderr, (
            f"hook must warn about RUNNING sessions only, mentioned {quiet}")
    assert "elate stop" in proc.stderr
    # start mode: same filter, but context goes to stdout and the exit
    # is 0 (a non-zero SessionStart exit would surface as a hook error).
    proc = _run(toolbox, "start")
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert "alpha" in proc.stdout and "still running" in proc.stdout
    for quiet in ("beta", "dee", "gamma"):
        assert quiet not in proc.stdout


# ---------------------------------------------------------------------------
# Fallback resolution order: elate on PATH, else `uvx --offline elate`,
# else `uv tool run --offline elate`. The fakes assert the EXACT argv the
# hook is contracted to use (offline-only -- a session start/end must
# never wait on the network), hermetically: no real uv, no network.

@pytest.mark.parametrize("mode", ["end", "start"])
def test_uvx_offline_fallback(toolbox: Path, mode: str):
    # No `elate` on PATH; the fake uvx answers ONLY the documented
    # offline argv, so any drift in the fallback command fails loudly.
    _fake_tool(toolbox, "uvx",
               '[ "$*" = "--offline elate --json list" ] || exit 9\n'
               f"printf '%s\\n' '{RUNNING_PAYLOAD}'")
    proc = _run(toolbox, mode)
    if mode == "end":
        assert proc.returncode == 1
        assert proc.stdout == ""
        assert "alpha" in proc.stderr and "elate stop" in proc.stderr
    else:
        assert proc.returncode == 0
        assert proc.stderr == ""
        assert "alpha" in proc.stdout and "still running" in proc.stdout


def test_uv_tool_run_offline_fallback(toolbox: Path):
    # No `elate`, no `uvx`: the last resort is `uv tool run --offline`.
    _fake_tool(toolbox, "uv",
               '[ "$*" = "tool run --offline elate --json list" ] || exit 9\n'
               f"printf '%s\\n' '{RUNNING_PAYLOAD}'")
    proc = _run(toolbox)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "alpha" in proc.stderr and "elate stop" in proc.stderr


def test_elate_on_path_wins_over_uvx(toolbox: Path):
    # Resolution order pin: a working `elate` must be preferred -- the
    # decoy uvx produces nothing, so the warning proves which ran.
    _fake_elate(toolbox, f"printf '%s\\n' '{RUNNING_PAYLOAD}'")
    _fake_tool(toolbox, "uvx", "exit 9")
    proc = _run(toolbox)
    assert proc.returncode == 1
    assert "alpha" in proc.stderr


@pytest.mark.parametrize("mode", ["end", "start"])
def test_uvx_fallback_failure_is_silent(toolbox: Path, mode: str):
    # uvx exists but cannot resolve elate offline (e.g. cold cache):
    # the fail-silent contract holds on the fallback path too.
    _fake_tool(toolbox, "uvx", "exit 1")
    proc = _run(toolbox, mode)
    assert proc.returncode == 0
    assert proc.stdout == "" and proc.stderr == ""


@pytest.mark.skipif(not HAVE_DEPS,
                    reason="emacs, emacsclient, and tmux are required")
def test_real_session_warns_then_goes_quiet(toolbox: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    # tempfile.mkdtemp, not tmp_path: macOS caps unix-socket paths at
    # ~104 bytes; pytest's nested tmp dirs are too deep for the
    # in-sandbox tmux socket (Phase 2 finding).
    home = Path(tempfile.mkdtemp(prefix="elate-test-"))
    monkeypatch.setenv("ELATE_HOME", str(home))
    from elate import session as S

    # The venv's bin dir provides the real `elate` entry point, the
    # toolbox the POSIX tools, and tmux's own dir the liveness checks
    # `elate list` performs. uv/uvx stay invisible.
    tmux_dir = Path(shutil.which("tmux")).parent  # type: ignore[arg-type]
    path = os.pathsep.join(
        str(p) for p in (Path(sys.executable).parent, toolbox, tmux_dir))
    name = f"hook{os.getpid()}"
    S.start_session(name, cols=80, rows=24)
    try:
        proc = _run(path, ELATE_HOME=str(home))
        assert proc.returncode == 1
        assert name in proc.stderr and "elate stop" in proc.stderr
        assert proc.stdout == ""
    finally:
        S.stop_session(name)
    proc = _run(path, ELATE_HOME=str(home))
    assert proc.returncode == 0, "stopped sessions must not warn"
    assert proc.stderr == "" and proc.stdout == ""
    S.purge_sessions([name])
    assert not (home / "sessions" / name).exists()
    shutil.rmtree(home, ignore_errors=True)
