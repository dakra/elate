"""S-expression scenarios (.eld): the Emacs-side reader, the writer,
`elate convert`, and running .eld scenarios (file, include, stdin)."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from elate import cli, eld
from elate import script as SC
from elate.errors import ElateError

ROOT = Path(__file__).resolve().parent.parent
HAVE_EMACS = bool(shutil.which("emacs"))
HAVE_DEPS = bool(HAVE_EMACS and shutil.which("tmux")
                 and shutil.which("emacsclient"))
pytestmark = pytest.mark.skipif(not HAVE_EMACS, reason="needs emacs")
needs_session = pytest.mark.skipif(not HAVE_DEPS, reason="needs emacs + tmux")


@pytest.fixture()
def elate_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="el-", dir="/tmp"))
    monkeypatch.setenv("ELATE_HOME", str(tmp))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read(tmp_path: Path, text: str) -> object:
    path = tmp_path / "s.eld"
    path.write_text(text, encoding="utf-8")
    return eld.load(path, name="s.eld")


def test_mapping(tmp_path: Path):
    value = _read(tmp_path, """;; leading comment
(:name "demo é"
 :session (:ui "tty" :eval ((require 'cl-lib) (setq x ?a)) :size [80 24])
 :params #s(hash-table)
 :steps ((:keys "C-x C-f")      ; trailing comment
         (:eval (progn
                  ;; kept: inside the form
                  (insert "{{name}}")
                  (setq y {{n}})))
         (:wait idle :timeout 2.5)
         (:assert (:eval #'ignore))
         (:flags (:no :false :none nil :yes t :null :null :kw :foo))
         (:list ("a" 1) :empty () :vec [] :heads [t nil] :quoted 'x)
         (:with-hyphen 1 :odd\\ key 2)))
""")
    assert value == {
        "name": "demo é",
        "session": {"ui": "tty",
                    "eval": ["(require 'cl-lib)", "(setq x ?a)"],
                    "size": [80, 24]},
        "params": {},
        "steps": [
            {"keys": "C-x C-f"},
            {"eval": '(progn\n                  ;; kept: inside the form\n'
                     '                  (insert "{{name}}")\n'
                     '                  (setq y {{n}}))'},
            {"wait": "idle", "timeout": 2.5},
            {"assert": {"eval": "#'ignore"}},
            {"flags": {"no": False, "none": None, "yes": True, "null": None,
                       "kw": "foo"}},
            {"list": ["a", 1], "empty": None, "vec": [], "heads": [True, None],
             "quoted": "'x"},
            {"with-hyphen": 1, "odd key": 2},
        ]}


@pytest.mark.parametrize(("text", "message"), [
    ("", "s.eld:1: no scenario form"),
    ("(:a 1)\n(:b 2)", "s.eld:2: text after the scenario form"),
    ("(steps)", "a scenario is one plist"),
    ("(:a 1\n :a 2)", "s.eld:2: duplicate key :a"),
    ("(:a\n (1 . 2))", "s.eld:2: a dotted pair"),
    ("(:a 1 :b)", "key :b has no value"),
    ("(:a 1\n b 2)", "s.eld:2: expected a keyword key, got b"),
    ('(:a\n\n "x', "s.eld:3: unterminated"),
    ("(:a ])", "unbalanced"),
    ("(:a #s(hash-table data (k 1)))", "write an object as a plist"),
    ("(:a (1 .'x))", "a dotted pair"),
    ("(:a [1 . 2])", "a dotted pair"),
    ("(:a 1 . 2)", "a dotted pair"),
    ('(:a\n "\\M-x")', "s.eld:2: .* holds raw bytes"),
    ("(:a 1.0e+INF)", "s.eld:1: 1.0e\\+INF has no JSON equivalent"),
    ('(:a 1 "x\ny" 2)', 'got "x\ny"'),
])
def test_errors_name_the_line(tmp_path: Path, text: str, message: str):
    with pytest.raises(ElateError, match=message.replace("(", r"\(")):
        _read(tmp_path, text)


def test_dots_that_are_not_pairs(tmp_path: Path):
    assert _read(tmp_path, "(:a .5 :b (1 .foo) :c (:x 1))") == {
        "a": 0.5, "b": [1, ".foo"], "c": {"x": 1}}


def test_non_ascii_path_under_c_locale(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LC_ALL", "C")
    d = tmp_path / "dé"
    d.mkdir()
    (d / "s.eld").write_text("(:k \"ü\"\n :k 2)", encoding="utf-8")
    with pytest.raises(ElateError) as err:
        eld.load(d / "s.eld")
    assert str(err.value) == f"{d / 's.eld'}:2: duplicate key :k"
    (d / "s.eld").write_text("(:k \"ü\")", encoding="utf-8")
    assert eld.load(d / "s.eld") == {"k": "ü"}


def test_lone_surrogate_is_an_error():
    with pytest.raises(ElateError, match="not valid Unicode"):
        eld.dumps({"steps": [{"eval": "(insert \"\ud800\")"}]})


def test_dumps_round_trips_every_example():
    for path in sorted((ROOT / "examples").glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        text = eld.dumps(value)        # verifies the read-back itself
        assert eld.loads(text) == value, path.name


def test_dumps_forms_and_edge_cases():
    value = {
        "session": {"eval": ["(require 'foo)", "(setq x\n      2)"]},
        "steps": [
            {"eval": "(dired \"~\")"},
            {"eval": "  (leading space)"},
            {"eval": "(trailing) ; comment"},
            {"eval": "symbol"},
            {"type": "(not a form key)"},
            {"assert": {"eval": "(eq a 'b)"}},
            {"wait": "until", "pred": "(get-buffer \"x\")"},
            {"heads": [True, 1], "null": [None], "empty": {}, "none": []},
            {"text": "q\"b\\s\x01\r\ttab\nnl", "k y": 1, "": 2, "a:b?": 3},
        ]}
    text = eld.dumps(value)
    assert eld.loads(text) == value
    assert ':eval (dired "~")' in text
    assert "(:eval (eq a 'b))" in text
    assert ':pred (get-buffer "x")' in text
    assert ':eval "  (leading space)"' in text
    assert ':eval "(trailing) ; comment"' in text
    assert ':eval "symbol"' in text
    assert ':type "(not a form key)"' in text
    assert "[t 1]" in text and "#s(hash-table)" in text
    assert ":k\\ y 1" in text
    with pytest.raises(ElateError, match="non-empty"):
        eld.dumps([1])


def test_convert_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                     monkeypatch: pytest.MonkeyPatch):
    src = ROOT / "examples" / "font-lock.json"
    out_eld = tmp_path / "fl.eld"
    assert cli.main(["--json", "convert", str(src), "-o", str(out_eld)]) == 0
    assert json.loads(capsys.readouterr().out)["to"] == "eld"
    back = tmp_path / "fl.json"
    assert cli.main(["--json", "convert", str(out_eld), "-o", str(back)]) == 0
    capsys.readouterr()
    assert json.loads(back.read_text()) == json.loads(src.read_text())
    # stdin, the direction sniffed from the text; --to wins.
    monkeypatch.setattr("sys.stdin", io.StringIO(out_eld.read_text()))
    assert cli.main(["--json", "convert", "-"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["to"] == "json"
    assert json.loads(out["text"]) == json.loads(src.read_text())
    assert cli.main(["--json", "convert", str(src), "--to", "json"]) == 0
    assert json.loads(json.loads(capsys.readouterr().out)["text"]) \
        == json.loads(src.read_text())
    bad = tmp_path / "bad.eld"
    bad.write_text("(:steps\n (:a 1 :a 2))")
    assert cli.main(["--json", "convert", str(bad)]) == 1
    assert "bad.eld:2: duplicate key :a" in json.loads(
        capsys.readouterr().out)["error"]
    # Never rewrite an .eld file (its comments would go), nor the input.
    assert cli.main(["--json", "convert", str(out_eld), "--to", "eld"]) == 1
    assert "already an .eld" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["--json", "convert", str(back), "-o", str(back)]) == 1
    assert "must not be the input" in json.loads(
        capsys.readouterr().out)["error"]


def test_convert_prints_the_text_when_piped(tmp_path: Path):
    import subprocess
    import sys

    src = ROOT / "examples" / "font-lock.json"
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from elate.cli import main; sys.exit(main())",
         "convert", str(src)],
        capture_output=True, text=True, check=True)
    assert proc.stdout.startswith("(:name ")
    assert proc.stdout.endswith(")\n") and not proc.stdout.endswith("\n\n")
    assert eld.loads(proc.stdout) == json.loads(src.read_text())


def test_include_may_be_eld(tmp_path: Path):
    (tmp_path / "lib.eld").write_text("""
(:templates (:greet (:params (:who "world")
                     :steps ((:eval (message "hi {{who}}"))))))
""")
    (tmp_path / "s.eld").write_text("""
(:include ("lib.eld")
 :steps ((:use "greet" :with (:who "you"))))
""")
    script, _ = SC.load_script(tmp_path / "s.eld", use_config=False)
    assert script["steps"] == [{"eval": '(message "hi you")'}]


@needs_session
def test_run_eld_file_and_stdin(elate_home: Path, tmp_path: Path,
                                capsys: pytest.CaptureFixture[str],
                                monkeypatch: pytest.MonkeyPatch):
    scenario = """;; an .eld scenario
(:name "eld"
 :session (:config "bare" :size "80x24")
 :params (:word "hello")
 :steps ((:eval (progn (switch-to-buffer "*scratch*") (erase-buffer)
                       (insert "{{word}} from eld")))
         (:wait "until" :pred (> (buffer-size) 0))
         (:assert (:buffer_contains "{{word}} from eld"))
         (:assert (:eval (string-prefix-p "{{word}}" (buffer-string))))))
"""
    path = tmp_path / "scenario.eld"
    path.write_text(scenario)
    code = cli.main(["--json", "run", str(path), "--set", "word=hi"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["passed"] == 4
    monkeypatch.setattr("sys.stdin", io.StringIO(scenario))
    code = cli.main(["--json", "run", "-"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
