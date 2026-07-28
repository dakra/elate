"""`elate purge` semantics (Phase I5).

The contract under test: purge deletes the sandbox directories of
sessions that are NOT running -- a named running session is a loud
error, --all skips and reports running sessions, dead sessions get the
same force-cleanup as stop before their files go, and corrupt
registries are purgeable too. Only paths directly under sessions_root
are ever removed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli
from elate import session as S
from elate.errors import ElateError, SessionNotFound

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux")
    and shutil.which("emacsclient")
)


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # tempfile.mkdtemp, not tmp_path: macOS caps unix-socket paths at
    # ~104 bytes and pytest's nested tmp dirs blow that budget for the
    # in-sandbox tmux socket (Phase 2 finding).
    tmp = Path(tempfile.mkdtemp(prefix="elate-test-"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_output_mode_default_and_overrides(
        elate_home: Path, capsys: pytest.CaptureFixture[str]):
    # Under pytest capture stdout is not a TTY, so the default is JSON --
    # what an agent or a pipe sees. No flag needed.
    assert cli.main(["list"]) == 0
    out = json.loads(capsys.readouterr().out)  # parses => it really is JSON
    assert out["ok"] is True and out["sessions"] == []
    # --human forces the table even when piped.
    assert cli.main(["--human", "list"]) == 0
    assert "no sessions" in capsys.readouterr().out
    # --json stays explicit.
    assert cli.main(["--json", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_field_prints_one_bare_value(
        elate_home: Path, capsys: pytest.CaptureFixture[str]):
    # Structured fields render as compact JSON, and nothing else prints.
    assert cli.main(["--field", "sessions", "list"]) == 0
    assert capsys.readouterr().out == "[]\n"
    # An unknown field is a usage error (exit 2) naming what exists,
    # with stdout kept clean for command substitution.
    assert cli.main(["--field", "nope", "list"]) == 2
    cap = capsys.readouterr()
    assert cap.out == "" and "no such field" in cap.err
    # A failing command with --field keeps stdout empty too.
    assert cli.main(["--field", "name", "info", "ghost"]) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "ghost" in cap.err


def test_field_text_rendering():
    from elate.cli import _field_text
    r = {"s": "emacs", "t": True, "f": False, "n": None, "i": 3, "x": 1.5,
         "d": {"a": 1}, "l": [1, "two"]}
    assert _field_text(r, "s") == "emacs"       # unquoted
    assert _field_text(r, "t") == "true"
    assert _field_text(r, "f") == "false"
    assert _field_text(r, "n") == "null"
    assert _field_text(r, "i") == "3"
    assert _field_text(r, "x") == "1.5"
    assert _field_text(r, "d") == '{"a": 1}'
    assert _field_text(r, "l") == '[1, "two"]'


def test_purge_needs_names_or_all(elate_home: Path):
    with pytest.raises(ElateError, match="--all"):
        S.purge_sessions()


def test_purge_unknown_name(elate_home: Path):
    with pytest.raises(SessionNotFound, match="nosuch"):
        S.purge_sessions(["nosuch"])


def test_purge_all_with_nothing_is_a_clean_noop(elate_home: Path):
    result = S.purge_sessions(all_sessions=True)
    assert result == {"purged": [], "skipped_running": [],
                      "skipped_recent": [], "freed_bytes": 0}


def test_purge_removes_corrupt_registry(elate_home: Path):
    broken = elate_home / "sessions" / "broken"
    broken.mkdir(parents=True)
    (broken / "session.json").write_text("{not json", encoding="utf-8")
    assert S.list_sessions()[0]["status"] == "corrupt"
    result = S.purge_sessions(all_sessions=True)
    assert [p["name"] for p in result["purged"]] == ["broken"]
    assert result["purged"][0]["status"] == "corrupt"
    assert not broken.exists()


# ---------------------------------------------------------------------------
# Containment: purge must never delete anything but the session entry
# directly under sessions_root, and must never follow a symlink out of it.
# Structural tests -- no Emacs/tmux session is booted.

def _plant_stopped_registry(session_dir: Path, name: str,
                            stopped_at: float | None = None,
                            owner: str | None = None,
                            ttl: float | None = None) -> None:
    """A minimal loadable registry for a stopped tty session.

    Good enough for purge: load_session succeeds, is_alive is False
    (status != "running"), and _force_cleanup's tmux kill-server on the
    nonexistent socket is a check=False no-op.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.json").write_text(json.dumps({
        "name": name,
        "session_dir": str(session_dir),
        "emacs": "/usr/bin/false",
        "emacsclient": "/usr/bin/false",
        "config": "minimal",
        "cols": 80,
        "rows": 24,
        "created_at": 0.0,
        "ui": "tty",
        "tmux_socket": str(session_dir / "tmux.sock"),
        "status": "stopped",
        "stopped_at": stopped_at,
        "owner": owner,
        "ttl": ttl,
    }), encoding="utf-8")


