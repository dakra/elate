"""Agent Skill integrity tests (Phase I2).

Pure filesystem/unit tests -- no Emacs or tmux needed. They pin the
contract the skill format imposes (frontmatter shape, name/description
constraints, body budget), that every supporting file the skill points at
exists, and that the generated REFERENCE.md is current (the same
regenerate-and-compare check CI runs, so drift fails locally first).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "skills" / "elate"
SKILL = SKILL_DIR / "SKILL.md"
GENERATOR = REPO / "scripts" / "gen-skill-ref.py"


def _frontmatter() -> tuple[dict[str, str], str]:
    """Parse SKILL.md frontmatter without a YAML dependency.

    Only the subset the skill format recognizes is supported: top-level
    `key: value` pairs whose values may continue on indented lines
    (YAML plain multi-line scalars), plus `#` comment lines. Returns
    (frontmatter, body).
    """
    text = SKILL.read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert m, "SKILL.md must start with a --- frontmatter block"
    fields: dict[str, str] = {}
    current: str | None = None
    for line in m.group(1).splitlines():
        if line.lstrip().startswith("#"):
            continue  # YAML comment
        kv = re.match(r"^([a-zA-Z_-]+):\s*(.*)$", line)
        if kv:
            current = kv.group(1)
            fields[current] = kv.group(2).strip()
        elif line.startswith((" ", "\t")) and current:
            fields[current] += (" " if fields[current] else "") + line.strip()
        else:
            pytest.fail(f"unparseable frontmatter line: {line!r}")
    return fields, m.group(2)


def test_skill_frontmatter_has_only_known_keys():
    fields, _ = _frontmatter()
    # name/description are what the skill format recognizes; version is
    # elate's skill CONTENT version (read by `elate start`'s staleness
    # check, harnesses ignore it). Anything else is silently ignored at
    # best and a validation error at worst.
    assert set(fields) == {"name", "description", "version"}


def test_skill_name_constraints():
    fields, _ = _frontmatter()
    name = fields["name"]
    assert len(name) <= 64
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), (
        f"name must be lowercase letters/digits/hyphens, got {name!r}")
    for forbidden in ("claude", "anthropic"):
        assert forbidden not in name.lower()


def test_skill_description_constraints():
    fields, _ = _frontmatter()
    desc = fields["description"]
    assert desc, "description must not be empty"
    assert len(desc) <= 1024, f"description is {len(desc)} chars (max 1024)"
    # Third person, states WHAT + WHEN: the trigger clause is the load-
    # bearing part -- pin that it exists.
    assert "Use when" in desc, "description must state trigger conditions"
    # First-person/imperative openers ("I spawn", "Spawn ...") read as
    # instructions to the model, not capability descriptions.
    assert not desc.lower().startswith(("i ", "you ", "use this")), (
        "description should be third person")


def _skill_content_hash() -> str:
    """sha256 over the skill's files: sorted names + bytes."""
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(SKILL_DIR.iterdir()):
        if path.name.startswith("."):
            continue
        assert path.is_file(), (
            f"skill dir grew a non-file entry ({path.name}); extend the "
            "content hash to cover it")
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_skill_content_version_tracks_content():
    """The frontmatter `version:` must bump whenever skill content changes.

    tests/skill_content_version.txt records `<version> <sha256>` for the
    current skill files; editing any of them without bumping the version
    (and refreshing the record) fails here instead of silently shipping a
    stale staleness stamp.
    """
    from elate.install import skill_content_version

    record = (REPO / "tests" / "skill_content_version.txt").read_text(
        encoding="utf-8").split()
    assert len(record) == 2, "record must be `<version> <sha256>`"
    recorded_version, recorded_hash = record
    fields, _ = _frontmatter()
    assert fields["version"] == recorded_version, (
        f"SKILL.md frontmatter version {fields['version']!r} != recorded "
        f"{recorded_version!r} in tests/skill_content_version.txt")
    assert skill_content_version(SKILL) is not None, (
        "SKILL.md `version:` must parse as a dotted int version")
    actual = _skill_content_hash()
    assert actual == recorded_hash, (
        "skill content changed: bump `version:` in SKILL.md frontmatter to "
        "the next release and update tests/skill_content_version.txt "
        f"(new hash: {actual})")


def test_skill_body_under_500_lines():
    _, body = _frontmatter()
    lines = body.count("\n") + 1
    assert lines < 500, f"SKILL.md body is {lines} lines (must stay < 500)"


def test_skill_supporting_files_exist():
    _, body = _frontmatter()
    referenced = set(re.findall(r"\b([A-Z][A-Z_-]+\.md)\b", body))
    assert {"REFERENCE.md", "RECIPES.md", "SCRIPTING.md"} <= referenced
    for name in referenced:
        if name == "SKILL.md":
            continue
        assert (SKILL_DIR / name).is_file(), (
            f"SKILL.md references {name}, which does not exist next to it")
    # Progressive disclosure requires one-level-deep relative links: no
    # absolute paths, no parent traversal in markdown links.
    for target in re.findall(r"\]\(([^)#]+)\)", body):
        if "://" in target:
            continue
        assert not target.startswith("/"), f"absolute link: {target}"
        assert ".." not in Path(target).parts, f"uplevel link: {target}"
        assert (SKILL_DIR / target).exists(), f"broken link: {target}"


