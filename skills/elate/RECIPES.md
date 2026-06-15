# elate recipes

Worked examples for common verification jobs. Four of them (transient
menu, font-lock, dired, clean-install) also exist as committed, CI-tested
scenario scripts under `examples/` in the elate repo —
`elate run examples/<name>.json` replays them with assertions. When you
build something similar, prefer ending with a scenario script (see
SCRIPTING.md) so the verification is replayable.

All commands assume a session started with `uvx elate start --name demo …`
and use `uvx elate` (swap for `uv run elate` inside an elate checkout).

## Test a transient menu

Transient UIs never show up in batch tests — they need a live command loop
and a window. (transient ships with Emacs >= 28.)

```sh
uvx elate start --name demo --size 100x30

# Define a tiny transient (yours would come from --load ./my-pkg.el):
uvx elate -s demo eval '(progn (require (quote transient))
  (transient-define-prefix demo-transient ()
    ["Demo actions" ("u" "upcase word" upcase-word)])
  t)'

# Fixture text, point at the start of "hello":
uvx elate -s demo eval '(progn (switch-to-buffer "*scratch*") (erase-buffer)
  (insert "hello world") (goto-char (point-min)) t)'

uvx elate -s demo eval '(demo-transient)'   # open the menu
uvx elate -s demo popups                    # == transient ==  u upcase word
uvx elate -s demo keys u                    # press the suffix key
uvx elate -s demo wait idle
uvx elate -s demo buffer '*scratch*'        # -> HELLO world
uvx elate stop demo
```

`state` flags the open menu (`popups: transient`), which is the cue to call
`popups`. Script version: `examples/transient-menu.json` — its assertions
are `{"assert": {"popup": "transient"}}` and
`{"assert": {"buffer_contains": "HELLO world"}}`.

## Verify font-lock / theme faces

Check rendering facts structurally instead of eyeballing screenshots:

```sh
uvx elate -s demo eval '(with-current-buffer (get-buffer-create "demo.el")
  (erase-buffer) (emacs-lisp-mode)
  (insert "(defun demo-add (x)\n  \"Add one to X.\"\n  (1+ x))\n")
  t)'

# Run-length encoded face runs for the whole buffer:
uvx elate -s demo buffer demo.el --props
#   2-7  (L1) face=font-lock-keyword-face 'defun'
#   8-16 (L1) face=font-lock-function-name-face 'demo-add'

# Point query (LINE:COL, 1-based line : 0-based column):
uvx elate -s demo faces-at 1:8 --buffer demo.el
#   face: font-lock-function-name-face
```

The buffer is never displayed — `--props` ensures font-lock on the range
first, so this works for background buffers. Overlay-based UI (hl-line,
company) shows up in the same dump under `overlays`. Script version:
`examples/font-lock.json`.

## Drive a real package interactively (the canonical loop)

act → wait → observe, never sleep-and-poll. Shown against dired (built-in,
works offline); driving any `--load`ed package is the same loop.

```sh
uvx elate start --name demo --size 100x30

# Fixture files live in the sandbox's fake $HOME, not the real one:
uvx elate -s demo eval '(progn (make-directory "~/demo" t)
  (with-temp-file "~/demo/notes.txt" (insert "remember the milk")) t)'

uvx elate -s demo eval '(dired "~/demo")'      # act
uvx elate -s demo wait text 'notes\.txt'       # wait: listing rendered
uvx elate -s demo state                        # observe: buffer "demo" (dired-mode)

uvx elate -s demo eval '(dired-goto-file (expand-file-name "~/demo/notes.txt"))'
uvx elate -s demo keys RET                     # act: visit it, like a user
uvx elate -s demo wait idle
uvx elate -s demo state                        # observe: buffer "notes.txt"
uvx elate -s demo buffer                       # -> remember the milk
uvx elate stop demo
```

Script version: `examples/drive-dired.json`.

## Clean-install: verify the package, not just the source tree

`--config minimal` puts the source on `load-path` — fast, but it cannot
tell you whether `;;;###autoload` cookies work, whether `Package-Requires`
is honest, or whether the byte-compiled installed copy behaves.
`--config clean-install` installs the package for real
(`package-install-file` into a sandbox-local `<session>/elpa/`):

```sh
uvx elate start --name fresh --config clean-install --load ./my-pkg.el
uvx elate -s fresh describe function my-pkg-command  # autoloaded: true, before any require
uvx elate -s fresh keys 'M-x my-pkg-command RET'     # invoking it loads the installed copy
uvx elate info fresh    # package_user_dir + installed [{name, version, dir, warnings}]
uvx elate stop fresh
```

- Accepts what `package-install-file` accepts: an `.el` file, a package
  tar, or a package directory.
- The sandbox has **no network** and `package-archives` is nil: missing
  dependencies fail the install with one clear `init_error` naming each.
- Per-package byte-compile warnings surface in `info` under `installed`.
- In a scenario script it is just
  `"session": {"config": "clean-install", "load": ["./my-pkg.el"]}` —
  see `examples/clean-install.json`.

## Reproduce "the keybinding doesn't work"

The pattern for keymap/interactive bugs that batch ERT cannot see:

