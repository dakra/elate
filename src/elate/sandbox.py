"""Per-session sandbox builder.

Each session directory contains:

    home/    fake $HOME (+ XDG dirs) so nothing touches the real config
    init/    --init-directory target with a generated init.el
    server/  server.el socket dir (mode 0700)
    log/     JSONL transcript

Config modes:
    bare           -Q plus the agent only
    minimal        sensible deterministic test defaults + package under test
    init-file      user-supplied init file loaded inside the sandbox
    clean-install  minimal defaults + the package under test installed FOR
                   REAL via package-install-file into a sandbox-local
                   package-user-dir (autoloads, Package-Requires, and
                   byte-compilation exercised like a user install)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from .errors import ElateError
from .paths import agent_el_path
from .semantic import elisp_string

CONFIG_MODES = ("minimal", "bare", "init-file", "clean-install")

_MINIMAL_DEFAULTS = """\
;; Deterministic, test-friendly defaults.
(setq inhibit-startup-screen t
      inhibit-startup-echo-area-message (user-login-name)
      initial-scratch-message nil
      debug-on-error t
      make-backup-files nil
      auto-save-default nil
      create-lockfiles nil
      use-dialog-box nil
      use-short-answers t
      ring-bell-function #'ignore
      confirm-kill-emacs nil
      confirm-kill-processes nil
      enable-recursive-minibuffers t
      load-prefer-newer t
      ;; Emacs silently turns a mouse-2 click arriving within 0.35s of a
      ;; wheel scroll into `ignore' (anti-accidental-paste).  Time-based
      ;; input dropping makes automated runs flaky: disable it.
      mouse-wheel-inhibit-click-time nil)
(menu-bar-mode -1)
(blink-cursor-mode -1)
(when (display-graphic-p)
  (setq use-file-dialog nil)
  (when (fboundp 'tool-bar-mode) (tool-bar-mode -1))
  (when (fboundp 'scroll-bar-mode) (scroll-bar-mode -1)))
"""


def _frame_geometry_forms(cols: int, rows: int) -> list[str]:
    """Elisp pinning the initial GUI frame to COLS x ROWS characters."""
    geometry = f"'((width . {cols}) (height . {rows}))"
    return [
        f"(setq initial-frame-alist (append {geometry} initial-frame-alist))",
        f"(setq default-frame-alist (append {geometry} default-frame-alist))",
    ]


def create_dirs(session_dir: Path) -> dict[str, Path]:
    """Create the sandbox directory tree; return the named subdirs."""
    dirs = {
        "home": session_dir / "home",
        "init": session_dir / "init",
        "server": session_dir / "server",
        "log": session_dir / "log",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    dirs["server"].chmod(0o700)
    # XDG dirs inside the fake home.
    for sub in (".config", ".local/share", ".local/state", ".cache"):
        (dirs["home"] / sub).mkdir(parents=True, exist_ok=True)
    return dirs


def _load_forms(loads: Sequence[str]) -> list[str]:
    """Elisp forms that put the package under test on `load-path` and load it."""
    forms: list[str] = []
    for entry in loads:
        path = Path(entry).expanduser().resolve()
        if not path.exists():
            raise ElateError(f"--load path does not exist: {entry}")
        if path.is_dir():
            forms.append(f"(add-to-list 'load-path {elisp_string(str(path))})")
        else:
            forms.append(f"(add-to-list 'load-path {elisp_string(str(path.parent))})")
            forms.append(f"(load {elisp_string(str(path))} nil t)")
    return forms


# clean-install: install for real instead of load-path injection.  The
# package archives are emptied so nothing can touch the network -- a
# dependency that is not built in fails the install with a clear,
# structured init-error (elate-clean-install pre-checks Package-Requires
# and names every missing dependency).
_CLEAN_INSTALL_PRELUDE = """\
;; clean-install: a real package install into a sandbox-local elpa/.
(require 'package)
(setq package-user-dir (expand-file-name "elpa" elate-session-dir)
      package-archives nil
      ;; package.el saves `package-selected-packages' via Custom after
      ;; init; without an explicit custom-file that save would rewrite
      ;; this generated init.el.
      custom-file (expand-file-name "custom.el" elate-session-dir))