def test_reference_md_is_current():
    """Same check as CI: regenerating REFERENCE.md must be a no-op."""
    generated = subprocess.run(
        [sys.executable, str(GENERATOR), "--stdout"],
        capture_output=True, text=True, check=True, cwd=str(REPO),
    ).stdout
    on_disk = (SKILL_DIR / "REFERENCE.md").read_text(encoding="utf-8")
    assert generated == on_disk, (
        "skills/elate/REFERENCE.md is stale -- regenerate with: "
        "uv run python scripts/gen-skill-ref.py")


def test_reference_md_covers_every_subcommand():
    from elate.cli import build_parser

    text = (SKILL_DIR / "REFERENCE.md").read_text(encoding="utf-8")
    parser = build_parser()
    sub = next(a for a in parser._actions
               if a.__class__.__name__ == "_SubParsersAction")
    for command in sub.choices:
        assert f"## elate {command}\n" in text, (
            f"REFERENCE.md lacks a section for `elate {command}`")


def test_skill_name_matches_directory():
    # I3's plugin namespacing (/elate:elate) relies on the skill name and
    # its directory name agreeing; a rename of either half must fail here.
    fields, _ = _frontmatter()
    assert fields["name"] == SKILL_DIR.name


def test_skill_numeric_claims_match_the_code():
    """Factual numbers in SKILL.md must track the code, not memory."""
    from elate.session import GUI_TYPE_LIMIT

    _, body = _frontmatter()
    # MCP tool count ("The N `elate_*` tools ..."): count the actual tool
    # definitions from source text -- importing mcp_server would build the
    # whole FastMCP server just to count functions.
    source = (REPO / "src" / "elate" / "mcp_server.py").read_text(
        encoding="utf-8")
    tool_count = len(re.findall(r"^def elate_", source, re.MULTILINE))
    claim = re.search(r"The (~?\d+) `elate_\*` tools", body)
    assert claim, "SKILL.md no longer states the MCP tool count"
    assert claim.group(1) == str(tool_count), (
        f"SKILL.md claims {claim.group(1)} MCP tools, source defines "
        f"{tool_count}")
    # GUI type cap.
    assert f"{GUI_TYPE_LIMIT:,}" in body, (
        f"SKILL.md's GUI type cap is out of date (code: {GUI_TYPE_LIMIT})")


def test_skill_names_every_cli_only_verb():
    """The "(X, Y, ... stay CLI-only)" sentence must name exactly the CLI
    verbs without an MCP counterpart -- the tool-count test above would
    not notice one verb (e.g. `purge`) silently dropping out of the list.
    """
    import argparse

    from elate.cli import build_parser

    sub = next(a for a in build_parser()._actions
               if isinstance(a, argparse._SubParsersAction))
    source = (REPO / "src" / "elate" / "mcp_server.py").read_text(
        encoding="utf-8")
    mcp = set(re.findall(r"^def elate_(\w+)", source, re.MULTILINE))
    # CLI->MCP name mapping: dashes become underscores; `run` is served
    # by elate_run_script; `mcp` itself is the server, not a feature.
    cli_only = {v for v in sub.choices
                if v != "mcp"
                and v.replace("-", "_") not in mcp
                and not (v == "run" and "run_script" in mcp)}
    _, body = _frontmatter()
    sentence = re.search(
        r"tools cover the core surface \(([^)]*)\)", body)
    assert sentence, "SKILL.md no longer lists the CLI-only verbs"
    named = set(re.findall(r"`([\w-]+)`", sentence.group(1)))
    assert named == cli_only, (
        f"SKILL.md's CLI-only list {sorted(named)} != actual "
        f"{sorted(cli_only)}")


def test_scripting_md_matches_script_validation():
    """SCRIPTING.md's tables are hand-written: pin them to script.py.

    Every step verb, per-verb option key, assertion kind, and session key
    that validation accepts must literally appear in SCRIPTING.md, and so
    must the load-bearing numeric bounds -- the same drift class
    REFERENCE.md is generated away from.
    """
    from elate import script as SC

    text = (SKILL_DIR / "SCRIPTING.md").read_text(encoding="utf-8")
    for verb in SC.VERBS:
        assert f"`{verb}`" in text, f"step verb {verb!r} undocumented"
        for option in SC._STEP_OPTIONS[verb]:
            assert f"`{option}`" in text, (
                f"step option {option!r} ({verb}) undocumented")
    for kind, extras in SC._ASSERT_KINDS.items():
        assert f"`{kind}`" in text, f"assert kind {kind!r} undocumented"
        for option in extras:
            assert f"`{option}`" in text, (
                f"assert option {option!r} ({kind}) undocumented")
    for key in SC._SESSION_KEYS:
        assert f"`{key}`" in text, f"session key {key!r} undocumented"
    # Numeric bounds and defaults the doc states explicitly.
    assert f"(0, {SC.MAX_STEP_TIMEOUT:g}]" in text
    for default in set(SC._DEFAULT_TIMEOUTS.values()):
        assert f"({default:g})" in text, (
            f"default timeout {default:g} not documented")
    assert "10x4" in text          # minimum session/resize size
    assert "120x36" in text        # default session size
    assert re.search(r"1[-–]3", text), "mouse button bounds missing"
    assert re.search(r"1[-–]50", text), "mouse count bounds missing"
    assert re.search(r"0[-–]60", text), "min_idle bounds missing"
