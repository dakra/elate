"""Pure unit tests for send-events token parsing and batch splitting.

These need no Emacs/tmux, so they always run.
"""

from __future__ import annotations

import pytest

from elate import session as S
from elate.errors import ElateError


def test_parse_focus_tokens() -> None:
    assert S.parse_event_token("focus-in") == {"type": "focus", "dir": "in"}
    assert S.parse_event_token("focus-out") == {"type": "focus", "dir": "out"}


def test_parse_mouse_tokens() -> None:
    assert S.parse_event_token("down-mouse-1@10,5") == {
        "type": "mouse", "event": "down-mouse", "button": 1,
        "loc": {"pos": None, "line": 10, "col": 5}}
    assert S.parse_event_token("mouse-2#42") == {
        "type": "mouse", "event": "mouse", "button": 2,
        "loc": {"pos": 42, "line": None, "col": None}}
    assert S.parse_event_token("double-mouse-3")["event"] == "double-mouse"
    assert S.parse_event_token("up-mouse-1")["event"] == "up-mouse"
    assert S.parse_event_token("wheel-up")["event"] == "wheel-up"
    assert S.parse_event_token("wheel-down@1,0")["loc"]["line"] == 1
    # No location -> window point (None).
    assert S.parse_event_token("mouse-1")["loc"] is None


def test_parse_key_token() -> None:
    assert S.parse_event_token("key:C-x") == {"type": "key", "keys": "C-x"}
    assert S.parse_event_token("key:RET")["keys"] == "RET"


@pytest.mark.parametrize("bad", [
    "bogus", "mouse-9", "mouse-0", "focus-sideways", "@1,2", "key:",
    "wheel-up@x", "down-mouse-1@1", "down-mouse-4",
])
def test_parse_rejects_bad_tokens(bad: str) -> None:
    with pytest.raises(ElateError):
        S.parse_event_token(bad)


def test_split_batches_focus_leads_its_turn() -> None:
    p = S.parse_event_token
    # Focus first: one turn (focus dispatches, then the click runs).
    one = S._split_event_batches([p(t) for t in
                                  ["focus-in", "down-mouse-1", "mouse-1"]])
    assert len(one) == 1
    # Focus trailing: split, so the focus event leads its own turn.
    two = S._split_event_batches([p(t) for t in
                                  ["down-mouse-1", "mouse-1", "focus-in"]])
    assert len(two) == 2
    assert two[0][0]["type"] == "mouse" and two[1][0]["type"] == "focus"
    # Two focus events: each leads a turn.
    assert len(S._split_event_batches([p("focus-in"), p("focus-out")])) == 2
