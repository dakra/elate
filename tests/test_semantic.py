"""Unit tests for the semantic channel's reply parsing -- no Emacs needed.

Covers the emacsclient < 31 reply-mangling recovery (`_unmangle_reply`):
server.el splits a long --eval reply into 1024-byte `-print`/`-print-nonl`
messages, and the pre-31 emacsclient answer loop processes each recv()
buffer as if it always held complete newline-terminated messages.  A
recv() boundary inside a message therefore (a) prints the first part of
the line immediately and (b) reports the remainder, arriving with the
next recv(), as `\\n*ERROR*: Unknown message: <rest>\\n` on stdout
(lib-src/emacsclient.c, identical in 29.4/30.2; fixed on master/31 by
real line buffering).  This bit the macOS CI run: a GUI `state` reply
crossed a recv boundary while the client was descheduled and the
`-emacs-pid` line coalesced with the reply stream, so cli `--json state`
got a mangled base64 reply.

The simulator below is a faithful port of both sides (server-quote-arg +
server-reply-print chunking; the old client loop incl. skiplf handling
and unquote_argument), so every recv alignment can be exercised
deterministically.  The invariant: recovery is either byte-exact or a
clean refusal -- never a wrong-but-well-formed reply (the base64+JSON
decode in `SemanticChannel.rpc` backstops it).
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from elate.semantic import _B64_REPLY, SemanticChannel, _unmangle_reply

SERVER_MSG_SIZE = 1024  # server.el server-msg-size
CLIENT_BUFSIZ = 1024    # stdio BUFSIZ on macOS (glibc: 8192); recv() may
                        # return any smaller size, which the sweep models


def quote_arg(s: str) -> str:
    """Port of server.el's server-quote-arg."""
    return re.sub(r"[-&\n ]",
                  lambda m: {"&": "&&", "-": "&-",
                             "\n": "&n", " ": "&_"}[m.group(0)],
                  s)


def server_stream(text: str, pid: int = 4242) -> str:
    """The byte stream server.el writes for one --eval reply.

    Port of server-reply-print's chunking, preceded by the `-emacs-pid`
    line the process filter sends just before executing the request.
    """
    qtext = quote_arg(text)
    msgs = [f"-emacs-pid {pid}\n"]
    prefix = "-print "
    while len(qtext) + len(prefix) + 1 > SERVER_MSG_SIZE:
        part = qtext[: SERVER_MSG_SIZE - len(prefix) - 1]
        # Don't split in the middle of a quote sequence (odd # of &).
        if re.search(r"(?:^|[^&])&(?:&&)*$", part):
            part = part[:-1]
        qtext = qtext[len(part):]
        msgs.append(prefix + part + "\n")
        prefix = "-print-nonl "
    msgs.append(prefix + qtext + "\n")
    return "".join(msgs)


def client_unquote(s: str) -> str:
    """Port of emacsclient.c's unquote_argument ("&_"->" ", "&n"->"\\n",
    "&C"->"C"); a trailing lone "&" terminates the string."""
    if s.endswith("&") and re.search(r"(?:^|[^&])&(?:&&)*$", s):
        s = s[:-1]
    return re.sub(r"&(.)",
                  lambda m: {"_": " ", "n": "\n"}.get(m.group(1),
                                                      m.group(1)),
                  s)


def old_client_stdout(stream: str, recv_sizes: list[int]) -> str:
    """What the pre-31 emacsclient prints for STREAM read in RECV_SIZES
    pieces (then BUFSIZ-sized reads).  Port of the answer loop."""
    out: list[str] = []
    skiplf = True

    def emit(s: str, lf_first: bool) -> None:
        nonlocal skiplf
        if lf_first and not skiplf:
            out.append("\n")
        out.append(s)
        if s:
            skiplf = s.endswith("\n")

    pos = 0
    sizes = iter(recv_sizes)
    while pos < len(stream):
        size = min(next(sizes, CLIENT_BUFSIZ), CLIENT_BUFSIZ)
        data = stream[pos:pos + size]
        pos += len(data)
        pieces = data.split("\n")
        for j, p in enumerate(pieces):
            terminated = j < len(pieces) - 1
            if not terminated and p == "":
                continue  # buffer ended exactly on a newline
            if p.startswith("-emacs-pid "):
                pass
            elif p.startswith("-print "):
                emit(client_unquote(p[len("-print "):]), lf_first=True)
            elif p.startswith("-print-nonl "):
                emit(client_unquote(p[len("-print-nonl "):]), lf_first=False)
            else:
                # printf (&"\n*ERROR*: Unknown message: %s\n"[skiplf], p)
                out.append(("" if skiplf else "\n")
                           + f"*ERROR*: Unknown message: {p}\n")
                skiplf = True
    return "".join(out)


