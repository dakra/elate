"""Claude Code plugin integrity tests (Phase I3).

Pure filesystem/unit tests -- no Emacs, tmux, or `claude` CLI needed.
They pin the cross-file invariants the plugin relies on: one version
everywhere (pyproject == plugin.json == marketplace.json), one name
everywhere (plugin namespacing /elate:elate requires plugin name ==
skills/ directory name), the .mcp.json launch command the docs promise
(`uvx elate mcp`), and marketplace metadata that matches both
plugin.json and pyproject's [project.urls].

Schema note (verified against `claude plugin validate` 2.1.175): the
marketplace manifest wants top-level `name` + `owner`. The plugin source
is the docs-recommended same-repo relative form `"source": "./"` -- one
fetch, and the installed plugin content always matches the marketplace
clone that listed it (no marketplace-HEAD vs plugin-HEAD skew). The
github form `{"source": "github", "repo": "owner/repo"}` also validates;
the `{"type": "git", "url": ...}` shape from older docs is rejected.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
MCP_CONFIG = REPO / ".mcp.json"
PYPROJECT = REPO / "pyproject.toml"
AGENT = REPO / "agents" / "emacs-tester.md"
HOOKS_JSON = REPO / "hooks" / "hooks.json"
HOOK_SCRIPT = REPO / "hooks" / "check-running.sh"


def _load(path: Path) -> dict:
    """json.loads with the filename in the failure message."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        raise AssertionError(f"{path.name} is not valid JSON: {exc}") from exc
    assert isinstance(data, dict), f"{path.name} must be a JSON object"
    return data


