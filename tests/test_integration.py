"""Integration tests: spawn a real Emacs in a real tmux and drive it.

These tests skip when emacs or tmux is not available. They share one
module-scoped session for speed; each test resets the state it needs.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from elate import cli
from elate import session as S
from elate.errors import ElateError, SessionExists, WaitTimeout

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux") and shutil.which("emacsclient")
)

pytestmark = pytest.mark.skipif(
    not HAVE_DEPS, reason="emacs, emacsclient, and tmux are required"
)

NAME = f"pt{os.getpid()}"


@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elate-test-")
    old = os.environ.get("ELATE_HOME")
    os.environ["ELATE_HOME"] = tmp
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop("ELATE_HOME", None)
        else:
            os.environ["ELATE_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def sess(elate_home: str) -> Iterator[S.Session]:
    session = S.start_session(NAME, cols=100, rows=30)
    try:
        yield session
    finally:
        try:
            S.stop_session(NAME)
        except Exception:
            session.raw().kill_server()


def reset_scratch(sess: S.Session) -> None:
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (erase-buffer))'
    )


# -- lifecycle ---------------------------------------------------------------

def test_start_and_info(sess: S.Session) -> None:
    info = S.session_info(NAME)
    assert info["alive"] is True
    assert info["busy"] is False
    assert info["pid"] and info["pid"] > 0
    assert info["emacs_version"]
    assert info["size"] == [100, 30]
    assert os.path.isdir(info["session_dir"])

def test_double_start_rejected(sess: S.Session) -> None:
    with pytest.raises(SessionExists):
        S.start_session(NAME)

def test_sandbox_isolation(sess: S.Session) -> None:
    data = sess.semantic().eval_form('(getenv "HOME")')
    assert data["error"] is None
    assert json.loads(data["value"]) == str(sess.dir / "home")


# -- eval --------------------------------------------------------------------

def test_eval_roundtrip(sess: S.Session) -> None:
    data = sess.semantic().eval_form("(+ 1 2)")
    assert data["error"] is None
    assert data["value"] == "3"

def test_eval_unicode_and_quoting(sess: S.Session) -> None:
    data = sess.semantic().eval_form('(concat "küß" "\\"q\\"" "\\\\n")')
    assert data["error"] is None
    assert json.loads(data["value"]) == 'küß"q"\\n'

def test_eval_error_with_backtrace(sess: S.Session) -> None:
    data = sess.semantic().eval_form('(elate-no-such-function 42)')
    assert data["value"] is None
    assert "elate-no-such-function" in data["error"]
    assert data["backtrace"] is not None
    assert "(elate-no-such-function 42)" in data["backtrace"]

def test_eval_messages_delta(sess: S.Session) -> None:
    data = sess.semantic().eval_form('(message "eval-delta-%d" 7)')
    assert "eval-delta-7" in data["messages"]

def test_eval_timeout_recovers(sess: S.Session) -> None:
    data = sess.semantic().eval_form("(sleep-for 5)", timeout=1.5)
    assert data["error"] is not None
    assert "timed out" in data["error"]
    # Emacs must answer again promptly afterwards.
    S.wait_idle(sess, timeout=10.0)
    assert sess.semantic().eval_form("(+ 2 2)")["value"] == "4"

def test_eval_large_output_truncated_not_timed_out(sess: S.Session) -> None:
    # emacsclient prints slowly; uncapped megabyte results used to surface
    # as a bogus "Emacs busy" timeout. Now they come back truncated.
    data = sess.semantic().eval_form("(make-string 100000 ?x)", timeout=30.0)
    assert data["error"] is None
    assert data["truncated"] is True
    assert data["value-length"] == 100002  # quotes included
    assert len(data["value"]) == 65536

def test_small_eval_output_not_truncated(sess: S.Session) -> None:
    data = sess.semantic().eval_form('"small"')
    assert data["truncated"] is False
    assert data["value-length"] == len(data["value"])


# -- non-Unicode payloads (regression: REVIEW-phase2 bug 1) -------------------

def test_eval_surrogate_value_sanitized(sess: S.Session) -> None:
    # (string #xD800) is a legal elisp string json-serialize rejects; it
    # used to escape the agent's RPC guard and crash the controller with
    # a raw UnicodeDecodeError.
    data = sess.semantic().eval_form("(string #xD800)")
    assert data["error"] is None
    assert "�" in data["value"]


def test_poisoned_echo_area_recovers_cleanly(sess: S.Session) -> None:
    # A surrogate in the echo area used to make every state/echo call fail,
    # each failure re-embedding the previous echo text (exponential nesting,
    # measured 29.5 KB in one review session). Now: clean snapshots, echo
    # text stable across calls.
    sess.semantic().eval_form('(message "bad-%s-msg" (string #xD800))')
    s1 = sess.semantic().rpc("state")
    s2 = sess.semantic().rpc("state")
    assert s1["echo"] == "bad-�-msg"
    assert s2["echo"] == s1["echo"]  # no growth, no nested errors
    echo = sess.semantic().rpc("echo")
    assert echo["echo"] == s1["echo"]
    sess.semantic().eval_form('(message nil)')


def test_state_and_buffer_with_invalid_utf8_file(sess: S.Session,
                                                 tmp_path) -> None:
    # A buffer visiting a file with invalid UTF-8 contains raw-byte chars;
    # state/buffer used to fail with json-value-p whenever such a buffer
    # was visible. Core use case: testing packages against binary files.
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"bin\xed\xa0\x80\xff\x80data")
    data = sess.semantic().eval_form(f'(find-file "{blob}")')
    assert data["error"] is None
    try:
        state = sess.semantic().rpc("state")
        assert state["buffer"] == "blob.bin"
        win = state["windows"]
        assert win["text"].startswith("bin") and "�" in win["text"]
        buf = sess.semantic().rpc("buffer", "blob.bin")
        assert "data" in buf["text"] and "�" in buf["text"]
        # The raw channel keeps working too (capture decodes with replace).
        assert "bin" in sess.raw().capture_pane()
    finally:
        sess.semantic().eval_form(
            '(progn (set-buffer-modified-p nil)'
            ' (kill-buffer "blob.bin") (switch-to-buffer "*scratch*"))'
        )


def test_state_many_long_line_windows_stays_bounded(sess: S.Session) -> None:
    # Visible text is budgeted across windows: N windows of truncated long
    # lines (window-end spans the whole logical line) must not push one
    # snapshot toward the RPC timeout (~50 KB/s emacsclient pipe).
    sess.semantic().eval_form(
        '(progn'
        ' (with-current-buffer (get-buffer-create "elate-big")'
        '  (erase-buffer) (setq truncate-lines t)'
        '  (dotimes (_ 60) (insert (make-string 10000 ?x) "\\n")))'
        ' (delete-other-windows) (switch-to-buffer "elate-big")'
        ' (dotimes (_ 2) (split-window-below) (split-window-right))'
        ' (balance-windows) t)', timeout=30.0)
    try:
        t0 = time.monotonic()
        state = sess.semantic().rpc("state")
        elapsed = time.monotonic() - t0

        def leaves(node):
            if "children" in node:
                for child in node["children"]:
                    yield from leaves(child)
            else:
                yield node

        windows = list(leaves(state["windows"]))
        assert len(windows) >= 4
        total = sum(len(w["text"]) for w in windows)
        assert total <= 131072 + 4096  # global budget (plus per-window slack)
        assert elapsed < 10.0
    finally:
        sess.semantic().eval_form(
            '(progn (delete-other-windows) (switch-to-buffer "*scratch*")'
            ' (kill-buffer "elate-big"))'
        )


# -- keys / type / buffer ----------------------------------------------------

def test_semantic_keys_insert_text(sess: S.Session) -> None:
    reset_scratch(sess)
    sess.semantic().rpc("keys", "semkeys", "macro")
    data = sess.semantic().rpc("buffer", "*scratch*")
    assert data["text"] == "semkeys"

def test_semantic_keys_events_open_prompt(sess: S.Session) -> None:
    sess.semantic().rpc("keys", "M-x", "events")
    prompt = S.wait_prompt(sess, timeout=5.0)
    assert prompt["prompt"].startswith("M-x")
    sess.raw().send_kbd("C-g")  # cancel
    S.wait_idle(sess, timeout=5.0)

def test_semantic_keys_report_resolved_command(sess: S.Session) -> None:
    reset_scratch(sess)
    # A single chord reports the command it resolves to in the focused buffer.
    data = sess.semantic().rpc("keys", "C-x h", "macro")  # mark-whole-buffer
    assert data["command"] == "mark-whole-buffer"
    # Events delivery carries it too (resolved before the keys are queued).
    data = sess.semantic().rpc("keys", "C-x h", "events")
    assert data["command"] == "mark-whole-buffer"
    # A sequence that runs more than one command has no single binding.
    data = sess.semantic().rpc("keys", "ab", "macro")
    assert data["command"] is None

def test_raw_type_and_wait_text(sess: S.Session) -> None:
    reset_scratch(sess)
    sess.raw().type_text("rawtyped42")
    match = S.wait_text(sess, "rawtyped4.", buffer="*scratch*", timeout=5.0)
    assert match["matched"] == "rawtyped42"
    assert match["buffer"] == "*scratch*"

def test_raw_kbd_keys(sess: S.Session) -> None:
    reset_scratch(sess)
    sess.raw().send_kbd("a b RET c")
    S.wait_text(sess, "ab\nc", buffer="*scratch*", timeout=5.0)

def test_raw_chord_opens_prompt(sess: S.Session) -> None:
    sess.raw().send_kbd("C-x C-f")
    prompt = S.wait_prompt(sess, timeout=5.0)
    assert "Find file" in prompt["prompt"]
    sess.raw().send_kbd("C-g")
    S.wait_idle(sess, timeout=5.0)

def test_raw_unicode_type(sess: S.Session) -> None:
    reset_scratch(sess)
    sess.raw().type_text("ünïcode🎉")
    S.wait_text(sess, "ünïcode🎉", buffer="*scratch*", timeout=5.0)

def test_buffer_line_ranges(sess: S.Session) -> None:
    sess.semantic().eval_form(
        '(with-current-buffer "*scratch*"'
        ' (erase-buffer) (insert "l1\\nl2\\nl3\\nl4\\n"))'
    )
    data = sess.semantic().rpc("buffer", "*scratch*", 2, 3)
    assert data["text"] == "l2\nl3\n"
    assert data["total-lines"] == 5


# -- messages / echo ---------------------------------------------------------

def test_messages_tailing_cursor(sess: S.Session) -> None:
    S.messages_delta(sess)  # swallow anything pending
    sess.semantic().eval_form('(message "tail-marker-1")')
    delta = S.messages_delta(sess)
    assert "tail-marker-1" in delta["text"]
    again = S.messages_delta(sess)
    assert "tail-marker-1" not in again["text"]

def test_echo_area(sess: S.Session) -> None:
    sess.semantic().eval_form('(message "echo-marker")')
    state = sess.semantic().rpc("state")
    assert state["echo"] == "echo-marker"


# -- state / screenshot ------------------------------------------------------

def test_state_snapshot(sess: S.Session) -> None:
    reset_scratch(sess)
    state = sess.semantic().rpc("state")
    assert state["buffer"] == "*scratch*"
    assert state["major-mode"] == "lisp-interaction-mode"
    assert state["line"] == 1
    assert state["minibuffer"] is None
    assert isinstance(state["minor-modes"], list)

def show_scratch_with(sess: S.Session, text: str) -> None:
    """Show *scratch* in the selected window with exactly TEXT in it.

    One eval: a separate `eval` call does not run in the selected window's
    buffer, so switch + insert must travel together.
    """
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (erase-buffer)'
        f' (insert "{text}"))'
    )

def test_state_window_layout_and_visible_text(sess: S.Session) -> None:
    show_scratch_with(sess, "visible-marker\\nline2")
    state = sess.semantic().rpc("state")
    win = state["windows"]  # single window: a leaf node
    assert win["buffer"] == "*scratch*"
    assert win["selected"] is True
    assert win["width"] == 100 and win["height"] > 0
    assert "visible-marker" in win["text"]
    assert win["text-truncated"] is False
    assert win["start-line"] == 1 and win["end-line"] >= 2
    assert "*scratch*" in win["mode-line"]
    assert state["narrowed"] is False
    assert isinstance(state["messages-tail"], str)
    assert state["minibuffer-depth"] == 0
    assert state["unread"] == 0

def test_state_split_window_tree(sess: S.Session) -> None:
    reset_scratch(sess)
    sess.semantic().eval_form("(split-window-below)")
    try:
        state = sess.semantic().rpc("state")
        tree = state["windows"]
        assert tree["split"] == "vertical"
        assert len(tree["children"]) == 2
        buffers = [c["buffer"] for c in tree["children"]]
        assert buffers == ["*scratch*", "*scratch*"]
        assert sum(1 for c in tree["children"] if c["selected"]) == 1
    finally:
        sess.semantic().eval_form("(delete-other-windows)")

def test_state_region_and_mark(sess: S.Session) -> None:
    show_scratch_with(sess, "0123456789")
    sess.semantic().rpc("keys", "C-x h", "macro")  # mark-whole-buffer
    state = sess.semantic().rpc("state")
    assert state["mark"] == 11
    assert state["region"] == {"start": 1, "end": 11, "size": 10}
    assert state["last-command"] == "mark-whole-buffer"
    sess.semantic().eval_form('(with-current-buffer "*scratch*" (deactivate-mark))')
    state = sess.semantic().rpc("state")
    assert state["region"] is None

def test_state_narrowing(sess: S.Session) -> None:
    show_scratch_with(sess, "abcdef")
    sess.semantic().eval_form(
        '(with-current-buffer "*scratch*" (narrow-to-region 2 4))'
    )
    try:
        assert sess.semantic().rpc("state")["narrowed"] is True
    finally:
        sess.semantic().eval_form('(with-current-buffer "*scratch*" (widen))')
    assert sess.semantic().rpc("state")["narrowed"] is False

def test_state_minibuffer_prompt_and_completions(sess: S.Session) -> None:
    sess.semantic().rpc("keys", "M-x", "events")
    S.wait_prompt(sess, timeout=5.0)
    try:
        state = sess.semantic().rpc("state")
        mb = state["minibuffer"]
        assert mb["prompt"].startswith("M-x")
        assert mb["contents"] == ""
        assert mb["depth"] == 1
        comp = mb["completions"]
        assert 0 < len(comp["candidates"]) <= 50
        assert comp["truncated"] is True  # M-x offers far more than 50
    finally:
        sess.raw().send_kbd("C-g")
        S.wait_idle(sess, timeout=5.0)

def test_state_compact_dump_elides_window_text(sess: S.Session) -> None:
    show_scratch_with(sess, "elide-me")
    dump = S.state_dump(sess)
    win = dump["state"]["windows"]
    assert "elide-me" not in win["text"]
    assert "chars elided" in win["text"]

def test_screenshot_plain_and_ansi(sess: S.Session) -> None:
    reset_scratch(sess)
    sess.raw().type_text("shotmarker")
    S.wait_text(sess, "shotmarker", buffer="*scratch*", timeout=5.0)
    screen = sess.raw().capture_pane()
    assert "shotmarker" in screen
    assert "*scratch*" in screen  # mode line
    ansi = sess.raw().capture_pane(ansi=True)
    assert "shotmarker" in ansi
    assert "\x1b[" in ansi


# -- describe ----------------------------------------------------------------

def test_describe_key(sess: S.Session) -> None:
    data = sess.semantic().rpc("describe", "key", "C-x C-f")
    assert data["bound"] is True
    assert data["binding"] == "find-file"
    assert data["prefix"] is False
    fn = data["function"]
    assert fn["command"] is True
    assert "FILENAME" in fn["doc"]
    assert "C-x C-f" in fn["keys"]

def test_describe_prefix_and_unbound_key(sess: S.Session) -> None:
    data = sess.semantic().rpc("describe", "key", "C-x")
    assert data["bound"] is True and data["prefix"] is True
    data = sess.semantic().rpc("describe", "key", "C-c C-x C-y C-z")
    assert data["bound"] is False and data["binding"] is None

def test_describe_function(sess: S.Session) -> None:
    data = sess.semantic().rpc("describe", "function", "car")
    assert data["defined"] is True
    assert data["command"] is False
    assert "car" in data["doc"] or "first" in data["doc"].lower()
    data = sess.semantic().rpc("describe", "function", "find-file")
    assert data["command"] is True
    assert data["arglist"].startswith("(filename")
    assert data["file"] and data["file"].endswith((".el", ".el.gz", ".elc"))
    missing = sess.semantic().rpc("describe", "function", "elate-no-such-xyzzy")
    assert missing["defined"] is False

def test_describe_variable(sess: S.Session) -> None:
    data = sess.semantic().rpc("describe", "variable", "fill-column")
    assert data["defined"] is True
    assert data["custom"] is True
    assert data["value"].isdigit()
    assert "column" in data["doc"].lower()

def test_describe_mode(sess: S.Session) -> None:
    reset_scratch(sess)
    data = sess.semantic().rpc("describe", "mode", "lisp-interaction-mode")
    assert data["defined"] is True
    assert data["minor"] is False
    assert data["enabled"] is True
    minor = sess.semantic().rpc("describe", "mode", "auto-fill-mode")
    assert minor["defined"] is True


def test_describe_mode_enabled_resolves_state_variable(sess: S.Session) -> None:
    # auto-fill-mode's state lives in auto-fill-function (define-minor-mode
    # :variable); "enabled" used to be a false "disabled" for such modes.
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (auto-fill-mode 1) t)')
    try:
        data = sess.semantic().rpc("describe", "mode", "auto-fill-mode")
        assert data["minor"] is True
        assert data["enabled"] is True
        # ... and the mode shows up in state's minor-modes list.
        assert "auto-fill-mode" in sess.semantic().rpc("state")["minor-modes"]
    finally:
        sess.semantic().eval_form(
            '(progn (switch-to-buffer "*scratch*") (auto-fill-mode -1) t)')
    data = sess.semantic().rpc("describe", "mode", "auto-fill-mode")
    assert data["enabled"] is False


def test_describe_mode_unknown_state_is_null(sess: S.Session) -> None:
    # A minor mode whose state variable cannot be resolved reports
    # enabled=null (unknown), never a false "disabled".
    sess.semantic().eval_form(
        "(progn (defun elate-phantom-mode ())"
        " (add-to-list 'minor-mode-list 'elate-phantom-mode) t)")
    try:
        data = sess.semantic().rpc("describe", "mode", "elate-phantom-mode")
        assert data["minor"] is True
        assert data["enabled"] is None
    finally:
        sess.semantic().eval_form(
            "(progn (setq minor-mode-list (delq 'elate-phantom-mode minor-mode-list))"
            " (fmakunbound 'elate-phantom-mode) t)")


def test_describe_function_autoload_and_obsolete(sess: S.Session) -> None:
    # Not-yet-loaded autoload: arglist is null + autoloaded flag, instead of
    # the quoted "[Arg list not available...]" sentence.
    sess.semantic().eval_form(
        '(autoload (quote elate-fake-autoload) "no-such-file" "Fake." t)')
    data = sess.semantic().rpc("describe", "function", "elate-fake-autoload")
    assert data["defined"] is True
    assert data["autoloaded"] is True
    assert data["arglist"] is None
    sess.semantic().eval_form("(fmakunbound 'elate-fake-autoload)")
    # Loaded functions: autoloaded false, arglist present, obsolete marked.
    data = sess.semantic().rpc("describe", "function", "find-file")
    assert data["autoloaded"] is False
    assert data["arglist"].startswith("(filename")
    assert data["obsolete"] is None
    data = sess.semantic().rpc("describe", "function", "point-at-bol")
    assert data["obsolete"] is not None
    assert data["obsolete"]["since"]
    data = sess.semantic().rpc("describe", "variable", "inhibit-point-motion-hooks")
    assert data["obsolete"] is not None and data["obsolete"]["since"]


def test_describe_key_malformed_kbd_is_an_error(sess: S.Session) -> None:
    # "C-x C-" must not masquerade as an unbound key.
    from elate.errors import RpcError
    with pytest.raises(RpcError, match="malformed"):
        sess.semantic().rpc("describe", "key", "C-x C-")
    with pytest.raises(RpcError, match="malformed"):
        sess.semantic().rpc("describe", "key", "M-")

def test_describe_unknown_kind_is_rpc_error(sess: S.Session) -> None:
    from elate.errors import RpcError
    with pytest.raises(RpcError, match="unknown describe kind"):
        sess.semantic().rpc("describe", "frobnicator", "x")


# -- wait --------------------------------------------------------------------

def test_wait_idle(sess: S.Session) -> None:
    data = S.wait_idle(sess, min_idle=0.1, timeout=10.0)
    assert data["idle"] >= 0.1

def test_wait_text_timeout_includes_state(sess: S.Session) -> None:
    with pytest.raises(WaitTimeout) as exc_info:
        S.wait_text(sess, "never-appears-xyzzy", buffer="*scratch*", timeout=1.0)
    err = exc_info.value
    assert "never-appears-xyzzy" in str(err)
    assert "state" in err.state or "screen_tail" in err.state

def test_wait_text_buffer_appears_later(sess: S.Session) -> None:
    # The waited-for buffer does not exist yet: must poll, not error out.
    sess.semantic().eval_form(
        '(run-at-time 0.5 nil (lambda ()'
        ' (with-current-buffer (get-buffer-create "later-buf")'
        ' (insert "appears-now"))))'
    )
    match = S.wait_text(sess, "appears-now", buffer="later-buf", timeout=8.0)
    assert match["buffer"] == "later-buf"
    sess.semantic().eval_form('(kill-buffer "later-buf")')

def test_wait_text_missing_buffer_times_out_with_probe_error(sess: S.Session) -> None:
    with pytest.raises(WaitTimeout) as exc_info:
        S.wait_text(sess, "x", buffer="*never-exists*", timeout=1.0)
    assert "no buffer named" in exc_info.value.state.get("last_probe_error", "")

def test_wait_text_invalid_regexp(sess: S.Session) -> None:
    with pytest.raises(ElateError, match="invalid regexp"):
        S.wait_text(sess, "(", buffer="*scratch*", timeout=2.0)


# -- CLI / transcript --------------------------------------------------------

def test_cli_json_roundtrip(sess: S.Session, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "eval", "(* 6 7)"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True
    assert out["value"] == "42"

def test_cli_unknown_session(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", "no-such-session", "echo"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is False
    assert "no-such-session" in out["error"]

def test_cli_state_human_and_json(sess: S.Session,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    show_scratch_with(sess, "cli-state-marker")
    assert cli.main(["--human", "-s", NAME, "state"]) == 0
    human = capsys.readouterr().out
    assert "buffer: *scratch* (lisp-interaction-mode)" in human
    assert "windows (1):" in human
    assert "messages tail:" in human
    assert cli.main(["--json", "-s", NAME, "state"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "cli-state-marker" in out["windows"]["text"]

def test_cli_describe(sess: S.Session, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--json", "-s", NAME, "describe", "key", "C-x C-f"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["binding"] == "find-file"
    assert cli.main(["--human", "-s", NAME, "describe", "variable", "fill-column"]) == 0
    human = capsys.readouterr().out
    assert "name: fill-column" in human

def test_transcript_written(sess: S.Session) -> None:
    transcript = sess.dir / "log" / "transcript.jsonl"
    assert transcript.is_file()
    events = [json.loads(line) for line in transcript.read_text().splitlines()]
    kinds = {e["event"] for e in events}
    assert "start" in kinds and "started" in kinds
    assert all("ts" in e for e in events)

def test_transcript_clips_large_values(sess: S.Session) -> None:
    sess.semantic().eval_form("(make-string 100000 ?y)", timeout=30.0)
    cli_args = ["--json", "-s", NAME, "eval", "(make-string 100000 ?y)"]
    assert cli.main(cli_args) == 0
    transcript = sess.dir / "log" / "transcript.jsonl"
    assert all(len(line) < 20000 for line in transcript.read_text().splitlines())

def test_cli_elisp_error_json_ok_false(sess: S.Session,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "eval", '(error "boom")'])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is False
    assert out["error"] == "boom"

def test_cli_invalid_inputs(sess: S.Session, capsys: pytest.CaptureFixture[str]) -> None:
    cases = [
        ["--json", "-s", NAME, "wait", "idle", "abc"],
        ["--json", "-s", NAME, "wait", "text", "("],
        ["--json", "-s", NAME, "screenshot", "-o", "/no-such-dir-xyzzy/shot.txt"],
        ["--json", "-s", NAME, "keys", "C-%", "--raw"],
        ["--json", "-s", NAME, "keys", "C-x", "--raw", "--events"],
    ]
    for argv in cases:
        code = cli.main(argv)
        out = json.loads(capsys.readouterr().out)
        assert code == 1, argv
        assert out["ok"] is False, argv

def test_cli_wait_timeout_exit_code_3(sess: S.Session,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "-s", NAME, "wait", "text", "never-xyzzy",
                     "--buffer", "*scratch*", "--timeout", "0.5"])
    out = json.loads(capsys.readouterr().out)
    assert code == 3
    assert out["ok"] is False
    assert "state" in out

def test_cli_lifecycle(elate_home: str, capsys: pytest.CaptureFixture[str]) -> None:
    name = f"{NAME}cli"
    assert cli.main(["--json", "start", "--name", name,
                     "--config", "bare", "--size", "80x24"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["alive"] is True
    try:
        assert cli.main(["--json", "list"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert any(s["name"] == name and s["status"] == "running"
                   for s in out["sessions"])
        # info/stop accept both a positional name and -s NAME.
        assert cli.main(["--json", "info", name]) == 0
        capsys.readouterr()
        assert cli.main(["--json", "-s", name, "info"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["name"] == name
        assert out["scratch_dir"].endswith("/scratch")
        # path: bare scratch path on stdout, dir created, ELATE_SCRATCH
        # points the in-Emacs code at the same place.
        assert cli.main(["--human", "-s", name, "path"]) == 0
        scratch = capsys.readouterr().out.strip()
        assert scratch == out["scratch_dir"] and Path(scratch).is_dir()
        sess = S.load_session(name)
        env = sess.semantic().eval_form('(getenv "ELATE_SCRATCH")')
        assert env["value"] == f'"{scratch}"'
        env = sess.semantic().eval_form('(getenv "ELATE_SESSION")')
        assert env["value"] == f'"{name}"'
        assert cli.main(["--json", "path", name, "--kind", "dir"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["path"] == str(sess.dir)
        # eval --raw: just the value, shell-comparable; errors keep stdout
        # empty; --json-result makes `value` a real JSON object.
        assert cli.main(["-s", name, "eval", "--raw", "(+ 40 2)"]) == 0
        assert capsys.readouterr().out == "42\n"
        assert cli.main(["-s", name, "eval", "--raw", "(car nil nil)"]) == 1
        cap = capsys.readouterr()
        assert cap.out == "" and "error" in cap.err
        assert cli.main(["--json", "-s", name, "eval", "--json-result",
                        "(list :mode 'emacs :point 316)"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["value"] == {"mode": "emacs", "point": 316}
        assert out["value-encoding"] == "json"
        assert cli.main(["-s", name, "eval", "--raw", "--json-result",
                        "(list :a (list 1 2))"]) == 0
        assert json.loads(capsys.readouterr().out) == {"a": [1, 2]}
    finally:
        assert cli.main(["--json", "-s", name, "stop"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["stopped"] is True


# -- startup error reporting ---------------------------------------------------

def test_start_init_error_reported(elate_home: str) -> None:
    name = f"{NAME}ie"
    s = S.start_session(name, evals=['(error "init boom")'])
    try:
        assert s.is_alive()  # the session survives the bad form
        info = S.session_info(name)
        assert info["init_error"] is not None
        assert "init boom" in info["init_error"]
        # ... and is not stuck in the debugger.
        state = s.semantic().rpc("state")
        assert state["buffer"] != "*Backtrace*"
    finally:
        S.stop_session(name)

def test_start_bad_load_leaves_no_orphan_dir(elate_home: str) -> None:
    name = f"{NAME}orphan"
    with pytest.raises(ElateError):
        S.start_session(name, loads=["/no/such/file.el"])
    assert not (S.sessions_root() / name).exists()


# -- death and cleanup (keep these last: they tear sessions down) -------------

def test_same_name_under_different_roots_no_collision(
    sess: S.Session, elate_home: str
) -> None:
    # Regression: the tmux socket used to be derived from the bare session
    # name, so a same-named session under another ELATE_HOME would kill and
    # hijack this one.
    other_root = tempfile.mkdtemp(prefix="elate-otherroot-")
    os.environ["ELATE_HOME"] = other_root
    try:
        s2 = S.start_session(NAME, config="bare", cols=80, rows=24)
        assert s2.is_alive()
        assert s2.tmux_socket != sess.tmux_socket
        S.stop_session(NAME)
    finally:
        os.environ["ELATE_HOME"] = elate_home
        shutil.rmtree(other_root, ignore_errors=True)
    assert sess.is_alive()
    assert sess.semantic().ping(timeout=3.0)

def test_postmortem_screenshot_of_crashed_emacs(
    elate_home: str, capsys: pytest.CaptureFixture[str]
) -> None:
    name = f"{NAME}pm"
    s = S.start_session(name, config="bare", cols=80, rows=24)
    try:
        assert s.emacs_pid
        os.kill(s.emacs_pid, 9)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and s.raw().is_pane_alive():
            time.sleep(0.1)
        assert not s.is_alive()
        # The dead pane is retained (remain-on-exit=failed): capture works.
        code = cli.main(["--json", "-s", name, "screenshot"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["ok"] is True and "screen" in out
        # ...while commands needing a live Emacs report the computed status.
        code = cli.main(["--json", "-s", name, "echo"])
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert "dead" in out["error"]
    finally:
        S.stop_session(name)

def test_dead_session_detected_and_restartable(elate_home: str) -> None:
    name = f"{NAME}dead"
    s = S.start_session(name, config="bare", cols=80, rows=24)
    try:
        # Kill the tmux server out from under the registry: half-dead session.
        s.raw().kill_server()
        time.sleep(0.2)
        assert not s.is_alive()
        listed = {e["name"]: e for e in S.list_sessions()}
        assert listed[name]["status"] == "dead"
        # Restart with the same name: stale state is cleaned up.
        s2 = S.start_session(name, config="bare", cols=80, rows=24)
        assert s2.is_alive()
    finally:
        S.stop_session(name)


# -- state --since deltas -----------------------------------------------------

def test_state_returns_token_and_mode(sess: S.Session) -> None:
    data = sess.semantic().rpc("state")
    assert data["mode"] == "full"
    assert isinstance(data.get("token"), str) and data["token"]
    assert "since-status" not in data


def test_state_delta_nothing_changed(sess: S.Session) -> None:
    sem = sess.semantic()
    s1 = sem.rpc("state")
    d = sem.rpc("state", s1["token"])
    assert d["mode"] == "delta"
    assert d["changed"] is False
    assert isinstance(d.get("token"), str) and d["token"]


def test_state_delta_new_and_killed_buffer(sess: S.Session) -> None:
    sem = sess.semantic()
    tok = sem.rpc("state")["token"]
    sem.eval_form('(get-buffer-create "*delta-new*")')
    d = sem.rpc("state", tok)
    assert d["changed"] is True
    assert "*delta-new*" in (d.get("buffers", {}).get("added") or [])
    sem.eval_form('(kill-buffer "*delta-new*")')
    d2 = sem.rpc("state", d["token"])
    assert "*delta-new*" in (d2.get("buffers", {}).get("removed") or [])


def test_state_delta_modified_buffer(sess: S.Session) -> None:
    sem = sess.semantic()
    sem.eval_form('(get-buffer-create "*delta-mod*")')
    tok = sem.rpc("state")["token"]
    sem.eval_form('(with-current-buffer "*delta-mod*" (insert "hello"))')
    d = sem.rpc("state", tok)
    assert "*delta-mod*" in (d.get("buffers", {}).get("modified") or [])


def test_state_delta_messages_tail_only(sess: S.Session) -> None:
    sem = sess.semantic()
    tok = sem.rpc("state")["token"]
    sem.eval_form('(message "delta-msg-xyz")')
    d = sem.rpc("state", tok)
    assert "delta-msg-xyz" in (d.get("messages") or "")
    # A fresh token re-anchors: the same message is not re-reported.
    d2 = sem.rpc("state", d["token"])
    assert "delta-msg-xyz" not in (d2.get("messages") or "")


def test_state_delta_point_move(sess: S.Session) -> None:
    sem = sess.semantic()
    sem.eval_form(
        '(progn (switch-to-buffer (get-buffer-create "*delta-pt*"))'
        ' (erase-buffer) (insert "abc\\ndef\\nghi")'
        ' (goto-char (point-min)) (set-window-point (selected-window) (point-min)))')
    tok = sem.rpc("state")["token"]
    sem.eval_form(
        '(let ((w (selected-window)))'
        ' (with-current-buffer (window-buffer w)'
        '  (goto-char (point-max)) (set-window-point w (point-max))))')
    d = sem.rpc("state", tok)
    assert d["changed"] is True
    pt = (d.get("current") or {}).get("point")
    assert pt and pt["to"] > pt["from"]


def test_state_delta_unknown_token_degrades(sess: S.Session) -> None:
    data = sess.semantic().rpc("state", "not-a-real-token!!!")
    assert data["mode"] == "full"
    assert data.get("since-status") == "unknown"
    assert data.get("buffer")  # the full snapshot is present
    assert isinstance(data.get("token"), str) and data["token"]


# -- eval --backtrace + trace -------------------------------------------------

def test_eval_backtrace_frames(sess: S.Session) -> None:
    data = sess.semantic().eval_form("(elate-no-such-fn 42)", backtrace=True)
    assert data["error"]
    frames = data.get("frames")
    assert isinstance(frames, list) and frames
    assert any("elate-no-such-fn" in (fr.get("fun") or "") for fr in frames)
    top = frames[0]
    assert top.get("args") == ["42"]
    # The rendered string backtrace is still present (no regression).
    assert data.get("backtrace")


def test_eval_no_frames_without_flag(sess: S.Session) -> None:
    assert sess.semantic().eval_form("(elate-no-such-fn 42)").get("frames") is None
    assert sess.semantic().eval_form("(+ 1 2)", backtrace=True).get("frames") is None


def test_trace_on_eval_read_cycle(sess: S.Session) -> None:
    sem = sess.semantic()
    sem.eval_form("(defun elate-tr-sq (x) (* x x))")
    on = S.trace_functions(sess, "on", ["elate-tr-sq"])
    assert on["traced"] == ["elate-tr-sq"]
    sem.eval_form("(elate-tr-sq 7)")
    r = S.trace_functions(sess, "read")
    assert "elate-tr-sq" in r["output"] and "49" in r["output"]
    assert r["cleared"] is True
    # Read-and-clear: a second read sees no new calls.
    assert S.trace_functions(sess, "read")["output"] == ""
    off = S.trace_functions(sess, "off")
    assert off["all"] is True


def test_trace_errors_on_macro_and_undefined(sess: S.Session) -> None:
    with pytest.raises(ElateError, match="no such function"):
        S.trace_functions(sess, "on", ["elate-definitely-not-defined"])
    with pytest.raises(ElateError, match="macro"):
        S.trace_functions(sess, "on", ["when"])
    # The session is still healthy afterwards.
    assert sess.semantic().eval_form("(+ 2 2)")["value"] == "4"


# -- focus and ordered-event injection ---------------------------------------

EVT_SETUP = (
    '(progn '
    '(setq el-seq 0 el-focus-seq nil el-down-seq nil el-click-seq nil) '
    '(setq after-focus-change-function '
    '  (lambda () (setq el-focus-seq (setq el-seq (1+ el-seq))))) '
    '(with-current-buffer (get-buffer-create "evt") '
    '  (erase-buffer) (insert "hello world") (goto-char (point-min)) '
    '  (use-local-map (make-sparse-keymap)) '
    '  (local-set-key [down-mouse-1] '
    '    (lambda () (interactive) (setq el-down-seq (setq el-seq (1+ el-seq))))) '
    '  (local-set-key [mouse-1] '
    '    (lambda (e) (interactive "e") '
    '      (setq el-click-seq (setq el-seq (1+ el-seq)))))) '
    '(switch-to-buffer "evt") t)'
)


def _seq(sess: S.Session, var: str) -> int | None:
    v = sess.semantic().eval_form(var)["value"]
    return None if v == "nil" else int(v)


def test_focus_event_fires_hook(sess: S.Session) -> None:
    sess.semantic().eval_form(EVT_SETUP)
    data = S.focus_event(sess, "in")
    assert data["focus"] == "in" and data["queued"] == 1
    S.wait_idle(sess, timeout=5.0)
    # The (focus-in FRAME) event ran handle-focus-in via special-event-map:
    # after-focus-change-function fired and last-focus-update flipped.
    assert _seq(sess, "el-focus-seq") == 1
    assert sess.semantic().eval_form(
        "(frame-parameter nil 'last-focus-update)")["value"] == "t"


def test_send_events_focus_then_click_ordering(sess: S.Session) -> None:
    sess.semantic().eval_form(EVT_SETUP)
    data = S.send_events(sess, ["focus-in", "down-mouse-1@1,0", "mouse-1@1,0"],
                         buffer="evt")
    assert data["batches"] == 1 and data["queued"] == 3
    S.wait_idle(sess, timeout=5.0)
    f, d, c = (_seq(sess, v)
               for v in ("el-focus-seq", "el-down-seq", "el-click-seq"))
    # Focus hook ran before the click command.
    assert f and d and c and f < d < c


def test_send_events_reverse_focus_last(sess: S.Session) -> None:
    sess.semantic().eval_form(EVT_SETUP)
    data = S.send_events(sess, ["down-mouse-1@1,0", "mouse-1@1,0", "focus-in"],
                         buffer="evt")
    # A trailing focus event only fires at the head of a turn, so it is
    # split into a second, drained batch -- the click runs first.
    assert data["batches"] == 2
    S.wait_idle(sess, timeout=5.0)
    f, d, c = (_seq(sess, v)
               for v in ("el-focus-seq", "el-down-seq", "el-click-seq"))
    assert f and d and c and d < c < f


def test_focus_set_focus_state_shim(sess: S.Session) -> None:
    # The shim derives (frame-focus-state) from last-focus-update.
    S.focus_event(sess, "out", set_focus_state=True)
    S.wait_idle(sess, timeout=5.0)
    assert sess.semantic().eval_form("(frame-focus-state)")["value"] == "nil"
    S.focus_event(sess, "in", set_focus_state=True)
    S.wait_idle(sess, timeout=5.0)
    assert sess.semantic().eval_form("(frame-focus-state)")["value"] == "t"


def test_send_events_bad_token_raises(sess: S.Session) -> None:
    with pytest.raises(ElateError, match="unknown event token"):
        S.send_events(sess, ["definitely-bogus"])


def test_stop_session(sess: S.Session) -> None:
    result = S.stop_session(NAME)
    assert result["stopped"] is True
    assert result["was_alive"] is True
    listed = {e["name"]: e for e in S.list_sessions()}
    assert listed[NAME]["status"] == "stopped"
    assert not S.load_session(NAME).is_alive()
