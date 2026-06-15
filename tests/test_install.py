"""`elate install` tests -- pure filesystem/unit, no Emacs or tmux.

They pin the skill-destination map per harness, the global/project/dry-run
scopes, idempotent re-runs, harness detection, and the --mcp wiring shapes
(cli vs manual snippet vs unsupported), all against a fake $HOME so the real
user config is never touched.
"""

from __future__ import annotations

import pytest

from elate import install, paths
from elate.cli import main
from elate.errors import UsageError


def test_bundled_skill_resolves():
    """skill_dir resolves (here via the dev-checkout fallback)."""
    assert (paths.skill_dir() / "SKILL.md").is_file()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point install's $HOME/XDG resolution at a throwaway tree."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(install, "_home", lambda: home)
    monkeypatch.setattr(install, "_xdg_config_home", lambda: home / ".config")
    return home


def test_install_all_copies_skill_to_every_harness(fake_home):
    result = install.run_install(["all"])
    assert result["scope"] == "global"
    assert {e["harness"] for e in result["installed"]} == set(install.HARNESS_KEYS)
    expected = {
        ".claude/skills/elate/SKILL.md",
        ".agents/skills/elate/SKILL.md",
        ".config/opencode/skills/elate/SKILL.md",
        ".pi/agent/skills/elate/SKILL.md",
        ".gemini/skills/elate/SKILL.md",
    }
    for rel in expected:
        assert (fake_home / rel).is_file(), rel
    # A full skill, not just SKILL.md, lands at the destination.
    assert (fake_home / ".claude/skills/elate/REFERENCE.md").is_file()


def test_dry_run_writes_nothing(fake_home):
    result = install.run_install(["claude"], dry_run=True)
    assert result["dry_run"] is True
    assert result["installed"][0]["action"] == "would install"
    assert not (fake_home / ".claude").exists()


def test_install_is_idempotent(fake_home):
    install.run_install(["claude"])
    install.run_install(["claude"])  # must not raise
    assert (fake_home / ".claude/skills/elate/SKILL.md").is_file()


def test_project_scope_uses_cwd_dirs(fake_home, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    install.run_install(["claude", "codex"], project=True)
    assert (proj / ".claude/skills/elate/SKILL.md").is_file()
    assert (proj / ".agents/skills/elate/SKILL.md").is_file()
    assert not (fake_home / ".claude").exists()  # global home untouched


def test_unknown_harness_raises(fake_home):
    with pytest.raises(UsageError):
        install.run_install(["bogus"])


def test_no_selection_and_no_detection_raises(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    with pytest.raises(UsageError):
        install.run_install([])


def test_detection_selects_present_harness(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    (fake_home / ".pi").mkdir()  # only pi looks present
    result = install.run_install([])
    assert [e["harness"] for e in result["installed"]] == ["pi"]


def test_mcp_plan_shapes(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = install.run_install(["all"], with_mcp=True, dry_run=True)
    mcp = {e["harness"]: e["mcp"] for e in result["installed"]}
    assert mcp["claude"]["status"] == "planned"
    # Claude's `mcp add` defaults to cwd-local scope; a global install must
    # pin --scope user. Codex is always user-global, so it takes no scope.
    assert "--scope user" in mcp["claude"]["command"]
    assert "--scope" not in mcp["codex"]["command"]
    assert mcp["codex"]["status"] == "planned"
    assert mcp["pi"]["status"] == "unsupported"
    assert mcp["opencode"]["status"] == "manual"
    assert mcp["opencode"]["config_path"].endswith("opencode.json")
    assert "uvx" in mcp["opencode"]["snippet"]
    assert mcp["antigravity"]["status"] == "manual"
    assert mcp["antigravity"]["config_path"].endswith("mcp_config.json")


def test_mcp_cli_skipped_when_binary_absent(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    result = install.run_install(["claude"], with_mcp=True)
    assert result["installed"][0]["mcp"]["status"] == "skipped"


def test_mcp_scope_project_for_claude(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = install.run_install(
        ["claude"], project=True, with_mcp=True, dry_run=True)
    assert "--scope project" in result["installed"][0]["mcp"]["command"]


def test_mcp_cli_already_exists_is_not_error(fake_home, monkeypatch):
    """Re-running `mcp add` (the documented refresh) is success, not failure."""
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(argv, **kw):
        return install.subprocess.CompletedProcess(
            argv, 1, stdout="",
            stderr="MCP server elate already exists in local config")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    result = install.run_install(["claude"], with_mcp=True)
    assert result["installed"][0]["mcp"]["status"] == "exists"


def test_mcp_cli_timeout_is_error(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(argv, **kw):
        raise install.subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    mcp = install.run_install(["codex"], with_mcp=True)["installed"][0]["mcp"]
    assert mcp["status"] == "error"
    assert "timed out" in mcp["note"]


def test_symlink_destination_is_not_clobbered(fake_home, tmp_path):
    """A symlinked skill dir is replaced, never written through to its target."""
    target = tmp_path / "orig_skill"
    target.mkdir()
    (target / "SKILL.md").write_text("ORIGINAL")
    dest_root = fake_home / ".claude" / "skills"
    dest_root.mkdir(parents=True)
    (dest_root / "elate").symlink_to(target, target_is_directory=True)

    result = install.run_install(["claude"])
    dest = dest_root / "elate"
    assert not dest.is_symlink() and dest.is_dir()
    assert (dest / "SKILL.md").read_text() != "ORIGINAL"  # real skill copied in
    assert (target / "SKILL.md").read_text() == "ORIGINAL"  # target untouched
    assert result["installed"][0].get("replaced_symlink") is True


def test_stale_file_removed_on_refresh(fake_home):
    install.run_install(["claude"])
    dest = fake_home / ".claude/skills/elate"
    (dest / "STALE.md").write_text("old")
    install.run_install(["claude"])  # refresh
    assert not (dest / "STALE.md").exists()
    assert (dest / "SKILL.md").is_file()


def test_duplicate_harness_is_deduped(fake_home):
    result = install.run_install(["claude", "claude"])
    assert [e["harness"] for e in result["installed"]] == ["claude"]


def test_cli_main_dry_run_returns_zero(capsys):
    assert main(["install", "--dry-run", "claude"]) == 0
