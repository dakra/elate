"""Semantic channel: emacsclient --eval against the per-session server socket.

All agent responses are base64-encoded JSON strings (see elate-agent.el),
so the only parsing this side ever does on emacsclient output is to strip
the surrounding double quotes from a string drawn from the base64
alphabet. That dodges emacsclient's escaping quirks entirely.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import EvalTimeout, RpcError, TransportError

_B64_REPLY = re.compile(r'^"([A-Za-z0-9+/=]*)"$')

# emacsclient < 31 mangles --eval replies that span several server
# messages (server.el splits long prints into 1024-byte `-print` /
# `-print-nonl` lines): its answer loop processes each recv() buffer as
# if it always held complete lines, so a recv() boundary falling inside
# a message makes it (a) print the first part of the line immediately
# and (b) treat the rest, arriving with the next recv(), as an unknown
# command, printing it wrapped in `\n*ERROR*: Unknown message: ...\n`
# (lib-src/emacsclient.c; fixed on master/31 by real line buffering).
# No payload bytes are lost -- they are interleaved with that wrapper
# (and, when the split lands inside the 7-12 char protocol prefix
# itself, with prefix remnants).  _unmangle_reply undoes exactly that.
_CLIENT_MANGLE = re.compile(r"\n?\*ERROR\*: Unknown message: ([^\n]*)\n?")

DEFAULT_TIMEOUT = 15.0


def _unmangle_reply(out: str) -> str:
    """Undo emacsclient<31's partial-recv mangling of a long reply.

    Every agent reply is `"<base64>"`, so the payload alphabet never
    contains `-`, space, or newline; anything of that shape inside a
    mangled fragment is a protocol-prefix remnant, not payload:

    - fragment with a space (e.g. "-nonl eyJ..." or "nl eyJ..."): a
      tail of a split `-print`/`-print-nonl` prefix glued to payload --
      keep what follows the space (a pre-space head matching
      `-emacs-pid` instead means the rest is the pid value: all noise);
    - fragment starting with "-" and no space (e.g. "-print-no"): the
      head of a split protocol prefix -- pure noise, dropped;
    - anything else is pure payload, spliced back after unquoting
      (unlike `-print`, the unknown-command branch never ran the
      fragment through unquote_argument, so server.el's &-escapes --
      e.g. "&n" for the trailing newline `pp` adds -- are still there).

    The result still has to pass the strict reply regex plus base64 and
    JSON decoding, so a wrong reconstruction can never be mistaken for
    a valid reply -- it falls through to the normal transport error.
    """
    def splice(m: re.Match[str]) -> str:
        frag = m.group(1)
        if " " in frag:
            head, frag = frag.split(" ", 1)
            if head and "-emacs-pid".endswith(head):
                # remnant of a split "-emacs-pid NNN" line: what follows
                # the space is the pid value, not payload
                return ""
            if not ("-print".endswith(head)
                    or "-print-nonl".endswith(head)):
                # not a recognizable prefix remnant, and payload never
                # contains a space: drop the fragment -- the strict
                # regex/decode below rejects any resulting wrong
                # reconstruction
                return ""
        elif frag.startswith("-"):
            return ""
        # unquote_argument: "&_" -> " ", "&n" -> "\n", "&C" -> "C".
        return re.sub(r"&(.)",
                      lambda q: {"_": " ", "n": "\n"}.get(q.group(1),
                                                          q.group(1)),
                      frag)

    return _CLIENT_MANGLE.sub(splice, out).strip()


def _client_env() -> dict[str, str]:
    """Environment for emacsclient subprocesses.

    ALTERNATE_EDITOR is dropped: on any connect failure (every command
    against a dead session reaches this) emacsclient would otherwise run
    the user's fallback editor -- appending shell garbage to the error,
    or, with ALTERNATE_EDITOR="" (a common setting), silently spawning an
    `emacs --daemon` behind elate's back.  EDITOR is dropped for the same
    reason (defensively; emacsclient itself does not consult it).
    """
    return {k: v for k, v in os.environ.items()
            if k not in ("ALTERNATE_EDITOR", "EDITOR")}


def elisp_string(s: str) -> str:
    """Quote S as an elisp string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def elisp_value(v: Any) -> str:
    """Render a Python scalar as an elisp expression (RPC arguments only)."""
    if v is None or v is False:
        return "nil"
    if v is True:
        return "t"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return elisp_string(v)
    raise TypeError(f"cannot pass {type(v).__name__} to elisp")