```sh
uvx elate start --name bug --load ./my-pkg.el
uvx elate -s bug eval '(my-pkg-mode 1)'
uvx elate -s bug describe key 'C-c g'      # what is it ACTUALLY bound to?
uvx elate -s bug keys 'C-c g'              # press it like a user
uvx elate -s bug messages                  # "C-c g is undefined"? error text?
uvx elate -s bug eval '(lookup-key my-pkg-mode-map (kbd "C-c g"))'
uvx elate -s bug eval '(lookup-key my-pkg-mode-map "C-c g")'  # string vs kbd!
uvx elate stop bug
```

`describe key` resolves through the live keymap stack (minor modes,
overlays), so it shows what the user's keypress would really run.

## Record a demo

```sh
uvx elate -s demo record start             # asciicast v2 via tmux pipe-pane
# … drive the session …
uvx elate -s demo record stop              # path, event count, duration
# render: asciinema play demo.cast / agg demo.cast demo.gif
```

TTY only; for GUI sessions use `snap start --interval 0.5` (PNG frame
series + manifest.json).

## Test a package that drives a subprocess / shell / REPL

Two things differ from the package-under-test loop: rc files must exist in
the sandbox `$HOME` *before* the subprocess spawns, and you synchronize on
the buffer's output settling (`wait stable`), not command-loop idle.

```sh
# A fixture HOME with the rc file the shell will read (kept isolated):
mkdir -p fixtures/home
printf 'PS1="rc-loaded$ "\n' > fixtures/home/.bashrc

uvx elate start --name sh --home-seed fixtures/home    # rc in place pre-launch
uvx elate -s sh eval '(let ((explicit-shell-file-name "/bin/bash")) (shell))'
uvx elate -s sh wait stable --buffer '*shell*' --quiet-ms 300   # prompt settled

# Drive the subprocess directly with send-process (talks to the process,
# not Emacs's command loop) — feed input, then interrupt a runaway job:
uvx elate -s sh send-process 'echo hi-$((1+1))\n' --buffer '*shell*'
uvx elate -s sh wait stable --buffer '*shell*' --quiet-ms 300   # output settled
uvx elate -s sh buffer '*shell*'                       # -> hi-2
uvx elate -s sh send-process 'sleep 99\n' --buffer '*shell*'
uvx elate -s sh send-process --char C-c --buffer '*shell*'      # ^C: abandon it

# Verify the package's own text properties (values, not just names). Use
# --pos/--run to compare adjacent cells (e.g. typed vs dimmed suggestion):
uvx elate -s sh faces-at 1:0 --buffer '*shell*'        # e.g. my-prompt=t
uvx elate -s sh faces-at --pos 12 --run 4 --buffer '*shell*'
uvx elate stop sh
```

## Reproduce a focus-vs-click ordering bug

Window systems deliver a focus change and a click as separate, ordered
events: `focus-in` → `after-focus-change-function` hooks → the click's
command. That ordering distinguishes a click that *refocuses* a frame from
a plain click (click-to-refocus vs click-to-select, paste-on-focus, …) and
is invisible to batch ERT. `send-events` injects them into one input stream
so they drain through the real command loop, in order:

```sh
uvx elate start --name foc --ui gui --load ./my-term.el
# … open the package's buffer in its click-to-select ("semi-char") mode …

# A refocusing click: focus-in, THEN the click (one command-loop turn).
# @LINE,COL is 1-based line, 0-based col.
uvx elate -s foc send-events 'focus-in' 'down-mouse-1@10,5' 'mouse-1@10,5'
uvx elate -s foc wait idle
uvx elate -s foc state            # treated as a focus click? point at the live cursor?

# The byte-identical click WITHOUT a preceding focus-in.
uvx elate -s foc send-events 'down-mouse-1@10,5' 'mouse-1@10,5'
uvx elate -s foc wait idle
uvx elate -s foc state            # now switched to copy mode

uvx elate stop foc
```

A focus event only fires at the head of a command-loop turn, so any focus
tokens are auto-split into separate, drained batches — the reverse order
(mouse-down dispatched *before* the focus-in, a distinct code path) is just
`send-events 'down-mouse-1@10,5' 'mouse-1@10,5' 'focus-in'`. Standalone,
`focus in` / `focus out` flip focus on their own. `send-events` is
lower-level than `mouse`: each mouse token is exactly one event (no implicit
down+click pair), and `key:KBD` tokens (e.g. `key:RET`) interleave keys.

Injected focus runs `handle-focus-in`/`-out` through `special-event-map`,
firing `after-focus-change-function` and setting the `last-focus-update`
frame parameter. It cannot move the C-owned `(frame-focus-state)`; add
`--set-focus-state` if the code under test reads it (a non-native shim that
derives the state from `last-focus-update`). Works in TTY and GUI sessions;
`examples/focus-ordering.json` is a self-contained, runnable version.

`type` and `keys` run through Emacs's command loop (so they obey the
buffer's keymaps); `send-process` bypasses it and writes raw bytes to the
buffer's process — the right tool for shells/REPLs that read from a PTY.

Reuse the same startup setup across many sessions with `--eval-file
setup.el` (a forms file, no `load-path` side effects) or `--profile NAME`
(`$XDG_CONFIG_HOME/elate/profiles/NAME.el`); both run before
`emacs-startup-hook`, so they set vars an auto-launch hook will read.