def _pyproject() -> dict:
    """Parse pyproject.toml (tomllib on 3.11+, minimal fallback on 3.10)."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        text = PYPROJECT.read_text(encoding="utf-8")
        version = re.search(r'(?m)^version = "([^"]+)"$', text)
        assert version, "pyproject.toml: no version"
        urls_block = re.search(
            r'(?ms)^\[project\.urls\]\n(.*?)(?=^\[|\Z)', text)
        assert urls_block, "pyproject.toml: no [project.urls]"
        urls = dict(re.findall(r'(?m)^(\w+) = "([^"]+)"$',
                               urls_block.group(1)))
        return {"project": {"version": version.group(1), "urls": urls}}
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_manifests_are_valid_json():
    for path in (PLUGIN_MANIFEST, MARKETPLACE, MCP_CONFIG):
        assert path.is_file(), f"missing {path}"
        _load(path)


def test_versions_are_in_sync():
    """One version everywhere: pyproject == plugin.json == marketplace."""
    pyproject_version = _pyproject()["project"]["version"]
    plugin = _load(PLUGIN_MANIFEST)
    entry = _marketplace_entry()
    assert plugin["version"] == pyproject_version, (
        f"plugin.json version {plugin['version']!r} != pyproject "
        f"{pyproject_version!r}")
    assert entry["version"] == pyproject_version, (
        f"marketplace.json version {entry['version']!r} != pyproject "
        f"{pyproject_version!r}")
    # The runtime string must track the same version (it is derived from
    # the installed distribution metadata; a hardcoded literal regressed
    # to 0.1.0 in the 0.2.0 release).
    import elate
    assert elate.__version__ == pyproject_version, (
        f"elate.__version__ {elate.__version__!r} != pyproject "
        f"{pyproject_version!r} (run `uv sync` if pyproject was just bumped)")


def _marketplace_entry() -> dict:
    market = _load(MARKETPLACE)
    plugins = market.get("plugins")
    assert isinstance(plugins, list) and len(plugins) == 1, (
        "marketplace.json must list exactly the one elate plugin")
    return plugins[0]


def test_plugin_name_is_the_namespace():
    """The plugin name IS the skill namespace: /elate:elate depends on
    plugin.json name == marketplace entry name == skills/<dir> name."""
    plugin = _load(PLUGIN_MANIFEST)
    assert plugin["name"] == "elate"
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", plugin["name"])
    assert _marketplace_entry()["name"] == plugin["name"]
    assert _load(MARKETPLACE)["name"] == plugin["name"]
    skill_dir = REPO / "skills" / plugin["name"]
    assert (skill_dir / "SKILL.md").is_file(), (
        f"plugin name {plugin['name']!r} has no matching skills/ dir")


def test_mcp_json_launches_uvx_elate_mcp():
    """README + plugin docs promise `uvx elate mcp`; pin the exact shape."""
    config = _load(MCP_CONFIG)
    assert set(config) == {"mcpServers"}
    servers = config["mcpServers"]
    assert set(servers) == {"elate"}
    server = servers["elate"]
    assert server["type"] == "stdio"
    assert server["command"] == "uvx"
    assert server["args"] == ["elate", "mcp"]


def test_marketplace_source_is_same_repo_relative():
    """The plugin source is the same-repo relative form: the installed
    plugin content is whatever the marketplace clone contains, so it can
    never skew against the marketplace.json that listed it."""
    assert _marketplace_entry()["source"] == "./"


def test_plugin_metadata_matches_everywhere():
    """plugin.json, the marketplace entry, and pyproject must agree on
    the listing metadata (description/author/urls/license)."""
    plugin = _load(PLUGIN_MANIFEST)
    entry = _marketplace_entry()
    for field in ("description", "author", "homepage", "repository",
                  "license"):
        assert plugin[field] == entry[field], (
            f"plugin.json and marketplace.json disagree on {field!r}")
    project = _pyproject()["project"]
    urls = project["urls"]
    assert plugin["homepage"] == urls["Homepage"]
    assert plugin["repository"] == urls["Repository"]
    # License and author come from pyproject (3.10 fallback parses only
    # version+urls, so read these two fields with targeted regexes).
    text = PYPROJECT.read_text(encoding="utf-8")
    license_m = re.search(r'(?m)^license = "([^"]+)"$', text)
    assert license_m and plugin["license"] == license_m.group(1)
    author_m = re.search(
        r'(?m)^authors = \[\{ name = "([^"]+)", email = "([^"]+)" \}\]$',
        text)
    assert author_m, "pyproject authors line moved; update this test"
    author = {"name": author_m.group(1), "email": author_m.group(2)}
    assert plugin["author"] == author
    # The marketplace's top-level owner block is the same person.
    assert _load(MARKETPLACE)["owner"] == author, (
        "marketplace.json owner drifted from pyproject authors")


def test_marketplace_keywords_match_pyproject():
    """Keywords are maintained once, in pyproject; the marketplace entry
    mirrors them verbatim (order included)."""
    text = PYPROJECT.read_text(encoding="utf-8")
    kw_m = re.search(r'(?m)^keywords = (\[[^\]]*\])$', text)
    assert kw_m, "pyproject keywords line moved; update this test"
    pyproject_keywords = json.loads(kw_m.group(1))
    assert _marketplace_entry()["keywords"] == pyproject_keywords, (
        "marketplace.json keywords drifted from pyproject keywords")


def test_claude_plugin_dir_contains_only_manifests():
    """Component dirs (skills/, agents/, hooks/, commands/) live at the
    plugin ROOT; .claude-plugin/ holds only the manifests."""
    names = sorted(p.name for p in (REPO / ".claude-plugin").iterdir()
                   if not p.name.startswith("."))  # ignore .DS_Store etc.
    assert names == ["marketplace.json", "plugin.json"]


# ---------------------------------------------------------------------------
# Phase I5: emacs-tester subagent + SessionEnd hook.
#
# Format facts (verified against code.claude.com/docs 2026-06-12):
# plugin agents are flat markdown files under agents/ -- a SUBFOLDER
# becomes part of the scoped identifier (agents/a/b.md -> elate:a:b),
# so the agent must stay at agents/emacs-tester.md to register as
# elate:emacs-tester. Plugin agents ignore hooks/mcpServers/
# permissionMode frontmatter. ${CLAUDE_PLUGIN_ROOT} is substituted
# inline in agent content, hook commands, and MCP configs. SessionEnd
# cannot block; its stdout is debug-log-only and stderr surfaces only
# on a non-zero exit (which is exactly how the hook script signals).


def _agent_frontmatter() -> tuple[dict[str, object], str]:
    """Parse the agent markdown's YAML frontmatter without yaml: plain
    `key: value` scalars plus simple `- item` block lists only."""
    text = AGENT.read_text(encoding="utf-8")
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert m, "agent file must start with a --- frontmatter block"
    fields: dict[str, object] = {}
    current: str | None = None
    for line in m.group(1).splitlines():
        if line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s+(.*)$", line)
        kv = re.match(r"^([a-zA-Z_-]+):\s*(.*)$", line)
        if item and current is not None:
            value = fields[current]
            assert isinstance(value, list), f"unexpected list item: {line!r}"
            value.append(item.group(1).strip())
        elif kv:
            current = kv.group(1)
            scalar = kv.group(2).strip()
            fields[current] = [] if not scalar else scalar
        elif line.startswith((" ", "\t")) and current:
            value = fields[current]
            assert isinstance(value, str), f"unparseable line: {line!r}"
            fields[current] = value + " " + line.strip()
        else:
            raise AssertionError(f"unparseable frontmatter line: {line!r}")
    return fields, m.group(2)


def test_agents_dir_holds_only_the_flat_agent_file():
    names = sorted(p.name for p in (REPO / "agents").iterdir()
                   if not p.name.startswith("."))
    assert names == ["emacs-tester.md"], (
        "plugin agents must be flat files; a subfolder changes the "
        "scoped identifier")


def test_agent_frontmatter():
    fields, body = _agent_frontmatter()
    assert fields["name"] == AGENT.stem == "emacs-tester"
    desc = fields["description"]
    assert isinstance(desc, str) and "test" in desc.lower()
    # Delegation triggers the description promises must stay present.
    assert "interactively" in desc.lower()
    assert "findings" in desc.lower()
    # Only fields that plugin agents actually support (hooks/mcpServers/
    # permissionMode are silently IGNORED for plugin agents -- relying
    # on one of those must fail here, not in production).
    supported = {"name", "description", "model", "effort", "maxTurns",
                 "tools", "disallowedTools", "skills", "memory",
                 "background", "isolation", "color"}
    assert set(fields) <= supported, set(fields) - supported
    tools = fields["tools"]
    assert isinstance(tools, str)
    # Write/Edit are for scenario scripts and sandbox fixtures, not the
    # user's files (the body's don't-modify rule is contractual, not
    # enforced -- Bash grants FS writes anyway); Edit so an
    # export-script output can be tweaked without a whole-file rewrite.
    assert [t.strip() for t in tools.split(",")] == [
        "Bash", "Read", "Write", "Edit", "Glob", "Grep"]
    assert body.strip(), "agent body (system prompt) must not be empty"


def test_agent_preloads_the_plugin_skill():
    """The skills field uses the plugin-scoped name (plugin:skill); it
    fails SILENTLY (debug-log warning) when the name is wrong, so pin
    it to the actual plugin name + skills/ directory."""
    fields, _ = _agent_frontmatter()
    plugin = _load(PLUGIN_MANIFEST)
    skill_dir = REPO / "skills" / "elate"
    assert (skill_dir / "SKILL.md").is_file()
    assert fields["skills"] == [f"{plugin['name']}:{skill_dir.name}"]


def test_agent_references_existing_skill_files():
    """Every ${CLAUDE_PLUGIN_ROOT}-relative path in the agent body must
    exist in the repo (the plugin payload is the repo clone)."""
    _, body = _agent_frontmatter()
    refs = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", body)
    assert refs, "agent body should point at the skill's deep references"
    for ref in refs:
        assert (REPO / ref).is_file(), f"agent references missing {ref}"
    for name in ("REFERENCE.md", "RECIPES.md", "SCRIPTING.md"):
        assert any(r.endswith(name) for r in refs), (
            f"agent no longer points at skills/elate/{name}")


def test_hooks_json_shape():
    """One script, two events: SessionEnd warns (stderr + exit 1),
    SessionStart injects context (stdout) -- the channel that is
    actually user-visible today (SessionEnd output empirically reaches
    only the debug log in 2.1.175)."""
    config = _load(HOOKS_JSON)
    assert set(config) == {"hooks"}
    events = config["hooks"]
    assert set(events) == {"SessionStart", "SessionEnd"}
    for event, arg in (("SessionStart", "start"), ("SessionEnd", "end")):
        (group,) = events[event]
        assert "matcher" not in group  # fire on every reason/source
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        # Exec form (args present): ${CLAUDE_PLUGIN_ROOT} is substituted
        # as a plain string with no shell quoting hazards.
        assert hook["command"] == (
            "${CLAUDE_PLUGIN_ROOT}/hooks/check-running.sh")
        assert hook["args"] == [arg]
        # These hooks delay session start/exit by up to their timeout;
        # keep it tight.
        assert 0 < hook["timeout"] <= 30


def test_hook_script_is_executable_posix_sh():
    assert HOOK_SCRIPT.is_file()
    assert os.access(HOOK_SCRIPT, os.X_OK), (
        "hooks/check-running.sh must be executable (exec-form hooks spawn "
        "it directly)")
    first = HOOK_SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh", "the hook must stay plain POSIX sh"
