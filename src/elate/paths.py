"""Filesystem locations for elate state."""

from __future__ import annotations

import os
from pathlib import Path


def sessions_root() -> Path:
    """Directory under which per-session sandboxes live.

    Override with $ELATE_HOME (used by the test suite); otherwise
    $XDG_CACHE_HOME/elate/sessions or ~/.cache/elate/sessions.
    """
    override = os.environ.get("ELATE_HOME")
    if override:
        return Path(override).expanduser() / "sessions"
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache).expanduser() if cache else Path.home() / ".cache"
    return base / "elate" / "sessions"


def agent_el_path() -> Path:
    """Path to the bundled elate-agent.el."""
    return Path(__file__).resolve().parent / "elisp" / "elate-agent.el"


def _bundled_or_repo(rel: str) -> Path:
    """Resolve a packaged data path, tolerating editable/dev checkouts.

    A built wheel carries the file under ``elate/_bundled/<rel>`` (see the
    force-include in pyproject.toml). An editable install / `uv run` from a
    checkout does not, so fall back to the canonical copy at the repo root,
    which sits two levels above this package (``src/elate`` -> repo).
    """
    bundled = Path(__file__).resolve().parent / "_bundled" / rel
    if bundled.exists():
        return bundled
    repo_root = Path(__file__).resolve().parents[2]
    dev = repo_root / rel
    if dev.exists():
        return dev
    raise FileNotFoundError(
        f"bundled resource {rel!r} not found (looked in {bundled} and {dev}); "
        "the install may be incomplete")


def skill_dir() -> Path:
    """Directory of the bundled elate Agent Skill (SKILL.md + companions)."""
    return _bundled_or_repo("skills/elate")
