"""Raw channel: drive the tmux pane hosting the TTY Emacs."""

from __future__ import annotations

import subprocess
from typing import Sequence

from .errors import TransportError
from .kbd import kbd_to_tmux

TMUX = "tmux"
SESSION = "elate"  # tmux session name inside our dedicated server
TARGET = f"{SESSION}:0.0"


class RawChannel:
    """tmux wrapper bound to one elate session's dedicated tmux server.

    The server socket lives inside the session sandbox (``tmux -S``), so
    equally-named sessions under different sessions roots can never
    collide with or hijack each other's panes.
    """

    def __init__(self, socket_path: str, tmux: str = TMUX) -> None:
        self.socket_path = socket_path
        self.tmux = tmux

    # -- plumbing ---------------------------------------------------------

    def _run(
        self, *args: str, check: bool = True, timeout: float = 10.0
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.tmux, "-S", self.socket_path, *args]
        try:
            # errors="replace": a pane showing non-UTF-8 content must not
            # make capture-pane raise UnicodeDecodeError.
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise TransportError(f"tmux not found: {self.tmux}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"tmux command timed out: {' '.join(cmd)}") from exc
        except OSError as exc:
            # E2BIG and friends: send-keys payloads are user-sized and can
            # exceed the OS argument-size limit before tmux even runs.
            raise TransportError(
                f"cannot run tmux: {exc} -- the payload is probably too "
                "large for the argv transport; deliver bulk text via eval "
                "(insert ...) instead"
            ) from exc
        if check and proc.returncode != 0:
            # Long args (e.g. new-session's full "exec env HOME=..." command
            # line) are noise next to tmux's own stderr: clip them.
            shown = " ".join(a if len(a) <= 60 else a[:57] + "..." for a in args)
            raise TransportError(
                f"tmux failed ({proc.returncode}): {shown}: {proc.stderr.strip()}"
            )
        return proc

    # -- lifecycle --------------------------------------------------------

    def new_session(
        self, shell_command: str, cols: int, rows: int,
        config_file: str = "/dev/null",
    ) -> None:
        # The config file (written by the session builder) disables the
        # status bar so captures show only the Emacs frame, and sets
        # remain-on-exit=failed so a crashed Emacs leaves its dying screen
        # behind for post-mortem capture. Applying it via -f (rather than
        # set-option after the fact) closes the window in which an
        # immediately-crashing Emacs would lose its pane.
        self._run(
            "-f", config_file,
            "new-session", "-d",
            "-s", SESSION,
            "-x", str(cols),
            "-y", str(rows),
            shell_command,
        )

    def has_server(self) -> bool:
        try:
            return self._run("has-session", "-t", SESSION, check=False).returncode == 0
        except TransportError:
            return False

    def pane_info(self) -> dict[str, str] | None:
        """pane_pid / pane_dead / dimensions for the Emacs pane, or None."""
        proc = self._run(
            "list-panes", "-t", SESSION, "-F",
            "#{pane_pid}\t#{pane_dead}\t#{pane_width}\t#{pane_height}\t#{pane_dead_status}",
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        fields = proc.stdout.splitlines()[0].split("\t")
        fields += [""] * (5 - len(fields))
        pid, dead, width, height, dead_status = fields[:5]
        return {
            "pane_pid": pid,
            "pane_dead": dead,
            "width": width,
            "height": height,
            "dead_status": dead_status,
        }

    def is_pane_alive(self) -> bool:
        info = self.pane_info()
        return info is not None and info["pane_dead"] == "0"

    def kill_server(self) -> None:
        self._run("kill-server", check=False)

    def resize(self, cols: int, rows: int) -> None:
        self._run("resize-window", "-t", TARGET, "-x", str(cols), "-y", str(rows))

    # -- input ------------------------------------------------------------

    def send_named_keys(self, names: Sequence[str]) -> None:
        if names:
            self._run("send-keys", "-t", TARGET, *names)

    def type_text(self, text: str) -> None:
        if text:
            self._run("send-keys", "-t", TARGET, "-l", "--", text)

    def send_kbd(self, keys: str) -> None:
        """Send an Emacs kbd string as raw terminal input."""
        for kind, payload in kbd_to_tmux(keys):
            if kind == "keys":
                self.send_named_keys(payload)  # type: ignore[arg-type]
            else:
                self.type_text(payload)  # type: ignore[arg-type]

    # -- recording ----------------------------------------------------------

    def pipe_pane(self, shell_command: str | None) -> None:
        """Pipe the pane's output bytes to SHELL_COMMAND (None: stop piping).

        tmux spawns SHELL_COMMAND via `sh -c` and feeds it everything the
        pane writes; closing the pipe (None) sends the command EOF.
        """
        args = ["pipe-pane", "-t", TARGET]
        if shell_command is not None:
            args += ["-O", shell_command]
        self._run(*args)

    def pane_pipe_open(self) -> bool:
        """True while a pipe-pane command is attached to the Emacs pane."""
        proc = self._run("display-message", "-p", "-t", TARGET,
                         "#{pane_pipe}", check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "1"

    def cursor_pos(self) -> tuple[int, int] | None:
        """(x, y) of the pane cursor (0-based), or None if unavailable."""
        proc = self._run("display-message", "-p", "-t", TARGET,
                         "#{cursor_x} #{cursor_y}", check=False)
        if proc.returncode != 0:
            return None
        parts = proc.stdout.split()
        try:
            return int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            return None

    # -- output -----------------------------------------------------------

    def capture_pane(self, ansi: bool = False, start: int | None = None) -> str:
        args = ["capture-pane", "-p", "-t", TARGET]
        if ansi:
            args.insert(1, "-e")
        if start is not None:
            # -S selects the first captured line; a negative value reaches
            # into the scrollback history (e.g. -50 == 50 lines back).
            args += ["-S", str(start)]
        return self._run(*args).stdout
