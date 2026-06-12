"""Phase 6 integration tests: profiler, benchmark, clean-install, examples.

Same conventions as the other integration suites: one module-scoped TTY
session against a real Emacs in a real tmux for the profiler/bench
tests; clean-install boots its own short-lived sessions (the config
mode is the thing under test); the committed examples/*.json recipes
run via the script runner.
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
from elate import script as SC
from elate import session as S
from elate.errors import ElateError, RpcError

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux") and shutil.which("emacsclient")
)

pytestmark = pytest.mark.skipif(
    not HAVE_DEPS, reason="emacs, emacsclient, and tmux are required"
)

NAME = f"p{os.getpid()}"

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

# A deterministic ~0.5s CPU burn: wall-clock bound, so the sample count
# is stable across machines (unlike a fixed iteration count).
BUSY_DEFUN = (
    "(defun pfix-busy () "
    " (let ((t0 (float-time)) (s 0))"
    "  (while (< (- (float-time) t0) 0.5) (setq s (1+ s)))"
    "  s))"
)

ALLOC_DEFUN = (
    "(defun pfix-alloc () "
    " (let (l) (dotimes (_ 200) (push (make-list 1000 t) l)) (length l)))"
)

ELPKG = """\
;;; elpkg.el --- elate clean-install fixture -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.3
;; Package-Requires: ((emacs "27.1"))

;;; Commentary:
;; A tiny valid package: one autoloaded command.

;;; Code:

;;;###autoload
(defun elpkg-greet ()
  "Insert a greeting at point."
  (interactive)
  (insert "elpkg says hi"))

