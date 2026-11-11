"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

# Variables an agent harness (or a developer's shell) may export that feed
# session defaults: owner, creator, auto-purge age. Tests opt in explicitly.
_AGENT_ENV = ("ELATE_OWNER", "ELATE_AGENT_PID", "CLAUDE_PID",
              "CLAUDE_CODE_SESSION_ID", "ELATE_PURGE_AFTER")


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _AGENT_ENV:
        monkeypatch.delenv(var, raising=False)
