"""Translate a useful subset of Emacs `kbd` notation to tmux send-keys syntax.

The raw channel delivers keys as real terminal bytes via ``tmux send-keys``.
tmux understands named keys (Enter, Escape, C-x, M-x, ...) when given
without ``-l``, and literal text with ``-l``.  We translate an Emacs kbd
string token by token and group the result into chunks so ordering is
preserved:

    "C-x C-f" -> [("keys", ["C-x", "C-f"])]
    "hello RET" -> [("literal", "hello"), ("keys", ["Enter"])]
"""

from __future__ import annotations

from .errors import ElateError

# Emacs kbd base key -> tmux key name
_NAMED: dict[str, str] = {
    "RET": "Enter",
    "TAB": "Tab",
    "SPC": "Space",
    "ESC": "Escape",
    "DEL": "BSpace",
    "<return>": "Enter",
    "<tab>": "Tab",
    "<backtab>": "BTab",
    "<escape>": "Escape",
    "<backspace>": "BSpace",
    "<delete>": "DC",
    "<deletechar>": "DC",
    "<insert>": "IC",
    "<insertchar>": "IC",
    "<up>": "Up",
    "<down>": "Down",
    "<left>": "Left",
    "<right>": "Right",
    "<home>": "Home",
    "<end>": "End",
    "<prior>": "PageUp",
    "<next>": "PageDown",
}
_NAMED.update({f"<f{n}>": f"F{n}" for n in range(1, 13)})

# Single characters a terminal can actually combine with Ctrl (they have a
# control code). Anything else (C-%, C-1, ...) cannot travel over a TTY and
# must use semantic delivery instead of silently degrading into garbage bytes.
_CTRL_OK = set("abcdefghijklmnopqrstuvwxyz@[]\\^_? ")

Chunk = tuple[str, object]  # ("keys", list[str]) | ("literal", str)


def _split_modifiers(token: str) -> tuple[list[str], str]:
    """Split leading C-/M-/S- modifiers off TOKEN, returning (mods, base)."""
    mods: list[str] = []
    while len(token) > 2 and token[1] == "-" and token[0] in "CMS":
        mods.append(token[0])
        token = token[2:]
    return mods, token


def kbd_to_tmux(keys: str) -> list[Chunk]:
    """Translate the kbd string KEYS to an ordered list of tmux chunks."""
    chunks: list[Chunk] = []

    def emit_key(name: str) -> None:
        if chunks and chunks[-1][0] == "keys":
            chunks[-1][1].append(name)  # type: ignore[union-attr]
        else:
            chunks.append(("keys", [name]))

    def emit_literal(text: str) -> None:
        if chunks and chunks[-1][0] == "literal":
            chunks[-1] = ("literal", chunks[-1][1] + text)  # type: ignore[operator]
        else:
            chunks.append(("literal", text))

    for token in keys.split():
        mods, base = _split_modifiers(token)
        if len(base) == 2 and base[1] == "-" and base[0] in "CMS":
            raise ElateError(f"incomplete modifier in raw key token {token!r}")
        if base in _NAMED:
            tmux_base = _NAMED[base]
        elif len(base) == 1:
            if not mods:
                # Plain character: send literally so tmux never interprets it.
                emit_literal(base)
                continue
            if "C" in mods and base.lower() not in _CTRL_OK:
                # This message is shown by both the CLI and the MCP server:
                # name both spellings of the way out.
                raise ElateError(
                    f"a terminal cannot encode {token!r}; deliver this chord "
                    "semantically instead (delivery='semantic', or drop --raw)"
                )
            tmux_base = base
        elif base.startswith("<") and base.endswith(">"):
            raise ElateError(f"unsupported key in raw mode: {token!r}")
        else:
            if mods:
                raise ElateError(
                    f"cannot apply modifiers to multi-char token {token!r} in raw mode"
                )
            # Bare multi-char token, e.g. "hello": each char is a keystroke.
            emit_literal(base)
            continue
        emit_key("".join(f"{m}-" for m in mods) + tmux_base)

    return chunks
