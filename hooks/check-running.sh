#!/bin/sh
# elate plugin hook: surface still-RUNNING elate sessions.
#
# usage: check-running.sh start|end   (exec-form "args" in hooks.json)
#
#   end   -> SessionEnd. Warning on stderr + exit 1: per the hooks docs
#            a non-zero exit's stderr is shown to the user (SessionEnd
#            stdout is debug-log-only). Empirically (Claude Code
#            2.1.175) that stderr also reaches only the debug log, so
#            this is best-effort until the documented contract is
#            honored -- which is why the `start` mode below exists.
#   start -> SessionStart. Plain stdout + exit 0: the docs guarantee
#            stdout is injected as context, so the model can tell the
#            user about leftovers at the next opportunity.
#
# Robustness contract (both modes run for EVERY user of the plugin, the
# start mode on every session start): never break or delay the session.
# If elate/uv is missing, `elate list` fails, or no session is RUNNING:
# silent exit 0, fast. Offline-only lookups -- never wait on the
# network. Stopped sessions are deliberately ignored (their sandboxes
# are inert; `elate purge` cleans them up whenever).
#
# Known limit (by design -- fail-silent forbids guessing): only sessions
# reachable via `elate`, `uvx elate`, or `uv tool run elate` are seen.
# Someone driving elate exclusively through `uv run` inside a checkout
# (never installed, never in the uv tool cache) gets a silent no-op.

set -u
mode="${1:-end}"

if command -v elate >/dev/null 2>&1; then
    set -- elate
elif command -v uvx >/dev/null 2>&1; then
    set -- uvx --offline elate
elif command -v uv >/dev/null 2>&1; then
    set -- uv tool run --offline elate
else
    exit 0
fi

out=$("$@" --json list 2>/dev/null) || exit 0

# Single-line JSON: {"ok": true, "sessions": [{"name": "s", ...,
# "status": "running", ...}, ...]}. Split the objects onto lines and
# keep the names of the running ones ("name" precedes "status").
running=$(printf '%s\n' "$out" | tr '{' '\n' \
    | sed -n 's/^"name": "\([^"]*\)".*"status": "running".*/\1/p') || exit 0

[ -n "$running" ] || exit 0

names=$(printf '%s\n' "$running" | tr '\n' ' ')

if [ "$mode" = "start" ]; then
    printf 'elate sessions from earlier work are still running: %s' "$names"
    # shellcheck disable=SC2016  # backticks are markdown for the model
    printf -- '-- reuse them or stop them (`elate stop NAME`, or `uvx elate stop NAME`); let the user know they exist.\n'
    exit 0
fi

{
    printf 'elate: session(s) still running: %s' "$names"
    printf '\nstop with: elate stop NAME   (or: uvx elate stop NAME)\n'
} >&2
exit 1
