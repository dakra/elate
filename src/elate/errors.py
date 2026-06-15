"""Exception types for elate."""

from __future__ import annotations

from typing import Any


class ElateError(Exception):
    """Base class for all elate errors."""


class SessionNotFound(ElateError):
    pass


class SessionExists(ElateError):
    pass


class SessionDead(ElateError):
    pass


class TransportError(ElateError):
    """emacsclient / tmux invocation failed at the transport level."""


class EvalTimeout(ElateError):
    """The emacsclient subprocess hit its hard timeout (Emacs busy/blocked)."""


class RpcError(ElateError):
    """The agent reported an elisp-level error."""

    def __init__(self, message: str, backtrace: str | None = None) -> None:
        super().__init__(message)
        self.backtrace = backtrace


class WaitTimeout(ElateError):
    """A wait command timed out. Carries a state dump for diagnosis."""

    def __init__(self, message: str, state: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.state = state or {}


class UsageError(ElateError):
    """A usage mistake (wrong context or arguments). Maps to CLI exit 2."""
