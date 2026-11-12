# GUI sessions

When TTY isn't enough: images, fringes, child frames, real fonts, pixel
colors, native menus, drag-and-drop. Every flag: REFERENCE.md.

Contents: starting and capturing · keeping frames out of your way ·
region, window, and pixel checks · native popup menus and dialogs ·
below the command loop (`window-info`, `pointer`, `dnd`)

## Starting and capturing

```sh
uvx elate start --name g --ui gui --size 100x35 --load ./my-pkg.el
uvx elate -s g screenshot -o shot.png          # real PNG; Read it
uvx elate -s g mouse click --buffer '*menu*' --line 3 --col 5
```

Mouse, keys, and focus work as on TTY (INPUT.md). GUI differences: no
`--raw`; `type` replays every character through the command loop and caps
at 10,000 characters (use `eval` with `insert` for bulk text); a GUI
Emacs that wedges beyond the semantic channel can be interrupted
(`interrupt`) or stopped, not driven with raw keys. Linux/CI: `--headless`
runs the frame on Xvfb.

- GUI capture on **macOS needs an awake, unlocked display** (`--headless`/Xvfb
  is Linux-only). A failed capture reports a `reason` in the error JSON —
  `locked` / `display_asleep` / `window_gone` / `permission` — so a locked or
  asleep Mac is distinguishable from a missing Screen Recording grant. For an
  unattended/CI Mac, keep a real GUI login awake and unlocked (auto-login +
  disable screen-lock + `caffeinate -dimsu`); a backgrounded `launchd` runner
  has no GUI session and always captures black.
- macOS capture needs the **Screen Recording** permission for the app
  running elate (your terminal); elate checks it first and says so.

## Keeping frames out of your way

```sh
uvx elate start --name g --ui gui --monitor 1 --position 40,40   # second monitor
uvx elate start --name g --ui gui --monitor "Built-in" --background
uvx elate info g                       # screen: monitor, scale_factor, monitors
```

- `--monitor NAME|INDEX` opens the frame on that monitor (an index into
  Emacs's monitor list, or a name — exact, else a unique substring);
  `--position X,Y` places it in that monitor's work area (a negative
  offset needs `--position=-20,30`). An unknown monitor fails `start`,
  listing the monitors.
- `--background` (or `ELATE_GUI_BACKGROUND=1` for a whole matrix) hands
  the focus back to the app that had it once the frame is up (macOS). A
  tiling window manager may still move the frame; every frame is titled
  `elate:<session>`, so a window-manager rule matching that title can
  keep elate frames floating or on a workspace of their own. The frame is
  briefly shown before the focus returns; to keep frames off your screen
  entirely, put them on another monitor (`--monitor`), or on Linux use
  `--headless`.
- `matrix` runs GUI combos one at a time unless `--parallel-gui`.

## Region, window, and pixel checks

Needs Pillow: `pip install 'elate[image]'` (or `uvx --from 'elate[image]'
elate`).

```sh
uvx elate -s g screenshot -o win.png --window '*scratch*'   # one window's body
uvx elate -s g screenshot -o box.png --region 0,0,200,80 --points
uvx elate -s g pixel 5 5 --window '*scratch*'   # {"rgb": "#123456", ...}
```

- Every GUI screenshot reports `scale` (device pixels per frame pixel; 2
  on Retina) and `units`: captures are device pixels unless `--points`.
  `--region X,Y,W,H` is in frame pixels — the space `window-pixel-edges`
  and mouse posns use; `--window [BUFFER]` crops to a window's body.
- `pixel X Y` reads one color, converted from the display's profile back
  to sRGB, so a color Emacs drew reads back within a few units.
- Coordinates for a buffer position: with `--window BUFFER`, x/y are
  relative to that window's body, which is what `posn-at-point` reports —
  `elate -s g eval --json-result '(let ((xy (posn-x-y (posn-at-point POS
  (get-buffer-window "BUFFER"))))) (list :x (car xy) :y (cdr xy)))'`
  gives the top-left of the glyph at POS; add a pixel or two to sample
  inside it. `lines --buffer BUFFER` gives each screen line's y and
  height.
- In scenarios: `{"assert": {"pixel": {"x": 5, "y": 5, "color":
  "#123456", "tolerance": 4, "window": true}}}`, screenshot steps with
  `"region"`/`"window"`, and image snapshots with `"tolerance"` (per
  channel) and `"max_diff"` (SCRIPTING.md); a mismatch writes
  `<name>.actual.png` and `<name>.diff.png` (differing pixels in red).

## Native popup menus and dialogs

A native menu (`popup-menu`, `context-menu-mode`, `mouse-buffer-menu`,
`C-down-mouse-3`) or dialog (`x-popup-dialog`, a mouse-invoked
`yes-or-no-p`, `message-box`) on a GUI frame runs its own event loop that
nothing outside Emacs can drive. Sessions intercept them: the code that
opened one waits, `popups`/`menu` show it, and `menu choose ITEM` or `menu
dismiss` answers it — the caller gets what the real menu would have
returned (nil for a dismissed mouse menu, a quit otherwise).

```sh
uvx elate -s g eval '(context-menu-mode 1)'
uvx elate -s g mouse click --button 3 --buffer demo.el --line 3 --col 5 --events
uvx elate -s g menu                             # kind, title, indexed items
uvx elate -s g menu choose Paste                # index, "Select > All", or unique last part
```

Labels match exactly (case-sensitive); a last-part match must be unique.
Open menus with asynchronous delivery (`mouse … --events`, `keys
--events`, `send-events`): a menu opened inside an elate request
(macro-delivered `keys`/`mouse`, `eval`) could never be answered, so it
fails at once with that hint. An unanswered menu
is dismissed after 60s. Scenario steps: `{"menu": "choose", "item":
"Paste"}` / `{"menu": "dismiss"}`. TTY menus read raw keys and are not
intercepted (`elate-intercept-native-menus` t intercepts them too, nil
restores real menus).

## Below the command loop

Three GUI-level commands reach *below* the command loop, where
`special-event-map` code (drag-and-drop in particular) lives:

```sh
uvx elate -s g window-info                                # X11 ids as ints, pixel edges per window
uvx elate -s g pointer warp --buffer '*scratch*' --line 3 --col 5
uvx elate -s g dnd drop --uris file:///tmp/a,file:///tmp/b --buffer dired
```

`pointer` moves/reads the REAL pointer (`mouse` synthesizes command-loop
events; dnd code reads the live pointer instead, so only `pointer` can
target it). `dnd drop` runs a full XDND exchange from an external X
client — the drop travels Emacs's C event dispatch and x-dnd.el exactly
like a user drag; X11 sessions only (`--ui gui --headless` on Linux),
needs `pip install 'elate[dnd]'`. A refused drop returns
`status: "rejected"` with exit 0. A drop handler that errors is
reported, not hidden: Emacs catches it and clears XdndFinished's
success bit — `finished-success: false`, error text in `messages`;
results also carry `in-debugger` for handlers that park the Lisp
debugger instead (inspect with `debug show`). `--hover` asserts drag
feedback without dropping. After a drop, `wait stable --buffer B` is
the settle primitive.