def reply_fixture(n: int = 9000) -> tuple[str, str]:
    """A realistic agent reply: ("<printed text>", "<expected stdout>").

    The printed text is what server.el sees from `pp`: the base64-of-JSON
    string in its elisp print syntax plus pp's trailing newline.
    """
    payload = {"ok": True, "data": {"buffer": "*big*", "text": "x" * n}}
    b64 = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode()
    expected = f'"{b64}"'
    return expected + "\n", expected


def test_aligned_reads_are_clean() -> None:
    text, expected = reply_fixture()
    stream = server_stream(text)
    # pid line read on its own (the idle-machine case), then aligned 1024s
    out = old_client_stdout(stream, [len("-emacs-pid 4242\n")]).strip()
    assert out == expected
    assert _unmangle_reply(out) == expected  # no-op on clean replies


def test_unmangle_recovers_every_recv_alignment() -> None:
    text, expected = reply_fixture()
    stream = server_stream(text)
    pid_line_len = len("-emacs-pid 4242\n")
    mangled_seen = 0
    recovered = 0
    refused = []
    for first in range(1, 1300):
        out = old_client_stdout(stream, [first]).strip()
        if out == expected:
            continue
        mangled_seen += 1
        fixed = _unmangle_reply(out)
        m = _B64_REPLY.match(fixed)
        if m:
            # the invariant: a match is always the exact original
            assert fixed == expected, f"wrong recovery at first={first}"
            recovered += 1
        else:
            refused.append(first)
    assert mangled_seen > 1000  # the sweep did exercise the mangling
    # Clean refusal is acceptable only for boundaries inside the pid
    # line (its remnants are indistinguishable from payload) and for
    # the at-most-2 alignments that split the final quoted newline's
    # "&n" pair (a bare "n" fragment is indistinguishable from a 1-char
    # payload fragment; payload is preferred, the &n case refuses).
    assert recovered >= mangled_seen - pid_line_len - 2
    assert all(f < pid_line_len or f >= 900 for f in refused), refused


def test_unmangle_recovers_multi_split_reads() -> None:
    text, expected = reply_fixture()
    stream = server_stream(text)
    for sizes in ([1024, 333, 500, 777],     # late odd boundaries
                  [40, 1000, 1],             # split inside chunk prefixes
                  [17, 1023, 1023, 1023],    # off-by-one drift
                  [3, 5, 7, 11, 1024]):
        out = old_client_stdout(stream, sizes).strip()
        fixed = _unmangle_reply(out)
        if _B64_REPLY.match(fixed):
            assert fixed == expected
        else:  # never silently wrong
            assert fixed != expected


def test_unmangle_leaves_unrelated_output_alone() -> None:
    for s in ("42", "nil", '"not base64 at all!"',
              "*ERROR*: Unknown function: foo"):
        out = _unmangle_reply(s)
        assert not _B64_REPLY.match(out) or s == out


def test_rpc_decodes_a_mangled_reply(monkeypatch) -> None:
    text, _ = reply_fixture(n=4000)
    stream = server_stream(text)
    out = old_client_stdout(stream, [777]).strip()
    assert "Unknown message" in out  # really mangled
    chan = SemanticChannel("emacsclient", Path("/nonexistent/sock"))
    monkeypatch.setattr(SemanticChannel, "eval_raw",
                        lambda self, form, timeout=15.0: out)
    data = chan.rpc("state")
    assert data["buffer"] == "*big*"
    assert data["text"] == "x" * 4000