def test_purge_stopped_older_than_filters_by_age(elate_home: Path):
    import time as _time
    root = elate_home / "sessions"
    now = _time.time()
    _plant_stopped_registry(root / "old", "old", stopped_at=now - 7200)    # 2h
    _plant_stopped_registry(root / "fresh", "fresh", stopped_at=now - 60)  # 1m
    # list_sessions reports each stopped session's idle age.
    ages = {s["name"]: s["idle_for"] for s in S.list_sessions()}
    assert ages["old"] > 3600 and ages["fresh"] < 600
    # Purge only those inert at least an hour; the fresh one is kept+reported.
    result = S.purge_sessions(all_sessions=True, stopped_older_than=3600)
    assert [p["name"] for p in result["purged"]] == ["old"]
    assert result["skipped_recent"] == ["fresh"]
    assert not (root / "old").exists() and (root / "fresh").is_dir()


def test_purge_all_preserves_foreign_entries(elate_home: Path,
                                             tmp_path: Path):
    """Non-elate files/dirs in sessions_root (no session.json) are never
    listed and never touched -- including a symlink pointing outside."""
    root = elate_home / "sessions"
    _plant_stopped_registry(root / "mine", "mine")
    (root / "notes.txt").write_text("keep me", encoding="utf-8")
    foreign = root / "not-a-session"
    foreign.mkdir()
    (foreign / "data.txt").write_text("keep me too", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "precious.txt").write_text("precious", encoding="utf-8")
    (root / "link-out").symlink_to(external)

    result = S.purge_sessions(all_sessions=True)
    assert [p["name"] for p in result["purged"]] == ["mine"]
    assert not (root / "mine").exists()
    assert (root / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (foreign / "data.txt").is_file()
    assert (root / "link-out").is_symlink()
    assert (external / "precious.txt").is_file()


def test_purge_does_not_follow_symlinks_inside_a_session_dir(
        elate_home: Path, tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "precious.txt").write_text("precious", encoding="utf-8")
    sess = elate_home / "sessions" / "esc"
    _plant_stopped_registry(sess, "esc")
    (sess / "out").symlink_to(external)

    result = S.purge_sessions(["esc"])
    assert [p["name"] for p in result["purged"]] == ["esc"]
    assert not sess.exists(), "the sandbox itself must go"
    assert (external / "precious.txt").is_file(), (
        "rmtree must remove the symlink, not its target")


def test_purge_symlinked_session_dir_removes_only_the_link(
        elate_home: Path, tmp_path: Path):
    """A session dir that IS a symlink (hand-made; elate never creates
    one): purge unlinks the link itself -- the target survives, the
    session leaves `elate list`, and the report says what happened
    instead of pretending the files were removed."""
    external = tmp_path / "real"
    _plant_stopped_registry(external, "ghost")
    (external / "payload.txt").write_text("x" * 4096, encoding="utf-8")
    root = elate_home / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    link = root / "ghost"
    link.symlink_to(external)
    assert [s["name"] for s in S.list_sessions()] == ["ghost"]

    result = S.purge_sessions(["ghost"])
    (entry,) = result["purged"]
    assert entry["name"] == "ghost" and entry["status"] == "stopped"
    assert "symlink" in entry["note"]
    assert result["freed_bytes"] == 0, (
        "nothing was freed; the report must not claim otherwise")
    assert not os.path.lexists(link), "the link itself must be removed"
    assert (external / "session.json").is_file(), "target must survive"
    assert (external / "payload.txt").is_file()
    assert S.list_sessions() == [], "purged session must leave the list"


# ---------------------------------------------------------------------------
# Owner tags and TTL reaping (multi-agent hygiene)

def test_purge_owner_filter(elate_home: Path):
    import time as _time
    root = elate_home / "sessions"
    now = _time.time()
    _plant_stopped_registry(root / "ax", "ax", stopped_at=now, owner="alice")
    _plant_stopped_registry(root / "ay", "ay", stopped_at=now, owner="alice")
    _plant_stopped_registry(root / "bz", "bz", stopped_at=now, owner="bob")
    _plant_stopped_registry(root / "cq", "cq", stopped_at=now)
    owners = {s["name"]: s.get("owner") for s in S.list_sessions()}
    assert owners == {"ax": "alice", "ay": "alice", "bz": "bob", "cq": None}
    result = S.purge_sessions(owner="alice")
    assert sorted(p["name"] for p in result["purged"]) == ["ax", "ay"]
    assert (root / "bz").is_dir() and (root / "cq").is_dir()
    # --owner composes with --glob.
    result = S.purge_sessions(owner="bob", name_glob="nope-*")
    assert result["purged"] == []
    result = S.purge_sessions(owner="bob", name_glob="b*")
    assert [p["name"] for p in result["purged"]] == ["bz"]


def test_purge_owner_cannot_combine_with_names(elate_home: Path):
    with pytest.raises(ElateError, match="cannot be combined"):
        S.purge_sessions(["x"], owner="alice")


def test_reap_expired_takes_only_expired_ttl_sessions(elate_home: Path):
    import time as _time
    root = elate_home / "sessions"
    now = _time.time()
    # Expired TTL, no TTL, and unexpired TTL: only the first is reaped.
    _plant_stopped_registry(root / "gone", "gone", stopped_at=now - 7200,
                            owner="alice", ttl=3600)
    _plant_stopped_registry(root / "no-ttl", "no-ttl", stopped_at=now - 7200)
    _plant_stopped_registry(root / "young", "young", stopped_at=now - 60,
                            ttl=3600)
    reaped = S.reap_expired()
    assert [r["name"] for r in reaped] == ["gone"]
    assert reaped[0]["owner"] == "alice" and reaped[0]["idle"] >= 7000
    assert not (root / "gone").exists()
    assert (root / "no-ttl").is_dir() and (root / "young").is_dir()
    # The excluded (currently targeted) session survives even when expired.
    _plant_stopped_registry(root / "target", "target", stopped_at=now - 7200,
                            ttl=3600)
    assert S.reap_expired(exclude="target") == []
    assert (root / "target").is_dir()


def test_maybe_reap_is_throttled(elate_home: Path):
    import time as _time
    root = elate_home / "sessions"
    _plant_stopped_registry(root / "r1", "r1",
                            stopped_at=_time.time() - 7200, ttl=3600)
    assert [r["name"] for r in S.maybe_reap_expired()] == ["r1"]
    # Within the throttle window a fresh expired session is left alone.
    _plant_stopped_registry(root / "r2", "r2",
                            stopped_at=_time.time() - 7200, ttl=3600)
    assert S.maybe_reap_expired() == []
    assert (root / "r2").is_dir()


def test_start_rejects_implausible_ttl(elate_home: Path):
    with pytest.raises(ElateError, match="implausible --ttl"):
        S.start_session("ttl-too-small", ttl=5)


@pytest.mark.skipif(not HAVE_DEPS,
                    reason="emacs, emacsclient, and tmux are required")
def test_stop_bulk_selectors_and_ttl_reap_live(
        elate_home: Path, capsys: pytest.CaptureFixture[str]):
    pid = os.getpid()
    a, b = f"own{pid}a", f"own{pid}b"
    # Generous TTL: the session must not be reapable while the test's own
    # CLI calls run (a ttl at the 30s minimum could expire on a slow CI).
    S.start_session(a, cols=80, rows=24, config="bare", owner="alice",
                    ttl=300)
    S.start_session(b, cols=80, rows=24, config="bare", owner="bob")
    try:
        # list carries owner + ttl countdown; --owner filters.
        assert cli.main(["--json", "list", "--owner", "alice"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [s["name"] for s in out["sessions"]] == [a]
        assert out["sessions"][0]["owner"] == "alice"
        assert 0 < out["sessions"][0]["expires_in"] <= 300
        # stop --owner stops only that agent's sessions.
        assert cli.main(["--json", "stop", "--owner", "alice"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["stopped"] == [a]
        statuses = {s["name"]: s["status"] for s in S.list_sessions()}
        assert statuses == {a: "stopped", b: "running"}
        # A running session whose TTL has expired (transcript idle past
        # --ttl) is reaped: restart a, then age its transcript.
        S.start_session(a, cols=80, rows=24, config="bare", ttl=30,
                        replace=True)
        tr = elate_home / "sessions" / a / "log" / "transcript.jsonl"
        old = _time_ago(3600)
        os.utime(tr, (old, old))
        reaped = S.reap_expired()
        assert [r["name"] for r in reaped] == [a]
        assert not (elate_home / "sessions" / a).exists()
        # b (no ttl) is untouched and still running.
        assert S.load_session(b).is_alive()
    finally:
        for name in (a, b):
            try:
                S.stop_session(name)
            except ElateError:
                pass


def _time_ago(seconds: float) -> float:
    import time as _time
    return _time.time() - seconds


@pytest.mark.skipif(not HAVE_DEPS,
                    reason="emacs, emacsclient, and tmux are required")
def test_purge_lifecycle(elate_home: Path):
    pid = os.getpid()
    stopped, running = f"pg{pid}a", f"pg{pid}b"
    S.start_session(stopped, cols=80, rows=24)
    S.start_session(running, cols=80, rows=24)
    try:
        S.stop_session(stopped)

        # Naming a running session is a loud error, nothing is removed.
        with pytest.raises(ElateError, match="still running"):
            S.purge_sessions([running])
        assert (elate_home / "sessions" / stopped).is_dir()

        # --all removes the stopped sandbox, skips + reports the
        # running one, and counts the freed bytes.
        result = S.purge_sessions(all_sessions=True)
        assert [p["name"] for p in result["purged"]] == [stopped]
        assert result["purged"][0]["status"] == "stopped"
        assert result["skipped_running"] == [running]
        assert result["freed_bytes"] > 0
        assert not (elate_home / "sessions" / stopped).exists()
        assert (elate_home / "sessions" / running).is_dir()

        # Kill the survivor behind elate's back: status reads "dead"
        # (registry still says running), and purge takes it -- after
        # the stop-grade force-cleanup, so nothing outlives the files.
        sess = S.load_session(running)
        sess.raw().kill_server()
        statuses = {s["name"]: s["status"] for s in S.list_sessions()}
        assert statuses[running] == "dead"
        result = S.purge_sessions([running])
        assert [p["name"] for p in result["purged"]] == [running]
        assert result["purged"][0]["status"] == "dead"
        assert not (elate_home / "sessions" / running).exists()
        assert S.list_sessions() == []
    finally:
        for name in (stopped, running):
            if (elate_home / "sessions" / name).exists():
                try:
                    S.stop_session(name)
                except ElateError:
                    pass
