"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

# Variables an agent harness (or a developer's shell) may export that feed
# session and run defaults: owner, creator, auto-purge age, timeout scale.
# Tests opt in explicitly.
_AGENT_ENV = ("ELATE_OWNER", "ELATE_AGENT_PID", "CLAUDE_PID",
              "CLAUDE_CODE_SESSION_ID", "ELATE_PURGE_AFTER",
              "ELATE_TIMEOUT_SCALE")


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _AGENT_ENV:
        monkeypatch.delenv(var, raising=False)
    # The checkout lives under a developer's $HOME: a project config there
    # must not leak into the suite's scenarios (tests that exercise the
    # lookup unset this).
    monkeypatch.setenv("ELATE_NO_PROJECT_CONFIG", "1")