(provide 'elpkg)
;;; elpkg.el ends here
"""

# Calls an undefined function -> the byte-compile of the installed copy
# emits a warning, which clean-install must surface in its notes.
WARNPKG = """\
;;; warnpkg.el --- elate clean-install warning fixture -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.1

;;; Commentary:
;; Produces a byte-compile warning when installed.

;;; Code:

(defun warnpkg-go ()
  "Call a function nothing defines."
  (warnpkg-no-such-helper 1))

(provide 'warnpkg)
;;; warnpkg.el ends here
"""

BROKENPKG = """\
;;; brokenpkg.el --- elate broken-dep fixture -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.1
;; Package-Requires: ((emacs "27.1") (no-such-dep "1.0"))

;;; Commentary:
;; Depends on a package no archive can provide (the sandbox is offline).

;;; Code:

(provide 'brokenpkg)
;;; brokenpkg.el ends here
"""

# Genuinely lacks the lexical-binding cookie: on Emacs >= 30 the byte
# compiler warns about the installed copy, and that REAL warning must
# survive elate's spurious-warning filter (REVIEW-CI1 finding 1).
CKLPKG = """\
;;; cklpkg.el --- elate cookie-less fixture

;; Author: elate <elate@example.com>
;; Version: 0.1

;;; Commentary:
;; Deliberately has no lexical-binding cookie on its first line.

;;; Code:

(defun cklpkg-go ()
  "Return a marker string."
  "cklpkg here")

(provide 'cklpkg)
;;; cklpkg.el ends here
"""

# No Version header: package-buffer-info rejects it outright.
MALFORMEDPKG = """\
;;; malformedpkg.el --- elate malformed fixture -*- lexical-binding: t; -*-
;;; Commentary:
;;; Code:
(provide 'malformedpkg)
;;; malformedpkg.el ends here
"""

# Requires an Emacs from the future: the dep pre-check must name it.
FUTUREPKG = """\
;;; futurepkg.el --- elate emacs-version fixture -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.1
;; Package-Requires: ((emacs "99.1"))

;;; Commentary:
;;; Code:

(provide 'futurepkg)
;;; futurepkg.el ends here
"""

# No Package-Requires at all: must install fine.
NOREQPKG = """\
;;; noreqpkg.el --- elate no-requires fixture -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.2

;;; Commentary:
;;; Code:

;;;###autoload
(defun noreqpkg-hi ()
  "Insert a marker."
  (interactive)
  (insert "noreqpkg here"))

(provide 'noreqpkg)
;;; noreqpkg.el ends here
"""

# Multi-file directory package: the main file requires the helper, so
# the install only works if the whole directory landed on load-path.
DIRPKG_MAIN = """\
;;; dirpkg.el --- elate directory-package fixture -*- lexical-binding: t; -*-

;; Author: elate <elate@example.com>
;; Version: 0.3
;; Package-Requires: ((emacs "27.1"))

;;; Commentary:
;;; Code:

(require 'dirpkg-extra)

;;;###autoload
(defun dirpkg-greet ()
  "Insert the helper's greeting at point."
  (interactive)
  (insert (dirpkg-extra-text)))

(provide 'dirpkg)
;;; dirpkg.el ends here
"""

DIRPKG_EXTRA = """\
;;; dirpkg-extra.el --- dirpkg helper library -*- lexical-binding: t; -*-
;;; Commentary:
;;; Code:

(defun dirpkg-extra-text ()
  "The greeting text."
  "dirpkg says hi")

(provide 'dirpkg-extra)
;;; dirpkg-extra.el ends here
"""


@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elate-p6-")
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
    tmp = Path(tempfile.mkdtemp(prefix="elate-p6fix-"))
    paths = {
        "elpkg": tmp / "elpkg.el",
        "warnpkg": tmp / "warnpkg.el",
        "broken": tmp / "brokenpkg.el",
        "malformed": tmp / "malformedpkg.el",
        "future": tmp / "futurepkg.el",
        "noreq": tmp / "noreqpkg.el",
        "cookieless": tmp / "cklpkg.el",
        "pkgdir": tmp / "dirpkg",
    }
    paths["elpkg"].write_text(ELPKG, encoding="utf-8")
    paths["warnpkg"].write_text(WARNPKG, encoding="utf-8")
    paths["cookieless"].write_text(CKLPKG, encoding="utf-8")
    paths["broken"].write_text(BROKENPKG, encoding="utf-8")
    paths["malformed"].write_text(MALFORMEDPKG, encoding="utf-8")
    paths["future"].write_text(FUTUREPKG, encoding="utf-8")
    paths["noreq"].write_text(NOREQPKG, encoding="utf-8")
    # A multi-file directory package (package-install-file's third
    # input kind); the main file requires the helper.
    paths["pkgdir"].mkdir()
    (paths["pkgdir"] / "dirpkg.el").write_text(DIRPKG_MAIN, encoding="utf-8")
    (paths["pkgdir"] / "dirpkg-extra.el").write_text(
        DIRPKG_EXTRA, encoding="utf-8")
    try:
        yield paths
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def sess(elate_home: str) -> Iterator[S.Session]:
    session = S.start_session(NAME, cols=100, rows=30)
    sem = session.semantic()
    assert sem.eval_form(BUSY_DEFUN)["error"] is None
    assert sem.eval_form(ALLOC_DEFUN)["error"] is None
    try:
        yield session
    finally:
        try:
            S.stop_session(NAME)
        except Exception:
            session.raw().kill_server()


def names_in_report(section: dict) -> set[str]:
    out = {f["name"] for f in section.get("functions") or []}

    def walk(nodes: list[dict]) -> None:
        for n in nodes:
            out.add(n["name"])
            walk(n.get("children") or [])

    walk(section.get("tree") or [])
    return out


# -- profiler -----------------------------------------------------------------

def test_profile_run_cpu(sess: S.Session) -> None:
    data = S.profile_run(sess, "(pfix-busy)", mode="cpu", timeout=15.0)
    assert data["mode"] == "cpu"
    assert data["sampling-interval"] > 0
    assert data["eval"]["error"] is None
    assert int(data["eval"]["value"]) > 0
    assert data["running"] is False
    assert "fresh session" in data["note"]
    cpu = data["cpu"]
    assert cpu["units"] == "samples"
    # ~0.5s of busy loop at the default 1ms sampling interval.
    assert cpu["total"] >= 50
    funcs = cpu["functions"]
    assert funcs, "no top functions in a busy profile"
    for f in funcs:
        assert {"name", "self", "total",
                "self-percent", "total-percent"} <= set(f)
        assert 0 <= f["self-percent"] <= 100
        assert 0 <= f["total-percent"] <= 100
        assert f["self"] <= f["total"]
    # Sorted by self count, descending.
    selves = [f["self"] for f in funcs]
    assert selves == sorted(selves, reverse=True)
    # The profiled function shows up (as total time somewhere in the
    # backtraces; interpreted bodies attribute self time to primitives).
    assert "pfix-busy" in names_in_report(cpu)
    busy = next(f for f in funcs if f["name"] == "pfix-busy")
    assert busy["total-percent"] > 10
    # Tree: nodes carry counts/percentages and truncation flags.
    assert cpu["tree"], "empty calltree"
    top = cpu["tree"][0]
    assert top["count"] > 0 and 0 <= top["percent"] <= 100
    assert "children" in top and "children-truncated" in top
    assert isinstance(cpu["tree-truncated"], bool)
    # One-shot stopped the profiler: a fresh start works immediately.
    S.profile_start(sess, "cpu")
    S.profile_stop(sess)


def test_profile_start_stop_report_cycle(sess: S.Session) -> None:
    started = S.profile_start(sess, "mem")
    assert started["started"] == "mem"
    assert started["sampling-interval"] is None
    assert sess.semantic().eval_form("(pfix-alloc)")["error"] is None
    # Report while running keeps the profiler going.
    live = S.profile_report(sess, depth=4)
    assert live["running"] is True
    assert live["mem"]["units"] == "bytes"
    assert live["mem"]["total"] > 0
    assert live["mem"]["depth"] == 4
    # More work after the live report must still be in the final report
    # (stock profiler-report would have discarded the drained samples).
    assert sess.semantic().eval_form("(pfix-alloc)")["error"] is None
    stopped = S.profile_stop(sess)
    assert stopped["stopped"] is True and stopped["mem"] is True
    assert stopped["cpu"] is False
    assert stopped["mem-bytes"] > live["mem"]["total"]
    final = S.profile_report(sess)
    assert final["running"] is False
    assert final["mem"]["total"] == stopped["mem-bytes"]
    assert "cpu" not in final  # only the profiled mode reports
    # The dominant allocator is named. (The *enclosing* interpreted
    # function need not appear: interpreted bodies push a frame per
    # form, and the profiler truncates backtraces at 16 frames -- same
    # as the stock profiler-report UI.)
    if int((sess.emacs_version or "30").split(".")[0]) >= 30:
        make_list = next(f for f in final["mem"]["functions"]
                         if f["name"] == "make-list")
        assert make_list["self"] > 1_000_000  # 400 x 1000-cons lists
    else:
        # Emacs 29's get_backtrace (eval.c) starts at the SECOND
        # backtrace frame, so the innermost frame -- the allocating
        # primitive itself -- is never recorded (the stock
        # profiler-report UI on 29 misses it the same way; Emacs 30
        # fixed the off-by-one). The dominant self entry is then
        # make-list's direct caller; assert the profile still names a
        # dominant allocator without pinning which frame survived.
        top = final["mem"]["functions"][0]
        assert top["self"] > 1_000_000, top
    # Stopping again is a no-op, not an error.
    again = S.profile_stop(sess)
    assert again["stopped"] is False


def test_profile_start_resets_and_guards(sess: S.Session) -> None:
    S.profile_start(sess, "cpu")
    with pytest.raises(RpcError, match="already running"):
        S.profile_start(sess, "cpu")
    S.profile_stop(sess)
    # A new start resets the previous logs: an immediate stop+report
    # reflects only the (nearly empty) new window.
    S.profile_start(sess, "mem")
    S.profile_stop(sess)
    data = S.profile_report(sess)
    assert "cpu" not in data
    assert data["mem"]["total"] < 1_000_000  # not the pfix-alloc megabytes
    with pytest.raises(ElateError, match="unknown profile mode"):
        S.profile_start(sess, "warp")
    with pytest.raises(ElateError, match="depth"):
        S.profile_report(sess, depth=0)


def max_tree_depth(nodes: list[dict], depth: int = 0) -> int:
    best = depth
    for n in nodes:
        best = max(best, max_tree_depth(n.get("children") or [], depth + 1))
    return best


def test_profile_deep_recursion_at_max_depth(sess: S.Session) -> None:
    # REVIEW-phase6 bug 1: json-serialize caps nesting at ~50 levels and
    # each calltree level costs two, so depth 24/32 reports on a
    # deep-recursion profile failed to encode (and `profile run`
    # discarded the whole result). The cap is now 20; the reviewer's
    # deep-recursion workload must report fine at the maximum depth.
    sem = sess.semantic()
    assert sem.eval_form(
        "(defun pfix-deep (n)"
        " (if (= n 0) (make-list 2000 t)"
        "  (cons (pfix-deep (1- n)) nil)))")["error"] is None
    data = S.profile_run(sess, "(dotimes (_ 500) (pfix-deep 40))",
                         mode="mem", timeout=30.0,
                         depth=S.PROFILE_MAX_DEPTH)
    assert data["eval"]["error"] is None
    mem = data["mem"]
    assert mem["total"] > 0
    assert mem["depth"] == S.PROFILE_MAX_DEPTH
    # The workload really produces a deep tree (the unified calltree
    # stitches the recursion chain), so the depth limit did real work
    # and the encode survived it.
    deepest = max_tree_depth(mem["tree"])
    assert 15 <= deepest <= S.PROFILE_MAX_DEPTH, deepest
    # Above the cap is rejected controller-side, never sent to Emacs.
    with pytest.raises(ElateError, match="between 1 and 20"):
        S.profile_report(sess, depth=S.PROFILE_MAX_DEPTH + 1)
    # Direct report at the max works on the same logs.
    again = S.profile_report(sess, depth=S.PROFILE_MAX_DEPTH)
    assert again["mem"]["total"] == mem["total"]


def test_cli_profile_report_mode_filter_is_loud(
        sess: S.Session, capsys: pytest.CaptureFixture[str]) -> None:
    # REVIEW-phase6 robustness 2: filtering the report down to a section
    # that was never collected used to be a silent empty success.
    S.profile_start(sess, "cpu")
    assert sess.semantic().eval_form("(pfix-busy)")["error"] is None
    S.profile_stop(sess)
    assert cli.main(["--json", "-s", NAME, "profile", "report", "--cpu"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and "cpu" in out and "mem" not in out
    code = cli.main(["--json", "-s", NAME, "profile", "report", "--mem"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert "no mem profile data" in out["error"]
    assert "profile start --mem" in out["error"]


def test_cli_profile_options_on_wrong_action_are_loud(
        sess: S.Session, capsys: pytest.CaptureFixture[str]) -> None:
    # REVIEW-phase6 code quality: --depth/--timeout on actions they
    # cannot affect must be loud, like the form check.
    code = cli.main(["--json", "-s", NAME, "profile", "stop", "--depth", "4"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "--depth applies" in out["error"]
    code = cli.main(["--json", "-s", NAME, "profile", "report",
                     "--timeout", "5"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "--timeout applies" in out["error"]


def test_profile_report_without_data_is_clean(elate_home: str) -> None:
    fresh = S.start_session(f"{NAME}rep", config="bare", cols=80, rows=24)
    try:
        with pytest.raises(RpcError, match="no profiler data"):
            S.profile_report(fresh)
    finally:
        S.stop_session(fresh.name)


def test_cli_profile_commands(sess: S.Session,
                              capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--json", "-s", NAME, "profile", "run", "(pfix-busy)"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["cpu"]["total"] > 0
    # Human rendering names the sections and the profiled function.
    assert cli.main(["-s", NAME, "profile", "run", "(pfix-busy)", "--cpu"]) == 0
    human = capsys.readouterr().out
    assert "== CPU:" in human and "top functions" in human
    assert "calltree" in human and "pfix-busy" in human
    # start/stop/report cycle through the CLI.
    assert cli.main(["-s", NAME, "profile", "start", "--mem"]) == 0
    assert "profiler started (mem)" in capsys.readouterr().out
    assert cli.main(["-s", NAME, "profile", "stop"]) == 0
    assert "profiler stopped" in capsys.readouterr().out
    assert cli.main(["--json", "-s", NAME, "profile", "report"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    # A form on a non-run action is a loud error.
    assert cli.main(["--json", "-s", NAME, "profile", "start", "(+ 1 1)"]) == 1
    assert "only 'profile run'" in json.loads(capsys.readouterr().out)["error"]
    # run without a form is a loud error.
    assert cli.main(["--json", "-s", NAME, "profile", "run"]) == 1
    assert "needs a form" in json.loads(capsys.readouterr().out)["error"]
    # An eval error during run: exit 1, ok false, report still attached.
    assert cli.main(["--json", "-s", NAME, "profile", "run",
                     '(error "prof-boom")']) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["eval"]["error"] == "prof-boom"
    assert out["eval"]["backtrace"]
    assert "cpu" in out  # the profile up to the error is reported


# -- benchmark ----------------------------------------------------------------

def test_bench_result_fields(sess: S.Session) -> None:
    data = S.bench_form(sess, "(make-list 1000 t)", repetitions=200)
    assert data["repetitions"] == 200
    assert data["compiled"] is True
    assert data["compile-error"] is None
    assert data["error"] is None
    assert data["elapsed"] > 0
    assert data["mean"] == pytest.approx(data["elapsed"] / 200)
    assert data["gc-runs"] >= 0 and data["gc-elapsed"] >= 0
    assert data["gcs-done-delta"] >= data["gc-runs"]
    deltas = data["memory-deltas"]
    assert {"conses", "floats", "vector-cells", "symbols",
            "string-chars", "intervals", "strings"} <= set(deltas)
    # 200 x 1000-element lists: at least 200k conses were allocated.
    assert deltas["conses"] >= 200_000
    assert "fresh session" in data["note"]


def test_bench_repetition_bounds(sess: S.Session) -> None:
    with pytest.raises(ElateError, match="repetitions"):
        S.bench_form(sess, "1", repetitions=0)
    with pytest.raises(ElateError, match="repetitions"):
        S.bench_form(sess, "1", repetitions=1_000_001)
    assert S.bench_form(sess, "1", repetitions=1)["repetitions"] == 1


def test_bench_error_and_timeout(sess: S.Session) -> None:
    data = S.bench_form(sess, '(error "bench-boom")')
    assert data["error"] == "bench-boom"
    assert "bench-boom" in data["backtrace"]
    assert "elapsed" not in data  # no timing for a failed run
    # Timer-servicing slow form: the in-Emacs timeout interrupts it.
    t0 = time.monotonic()
    data = S.bench_form(sess, "(sleep-for 30)", timeout=2.0)
    assert time.monotonic() - t0 < 10.0
    assert "timed out" in data["error"]
    # The session stays usable.
    assert sess.semantic().eval_form("(+ 1 2)")["value"] == "3"


def test_bench_interpreted_fallback(sess: S.Session) -> None:
    # Disable the byte compiler in-session: the agent must fall back to
    # benchmarking the interpreted closure and say so.
    sem = sess.semantic()
    assert sem.eval_form(
        "(advice-add 'byte-compile :override"
        ' (lambda (&rest _) (error "p6: compiler disabled"))'
        " '((name . p6-disable)))")["error"] is None
    try:
        data = S.bench_form(sess, "(make-list 10 t)", repetitions=5)
        assert data["compiled"] is False
        assert "compiler disabled" in data["compile-error"]
        assert data["error"] is None
        assert data["elapsed"] >= 0  # the interpreted run still happened
    finally:
        assert sem.eval_form(
            "(advice-remove 'byte-compile 'p6-disable)")["error"] is None
        # Belt and braces: a fresh compile works again.
        check = S.bench_form(sess, "1")
        assert check["compiled"] is True


def test_cli_bench_command(sess: S.Session,
                           capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["-s", NAME, "bench", "(make-list 100 t)", "-n", "50"]) == 0
    human = capsys.readouterr().out
    assert "50 repetition(s)" in human and "byte-compiled" in human
    assert "GC:" in human and "allocations:" in human and "conses" in human
    code = cli.main(["--json", "-s", NAME, "bench", '(error "cli-bench-boom")'])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert out["error"] == "cli-bench-boom" and out["backtrace"]
    code = cli.main(["--json", "-s", NAME, "bench", "1", "-n", "0"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "repetitions" in out["error"]


# -- clean-install -------------------------------------------------------------

def test_clean_install_happy_path(elate_home: str,
                                  fixtures: dict[str, Path]) -> None:
    name = f"{NAME}ci"
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["elpkg"]),
                                  str(fixtures["warnpkg"])],
                           cols=90, rows=24)
    try:
        assert sess.init_error() is None
        sem = sess.semantic()
        # The autoloaded command exists BEFORE the feature was required:
        # exactly what load-path injection cannot verify.
        data = sem.eval_form(
            "(list (featurep 'elpkg) (fboundp 'elpkg-greet)"
            " (commandp 'elpkg-greet)"
            " (autoloadp (symbol-function 'elpkg-greet)))")
        assert data["error"] is None
        assert data["value"] == "(nil t t t)"
        # Invoking it like a user loads the installed (byte-compiled) copy.
        data = sem.eval_form(
            '(progn (switch-to-buffer "*scratch*") (erase-buffer)'
            " (elpkg-greet) (buffer-string))")
        assert "elpkg says hi" in data["value"]
        assert sem.eval_form("(featurep 'elpkg)")["value"] == "t"
        # load-path correctness: the feature resolves from the sandbox
        # elpa, and the installed copy was byte-compiled.
        lib = sem.eval_form('(locate-library "elpkg")')["value"].strip('"')
        elpa = str(sess.dir / "elpa")
        assert lib.startswith(elpa) and lib.endswith(".elc")
        assert list((sess.dir / "elpa").glob("elpkg-0.3/elpkg-autoloads.el"))
        # info exposes where the packages were installed + warnings.
        info = S.session_info(name)
        assert info["package_user_dir"] == elpa
        installed = {p["name"]: p for p in info["installed"]}
        assert installed["elpkg"]["version"] == "0.3"
        assert installed["elpkg"]["dir"].startswith(elpa)
        assert installed["elpkg"]["warnings"] == []
        # The warning-producing package surfaces its byte-compile warning.
        assert any("warnpkg-no-such-helper" in w
                   for w in installed["warnpkg"]["warnings"])
        # state exposes the clean-install facts too.
        state = sem.rpc("state")
        assert state["clean-install"]["package-user-dir"] == elpa
        assert set(state["clean-install"]["packages"]) == {"elpkg", "warnpkg"}
    finally:
        S.stop_session(name)
    # info works on the stopped session as well (file-based).
    info = S.session_info(name)
    assert any(p["name"] == "elpkg" for p in info["installed"])


def test_clean_install_real_cookie_warning_survives_filter(
        elate_home: str, fixtures: dict[str, Path]) -> None:
    # REVIEW-CI1 finding 1: the spurious-lexical-warning filter used to
    # fund its removal budget from files on disk (the generated
    # NAME-pkg.el) instead of from whether THIS Emacs emits the spurious
    # warning. On Emacs >= 31 the -pkg.el produces no warning (bytecomp
    # checks no-byte-compile first), so the budget ate the package's
    # own genuine, file-context-free missing-cookie warning. The budget
    # is now gated to Emacs 30.x, the only major with the
    # warn-before-no-byte-compile ordering.
    name = f"{NAME}ckl"
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["cookieless"])],
                           cols=90, rows=24)
    try:
        assert sess.init_error() is None
        # The package installs and works regardless of the warning.
        assert sess.semantic().eval_form(
            "(progn (require 'cklpkg) (cklpkg-go))")["value"] \
            == '"cklpkg here"'
        installed = {p["name"]: p
                     for p in S.session_info(name)["installed"]}
        cookie_warns = [w for w in installed["cklpkg"]["warnings"]
                        if "lexical-binding" in w]
        major = int((sess.emacs_version or "30").split(".")[0])
        if major >= 30:
            # 30.x: the real warning AND the spurious -pkg.el one both
            # arrive; the filter removes exactly the spurious one.
            # >= 31: only the real warning arrives (no-byte-compile is
            # checked first) and the budget must not be funded.
            # Either way exactly the genuine warning survives.
            assert len(cookie_warns) == 1, installed["cklpkg"]["warnings"]
        else:
            # Emacs 29's bytecomp has no missing-cookie warning at all.
            assert cookie_warns == [], installed["cklpkg"]["warnings"]
    finally:
        S.stop_session(name)


def test_clean_install_directory_package(elate_home: str,
                                         fixtures: dict[str, Path]) -> None:
    name = f"{NAME}cid"
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["pkgdir"])],
                           cols=90, rows=24)
    try:
        assert sess.init_error() is None
        sem = sess.semantic()
        assert sem.eval_form(
            "(and (fboundp 'dirpkg-greet)"
            " (autoloadp (symbol-function 'dirpkg-greet)))")["value"] == "t"
        # Multi-file: invoking the autoload loads the main file, whose
        # (require 'dirpkg-extra) must resolve from the installed dir.
        data = sem.eval_form(
            '(progn (switch-to-buffer "*scratch*") (erase-buffer)'
            " (dirpkg-greet) (buffer-string))")
        assert "dirpkg says hi" in data["value"]
        assert sem.eval_form("(featurep 'dirpkg-extra)")["value"] == "t"
    finally:
        S.stop_session(name)


def test_clean_install_emacs_version_requirement(
        elate_home: str, fixtures: dict[str, Path]) -> None:
    name = f"{NAME}cifu"
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["future"])],
                           cols=90, rows=24)
    try:
        err = sess.init_error()
        assert err is not None
        assert "missing dependencies" in err
        assert "emacs 99.1" in err and "this is" in err
    finally:
        S.stop_session(name)


def test_clean_install_no_requires_and_duplicate_load_dedup(
        elate_home: str, fixtures: dict[str, Path]) -> None:
    name = f"{NAME}cinq"
    # No Package-Requires header at all: installs fine. The same
    # package given twice installs (and is recorded) once.
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["noreq"]),
                                  str(fixtures["noreq"])],
                           cols=90, rows=24)
    try:
        assert sess.init_error() is None
        info = S.session_info(name)
        assert [p["name"] for p in info["installed"]] == ["noreqpkg"]
        assert sess.semantic().eval_form(
            "(fboundp 'noreqpkg-hi)")["value"] == "t"
    finally:
        S.stop_session(name)


def test_init_file_config_conflicts_are_loud(
        elate_home: str, fixtures: dict[str, Path], tmp_path: Path) -> None:
    # REVIEW-phase6 bug 2: an explicit bare/clean-install config used to
    # be silently downgraded to init-file -- the --load targets were
    # load-path injected instead of installed, faking exactly the checks
    # clean-install exists for.
    init = tmp_path / "myinit.el"
    init.write_text(";; -*- lexical-binding: t; -*-\n", encoding="utf-8")
    for config in ("bare", "clean-install"):
        with pytest.raises(ElateError, match="conflicts"):
            S.start_session(f"{NAME}cfl", config=config,
                            init_file=str(init), cols=80, rows=24)
    # Nothing half-built left behind, and scripts reject it at load time.
    assert not (Path(elate_home) / "sessions" / f"{NAME}cfl").exists()
    with pytest.raises(ElateError, match="conflicts"):
        SC.validate_script({
            "session": {"config": "clean-install", "init_file": "x.el",
                        "load": ["y.el"]},
            "steps": [{"eval": "1"}],
        })
    # The convenience default is untouched: init_file alone still
    # implies config init-file.
    sess = S.start_session(f"{NAME}cfl", init_file=str(init),
                           cols=80, rows=24)
    try:
        assert sess.config == "init-file"
    finally:
        S.stop_session(f"{NAME}cfl")


def test_clean_install_missing_dep_structured_error(
        elate_home: str, fixtures: dict[str, Path]) -> None:
    name = f"{NAME}cib"
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["broken"])],
                           cols=90, rows=24)
    try:
        err = sess.init_error()
        assert err is not None
        assert "missing dependencies" in err
        assert "no-such-dep (1.0)" in err
        assert "no network" in err
        # Nothing was installed; the session is up and usable regardless.
        assert S.session_info(name)["installed"] is None
        assert sess.semantic().eval_form("(+ 1 1)")["value"] == "2"
    finally:
        S.stop_session(name)


def test_clean_install_malformed_package(elate_home: str,
                                         fixtures: dict[str, Path]) -> None:
    name = f"{NAME}cim"
    sess = S.start_session(name, config="clean-install",
                           loads=[str(fixtures["malformed"])],
                           cols=90, rows=24)
    try:
        err = sess.init_error()
        assert err is not None  # package-buffer-info rejected the headers
        assert "version" in err.lower() or "header" in err.lower()
    finally:
        S.stop_session(name)


def test_clean_install_requires_load(elate_home: str) -> None:
    with pytest.raises(ElateError, match="clean-install.*--load"):
        S.start_session(f"{NAME}cino", config="clean-install",
                        cols=80, rows=24)
    # Nothing half-built is left behind.
    assert not (Path(elate_home) / "sessions" / f"{NAME}cino").exists()


def test_clean_install_in_scenario_script(elate_home: str,
                                          fixtures: dict[str, Path],
                                          tmp_path: Path) -> None:
    # The Phase-5 prediction: clean-install slots into the script
    # "session" block unchanged.
    script = {
        "session": {"config": "clean-install", "size": "90x24",
                    "load": ["./elpkg.el"]},
        "steps": [
            {"assert": {"eval": "(and (fboundp 'elpkg-greet)"
                                " (not (featurep 'elpkg)))"}},
            {"keys": "M-x elpkg-greet RET"},
            {"wait": "text", "pattern": "elpkg says hi"},
            {"assert": {"eval": "(featurep 'elpkg)"}},
        ],
    }
    shutil.copy(fixtures["elpkg"], tmp_path / "elpkg.el")
    path = tmp_path / "ci.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    sc, base = SC.load_script(path)
    result = SC.run_script(sc, base_dir=base)
    assert result["success"] is True, result
    # A broken-dep package fails the run via the init_error contract.
    bad = dict(script, session={"config": "clean-install", "size": "90x24",
                                "load": ["./brokenpkg.el"]})
    shutil.copy(fixtures["broken"], tmp_path / "brokenpkg.el")
    path.write_text(json.dumps(bad), encoding="utf-8")
    sc, base = SC.load_script(path)
    result = SC.run_script(sc, base_dir=base)
    assert result["success"] is False
    assert "missing dependencies" in result["error"]
    assert all(r["status"] == "not-run" for r in result["steps"])


# -- committed example recipes ---------------------------------------------------

@pytest.mark.parametrize("example", sorted(
    p.name for p in EXAMPLES_DIR.glob("*.json")))
def test_example_scripts_run_green(elate_home: str, example: str,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--json", "run", str(EXAMPLES_DIR / example)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["success"] is True
    assert out["failed"] == 0 and out["not_run"] == 0


def test_examples_exist() -> None:
    # The README recipes point at these by name.
    present = {p.name for p in EXAMPLES_DIR.glob("*.json")}
    assert {"transient-menu.json", "font-lock.json",
            "drive-dired.json", "clean-install.json"} <= present
