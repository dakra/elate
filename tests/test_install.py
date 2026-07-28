"""`elate install` tests -- pure filesystem/unit, no Emacs or tmux.

They pin the skill-destination map per harness, the project/global/dry-run
scopes (the CLI defaults to project-local; --global opts out), the scope
guards and preflight notices, idempotent re-runs, harness detection, the
--mcp wiring shapes (cli vs manual snippet vs unsupported), and the skill
content-version staleness machinery, all against a fake $HOME so the real
user config is never touched.
"""

from __future__ import annotations

import re

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


# ---------------------------------------------------------------------------
# CLI scope: project-local by default, --global for user-wide.


def test_cli_default_scope_is_project(fake_home, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    assert main(["install", "claude"]) == 0
    assert (proj / ".claude/skills/elate/SKILL.md").is_file()
    assert not (fake_home / ".claude").exists()


def test_cli_global_flag_installs_user_wide(fake_home, tmp_path, monkeypatch,
                                            capsys):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    assert main(["install", "--global", "claude"]) == 0
    assert (fake_home / ".claude/skills/elate/SKILL.md").is_file()
    assert not (proj / ".claude").exists()


def test_project_install_warns_without_markers(fake_home, tmp_path,
                                               monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    result = install.run_install(["claude"], project=True)
    assert result["scope"] == "project"
    assert any("no agent/project files" in n for n in result["notices"])
    assert (bare / ".claude/skills/elate/SKILL.md").is_file()


def test_project_install_quiet_with_marker(fake_home, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    result = install.run_install(["claude"], project=True)
    assert not any("no agent/project files" in n for n in result["notices"])


def test_project_install_in_home_becomes_global(fake_home, monkeypatch):
    monkeypatch.chdir(fake_home)
    result = install.run_install(["claude"], project=True)
    assert result["scope"] == "global"
    assert any("home directory" in n for n in result["notices"])
    assert (fake_home / ".claude/skills/elate/SKILL.md").is_file()
    assert not (fake_home / ".opencode").exists()


def test_preflight_notices_when_tools_missing(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _name: None)
    result = install.run_install(["claude"])
    notices = "\n".join(result["notices"])
    assert "no emacs found" in notices
    assert "tmux not found" in notices


def test_preflight_quiet_when_tools_present(fake_home, monkeypatch):
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    result = install.run_install(["claude"])
    assert result["notices"] == []


# ---------------------------------------------------------------------------
# Skill content version + staleness notice.


def test_skill_content_version_parses(tmp_path):
    md = tmp_path / "SKILL.md"
    md.write_text("---\nname: elate\nversion: 0.14.2\n---\nbody\n")
    assert install.skill_content_version(md) == (0, 14, 2)
    md.write_text("---\nname: elate\n---\nbody\n")
    assert install.skill_content_version(md) is None
    md.write_text("---\nversion: not.a.version\n---\n")
    assert install.skill_content_version(md) is None
    assert install.skill_content_version(tmp_path / "missing.md") is None


def test_bundled_skill_carries_a_content_version():
    assert install.skill_content_version(
        paths.skill_dir() / "SKILL.md") is not None


@pytest.fixture
def project_copy(fake_home, tmp_path, monkeypatch):
    """A project-scoped skill copy installed into a fresh project cwd."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    install.run_install(["claude"], project=True)
    return proj / ".claude/skills/elate/SKILL.md"


def _set_copy_version(skill_md, version):
    text = skill_md.read_text(encoding="utf-8")
    if version is None:
        text = re.sub(r"(?m)^version:.*\n", "", text)
    else:
        text = re.sub(r"(?m)^version:.*$", f"version: {version}", text)
    skill_md.write_text(text, encoding="utf-8")


def test_staleness_equal_copy_is_quiet(project_copy):
    assert install.staleness_notice() is None


def test_staleness_older_copy_says_reinstall(project_copy):
    _set_copy_version(project_copy, "0.1.0")
    notice = install.staleness_notice()
    assert notice is not None
    assert "outdated" in notice and "elate install" in notice
    assert str(project_copy.parent) in notice


def test_staleness_newer_copy_says_upgrade(project_copy):
    _set_copy_version(project_copy, "99.0.0")
    notice = install.staleness_notice()
    assert notice is not None
    assert "expects a newer elate" in notice
    assert install.UPDATE_HINTS[install.install_channel()] in notice


def test_staleness_missing_version_sorts_as_oldest(project_copy):
    _set_copy_version(project_copy, None)
    notice = install.staleness_notice()
    assert notice is not None and "outdated" in notice


def test_staleness_both_directions_one_line_each(project_copy, fake_home):
    _set_copy_version(project_copy, "0.1.0")
    install.run_install(["claude"])  # global copy
    global_copy = fake_home / ".claude/skills/elate/SKILL.md"
    _set_copy_version(global_copy, "99.0.0")
    notice = install.staleness_notice()
    assert notice is not None
    assert len(notice.splitlines()) == 2
    assert "outdated" in notice and "expects a newer elate" in notice


def test_find_skill_copies_walks_up_and_dedupes(fake_home, tmp_path,
                                                monkeypatch):
    proj = tmp_path / "proj"
    sub = proj / "deep" / "sub"
    sub.mkdir(parents=True)
    proj_md = proj / ".claude/skills/elate/SKILL.md"
    proj_md.parent.mkdir(parents=True)
    proj_md.write_text("version: 0.14.0\n")
    global_md = fake_home / ".claude/skills/elate/SKILL.md"
    global_md.parent.mkdir(parents=True)
    global_md.write_text("version: 0.14.0\n")
    monkeypatch.chdir(sub)
    copies = install.find_skill_copies()
    assert proj_md in copies
    assert global_md in copies
    assert len(copies) == len({c.resolve() for c in copies})


def test_find_skill_copies_collapses_home_collision(fake_home, monkeypatch):
    """cwd under $HOME sees ~/.claude/skills both as a walked-up project
    dir and as the global one -- it must appear once."""
    sub = fake_home / "code" / "x"
    sub.mkdir(parents=True)
    md = fake_home / ".claude/skills/elate/SKILL.md"
    md.parent.mkdir(parents=True)
    md.write_text("version: 0.14.0\n")
    monkeypatch.chdir(sub)
    copies = install.find_skill_copies()
    assert [c.resolve() for c in copies] == [md.resolve()]


def test_classify_copy_maps_scope_and_harness(fake_home, tmp_path):
    proj = tmp_path / "proj"
    proj_md = proj / ".claude/skills/elate/SKILL.md"
    proj_md.parent.mkdir(parents=True)
    proj_md.write_text("x")
    global_md = fake_home / ".agents/skills/elate/SKILL.md"
    global_md.parent.mkdir(parents=True)
    global_md.write_text("x")
    assert install.classify_copy(proj_md) == ("claude", proj)
    assert install.classify_copy(global_md) == ("codex", None)
    assert install.classify_copy(tmp_path / "elsewhere/SKILL.md") is None


def test_project_install_walks_up_to_marker_root(fake_home, tmp_path,
                                                 monkeypatch):
    """From a repo subdirectory the skill must land at the project ROOT --
    a harness launched there never loads a subdirectory's skills dir."""
    repo = tmp_path / "repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    (repo / ".git").mkdir()
    monkeypatch.chdir(sub)
    result = install.run_install(["claude"], project=True)
    assert (repo / ".claude/skills/elate/SKILL.md").is_file()
    assert not (sub / ".claude").exists()
    assert any("project root" in n for n in result["notices"])


def test_project_install_walk_up_stops_below_home(fake_home, monkeypatch):
    """$HOME is never a project root: a markerless dir under it installs
    in place with the no-markers warning, not into $HOME (whose .claude
    dir would otherwise always match)."""
    sub = fake_home / "code" / "scratch"
    sub.mkdir(parents=True)
    (fake_home / ".claude").mkdir()
    monkeypatch.chdir(sub)
    result = install.run_install(["claude"], project=True)
    assert any("no agent/project files" in n for n in result["notices"])
    assert (sub / ".claude/skills/elate/SKILL.md").is_file()
    assert not (fake_home / ".claude/skills").exists()
