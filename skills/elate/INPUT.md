# Input: keys, mouse, subprocesses, focus, recovery

How input reaches a session, what can swallow it, and how to get a session
back when it stops answering. Every flag: REFERENCE.md.

Contents: key delivery modes · when keys don't do what you sent · mouse ·
subprocesses (`send-process`) · window-system focus · a wedged Emacs or
the Lisp debugger

## Key delivery modes

`keys` takes Emacs kbd notation (`'C-x C-f'`, `'M-x foo RET'`, `'TAB'`).

| situation | use |
|---|---|
| normal key sequence that completes | `keys 'M-x foo RET'` (semantic, default) |
| sequence that **opens a prompt and leaves it open** | `keys 'C-x C-f' --events` |
| answering an already-open prompt | `type 'filename'` then `keys RET --events` |
| Emacs is wedged/busy, nothing responds | `interrupt` (raw C-g on TTY; signals a GUI Emacs) |
| literal text into a buffer | `type 'hello'` (or `eval '(insert …)'` for bulk) |
| drive a **subprocess** (shell/REPL): ^C, feed input | `send-process --char C-c` / `send-process 'cmd\n'` |

Why: semantic delivery runs `execute-kbd-macro`, which runs the keys
**through the command loop** — so they obey whatever keymaps are active.
In an evil buffer in *normal* state, `type "abc"` sends the commands `a`,
`b`, `c`, not the text; enter insert state (or use `--raw`) first. And it
does **not** block on an open minibuffer prompt — it exits the prompt with
empty input (bare `M-x` errors with "'' is not a valid command name").
`--events` queues on `unread-command-events` instead, so the prompt stays
open for you to inspect (`state` shows prompt + candidates) and answer.

`--events` delivery is **asynchronous**: the call returns once the keys
are queued, so follow it with a `wait` (`wait prompt`, `wait idle`, …)
before you observe.

In scenarios, `"defaults": {"keys_delivery": "events"}` makes events
delivery the default for every `keys` step (SCRIPTING.md) — use it when
the code under test reads keys itself (`read-key`, a `ding`, echo-area
checks).

## When keys don't do what you sent

- Because keys obey the focused buffer's keymaps, a buffer that intercepts
  keys can **swallow** one: a terminal emulator in char mode (vterm/eat,
  and the like) forwards most keystrokes to its PTY, and many special-mode
  buffers rebind keys, so a globally-bound key never runs as the command
  you meant. The result's `command` field is what the sequence resolves to
  in the focused buffer (`null` for an unbound key or a multi-command
  sequence) — assert on it instead of inferring from `delivered`:
  `keys '<f8>'` returning `"command": "term-send-raw"` tells you the
  terminal ate the key. When a command **must** run regardless of buffer
  bindings, call it directly with `eval`.
- `--raw` sends real terminal bytes via tmux: works even when Emacs is
  stuck, but rejects chords a terminal cannot encode (e.g. `C-%`) and does
  not exist for GUI sessions. To simply unwedge a busy Emacs, prefer
  `interrupt` (below) over raw key plumbing.
- A command that **rings the bell** aborts the whole semantic macro. elate
  reports the culprit — `key delivery aborted -- COMMAND rang the bell in
  BUFFER at point N` — so diagnose with `describe key …` / `messages`. To
  deliver *past* a spurious bell (e.g. evil insert off the prompt row),
  use `keys … --no-abort-on-bell` (queued via events, so asynchronous —
  follow with a `wait`).
- If a semantic `keys` call times out, the sequence probably left Emacs
  reading input: retry with `--events`, or recover with `interrupt`.

## Mouse

`mouse` is semantic — a real event sequence through the command loop, no
OS permissions — and works on TTY and GUI alike:

```sh
uvx elate -s s mouse click --buffer '*menu*' --line 3 --col 5
uvx elate -s s mouse click --buffer demo.el --area left-fringe --line 2
uvx elate -s s mouse drag --buffer demo.el --line 1 --col 0 --to-line 3 --to-col 4 --steps 6
uvx elate -s s lines --buffer demo.el     # per-screen-line geometry: y, height, partial rows
```

It targets any window part (`--area header-line|tab-line|left-fringe|…|
outside`, `--mode-line`), takes pixel offsets (`--dx/--dy` from the glyph,
`--x/--y` raw text-area pixels — pair with `lines` for row heights), and
`drag --steps N` emits real `mouse-movement` events so drag maps run;
`--to-buffer`/`--to-area` end a drag in another window or part. Results
carry the resolved posn. `--events` queues the click instead of running
it synchronously — needed when the click opens something that waits for
input, like a native context menu (GUI.md); follow it with a `wait`.
`send-events` queues raw events (clicks, keys,
`focus-in`) in one ordered batch. Drag-and-drop from outside Emacs and the
real pointer are GUI-only: GUI.md.

## Subprocesses: `send-process`

- `send-process` writes straight to a buffer's subprocess
  (`process-send-string`), bypassing the command loop: `--char C-c`
  interrupts a job, `send-process 'cmd\n'` feeds a shell/REPL, `--file`
  seeds a large payload. `keys`/`type` drive Emacs; this drives the process.
  `--buffer NAME` targets *any* buffer with a live subprocess — a `shell`,
  `comint` REPL, or a terminal buffer (`term`/`vterm`/`eat`). To drive a
  program running inside a terminal buffer, send its bytes there directly,
  e.g. `send-process --buffer '*ghostel*' --char 'C-c'` then
  `send-process --buffer '*ghostel*' 'git status\n'`.
  Targeting rule: the buffer must have exactly **one** live process — none
  or several is an error naming the candidates, never a silent pick; pick
  one with `--process NAME` (by process name, buffer optional). The result
  echoes the chosen process's name and command line — check it when input
  seems to vanish: a package can also write through its **own** channel (a
  raw fd its Emacs process object doesn't front), in which case no process
  is the write path and you call the package's send function via `eval`
  instead.

## Window-system focus

- The sandboxed frame **never has real window-system focus** while an agent
  drives it, so code gated on focus (paste-on-focus, focus-dimming, a
  click-to-refocus mode) sees an unfocused frame and behaves differently.
  Don't fight it with window managers: inject focus with `focus in` /
  `send-events 'focus-in' …` (ordered with clicks/keys) — that runs
  `after-focus-change-function` and sets `last-focus-update`; add
  `--set-focus-state` when the code reads `(frame-focus-state)` (C-owned,
  shimmed). Worked example: the focus-vs-click recipe in RECIPES.md.
- `focus in|out` injects the event Emacs gets from the window system.
  `(frame-focus-state)` reads C-level state elisp cannot set, so it keeps
  reporting the real (unfocused) state unless you pass `--set-focus-state`.

## A wedged Emacs, or one in the debugger

- After an eval/keys timeout where Emacs stays busy (`info` shows
  `busy: true`): `interrupt` unblocks it — raw C-g on TTY; on GUI it
  breaks Emacs into the Lisp debugger with SIGUSR2 and unwinds it back to
  top level (`--signal usr2` instead stays in the debugger so `debug show`
  reveals where it was stuck). Never use `--signal int` to recover: SIGINT
  *terminates* a GUI-only Emacs. Stop the session only if it stays wedged
  after an interrupt.
- A session sitting in the Lisp debugger (code under test signalled and
  `*Backtrace*` popped up) is the opposite failure mode: it answers the
  channel and looks idle, but every key/eval lands inside the debugger.
  `info` reports `in_debugger`/`recursion_depth` and `state` reports
  `in-debugger`/`recursion-depth` (note the spelling difference), `wait
  idle` fails fast pointing there; `debug show` prints the backtrace,
  `debug abort` unwinds to top level with the session intact.
