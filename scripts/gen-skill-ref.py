#!/usr/bin/env python3
"""Generate skills/elate/REFERENCE.md from the elate argparse tree.

Stdlib only. The output is deterministic: subcommands render in parser
definition order, arguments in their definition order, and nothing
depends on the environment, so CI can regenerate the file and fail on
drift (`git diff --exit-code -- skills/elate/REFERENCE.md`).

Usage:
    uv run python scripts/gen-skill-ref.py            # rewrite REFERENCE.md
    uv run python scripts/gen-skill-ref.py --stdout   # print to stdout
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from elate.cli import build_parser  # noqa: E402

OUTPUT = REPO_ROOT / "skills" / "elate" / "REFERENCE.md"

HEADER = """\
# elate CLI reference

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with: uv run python scripts/gen-skill-ref.py
     CI fails when this file drifts from the CLI. -->

Generated from the `elate` argparse tree. Every command also accepts the
global options below; `--json` makes the output machine-readable and is
the right default for programmatic use.
"""


def _clean(text: str | None) -> str:
    """Collapse argparse help text whitespace into one line."""
    return " ".join((text or "").split())


def _fmt_default(action: argparse.Action) -> str | None:
    val = action.default
    # Identity checks: `0 in (None, False)` is True by equality, which
    # would silently hide a legitimate numeric default of 0.
    if val is None or val is False or val is argparse.SUPPRESS or val == []:
        return None
    if isinstance(val, tuple) and all(isinstance(x, int) for x in val):
        return "x".join(str(x) for x in val)  # --size (120, 36) -> 120x36
    if isinstance(val, float):
        return f"{val:g}"
    return str(val)


def _invocation(action: argparse.Action) -> str:
    """Render how the argument is spelled on the command line."""
    if action.option_strings:
        spelled = ", ".join(action.option_strings)
        if action.nargs == 0:
            return spelled
        if action.choices is not None:
            return f"{spelled} {{{','.join(str(c) for c in action.choices)}}}"
        metavar = action.metavar or action.dest.upper()
        return f"{spelled} {metavar}"
    # Positional.
    name = action.metavar or action.dest
    if action.choices is not None:
        name = f"{{{','.join(str(c) for c in action.choices)}}}"
    if action.nargs in ("?", "*"):
        return f"[{name}]"
    if action.nargs == "+":
        return f"{name}..."
    return str(name)


def _argument_lines(parser: argparse.ArgumentParser) -> list[str]:
    lines: list[str] = []
    for action in parser._actions:  # noqa: SLF001 - argparse has no public walk API
        if isinstance(action, (argparse._HelpAction,  # noqa: SLF001
                               argparse._SubParsersAction)):  # noqa: SLF001
            continue
        bits: list[str] = []
        if action.required and action.option_strings:
            bits.append("required")
        if action.nargs in ("*", "+") or (
                action.option_strings
                and isinstance(action, argparse._AppendAction)):  # noqa: SLF001
            bits.append("repeatable")
        default = _fmt_default(action)
        if default is not None:
            bits.append(f"default: {default}")
        suffix = f" ({'; '.join(bits)})" if bits else ""
        help_text = _clean(action.help)
        line = f"- `{_invocation(action)}`{suffix}"
        if help_text:
            line += f" -- {help_text}"
        lines.append(line)
    for group in parser._mutually_exclusive_groups:  # noqa: SLF001
        spelled = " | ".join(
            a.option_strings[-1] for a in group._group_actions)  # noqa: SLF001
        lines.append(f"- mutually exclusive: `{spelled}`")
    return lines


def render() -> str:
    parser = build_parser()
    out: list[str] = [HEADER]

    out.append("## Global options\n")
    out.extend(_argument_lines(parser))
    out.append("")

    sub = next(a for a in parser._actions  # noqa: SLF001
               if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001
    helps = {ca.dest: _clean(ca.help)
             for ca in sub._choices_actions}  # noqa: SLF001

    out.append("## Commands\n")
    out.extend(f"- [`elate {name}`](#elate-{name})" for name in sub.choices)
    out.append("")

    for name, sp in sub.choices.items():
        out.append(f"## elate {name}\n")
        if helps.get(name):
            out.append(f"{helps[name]}\n")
        if sp.description and _clean(sp.description) != helps.get(name):
            out.append(f"{_clean(sp.description)}\n")
        args = _argument_lines(sp)
        if args:
            out.extend(args)
        else:
            out.append("(no arguments)")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--stdout" in argv:
        sys.stdout.write(text)
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
