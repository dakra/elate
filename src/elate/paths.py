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