(package-initialize)"""


def _install_forms(loads: Sequence[str]) -> list[str]:
    """Elisp forms that package-install the package(s) under test."""
    forms: list[str] = [_CLEAN_INSTALL_PRELUDE]
    seen: set[str] = set()
    for entry in loads:
        path = Path(entry).expanduser().resolve()
        if not path.exists():
            raise ElateError(f"--load path does not exist: {entry}")
        # Dedup on the real path (matrix-style): the same package via
        # different spellings must not install twice and show up twice
        # in info's "installed".
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        forms.append(f"(elate-clean-install {elisp_string(str(path))})")
    return forms


def build_sandbox(
    session_dir: Path,
    *,
    config: str = "minimal",
    init_file: str | None = None,
    loads: Sequence[str] = (),
    evals: Sequence[str] = (),
    ui: str = "tty",
    cols: int = 120,
    rows: int = 36,
) -> list[str]:
    """Build the sandbox and return the Emacs argument vector (sans binary).

    TTY sessions get -nw; GUI sessions get a windowed Emacs whose initial
    frame is pinned to COLS x ROWS characters. The agent is loaded and
    initialized in every mode.
    """
    if config not in CONFIG_MODES:
        raise ElateError(f"unknown config mode: {config}")
    if config == "init-file" and not init_file:
        raise ElateError("config mode 'init-file' requires --init-file PATH")
    if config == "clean-install" and not loads:
        raise ElateError(
            "config mode 'clean-install' needs --load PATH(s) pointing at "
            "the package to install (an .el file, a package tar, or a "
            "package directory)")
    if init_file:
        if config not in ("minimal", "init-file"):
            # Mirrors start_session: never silently downgrade an
            # explicitly conflicting mode (bare / clean-install).
            raise ElateError(
                f"an init file conflicts with config {config!r}: an init "
                "file implies config 'init-file'; drop one of the two")
        config = "init-file"
        init_path = Path(init_file).expanduser().resolve()
        if not init_path.is_file():
            raise ElateError(f"--init-file does not exist: {init_file}")

    dirs = create_dirs(session_dir)
    agent = agent_el_path()
    set_session_dir = f"(setq elate-session-dir {elisp_string(str(session_dir))})"

    ui_args = ["-nw"] if ui == "tty" else []

    if config == "bare":
        # User forms run inside elate-guard so a failing form is recorded
        # in <session>/init-error (surfaced by `elate start`) instead of
        # silently dropping the session into the debugger.
        args = [*ui_args, "-Q", "--eval", set_session_dir,
                "-l", str(agent), "--eval", "(elate-agent-init)"]
        if ui == "gui":
            # Command-line --eval runs before frame-notice-user-settings
            # applies the user frame settings, so the alists set here
            # shape the initial frame exactly like an init file would.
            for form in _frame_geometry_forms(cols, rows):
                args += ["--eval", form]
        for form in _load_forms(loads):
            args += ["--eval", f"(elate-guard {form})"]
        for form in evals:
            args += ["--eval", f"(elate-guard {form})"]
        return args

    # minimal / init-file: generate init/init.el and use --init-directory.
    lines = [
        ";;; init.el --- generated by elate -- do not edit  -*- lexical-binding: t; -*-",
        set_session_dir,
    ]
    if ui == "gui":
        lines += _frame_geometry_forms(cols, rows)
    if config in ("minimal", "clean-install"):
        lines.append(_MINIMAL_DEFAULTS)
    # Agent first, so the semantic channel is up even if user code errors.
    lines += [
        f"(load {elisp_string(str(agent))} nil t)",
        "(elate-agent-init)",
    ]
    # User-supplied startup code runs inside elate-guard: errors land in
    # <session>/init-error for `elate start` to report, instead of leaving
    # the session in the debugger with a green "started" message.
    user_lines: list[str] = []
    if config == "init-file":
        user_lines.append(f"(load {elisp_string(str(init_path))} nil t)")
    if config == "clean-install":
        user_lines += _install_forms(loads)
    else:
        user_lines += _load_forms(loads)
    user_lines += list(evals)
    if user_lines:
        lines.append("(elate-guard\n " + "\n ".join(user_lines) + ")")
    (dirs["init"] / "init.el").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return [*ui_args, "--init-directory", str(dirs["init"])]


def environment(session_dir: Path) -> dict[str, str]:
    """Environment overrides isolating the session from the real $HOME."""
    home = session_dir / "home"
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
