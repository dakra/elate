"""CLI contracts: start's init-error exit, --mine, termination teardown."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli
from elate import session as S

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux")
    and shutil.which("emacsclient")
)
needs_emacs = pytest.mark.skipif(not HAVE_DEPS, reason="needs emacs + tmux")


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # /tmp, not the default temp dir: run-<hex> session names plus the
    # server socket must fit macOS's ~104-byte unix-socket path limit.
    tmp = Path(tempfile.mkdtemp(prefix="el-", dir="/tmp"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_emacs
def test_start_fails_on_init_error_unless_allowed(
        elate_home: Path, capsys: pytest.CaptureFixture[str]):
    try:
        code = cli.main(["--json", "start", "--name", "ie1",
                         "--eval", '(error "setup boom")'])
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["ok"] is False and "setup boom" in out["init_error"]
        assert "still running" in out["error"]
        assert S.load_session("ie1").is_alive()  # kept for inspection

        code = cli.main(["--json", "start", "--name", "ie2",
                         "--allow-init-error", "--eval", '(error "boom2")'])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["ok"] is True
        assert "boom2" in out["init_error"]
    finally:
        S.stop_session("ie1")
        S.stop_session("ie2")


def test_mine_needs_a_default_owner(elate_home: Path,
                                    monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]):
    assert cli.main(["list", "--mine"]) == 2
    assert "--mine needs a default owner" in capsys.readouterr().err
    assert cli.main(["list", "--mine", "--owner", "x"]) == 2
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "0123abcd-ffff")
    assert S.default_owner() == "claude-0123abcd"
    monkeypatch.setenv("ELATE_OWNER", "ci")
    assert S.default_owner() == "ci"
    assert cli.main(["--json", "list", "--mine"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"] == []


@needs_emacs
def test_sigterm_during_run_stops_the_throwaway_session(elate_home: Path,
                                                         tmp_path: Path):
    script = tmp_path / "slow.json"
    script.write_text(json.dumps({
        "name": "slow", "session": {"ui": "tty"},
        "steps": [{"eval": "(sleep-for 30)", "timeout": 60}]}))
    proc = subprocess.Popen(
        [sys.executable, "-m", "elate.cli", "run", str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "ELATE_HOME": str(elate_home)})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if any(s["status"] == "running" for s in S.list_sessions()):
            break
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(f"the run never started its session: {proc.communicate()}")
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM, err
    assert "interrupted" in err
    assert not [s for s in S.list_sessions() if s["status"] == "running"]
