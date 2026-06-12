"""Unit tests for kbd -> tmux key translation (no Emacs/tmux needed)."""

import pytest

from elate.errors import ElateError
from elate.kbd import kbd_to_tmux


def test_control_sequence() -> None:
    assert kbd_to_tmux("C-x C-f") == [("keys", ["C-x", "C-f"])]

def test_meta_key() -> None:
    assert kbd_to_tmux("M-x") == [("keys", ["M-x"])]

def test_named_keys() -> None:
    assert kbd_to_tmux("RET TAB SPC ESC DEL") == [
        ("keys", ["Enter", "Tab", "Space", "Escape", "BSpace"])
    ]

def test_angle_named_keys() -> None:
    assert kbd_to_tmux("<up> <f5> <prior>") == [("keys", ["Up", "F5", "PageUp"])]

def test_literal_chars_grouped() -> None:
    assert kbd_to_tmux("h i") == [("literal", "hi")]

def test_bare_word_is_literal() -> None:
    assert kbd_to_tmux("hello RET") == [("literal", "hello"), ("keys", ["Enter"])]

def test_mixed_order_preserved() -> None:
    assert kbd_to_tmux("M-x foo RET") == [
        ("keys", ["M-x"]),
        ("literal", "foo"),
        ("keys", ["Enter"]),
    ]

def test_combined_modifiers() -> None:
    assert kbd_to_tmux("C-M-f") == [("keys", ["C-M-f"])]

def test_modified_named_key() -> None:
    assert kbd_to_tmux("M-RET") == [("keys", ["M-Enter"])]

def test_punctuation_is_literal() -> None:
    assert kbd_to_tmux("; -") == [("literal", ";-")]

def test_unsupported_angle_key_raises() -> None:
    with pytest.raises(ElateError):
        kbd_to_tmux("<kp-enter>")

def test_modifier_on_word_raises() -> None:
    with pytest.raises(ElateError):
        kbd_to_tmux("C-hello")

def test_ctrl_punctuation_unencodable_raises() -> None:
    # A TTY has no byte sequence for ctrl+percent; silent garbage is worse
    # than an error pointing at semantic delivery.
    for keys in ("C-%", "C-M-%", "C-1", "C-."):
        with pytest.raises(ElateError, match="cannot encode"):
            kbd_to_tmux(keys)

def test_ctrl_with_control_code_ok() -> None:
    assert kbd_to_tmux("C-SPC C-_ C-x") == [("keys", ["C-Space", "C-_", "C-x"])]

def test_dangling_modifier_raises() -> None:
    with pytest.raises(ElateError, match="incomplete modifier"):
        kbd_to_tmux("C-")
    with pytest.raises(ElateError, match="incomplete modifier"):
        kbd_to_tmux("C-M-")
