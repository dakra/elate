"""Phase 4 integration tests: ERT runner, lint, props/faces dumps, popups.

Same conventions as test_integration.py: one module-scoped TTY session
against a real Emacs in a real tmux; each test resets what it needs.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from elate import cli
from elate import session as S
from elate.errors import ElateError, EvalTimeout, RpcError

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux") and shutil.which("emacsclient")
)

pytestmark = pytest.mark.skipif(
    not HAVE_DEPS, reason="emacs, emacsclient, and tmux are required"
)

NAME = f"q{os.getpid()}"

ERT_FIXTURE = """\
;;; elfix-tests.el --- ERT fixture for elate -*- lexical-binding: t; -*-
;;; Commentary:
;; Passing / failing / erroring / skipped / tagged / slow tests.
;;; Code:
(require 'ert)

(ert-deftest elfix-pass-simple () (should (= (+ 1 2) 3)))
(ert-deftest elfix-pass-interactive ()
  "Proves the suite runs in a live interactive Emacs, not batch."
  (should (not noninteractive)))
(ert-deftest elfix-pass-message ()
  (message "elfix-message-marker")
  (should t))
(ert-deftest elfix-fail-should ()
  (should (equal (list 1 2) (list 1 3))))
(ert-deftest elfix-error-void ()
  (elfix-no-such-function 7))
(ert-deftest elfix-skip ()
  (ert-skip "skipped on purpose"))
(ert-deftest elfix-tagged ()
  :tags '(quick)
  (should t))
(ert-deftest elfix-slow-sleep30 ()
  :tags '(slow)
  (sleep-for 30))
(ert-deftest elfix-slow-sleep5 ()
  :tags '(slow)
  (sleep-for 5))

;; Named outside the elfix- prefix so SUITE_SELECTOR never picks it up.
(ert-deftest elquit-kbd-quit ()
  "Signals quit, exactly like raw C-g hitting the test body would."
  (keyboard-quit))

(provide 'elfix-tests)
;;; elfix-tests.el ends here
"""

# Selector for "the whole fixture suite except the deliberately slow tests".
SUITE_SELECTOR = '(and "\\\\`elfix-" (not (tag slow)))'

LINT_DIRTY = """\
;;; lintme.el --- lint fixture -*- lexical-binding: t; -*-
;;; Commentary:
;; Known-dirty fixture: free variable on line 8, missing docstring below.
;;; Code:

(defun lintme-free-var ()
  "Touch an undeclared variable."
  (setq lintme-undeclared (1+ lintme-undeclared)))

(defun lintme-no-docstring ()
  nil)

(provide 'lintme)
;;; lintme.el ends here
"""

LINT_CLEAN = """\
;;; cleanme.el --- clean lint fixture -*- lexical-binding: t; -*-

;; Copyright (C) 2026  elate

;; Author: elate <elate@example.com>

;;; Commentary:

;; A fixture that should produce no findings.

;;; Code:

(defun cleanme-add-one (n)
  "Return N plus one."
  (1+ n))

(provide 'cleanme)
;;; cleanme.el ends here
"""

# A file whose *compile* hard-loops: the loop runs at macro-expansion
# time, i.e. while byte-compile-file expands `evilmacro-spin'. It
# services timers (sleep-for) so the agent's with-timeout can fire --
# the analogue of the timer-servicing hung ERT test.
LINT_EVIL = """\
;;; evilmacro.el --- lint fixture whose compile hangs -*- lexical-binding: t; -*-
(defmacro evilmacro-spin ()
  (while t (sleep-for 0.05))
  nil)
(defun evilmacro-go () (evilmacro-spin))
(provide 'evilmacro)
;;; evilmacro.el ends here
"""

# Pins the documented R1 behavior: byte-compilation EXECUTES top-level
# compile-time code in the live session.
LINT_SIDE_EFFECT = """\
;;; sideeffect.el --- lint fixture with compile-time effects -*- lexical-binding: t; -*-
(eval-when-compile (defvar qfix-compile-ran 99))
(provide 'sideeffect)
;;; sideeffect.el ends here
"""

# Session-history dependence fixtures (review suspicion 3):
# eval-when-compile definitions leak into the session and silence a
# later lint's undefined-function warning; plain defmacro does not.
LINT_HIST_FUN = """\
;;; histfun.el --- -*- lexical-binding: t; -*-
(eval-when-compile (defun histfix-fn (x) (list x)))
(provide 'histfun)
;;; histfun.el ends here
"""

LINT_USE_FUN = """\
;;; usefun.el --- -*- lexical-binding: t; -*-
(defun usefun-go ()
  "Call a function this file never defines or requires."
  (histfix-fn 1))
(provide 'usefun)
;;; usefun.el ends here
"""

LINT_HIST_MAC = """\
;;; histmac.el --- -*- lexical-binding: t; -*-
(defmacro histfix-mac (x)
  "Wrap X in a list."
  `(list ,x))
(provide 'histmac)
;;; histmac.el ends here
"""

LINT_USE_MAC = """\
;;; usemac.el --- -*- lexical-binding: t; -*-
(defun usemac-go ()
  "Call a macro this file never defines or requires."
  (histfix-mac 1))
(provide 'usemac)
;;; usemac.el ends here
"""

# package-lint test approach (read this before touching the fixtures
# below).  The default suite must be HERMETIC -- no live MELPA -- so the
# happy-path test builds a tiny local `archive-contents' package archive
# and lints with --archive-dir, which keeps everything offline.
#
# The package the archive provides is a STUB, not the real package-lint:
# it `provide's `package-lint' and defines `package-lint-buffer' to
# return a canned (LINE COL TYPE MESSAGE) finding list, exactly the
# shape the real package-lint-buffer returns (verified against
# package-lint 0.26's `package-lint-buffer' docstring/source).  This
# proves the full elate plumbing end to end -- configure a file://-style
# local archive, package-install it into the sandbox elpa/, populate
# package-archive-contents, call package-lint-buffer over a visited
# emacs-lisp-mode buffer, map each tuple to a {tool:"package-lint", ...}
# item with a normalized severity -- WITHOUT vendoring third-party GPL
# code into the repo or depending on a machine-local package cache that
# CI would not have.  Whether the real package-lint's *checks* are
# correct is package-lint's own test suite's job, not elate's; elate
# owns the install/call/map/error-handling glue, which the stub
# exercises completely.
PL_STUB_EL = """\
;;; package-lint.el --- stub package-lint for elate tests -*- lexical-binding: t; -*-
;; Version: 9.9
;; Package-Requires: ((emacs "24.1"))
;;; Commentary:
;; A stand-in `package-lint' used only by elate's offline test archive.
;;; Code:
(defun package-lint-buffer (&optional buffer)
  "Return a canned (LINE COL TYPE MESSAGE) finding list for BUFFER.
Mirrors the real `package-lint-buffer' return shape: a list whose
elements are (LINE COL TYPE MESSAGE) with TYPE in (error warning info)."
  (ignore buffer)
  (list (list 1 0 'error "stub: \\"lexical-binding\\" should be set")
        (list 3 2 'warning "stub: example warning")
        (list 5 4 'info "stub: example info")))
(provide 'package-lint)
;;; package-lint.el ends here
"""

# The on-disk package archive index.  Format: (1 (NAME . [VERSION-LIST
# REQS DOC KIND PROPS])); KIND `single' = a one-file package.
PL_ARCHIVE_CONTENTS = """\
(1
 (package-lint . [(9 9) ((emacs (24 1))) "stub package-lint" single nil]))
"""

# A clean .el to lint: it has zero byte-compile/checkdoc findings, so any
# finding in the happy-path result must have come from package-lint.
PL_FIXTURE_EL = """\
;;; plfix.el --- package-lint fixture -*- lexical-binding: t; -*-

;; Copyright (C) 2026  elate

;; Author: elate <elate@example.com>

;;; Commentary:

;; A clean fixture (no bytecomp/checkdoc findings) for package-lint.

;;; Code:

(defun plfix-add-one (n)
  "Return N plus one."
  (1+ n))

(provide 'plfix)
;;; plfix.el ends here
"""


def _make_pl_archive(root: Path) -> Path:
    """Build a local package archive providing the stub package-lint.

    ROOT/archive holds package-lint-9.9.el + archive-contents; returns
    that archive directory (suitable as --archive-dir).
    """
    archive = root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "package-lint-9.9.el").write_text(PL_STUB_EL, encoding="utf-8")
    (archive / "archive-contents").write_text(
        PL_ARCHIVE_CONTENTS, encoding="utf-8")
    return archive


@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elate-q-")
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
def fixtures(elate_home: str) -> Iterator[dict[str, Path]]:
    tmp = Path(tempfile.mkdtemp(prefix="elate-qfix-"))
    paths = {
        "ert": tmp / "elfix-tests.el",
        "dirty": tmp / "lintme.el",
        "clean": tmp / "cleanme.el",
        "evil": tmp / "evilmacro.el",
        "sideeffect": tmp / "sideeffect.el",
        "histfun": tmp / "histfun.el",
        "usefun": tmp / "usefun.el",
        "histmac": tmp / "histmac.el",
        "usemac": tmp / "usemac.el",
    }
    paths["ert"].write_text(ERT_FIXTURE, encoding="utf-8")
    paths["dirty"].write_text(LINT_DIRTY, encoding="utf-8")
    paths["clean"].write_text(LINT_CLEAN, encoding="utf-8")
    paths["evil"].write_text(LINT_EVIL, encoding="utf-8")
    paths["sideeffect"].write_text(LINT_SIDE_EFFECT, encoding="utf-8")
    paths["histfun"].write_text(LINT_HIST_FUN, encoding="utf-8")
    paths["usefun"].write_text(LINT_USE_FUN, encoding="utf-8")
    paths["histmac"].write_text(LINT_HIST_MAC, encoding="utf-8")
    paths["usemac"].write_text(LINT_USE_MAC, encoding="utf-8")
    try:
        yield paths
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def sess(elate_home: str, fixtures: dict[str, Path]) -> Iterator[S.Session]:
    session = S.start_session(NAME, cols=100, rows=30)
    # The ERT fixture is loaded once for the whole module.
    session.semantic().rpc("load-file", str(fixtures["ert"]))
    try:
        yield session
    finally:
        try:
            S.stop_session(NAME)
        except Exception:
            session.raw().kill_server()


def by_name(data: dict, name: str) -> dict:
    return next(t for t in data["tests"] if t["name"] == name)


# -- ERT runner ----------------------------------------------------------------

def test_ert_suite_structured_results(sess: S.Session) -> None:
    data = S.run_ert(sess, selector=SUITE_SELECTOR)
    assert data["total"] == 7
    assert data["passed"] == 4
    assert data["failed"] == 1
    assert data["errors"] == 1
    assert data["skipped"] == 1
    assert data["unexpected"] == 2
    assert data["timed-out"] is False
    assert isinstance(data["duration"], float)

    failed = by_name(data, "elfix-fail-should")
    assert failed["status"] == "failed"
    assert failed["expected"] is False
    assert "ert-test-failed" in failed["condition"]
    assert "(= 1 2)" not in failed["condition"]  # the *fixture's* form is there
    assert "(list 1 2)" in failed["condition"] or "(1 2)" in failed["condition"]
    assert failed["backtrace"] and "ert-fail" in failed["backtrace"]
    assert failed["duration"] >= 0

    erring = by_name(data, "elfix-error-void")
    assert erring["status"] == "error"
    assert "void-function" in erring["condition"]
    assert "elfix-no-such-function" in erring["condition"]
    assert erring["backtrace"] and "elfix-no-such-function" in erring["backtrace"]

    # Both unexpected results in one run carry backtraces (the
    # internal-when-entered-debugger re-arm holds inside ERT too).
    assert failed["backtrace"] and erring["backtrace"]

    skipped = by_name(data, "elfix-skip")
    assert skipped["status"] == "skipped"
    assert skipped["expected"] is True
    assert "skipped on purpose" in skipped["condition"]

    passed = by_name(data, "elfix-pass-simple")
    assert passed["status"] == "passed"
    assert passed["condition"] is None and passed["backtrace"] is None

    # Per-test *Messages* capture.
    msg = by_name(data, "elfix-pass-message")
    assert "elfix-message-marker" in msg["messages"]

    # Interactive semantics: (should (not noninteractive)) passed.
    assert by_name(data, "elfix-pass-interactive")["status"] == "passed"


def test_ert_selector_forms(sess: S.Session) -> None:
    # Exact test name (bare symbol that is a known test).
    data = S.run_ert(sess, selector="elfix-pass-simple")
    assert data["total"] == 1 and data["passed"] == 1
    # Bare symbol that is NOT a known test: used as a name regexp.
    data = S.run_ert(sess, selector="elfix-pass-")
    assert data["total"] == 3 and data["passed"] == 3
    # Tag selector.
    data = S.run_ert(sess, selector="(tag quick)")
    assert data["total"] == 1
    assert data["tests"][0]["name"] == "elfix-tagged"
    # Quoted string regexp.
    data = S.run_ert(sess, selector='"elfix-skip"')
    assert data["total"] == 1 and data["skipped"] == 1
    # Nothing matches: empty run, not an error.
    data = S.run_ert(sess, selector="no-such-prefix-xyzzy")
    assert data["total"] == 0 and data["unexpected"] == 0


def test_ert_timeout_interrupts_and_session_recovers(sess: S.Session) -> None:
    t0 = time.monotonic()
    data = S.run_ert(sess, selector="elfix-slow-sleep30", timeout=2.0)
    assert time.monotonic() - t0 < 8.0  # interrupted, not slept out
    assert data["timed-out"] is True
    assert "timed out" in data["error"]
    # ERT's unwind records the interrupted test as aborted, and the
    # listener captures its name into "interrupted" (the elate_test
    # description promises both).
    aborted = [t for t in data["tests"] if t["status"] == "aborted"]
    assert aborted and aborted[0]["name"] == "elfix-slow-sleep30"
    assert data["interrupted"] == "elfix-slow-sleep30"
    assert "elfix-slow-sleep30" in data["error"]
    assert data["unexpected"] >= 1
    # The session is immediately usable...
    assert sess.semantic().eval_form("(+ 3 4)")["value"] == "7"
    # ...and a follow-up ERT run works.
    again = S.run_ert(sess, selector="elfix-pass-simple")
    assert again["passed"] == 1 and again["unexpected"] == 0


def test_ert_quit_in_test_returns_instantly(sess: S.Session) -> None:
    # REVIEW-phase4 bug 1, repro A: a test that signals quit used to park
    # ert-run-tests at a hidden "Abort testing?" y-or-n-p until the whole
    # timeout budget was gone, then misreport the run as timed out.
    t0 = time.monotonic()
    data = S.run_ert(sess, selector="elquit-kbd-quit", timeout=60.0)
    assert time.monotonic() - t0 < 5.0  # instant, not the 60s budget
    assert data["timed-out"] is False
    assert data["error"] is None
    quit_t = by_name(data, "elquit-kbd-quit")
    assert quit_t["status"] == "quit"
    assert quit_t["expected"] is False
    assert data["unexpected"] == 1
    # The channel stays alive and a follow-up run works.
    assert sess.semantic().eval_form("(+ 1 1)")["value"] == "2"
    again = S.run_ert(sess, selector="elfix-pass-simple")
    assert again["passed"] == 1


def test_ert_raw_cg_during_run_quits_test_not_session(sess: S.Session) -> None:
    # REVIEW-phase4 bug 1, repro B: raw C-g (the documented universal
    # unblock) sent mid-run quits the running test; the run must return
    # immediately with a "quit" result instead of hanging at the abort
    # prompt for the rest of the timeout.
    result: dict = {}

    def run() -> None:
        try:
            result["data"] = S.run_ert(sess, selector="elfix-slow-sleep30",
                                       timeout=25.0)
        except Exception as exc:  # surfaced via the asserts below
            result["exc"] = exc

    th = threading.Thread(target=run)
    t0 = time.monotonic()
    th.start()
    time.sleep(2.0)
    sess.raw().send_kbd("C-g")
    th.join(timeout=15.0)
    assert not th.is_alive(), "ERT run did not return after raw C-g"
    assert "exc" not in result, result.get("exc")
    assert time.monotonic() - t0 < 10.0  # no 25s park at a hidden prompt
    data = result["data"]
    assert data["timed-out"] is False
    assert by_name(data, "elfix-slow-sleep30")["status"] == "quit"
    # No "Abort testing?" prompt anywhere, and the session is usable.
    assert "Abort testing?" not in sess.raw().capture_pane()
    assert sess.semantic().eval_form("(+ 2 2)")["value"] == "4"


def test_ert_selector_is_strictly_one_form(sess: S.Session) -> None:
    # A typo'd selector must not silently run a different set of tests.
    with pytest.raises(RpcError, match="trailing"):
        S.run_ert(sess, selector="elfix-pass-simple garbage-after")
    # ...and an unbalanced selector must not silently match 0 tests.
    with pytest.raises(RpcError, match="unreadable"):
        S.run_ert(sess, selector="(tag")
    # The documented unknown-symbol -> name-regexp fallback still works.
    data = S.run_ert(sess, selector="elfix-pass-")
    assert data["total"] == 3


def test_ert_python_hard_timeout_dead_socket_recovery(sess: S.Session) -> None:
    # The controller-side hard timeout kills emacsclient mid-run; the agent
    # later answers the dead socket -- the server-filter debugger shield is
    # what keeps the semantic channel alive (Phase 3 wedge pattern).
    sem = sess.semantic()
    b64 = base64.b64encode(b"elfix-slow-sleep5").decode("ascii")
    with pytest.raises(EvalTimeout):
        sem.rpc("ert", b64, 600, timeout=1.5)
    S.wait_idle(sess, timeout=20.0)
    assert sem.eval_form("(+ 5 5)")["value"] == "10"
    data = S.run_ert(sess, selector="elfix-pass-simple")
    assert data["passed"] == 1


def test_ert_load_file_errors_are_clean(sess: S.Session, tmp_path: Path) -> None:
    bad = tmp_path / "bad-tests.el"
    bad.write_text(';;; bad -*- lexical-binding: t; -*-\n(error "boom-at-load")\n',
                   encoding="utf-8")
    with pytest.raises(RpcError, match="boom-at-load"):
        S.run_ert(sess, load_files=[str(bad)], selector="elfix-pass-simple")
    assert sess.semantic().eval_form("(+ 1 1)")["value"] == "2"
    with pytest.raises(ElateError, match="does not exist"):
        S.run_ert(sess, load_files=["/no/such/dir/tests.el"])


def test_cli_test_command(sess: S.Session, fixtures: dict[str, Path],
                          capsys: pytest.CaptureFixture[str]) -> None:
    # All-pass run: exit 0, ok true; --load-file re-loads harmlessly.
    code = cli.main(["--json", "-s", NAME, "test", "elfix-pass-simple",
                     "--load-file", str(fixtures["ert"])])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True and out["passed"] == 1
    assert out["loaded"] == [str(fixtures["ert"].resolve())]
    # Failing run: exit 1, ok false, human summary names the test.
    code = cli.main(["-s", NAME, "test", "elfix-fail-should"])
    human = capsys.readouterr().out
    assert code == 1
    assert "Ran 1 test(s)" in human
    assert "FAILED: elfix-fail-should" in human
    assert "condition:" in human and "backtrace:" in human
    code = cli.main(["--json", "-s", NAME, "test", "elfix-fail-should"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False and out["unexpected"] == 1


# -- lint ------------------------------------------------------------------------

def test_lint_dirty_fixture_items_with_lines(sess: S.Session,
                                             fixtures: dict[str, Path]) -> None:
    data = S.lint_files(sess, [str(fixtures["dirty"])])
    assert data["clean"] is False
    assert data["files"] == [str(fixtures["dirty"].resolve())]
    items = data["items"]
    assert all({"file", "tool", "line", "col", "severity", "message"}
               <= set(it) for it in items)
    # byte-compile: free-variable warnings with the exact line number.
    bc = [it for it in items if it["tool"] == "byte-compile"]
    assert any(it["line"] == 8 and "free variable" in it["message"]
               and "lintme-undeclared" in it["message"]
               and it["severity"] == "warning" for it in bc)
    assert all(it["file"] == str(fixtures["dirty"].resolve()) for it in bc)
    # checkdoc: missing docstring near the second defun.
    cd = [it for it in items if it["tool"] == "checkdoc"]
    assert any(isinstance(it["line"], int) and 9 <= it["line"] <= 12
               and "documentation" in it["message"] for it in cd)
    # Lint notes document the deliberate omissions.
    assert any("package-lint" in n for n in data["notes"])
    assert any("native-comp" in n for n in data["notes"])


def _assert_no_lint_residue(sess: S.Session, source: Path) -> None:
    assert not source.with_suffix(".elc").exists()
    assert list((sess.dir / "lint").glob("*.elc")) == []
    data = sess.semantic().eval_form('(mapcar #\'buffer-name (buffer-list))')
    assert source.name not in data["value"]
    assert "*Compile-Log*" not in data["value"]


def test_lint_leaves_no_elc_behind(sess: S.Session,
                                   fixtures: dict[str, Path]) -> None:
    # Happy path: no .elc, no visit buffer, no *Compile-Log*.
    S.lint_files(sess, [str(fixtures["dirty"])])
    _assert_no_lint_residue(sess, fixtures["dirty"])
    # Interrupted path (REVIEW-phase4 R2): the cleanup now lives in the
    # unwind-protect, so a lint aborted mid-compile (the in-Emacs timeout
    # firing) leaves the same nothing behind.
    with pytest.raises(RpcError, match="timed out"):
        S.lint_files(sess, [str(fixtures["evil"])], timeout=2.0)
    _assert_no_lint_residue(sess, fixtures["evil"])


def test_lint_hard_loop_compile_times_out_cleanly(
        sess: S.Session, fixtures: dict[str, Path]) -> None:
    # REVIEW-phase4 R1: a file whose *compile* hangs (macro expansion in
    # a timer-servicing loop) used to wedge the channel for the whole
    # subprocess timeout with no in-Emacs backstop. Now the same
    # with-timeout discipline as eval/ert interrupts it.
    t0 = time.monotonic()
    with pytest.raises(RpcError, match="timed out"):
        S.lint_files(sess, [str(fixtures["evil"])], timeout=2.0)
    assert time.monotonic() - t0 < 10.0
    # The session survives and is immediately usable...
    assert sess.semantic().eval_form("(+ 5 5)")["value"] == "10"
    # ...including for a follow-up lint.
    data = S.lint_files(sess, [str(fixtures["clean"])])
    assert data["clean"] is True


def test_lint_executes_compile_time_code_in_session(
        sess: S.Session, fixtures: dict[str, Path]) -> None:
    # REVIEW-phase4 R1, behavior pin: in-session byte-compilation
    # EXECUTES the file's top-level compile-time code. This is inherent
    # to linting against the session's load-path; the mitigations are
    # the timeout, and the warnings in the docs and the lint "notes".
    sem = sess.semantic()
    assert sem.eval_form("(boundp 'qfix-compile-ran)")["value"] == "nil"
    data = S.lint_files(sess, [str(fixtures["sideeffect"])])
    assert sem.eval_form("(bound-and-true-p qfix-compile-ran)")["value"] == "99"
    assert any("throwaway session" in n for n in data["notes"])


def test_lint_results_depend_on_session_history(
        sess: S.Session, fixtures: dict[str, Path]) -> None:
    # REVIEW-phase4 unverified suspicion 3, pinned both ways:
    # eval-when-compile definitions executed by an earlier lint DO leak
    # into the session and silence a later undefined-function warning;
    # plain defmacro definitions do NOT (they stay compile-local in
    # byte-compile-macro-environment).
    def unresolved(data: dict, name: str) -> list[dict]:
        return [i for i in data["items"] if name in i["message"]]

    sem = sess.semantic()
    # defmacro variant: refuted -- no leak, the warning persists.
    first = S.lint_files(sess, [str(fixtures["usemac"])])
    assert unresolved(first, "histfix-mac")
    S.lint_files(sess, [str(fixtures["histmac"])])
    assert sem.eval_form("(fboundp 'histfix-mac)")["value"] == "nil"
    again = S.lint_files(sess, [str(fixtures["usemac"])])
    assert unresolved(again, "histfix-mac")
    # eval-when-compile variant: confirmed -- the definition leaks and
    # the warning a fresh session would emit is silenced.
    first = S.lint_files(sess, [str(fixtures["usefun"])])
    assert unresolved(first, "histfix-fn")
    S.lint_files(sess, [str(fixtures["histfun"])])
    assert sem.eval_form("(fboundp 'histfix-fn)")["value"] == "t"
    again = S.lint_files(sess, [str(fixtures["usefun"])])
    assert not unresolved(again, "histfix-fn")


def test_lint_clean_fixture(sess: S.Session, fixtures: dict[str, Path]) -> None:
    data = S.lint_files(sess, [str(fixtures["clean"])])
    assert data["items"] == []
    assert data["clean"] is True


def test_lint_missing_file_and_empty(sess: S.Session) -> None:
    with pytest.raises(ElateError, match="does not exist"):
        S.lint_files(sess, ["/no/such/file.el"])
    with pytest.raises(ElateError, match="at least one"):
        S.lint_files(sess, [])


def test_cli_lint_command(sess: S.Session, fixtures: dict[str, Path],
                          capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["-s", NAME, "lint", str(fixtures["dirty"])])
    human = capsys.readouterr().out
    assert code == 1
    assert "[byte-compile]" in human and "[checkdoc]" in human
    assert "finding(s) in 1 file(s)" in human
    # Clean + dirty together: still exit 1, both files reported.
    code = cli.main(["--json", "-s", NAME, "lint",
                     str(fixtures["clean"]), str(fixtures["dirty"])])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ok"] is False and len(out["files"]) == 2
    code = cli.main(["--json", "-s", NAME, "lint", str(fixtures["clean"])])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True and out["clean"] is True


# -- opt-in package-lint ----------------------------------------------------------
#
# Hermetic by construction: every test below installs the STUB
# package-lint from a local archive via --archive-dir, so nothing
# touches the network.  See the PL_* fixtures above for why a stub is
# used.  Each test that needs a *fresh* install state uses its own
# throwaway session (the install is cached per session by the agent's
# fboundp guard), so the happy-path and the no-install error paths don't
# interfere.


@contextlib.contextmanager
def _pl_session(name: str) -> Iterator[S.Session]:
    session = S.start_session(name, cols=100, rows=30)
    try:
        yield session
    finally:
        try:
            S.stop_session(name)
        except Exception:
            session.raw().kill_server()


def test_lint_package_lint_offline_archive_happy(
        elate_home: str, tmp_path: Path) -> None:
    archive = _make_pl_archive(tmp_path)
    fixture = tmp_path / "plfix.el"
    fixture.write_text(PL_FIXTURE_EL, encoding="utf-8")
    with _pl_session(f"{NAME}pl1") as session:
        # Default lint (no flag) is unchanged: no package-lint items, and
        # this clean fixture has no bytecomp/checkdoc findings either.
        base = S.lint_files(session, [str(fixture)])
        assert base["clean"] is True
        assert not any(i["tool"] == "package-lint" for i in base["items"])

        # Opt-in, offline, via the local archive: package-lint is
        # installed into the sandbox elpa/ and its findings surface.
        data = S.lint_files(session, [str(fixture)],
                            package_lint=True, archive_dir=str(archive))
        pls = [i for i in data["items"] if i["tool"] == "package-lint"]
        assert pls, "expected package-lint items from the stub"
        assert all({"file", "tool", "line", "col", "severity", "message"}
                   <= set(i) for i in pls)
        assert all(i["file"] == str(fixture.resolve()) for i in pls)
        # The stub's canned (LINE COL TYPE MESSAGE) tuples mapped through,
        # severities normalized from package-lint's error/warning/info.
        sev_by_line = {i["line"]: i["severity"] for i in pls}
        assert sev_by_line[1] == "error"
        assert sev_by_line[3] == "warning"
        assert sev_by_line[5] == "info"
        assert any("lexical-binding" in i["message"] for i in pls)
        assert data["clean"] is False  # package-lint added findings

        # Idempotent: a second opt-in lint reuses the install (no
        # re-install) and still yields the items.
        again = S.lint_files(session, [str(fixture)],
                             package_lint=True, archive_dir=str(archive))
        assert len([i for i in again["items"]
                    if i["tool"] == "package-lint"]) == len(pls)

        # The notes document the opt-in availability and the
        # archive/network tradeoff.
        notes = " ".join(data["notes"])
        assert "package-lint" in notes and "--archive-dir" in notes
        assert "opt-in" in notes and "reproducible" in notes


def test_lint_package_lint_empty_archive_structured_error(
        elate_home: str, tmp_path: Path) -> None:
    # The offline/missing path, kept network-free: an --archive-dir with
    # no archive-contents -> a structured error naming the cause and
    # pointing at --archive-dir, with the session + channel surviving.
    empty = tmp_path / "empty"
    empty.mkdir()
    fixture = tmp_path / "plfix.el"
    fixture.write_text(PL_FIXTURE_EL, encoding="utf-8")
    with _pl_session(f"{NAME}pl2") as session:
        with pytest.raises(RpcError) as ei:
            S.lint_files(session, [str(fixture)],
                         package_lint=True, archive_dir=str(empty))
        msg = str(ei.value)
        assert "archive-contents" in msg
        assert "--archive-dir" in msg
        # The semantic channel and the session survive the failure: other
        # tools keep working, and a follow-up DEFAULT lint is clean.
        assert session.semantic().eval_form("(+ 21 21)")["value"] == "42"
        post = S.lint_files(session, [str(fixture)])
        assert post["clean"] is True
        assert not any(i["tool"] == "package-lint" for i in post["items"])


def test_lint_package_lint_session_guards(
        elate_home: str, tmp_path: Path) -> None:
    # Controller-side guards, no Emacs work needed beyond a live session.
    fixture = tmp_path / "plfix.el"
    fixture.write_text(PL_FIXTURE_EL, encoding="utf-8")
    with _pl_session(f"{NAME}pl3") as session:
        with pytest.raises(ElateError, match="applies to --package-lint"):
            S.lint_files(session, [str(fixture)],
                         package_lint=False, archive_dir=str(tmp_path))
        with pytest.raises(ElateError, match="not a directory"):
            S.lint_files(session, [str(fixture)],
                         package_lint=True, archive_dir="/no/such/archive/dir")


def test_cli_lint_package_lint(elate_home: str, tmp_path: Path,
                               capsys: pytest.CaptureFixture[str]) -> None:
    archive = _make_pl_archive(tmp_path)
    fixture = tmp_path / "plfix.el"
    fixture.write_text(PL_FIXTURE_EL, encoding="utf-8")
    name = f"{NAME}pl4"
    with _pl_session(name):
        # Default CLI lint of the clean fixture: exit 0, no package-lint.
        code = cli.main(["--json", "-s", name, "lint", str(fixture)])
        out = json.loads(capsys.readouterr().out)
        assert code == 0 and out["clean"] is True

        # Opt-in via the CLI flags: package-lint items appear, exit 1
        # (findings present).
        code = cli.main(["--json", "-s", name, "lint", "--package-lint",
                         "--archive-dir", str(archive), str(fixture)])
        out = json.loads(capsys.readouterr().out)
        assert code == 1 and out["ok"] is False
        assert any(i["tool"] == "package-lint" for i in out["items"])

        # Human output tags the tool.
        code = cli.main(["-s", name, "lint", "--package-lint",
                         "--archive-dir", str(archive), str(fixture)])
        human = capsys.readouterr().out
        assert "[package-lint]" in human

        # --archive-dir without --package-lint is a loud usage-ish error
        # (ElateError -> exit 1), network never touched.
        code = cli.main(["--json", "-s", name, "lint",
                         "--archive-dir", str(archive), str(fixture)])
        out = json.loads(capsys.readouterr().out)
        assert code == 1 and out["ok"] is False
        assert "--package-lint" in out["error"]


@pytest.mark.skip(reason=(
    "network-gated: installs the REAL package-lint from live MELPA; "
    "the default suite is hermetic and must not hit the network. "
    "Unskip and run manually to qualify against the real package-lint."))
def test_lint_package_lint_live_melpa(
        elate_home: str, tmp_path: Path) -> None:  # pragma: no cover
    # Opt-in WITHOUT --archive-dir: configures the standard archives and
    # refreshes them over the network. Deliberately skipped by default.
    fixture = tmp_path / "plfix.el"
    fixture.write_text(PL_FIXTURE_EL, encoding="utf-8")
    with _pl_session(f"{NAME}pllive") as session:
        data = S.lint_files(session, [str(fixture)], package_lint=True)
        assert any(i["tool"] == "package-lint" for i in data["items"])


# -- faces / text properties / overlays -------------------------------------------

PROPS_SETUP = (
    '(progn'
    ' (with-current-buffer (get-buffer-create "elprops")'
    '  (erase-buffer)'
    '  (emacs-lisp-mode)'
    '  (insert "(defun props-fn ()\\n  \\"Doc string.\\"\\n  nil)\\n"))'
    ' t)'
)

OVERLAY_SETUP = (
    '(progn'
    ' (with-current-buffer (get-buffer-create "elover")'
    '  (erase-buffer) (fundamental-mode)'
    '  (insert "abcdefghij")'
    '  (remove-overlays)'
    '  (let ((ov (make-overlay 2 6)))'
    '   (overlay-put ov \'face \'highlight)'
    '   (overlay-put ov \'before-string "[B]")'
    '   (overlay-put ov \'after-string "[A]")'
    '   (overlay-put ov \'priority 7))'
    '  (put-text-property 7 10 \'face \'(:weight bold))'
    '  (put-text-property 1 3 \'invisible t))'
    ' t)'
)


def test_buffer_props_font_lock_runs(sess: S.Session) -> None:
    assert sess.semantic().eval_form(PROPS_SETUP)["error"] is None
    data = sess.semantic().rpc("buffer", "elprops", None, None, True)
    props = data["props"]
    assert props["truncated"] is False
    runs = props["runs"]
    # Run-length encoded: contiguous, far fewer runs than characters.
    assert runs[0]["start"] == 1
    for prev, nxt in zip(runs, runs[1:]):
        assert prev["end"] == nxt["start"]
    assert len(runs) < len(data["text"])
    keyword = next(r for r in runs if r["text"] == "defun")
    assert keyword["face"] == ["font-lock-keyword-face"]
    assert keyword["line"] == 1
    name = next(r for r in runs if r["text"] == "props-fn")
    assert name["face"] == ["font-lock-function-name-face"]
    doc = next(r for r in runs if "Doc string." in r["text"])
    assert any("doc" in f for f in doc["face"])
    # Unfaced gaps carry no face key at all.
    gap = next(r for r in runs if r["text"] == " ")
    assert "face" not in gap
    # Without the flag, no props payload is computed.
    plain = sess.semantic().rpc("buffer", "elprops")
    assert "props" not in plain and "overlays" not in plain


def test_buffer_props_overlays_and_anonymous_face(sess: S.Session) -> None:
    assert sess.semantic().eval_form(OVERLAY_SETUP)["error"] is None
    data = sess.semantic().rpc("buffer", "elover", None, None, True)
    ov = next(o for o in data["overlays"] if o["start"] == 2)
    assert ov["end"] == 6
    assert ov["face"] == ["highlight"]
    assert ov["before-string"] == "[B]"
    assert ov["after-string"] == "[A]"
    assert ov["priority"] == "7"
    assert data["overlays-truncated"] is False
    runs = data["props"]["runs"]
    inv = next(r for r in runs if r["start"] == 1)
    assert inv["end"] == 3 and inv["invisible"] == "t"
    anon = next(r for r in runs if r["start"] == 7)
    assert anon["end"] == 10
    assert ":weight bold" in anon["face"][0]


def test_buffer_props_zwj_cluster_face_boundary(sess: S.Session) -> None:
    # REVIEW-phase4 unverified suspicion 1: a face boundary in the middle
    # of a multi-codepoint grapheme cluster (woman + ZWJ + rocket). The
    # RLE walks raw buffer positions, so the boundary may legitimately
    # fall inside the cluster -- the runs must stay contiguous, monotone,
    # and put the face on exactly the requested codepoint.
    sem = sess.semantic()
    assert sem.eval_form(
        '(progn (with-current-buffer (get-buffer-create "elzwj")'
        ' (erase-buffer) (fundamental-mode)'
        ' (insert "ab\\U0001F469\\u200D\\U0001F680cd")'  # ab + 👩‍🚀 + cd
        " (put-text-property 3 4 'face 'bold))"  # face on the WOMAN cp only
        " t)")["error"] is None
    data = sem.rpc("buffer", "elzwj", None, None, True)
    runs = data["props"]["runs"]
    assert runs[0]["start"] == 1
    for prev, nxt in zip(runs, runs[1:]):
        assert prev["end"] == nxt["start"]
    assert runs[-1]["end"] == 8  # 2 + 3 codepoints + 2, eob position
    bold = next(r for r in runs if r.get("face") == ["bold"])
    assert (bold["start"], bold["end"]) == (3, 4)
    assert bold["text"] == "\U0001F469"


def test_buffer_props_range_only(sess: S.Session) -> None:
    assert sess.semantic().eval_form(PROPS_SETUP)["error"] is None
    data = sess.semantic().rpc("buffer", "elprops", 2, 2, True)
    assert data["text"] == '  "Doc string."\n'
    runs = data["props"]["runs"]
    assert all(r["line"] == 2 for r in runs)
    assert any("font-lock-doc-face" in (r.get("face") or []) for r in runs)


def test_faces_at_point_query(sess: S.Session) -> None:
    assert sess.semantic().eval_form(PROPS_SETUP)["error"] is None
    data = sess.semantic().rpc("faces-at", 1, 2, "elprops")
    assert data["char"] == "e"
    assert data["face"] == ["font-lock-keyword-face"]
    assert data["button"] is False and data["keymap"] is False
    assert "face" in data["properties"]
    # Overlay resolution: char-face sees the overlay, face stays textual.
    # (Own fixture buffer: invisible text shifts move-to-column targets.)
    assert sess.semantic().eval_form(
        '(progn (with-current-buffer (get-buffer-create "elface")'
        ' (erase-buffer) (fundamental-mode) (insert "abcdefghij")'
        " (remove-overlays)"
        " (overlay-put (make-overlay 2 6) 'face 'highlight)) t)"
    )["error"] is None
    data = sess.semantic().rpc("faces-at", 1, 3, "elface")
    assert data["char"] == "d"
    assert data["char-face"] == ["highlight"]
    assert data["face"] is None
    ov = next(o for o in data["overlays"] if o["start"] == 2)
    assert ov["face"] == ["highlight"]
    # Past EOL clamps to end of line; line 1 col 999 in elface -> eob.
    data = sess.semantic().rpc("faces-at", 1, 999, "elface")
    assert data["char"] is None  # end of buffer


def test_cli_faces_at_and_buffer_props(sess: S.Session,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    assert sess.semantic().eval_form(PROPS_SETUP)["error"] is None
    assert cli.main(["--json", "-s", NAME, "faces-at", "1:1",
                     "--buffer", "elprops"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["face"] == ["font-lock-keyword-face"]
    assert cli.main(["-s", NAME, "buffer", "elprops", "--props"]) == 0
    human = capsys.readouterr().out
    assert "property runs" in human and "font-lock-keyword-face" in human
    code = cli.main(["--json", "-s", NAME, "faces-at", "nonsense"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "LINE:COL" in out["error"]


# -- popups ------------------------------------------------------------------------

def popup_kinds(sess: S.Session) -> list[str]:
    return [p["kind"] for p in sess.semantic().rpc("popups")["popups"]]


def test_popups_empty_baseline(sess: S.Session) -> None:
    sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (delete-other-windows) t)')
    S.wait_idle(sess, timeout=10.0)
    assert sess.semantic().rpc("popups")["popups"] == []
    assert sess.semantic().rpc("state")["popups"] == []


def test_popup_transient(sess: S.Session) -> None:
    data = sess.semantic().eval_form(
        "(progn (require 'transient)"
        " (transient-define-prefix elfix-transient ()"
        '  ["Fixture actions"'
        '   ("a" "alpha action" ignore)'
        '   ("b" "beta action" ignore)])'
        " t)")
    assert data["error"] is None
    assert sess.semantic().eval_form("(elfix-transient)")["error"] is None
    try:
        pops = sess.semantic().rpc("popups")["popups"]
        tr = next(p for p in pops if p["kind"] == "transient")
        assert "alpha action" in tr["text"]
        assert "beta action" in tr["text"]
        # state flags the popup so an AI knows to look.
        assert "transient" in sess.semantic().rpc("state")["popups"]
    finally:
        sess.raw().send_kbd("C-g")
        S.wait_idle(sess, timeout=10.0)
    assert "transient" not in popup_kinds(sess)


def test_popup_which_key(sess: S.Session) -> None:
    data = sess.semantic().eval_form(
        "(progn (require 'which-key)"
        " (setq which-key-idle-delay 0.05)"
        " (which-key-mode 1) t)")
    if data["error"]:
        pytest.skip(f"which-key not available: {data['error']}")
    try:
        sess.semantic().rpc("keys", "C-x", "events")
        pops: list[dict] = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            pops = sess.semantic().rpc("popups")["popups"]
            if any(p["kind"] == "which-key" for p in pops):
                break
            time.sleep(0.2)
        wk = next(p for p in pops if p["kind"] == "which-key")
        # which-key renders "KEY : COMMAND" columns; the C-x map is big,
        # so page 1 contents vary -- assert the format, not one binding.
        assert " : " in wk["text"]
        assert "prefix" in wk["text"] or "-" in wk["text"]
    finally:
        sess.raw().send_kbd("C-g")
        sess.semantic().eval_form("(which-key-mode -1)")
        S.wait_idle(sess, timeout=10.0)
    assert "which-key" not in popup_kinds(sess)


def test_popup_childframe(sess: S.Session) -> None:
    data = sess.semantic().eval_form(
        "(progn"
        ' (with-current-buffer (get-buffer-create "elchild")'
        '  (erase-buffer) (insert "childframe-marker-text"))'
        " (defvar elfix-child-frame nil)"
        " (setq elfix-child-frame"
        "  (make-frame (list (cons 'parent-frame (selected-frame))"
        "                    (cons 'minibuffer nil)"
        "                    (cons 'width 30) (cons 'height 4)"
        "                    (cons 'left 2) (cons 'top 2))))"
        " (set-window-buffer (frame-root-window elfix-child-frame)"
        '  (get-buffer "elchild"))'
        " (make-frame-visible elfix-child-frame) t)")
    if data["error"]:
        pytest.skip(f"child frames not supported here: {data['error']}")
    try:
        pops = sess.semantic().rpc("popups")["popups"]
        cf = next(p for p in pops if p["kind"] == "childframe")
        assert "childframe-marker-text" in cf["text"]
        assert cf["buffer"] == "elchild"
        assert "childframe" in sess.semantic().rpc("state")["popups"]
    finally:
        sess.semantic().eval_form(
            "(progn (when (frame-live-p elfix-child-frame)"
            ' (delete-frame elfix-child-frame))'
            ' (kill-buffer "elchild") t)')
    assert "childframe" not in popup_kinds(sess)


def test_popup_completion_preview(sess: S.Session) -> None:
    data = sess.semantic().eval_form(
        '(progn (switch-to-buffer "*scratch*") (erase-buffer)'
        " (completion-preview-mode 1) t)")
    if data["error"]:
        pytest.skip(f"completion-preview not available: {data['error']}")
    try:
        sess.semantic().rpc("keys", "( d e f u", "macro")
        pops = sess.semantic().rpc("popups")["popups"]
        cp = next(p for p in pops if p["kind"] == "completion-preview")
        # The preview suggests a completion suffix for "defu" (e.g. "n").
        assert cp["text"].strip()
    finally:
        sess.semantic().eval_form(
            '(progn (with-current-buffer "*scratch*"'
            " (completion-preview-mode -1) (erase-buffer)) t)")
    assert "completion-preview" not in popup_kinds(sess)


def test_cli_popups_command(sess: S.Session,
                            capsys: pytest.CaptureFixture[str]) -> None:
    S.wait_idle(sess, timeout=10.0)
    assert cli.main(["-s", NAME, "popups"]) == 0
    assert "no popups" in capsys.readouterr().out
    assert sess.semantic().eval_form("(elfix-transient)")["error"] is None
    try:
        assert cli.main(["-s", NAME, "popups"]) == 0
        human = capsys.readouterr().out
        assert "== transient" in human and "alpha action" in human
        assert cli.main(["--json", "-s", NAME, "popups"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert [p["kind"] for p in out["popups"]] == ["transient"]
        # The human state output points at the popups command.
        assert cli.main(["-s", NAME, "state"]) == 0
        assert "popups: transient" in capsys.readouterr().out
    finally:
        sess.raw().send_kbd("C-g")
        S.wait_idle(sess, timeout=10.0)
