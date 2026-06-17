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
    """The emacsclient subprocess hit its hard timeout (Emacs busy/blocked).

    Carries an optional ``sample`` -- a best-effort thread backtrace of the
    wedged Emacs captured by ``eval --on-timeout sample`` (see
    :mod:`elate.diagnostics`); ``None`` unless that was requested.
    """

    def __init__(self, message: str,
                 sample: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.sample = sample


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


class ScreenshotError(ElateError):
    """A GUI capture failed. Carries a machine-readable reason code.

    `reason` is one of "permission", "locked", "display_asleep", or
    "window_gone", so a caller can tell a missing Screen Recording grant
    apart from a locked/asleep Mac (which look identical to screencapture)
    without parsing the message.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class UsageError(ElateError):
    """A usage mistake (wrong context or arguments). Maps to CLI exit 2."""
