"""Shared GUI-availability probe for the test suite (not a test module).

GUI Emacs needs a working window server. Probed exactly once per pytest
run (module import), cheaply and conservatively: only conditions that
make GUI frames impossible cause a skip -- when the probe cannot tell,
the tests run, because a real failure is more honest than a wrong skip.

First-CI-run context: the original macOS GUI failures (run 27443364384)
were NOT a missing window server -- GitHub's macOS runners do have one
and the cask Emacs opened frames fine; the failures were the exec-chain
identity bug fixed in gui.pid_alive/session._boot_gui. This probe exists
for the environments where GUI Emacs genuinely cannot start (an SSH
login on macOS, a Linux box without $DISPLAY), turning a guaranteed
startup failure into an honest skip.
"""

from __future__ import annotations

import os
import sys


def gui_unavailable_reason() -> str | None:
    """Why GUI Emacs cannot start here, or None when it should work."""
    if sys.platform == "darwin":
        try:
            import Quartz  # pyobjc; an elate dependency on macOS
        except ImportError:
            return None  # cannot tell; let the tests speak
        # No window-server session (SSH login, GUI-less environment):
        # an NS Emacs cannot connect to the display and dies at startup.
        if Quartz.CGSessionCopyCurrentDictionary() is None:
            return ("no macOS window-server session (GUI login required); "
                    "GUI Emacs cannot open frames here")
        return None
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY"):
            return "no $DISPLAY on Linux"
        return None
    return f"GUI sessions are unsupported on {sys.platform}"


GUI_UNAVAILABLE_REASON = gui_unavailable_reason()
