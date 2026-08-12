"""XDND drop-source client: synthesize a real X11 drag-and-drop.

Speaks the XDND protocol (v5) at an Emacs frame from a separate X
connection, so the drop exercises Emacs's C-level event dispatch,
special-event-map, and x-dnd.el -- the layers that in-process event
synthesis cannot reach. The pointer must already sit at the drop
coordinates: XdndDrop carries no position, the target reads the live
pointer.

Needs python-xlib (the ``dnd`` extra); everything importable here works
without it except :func:`xdnd_drop` itself.
"""

from __future__ import annotations

import select
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .errors import ElateError

try:
    from Xlib import X, Xatom
    from Xlib import display as _xdisplay
    from Xlib import error as _xerror
    from Xlib.protocol import event as _xevent
    _XLIB_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
    _XLIB_ERROR = exc

_INSTALL_HINT = ("python-xlib is required for real XDND drops: "
                 "pip install 'elate[dnd]' (or: uv sync --extra dnd)")

_ACTIONS = {"copy": "XdndActionCopy", "move": "XdndActionMove"}


class XdndError(ElateError):
    """An XDND protocol attempt failed.

    ``reason`` is machine-readable: "import" (python-xlib missing),
    "connect" (DISPLAY unreachable), "not-aware" (target window has no
    usable XdndAware property), "status-timeout" / "finished-timeout"
    (target never answered the named phase), "selection-lost" (another
    client took XdndSelection mid-drag), or "protocol" (an X error such
    as a stale window id, or malformed input). A "finished-timeout"
    error additionally carries ``xdnd_version`` and ``served_selection``
    from the aborted exchange (None/False-free facts the caller can
    still use).
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.xdnd_version: int | None = None
        self.served_selection: bool | None = None


class _PumpTimeout(Exception):
    pass


@dataclass
class DropResult:
    target_window: int
    xdnd_version: int
    x: int
    y: int
    status: str                 # "accepted" | "rejected"
    dropped: bool
    finished: bool
    finished_success: bool | None   # XdndFinished bit 0 (v5): the target's
                                    # handler succeeded; False = it errored
    finished_action: str | None
    served_selection: bool


def uri_list_payload(uris: Sequence[str]) -> bytes:
    """The text/uri-list wire form: CRLF-joined with a trailing CRLF."""
    return "".join(u + "\r\n" for u in uris).encode("ascii")


class _XdndSource:
    def __init__(self, display: str, target_window: int, uris: Sequence[str],
                 action: str) -> None:
        self.uris = list(uris)
        self.served_selection = False
        self._async_error: Any = None
        try:
            self.d = _xdisplay.Display(display)
        except Exception as exc:
            raise XdndError(f"cannot open X display {display!r}: {exc}",
                            reason="connect") from exc
        self.d.set_error_handler(self._on_async_error)
        names = ["XdndAware", "XdndSelection", "XdndEnter", "XdndPosition",
                 "XdndStatus", "XdndDrop", "XdndFinished", "XdndLeave",
                 "XdndActionCopy", "XdndActionMove", "text/uri-list",
                 "TARGETS", "TIMESTAMP", "ELATE_DND_TS"]
        self.atoms = {n: self.d.intern_atom(n) for n in names}
        self.action_atom = self.atoms[_ACTIONS[action]]
        self.screen = self.d.screen()
        self.target = self.d.create_resource_object("window", target_window)
        self.src: Any = None
        self.time = X.CurrentTime

    # -- plumbing -----------------------------------------------------------

    def _on_async_error(self, err: Any, request: Any) -> None:
        if self._async_error is None:
            self._async_error = err

    def _checkpoint(self) -> None:
        """Round-trip to the server and surface any queued async error."""
        try:
            self.d.sync()
        except Exception as exc:
            raise XdndError(f"X protocol failure: {exc}",
                            reason="protocol") from exc
        if self._async_error is not None:
            err, self._async_error = self._async_error, None
            raise XdndError(f"X protocol failure: {err}", reason="protocol")

    def _atom_name(self, atom: int) -> str | None:
        if not atom:
            return None
        try:
            return self.d.get_atom_name(atom)
        except Exception:
            return None

    def _client_message(self, kind: str, data: list[int]) -> None:
        ev = _xevent.ClientMessage(window=self.target,
                                   client_type=self.atoms[kind],
                                   data=(32, data + [0] * (5 - len(data))))
        self.target.send_event(ev, event_mask=0)
        self._checkpoint()

    def _dispatch(self, ev: Any) -> bool:
        """Handle protocol housekeeping events; True when consumed."""
        if ev.type == X.SelectionRequest:
            self._serve_selection(ev)
            return True
        if (ev.type == X.SelectionClear
                and ev.atom == self.atoms["XdndSelection"]):
            raise XdndError(
                "lost XdndSelection ownership mid-drag (another client "
                "took the selection)", reason="selection-lost")
        return False

    def _serve_selection(self, ev: Any) -> None:
        # ICCCM: an obsolete requestor may pass property None; use the
        # target atom as the property then.
        prop = ev.property or ev.target
        if ev.target == self.atoms["text/uri-list"]:
            ev.requestor.change_property(prop, self.atoms["text/uri-list"],
                                         8, uri_list_payload(self.uris))
            self.served_selection = True
        elif ev.target == self.atoms["TARGETS"]:
            ev.requestor.change_property(
                prop, Xatom.ATOM, 32,
                [self.atoms["TARGETS"], self.atoms["TIMESTAMP"],
                 self.atoms["text/uri-list"]])
        elif ev.target == self.atoms["TIMESTAMP"]:
            ev.requestor.change_property(prop, Xatom.INTEGER, 32, [self.time])
        else:
            prop = 0    # refuse the conversion
        notify = _xevent.SelectionNotify(
            time=ev.time, requestor=ev.requestor, selection=ev.selection,
            target=ev.target, property=prop)
        ev.requestor.send_event(notify, event_mask=0)
        self.d.flush()

    def _pump(self, deadline: float,
              want: Callable[[Any], bool] | None = None) -> Any:
        """Serve housekeeping until WANT matches or DEADLINE passes.

        With want=None, pumps until the deadline (a dwell), returning
        None instead of raising.
        """
        while True:
            while self.d.pending_events():
                ev = self.d.next_event()
                if self._dispatch(ev):
                    continue
                if want is not None and want(ev):
                    return ev
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if want is None:
                    return None
                raise _PumpTimeout()
            r, _, _ = select.select([self.d.fileno()], [], [],
                                    min(remaining, 0.25))
            if not r and want is None and deadline - time.monotonic() <= 0:
                return None

    def _want_client_message(self, kind: str) -> Callable[[Any], bool]:
        atom = self.atoms[kind]
        return (lambda ev: ev.type == X.ClientMessage
                and ev.client_type == atom)

    # -- protocol phases ----------------------------------------------------

    def check_aware(self) -> int:
        # Reply-bearing requests raise Xlib errors synchronously (a stale
        # target id is BadWindow right here, not via the async handler).
        try:
            prop = self.target.get_full_property(self.atoms["XdndAware"],
                                                 X.AnyPropertyType)
        except XdndError:
            raise
        except Exception as exc:
            raise XdndError(f"X protocol failure reading XdndAware off "
                            f"window {self.target.id}: {exc}",
                            reason="protocol") from exc
        self._checkpoint()
        version = prop.value[0] if prop is not None and len(prop.value) else 0
        if version < 3:
            raise XdndError(
                f"window {self.target.id} ({hex(self.target.id)}) is not "
                "XdndAware (or speaks XDND < 3) -- is this the frame's "
                "outer-window-id? (see window-info)", reason="not-aware")
        return min(5, int(version))

    def make_source(self) -> None:
        try:
            self.src = self.screen.root.create_window(
                0, 0, 1, 1, 0, self.screen.root_depth,
                window_class=X.InputOutput, visual=self.screen.root_visual,
                event_mask=X.PropertyChangeMask)
            self.src.set_wm_name("elate-xdnd")
        except Exception as exc:
            raise XdndError(f"X protocol failure creating the source "
                            f"window: {exc}", reason="protocol") from exc
        self._checkpoint()

    def harvest_timestamp(self, deadline: float) -> None:
        # A zero-length append is a no-op change that still generates a
        # PropertyNotify stamped with the server time -- the ICCCM way to
        # get a real timestamp (CurrentTime is forbidden for selections).
        self.src.change_property(self.atoms["ELATE_DND_TS"], Xatom.STRING,
                                 8, b"", X.PropModeAppend)
        self.d.flush()
        ev = self._pump(deadline,
                        lambda ev: ev.type == X.PropertyNotify
                        and ev.window.id == self.src.id)
        self.time = ev.time

    def own_selection(self) -> None:
        self.src.set_selection_owner(self.atoms["XdndSelection"], self.time)
        self._checkpoint()
        owner = self.d.get_selection_owner(self.atoms["XdndSelection"])
        if getattr(owner, "id", None) != self.src.id:
            raise XdndError("could not take XdndSelection ownership",
                            reason="protocol")

    def enter_and_position(self, version: int, x: int, y: int,
                           deadline: float) -> str:
        self._client_message("XdndEnter",
                             [self.src.id, version << 24,
                              self.atoms["text/uri-list"], 0, 0])
        self._client_message("XdndPosition",
                             [self.src.id, 0,
                              ((x & 0xFFFF) << 16) | (y & 0xFFFF),
                              self.time, self.action_atom])
        try:
            status = self._pump(deadline,
                                self._want_client_message("XdndStatus"))
        except _PumpTimeout:
            raise XdndError(
                "target never answered XdndPosition with XdndStatus "
                "(is the pointer over the target window?)",
                reason="status-timeout") from None
        accepted = bool(status.data[1][1] & 1)
        return "accepted" if accepted else "rejected"

    def leave(self) -> None:
        self._client_message("XdndLeave", [self.src.id])

    def drop(self, deadline: float,
             version: int) -> tuple[bool, bool | None, str | None]:
        # The one and only Drop path; no code path sends Drop after Leave.
        self._client_message("XdndDrop", [self.src.id, 0, self.time])
        try:
            fin = self._pump(deadline,
                             self._want_client_message("XdndFinished"))
        except _PumpTimeout:
            raise XdndError(
                "target never sent XdndFinished after the drop -- the "
                "drop handler may have errored (check `debug show`)",
                reason="finished-timeout") from None
        # Bit 0 of l[1] is defined from XDND v5: did the target's handler
        # succeed? Emacs catches drop-handler errors and reports them here
        # (plus *Messages*) rather than letting them reach the debugger.
        success = bool(fin.data[1][1] & 1) if version >= 5 else None
        return True, success, self._atom_name(fin.data[1][2])

    def close(self) -> None:
        # Destroying the source window disowns XdndSelection server-side
        # (the owner is the window, not the client).
        try:
            if self.src is not None:
                self.src.destroy()
                self.d.flush()
        except Exception:
            pass
        try:
            self.d.close()
        except Exception:
            pass


def xdnd_drop(display: str, target_window: int, x: int, y: int,
              uris: Sequence[str], *, action: str = "copy",
              hover: bool = False, hover_ms: int = 500,
              timeout: float = 15.0) -> DropResult:
    """Run one XDND exchange against TARGET_WINDOW on DISPLAY.

    X and Y are root-absolute pixels and must match the live pointer
    position (XdndDrop carries no coordinates; the target reads the
    pointer). With hover=True: Enter + Position, dwell ``hover_ms``
    milliseconds while serving selection requests, then Leave -- no
    drop. A rejected XdndStatus is a result (status="rejected"), not an
    error.
    """
    if _XLIB_ERROR is not None:
        raise XdndError(_INSTALL_HINT, reason="import")
    if action not in _ACTIONS:
        raise XdndError(f"unknown dnd action {action!r} (use copy/move)",
                        reason="protocol")
    bad = [u for u in uris if not u.isascii()]
    if bad:
        # text/uri-list is ASCII by spec; non-ASCII must arrive
        # percent-encoded (Path.as_uri() does that).
        raise XdndError(f"URI is not ASCII (percent-encode it, e.g. via "
                        f"Path(p).resolve().as_uri()): {bad[0]!r}",
                        reason="protocol")
    deadline = time.monotonic() + timeout
    source = _XdndSource(display, target_window, uris, action)
    version = None
    try:
        version = source.check_aware()
        source.make_source()
        source.harvest_timestamp(deadline)
        source.own_selection()
        status = source.enter_and_position(version, x, y, deadline)
        result = DropResult(target_window=target_window,
                            xdnd_version=version, x=x, y=y, status=status,
                            dropped=False, finished=False,
                            finished_success=None, finished_action=None,
                            served_selection=source.served_selection)
        if status == "rejected":
            source.leave()
            return result
        if hover:
            source._pump(min(time.monotonic() + hover_ms / 1000.0, deadline))
            source.leave()
            result.served_selection = source.served_selection
            return result
        finished, success, finished_action = source.drop(deadline, version)
        result.dropped = True
        result.finished = finished
        result.finished_success = success
        result.finished_action = finished_action
        result.served_selection = source.served_selection
        return result
    except _PumpTimeout:
        raise XdndError("XDND protocol timed out", reason="protocol") \
            from None
    except XdndError as exc:
        # Facts from the aborted exchange the caller can still use (the
        # finished-timeout downgrade path builds its result from these).
        exc.xdnd_version = version
        exc.served_selection = source.served_selection
        raise
    finally:
        source.close()
