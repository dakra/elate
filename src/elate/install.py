"""`elate install`: wire elate into AI coding harnesses.

elate is CLI-centric, and the integration that works across every harness we
target is the **Agent Skill** (a ``SKILL.md`` directory): it teaches a harness
to drive the ``elate`` CLI, and Claude Code, Codex CLI, opencode, pi, and
Antigravity all read the same SKILL.md format. So "install" means copying the
bundled skill into each harness's skills directory.

MCP is an optional add-on (``--mcp``) and uneven across harnesses (pi has no
MCP at all), so it is wired only where it is safe: by shelling out to the
harness's own ``mcp add`` CLI where one exists (Claude Code, Codex), and by
printing a ready-to-paste config snippet where it does not (opencode,
Antigravity). Editing those JSON/JSONC/TOML files in place is deliberately
left to the user to avoid clobbering existing servers and comments.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .errors import UsageError

MCP_COMMAND = ["uvx", "elate", "mcp"]

_OPENCODE_SNIPPET = """\
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "elate": { "type": "local", "command": ["uvx", "elate", "mcp"] }
  }
}"""

_ANTIGRAVITY_SNIPPET = """\
{
  "mcpServers": {
    "elate": { "command": "uvx", "args": ["elate", "mcp"] }
  }
}"""


@dataclass(frozen=True)
class McpPlan:
    """How to wire the MCP server for one harness under ``--mcp``."""

    kind: str  # "cli" | "manual" | "none"
    cli_bin: str | None = None
    # Whether the harness's `mcp add` takes a --scope flag. Claude Code
    # defaults to cwd-local scope, so a global skill install must pass
    # --scope user (and --project must pass --scope project) to match.
    cli_supports_scope: bool = False
    config_path: Callable[[], Path] | None = None
    snippet: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Harness:
    key: str
    label: str
    # Skill destination: global lives under $HOME/XDG; project is relative to cwd.
    skill_root_global: Callable[[], Path]
    skill_root_project: str
    detect: Callable[[], bool]
    mcp: McpPlan


def _home() -> Path:
    return Path.home()


def _xdg_config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base).expanduser() if base else _home() / ".config"


def _codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser() if override else _home() / ".codex"


def _exists_or_on_path(rel_dir: str, exe: str) -> Callable[[], bool]:
    def detect() -> bool:
        return (_home() / rel_dir).is_dir() or shutil.which(exe) is not None

    return detect


# Skill destinations and MCP plans are grounded in each harness's docs:
#   - Codex/opencode/pi all also read the shared ~/.agents/skills dir, but each
#     gets its own canonical location here so a single-harness install is
#     predictable.
#   - Codex *config* is ~/.codex but its *skills* live at ~/.agents/skills.
#   - pi has no MCP; Antigravity's MCP path is the best-attested one.
HARNESSES: dict[str, Harness] = {
    "claude": Harness(
        key="claude",
        label="Claude Code",
        skill_root_global=lambda: _home() / ".claude" / "skills",
        skill_root_project=".claude/skills",
        detect=_exists_or_on_path(".claude", "claude"),
        mcp=McpPlan(kind="cli", cli_bin="claude", cli_supports_scope=True),
    ),
    "codex": Harness(
        key="codex",
        label="Codex CLI",
        skill_root_global=lambda: _home() / ".agents" / "skills",
        skill_root_project=".agents/skills",
        # Codex skills live at ~/.agents/skills, but its config/home is
        # ~/.codex (relocatable via CODEX_HOME) -- use that to detect it.
        detect=lambda: _codex_home().is_dir() or shutil.which("codex") is not None,
        # `codex mcp add` writes ~/.codex/config.toml (always user-global);
        # it has no project scope, so no --scope flag.
        mcp=McpPlan(kind="cli", cli_bin="codex"),
    ),
    "opencode": Harness(
        key="opencode",
        label="opencode",
        skill_root_global=lambda: _xdg_config_home() / "opencode" / "skills",
        skill_root_project=".opencode/skills",
        detect=lambda: (_xdg_config_home() / "opencode").is_dir()
        or shutil.which("opencode") is not None,
        mcp=McpPlan(
            kind="manual",
            config_path=lambda: _xdg_config_home() / "opencode" / "opencode.json",
            snippet=_OPENCODE_SNIPPET,
            note="opencode has no `mcp add` CLI; merge this into the `mcp` "
            "object (preserve any existing servers and JSONC comments).",
        ),
    ),
    "pi": Harness(
        key="pi",
        label="pi (earendil-works)",
        skill_root_global=lambda: _home() / ".pi" / "agent" / "skills",
        skill_root_project=".pi/skills",
        detect=_exists_or_on_path(".pi", "pi"),
        mcp=McpPlan(
            kind="none",
            note="pi has no MCP support; the skill is the integration.",
        ),
    ),
    "antigravity": Harness(
        key="antigravity",
        label="Google Antigravity",
        skill_root_global=lambda: _home() / ".gemini" / "skills",
        skill_root_project=".agents/skills",
        # ~/.gemini is shared with the Gemini CLI; key on the Antigravity-
        # specific subdir (or its binary) so we don't auto-target Gemini-only
        # users.
        detect=lambda: (_home() / ".gemini" / "antigravity").is_dir()
        or shutil.which("antigravity") is not None,
        mcp=McpPlan(
            kind="manual",
            config_path=lambda: _home() / ".gemini" / "config" / "mcp_config.json",
            snippet=_ANTIGRAVITY_SNIPPET,
            note="Antigravity has no `mcp add` CLI; merge into `mcpServers`. "
            "If this path is absent, check ~/.gemini/antigravity/mcp_config.json "
            "or add it via the IDE (Settings -> Add MCP).",
        ),
    ),
}

HARNESS_KEYS = list(HARNESSES)


def _resolve_targets(selected: list[str]) -> list[Harness]:
    if selected and "all" in selected:
        return list(HARNESSES.values())
    if selected:
        targets = []
        for key in dict.fromkeys(selected):  # dedup, preserve order
            harness = HARNESSES.get(key)
            if harness is None:
                raise UsageError(
                    f"unknown harness {key!r}; choose from "
                    f"{', '.join(HARNESS_KEYS)} (or 'all')")
            targets.append(harness)
        return targets
    detected = [h for h in HARNESSES.values() if h.detect()]
    if not detected:
        raise UsageError(
            "no supported harness detected here; name one explicitly: "
            f"{' '.join(HARNESS_KEYS)} (or 'all')")
    return detected


def _mcp_cli_cmd(plan: McpPlan, project: bool) -> list[str]:
    """The `<bin> mcp add ...` argv for a cli-kind harness."""
    cmd = [plan.cli_bin, "mcp", "add"]  # type: ignore[list-item]
    if plan.cli_supports_scope:
        cmd += ["--scope", "project" if project else "user"]
    cmd += ["elate", "--", *MCP_COMMAND]
    return cmd


def _wire_mcp(harness: Harness, project: bool, dry_run: bool) -> dict:
    plan = harness.mcp
    if plan.kind == "none":
        return {"status": "unsupported", "note": plan.note}
    if plan.kind == "manual":
        return {
            "status": "manual",
            "config_path": str(plan.config_path()),  # type: ignore[misc]
            "snippet": plan.snippet,
            "note": plan.note,
        }
    # kind == "cli": shell out to the harness's own `mcp add`.
    bin_name = plan.cli_bin
    assert bin_name is not None
    cmd = _mcp_cli_cmd(plan, project)
    pretty = " ".join(cmd)
    exe = shutil.which(bin_name)
    if exe is None:
        return {
            "status": "skipped",
            "note": f"{bin_name} not on PATH; run manually: {pretty}",
        }
    if dry_run:
        return {"status": "planned", "command": pretty}
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"status": "error", "command": pretty,
                "note": f"{bin_name} mcp add timed out"}
    if proc.returncode == 0:
        return {"status": "configured", "command": pretty}
    detail = (proc.stderr or proc.stdout or "").strip()
    # `mcp add` is not idempotent: re-running (the documented refresh) hits
    # "already exists" and exits non-zero. That is success, not failure.
    if "already exists" in detail.lower():
        return {"status": "exists", "command": pretty,
                "note": "already configured"}
    return {
        "status": "error",
        "command": pretty,
        "note": detail or f"{bin_name} mcp add exited {proc.returncode}",
    }


def run_install(
    selected: list[str],
    *,
    project: bool = False,
    with_mcp: bool = False,
    dry_run: bool = False,
) -> dict:
    """Copy the bundled skill into each target harness (and optionally MCP)."""
    targets = _resolve_targets(selected)
    src = paths.skill_dir()
    entries: list[dict] = []
    for harness in targets:
        if project:
            root = Path.cwd() / harness.skill_root_project
        else:
            root = harness.skill_root_global()
        dest = root / "elate"
        replaced_symlink = dest.is_symlink()
        if not dry_run:
            root.mkdir(parents=True, exist_ok=True)
            # Refresh cleanly: remove any prior install first so stale files
            # don't linger. Crucially, unlink a *symlink* rather than letting
            # copytree write through it -- a user who symlinked the skill (as
            # the README suggests) would otherwise have its target (possibly
            # this very checkout) clobbered.
            if dest.is_symlink():
                dest.unlink()
            elif dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        entry: dict = {
            "harness": harness.key,
            "label": harness.label,
            "skill_dir": str(dest),
            "action": "would install" if dry_run else "installed",
        }
        if replaced_symlink:
            entry["replaced_symlink"] = True
        if with_mcp:
            entry["mcp"] = _wire_mcp(harness, project, dry_run)
        entries.append(entry)
    return {
        "installed": entries,
        "skill_source": str(src),
        "scope": "project" if project else "global",
        "dry_run": dry_run,
        "with_mcp": with_mcp,
    }


def format_summary(result: dict) -> str:
    """Human-readable summary of a run_install() result."""
    lines: list[str] = []
    verb = "Would install" if result["dry_run"] else "Installed"
    scope = result["scope"]
    lines.append(f"{verb} the elate skill ({scope}) into:")
    for i, entry in enumerate(result["installed"]):
        if i and result["with_mcp"]:
            lines.append("")  # breathing room when each entry has MCP detail
        suffix = " (replaced existing symlink)" if entry.get("replaced_symlink") else ""
        lines.append(f"  • {entry['label']}: {entry['skill_dir']}{suffix}")
        mcp = entry.get("mcp")
        if not mcp:
            continue
        status = mcp["status"]
        if status in ("configured", "planned"):
            lines.append(f"      MCP: {status} ({mcp['command']})")
        elif status == "exists":
            lines.append(f"      MCP: already configured ({mcp['command']})")
        elif status == "manual":
            lines.append(f"      MCP: add to {mcp['config_path']} —")
            lines.append("        " + mcp["snippet"].replace("\n", "\n        "))
            if mcp.get("note"):
                lines.append(f"      ({mcp['note']})")
        elif status == "unsupported":
            lines.append(f"      MCP: {mcp['note']}")
        elif status == "skipped":
            lines.append(f"      MCP: skipped — {mcp['note']}")
        elif status == "error":
            lines.append(f"      MCP: failed — {mcp['note']}")
    if not result["dry_run"]:
        lines.append("")
        lines.append(
            "The skill auto-loads when the agent needs Emacs work; just ask it "
            "to use elate. Re-run anytime to refresh after an elate upgrade.")
    return "\n".join(lines)