class SemanticChannel:
    def __init__(self, emacsclient: str, socket_path: Path) -> None:
        self.emacsclient = emacsclient
        self.socket_path = Path(socket_path)

    def eval_raw(self, form: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Evaluate FORM via emacsclient; return raw printed output."""
        cmd = [self.emacsclient, "-s", str(self.socket_path), "--eval", form]
        try:
            # errors="replace": emacsclient error text can embed raw bytes
            # from elisp strings (e.g. a json-value-p error quoting a
            # non-UTF-8 buffer); a reply must never raise UnicodeDecodeError.
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                env=_client_env(),
            )
        except FileNotFoundError as exc:
            raise TransportError(f"emacsclient not found: {self.emacsclient}") from exc
        except subprocess.TimeoutExpired as exc:
            raise EvalTimeout(
                f"emacsclient timed out after {timeout:g}s (Emacs busy or blocked)"
            ) from exc
        except OSError as exc:
            # E2BIG and friends: the form travels in emacsclient's argv,
            # so a user-sized payload (huge eval/type text) can exceed the
            # OS argument-size limit (~1 MiB on macOS).
            raise TransportError(
                f"cannot run emacsclient: {exc} -- the payload is probably "
                "too large for the argv transport; deliver bulk text via a "
                "file instead, e.g. eval (insert-file-contents \"...\")"
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip() or "no output")
            raise TransportError(f"emacsclient failed: {detail}")
        return proc.stdout.strip()

    def rpc(self, fn: str, *args: Any, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Call an agent RPC function; decode and return its data payload."""
        form = "(elate-rpc {})".format(
            " ".join([elisp_string(fn), *(elisp_value(a) for a in args)])
        )
        out = self.eval_raw(form, timeout=timeout)
        # Error snippets always show the ORIGINAL output, never the
        # unmangled reconstruction.
        snippet = out if len(out) <= 200 else out[:130] + "..." + out[-60:]
        m = _B64_REPLY.match(out)
        if not m:
            # A reply that does not look like `"base64"` may be a long
            # reply mangled by emacsclient < 31 (see _CLIENT_MANGLE);
            # reassemble before giving up.  The decode below validates.
            m = _B64_REPLY.match(_unmangle_reply(out))
        if not m:
            raise TransportError(
                f"unexpected reply from agent (is elate-agent.el loaded?): {snippet!r}"
            )
        try:
            payload = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError(f"undecodable reply from agent: {snippet!r}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = "unknown agent error"
            backtrace = None
            if isinstance(payload, dict):
                error = payload.get("error") or error
                backtrace = payload.get("backtrace")
            raise RpcError(error, backtrace=backtrace)
        return payload.get("data") or {}

    def ping(self, timeout: float = 1.0) -> bool:
        """True if the agent answers quickly; False if busy/dead."""
        try:
            return bool(self.rpc("ping", timeout=timeout).get("pong"))
        except (EvalTimeout, TransportError, RpcError):
            return False

    def eval_form(self, source: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Evaluate elisp SOURCE (one or more forms) with full error capture.

        Returns {"value": str|None, "error": str|None, "backtrace": str|None,
        "messages": str}. The source travels base64-encoded to dodge double
        escaping, and the agent also arms an in-Emacs `with-timeout` slightly
        below our hard subprocess timeout.
        """
        b64 = base64.b64encode(source.encode("utf-8")).decode("ascii")
        inner = max(timeout - 1.0, timeout * 0.8)
        return self.rpc("eval", b64, round(inner, 3), timeout=timeout)
