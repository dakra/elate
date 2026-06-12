"""End-to-end MCP server tests: real stdio server, real Emacs, real tmux.

Each test spawns the server subprocess (``python -m elate.cli mcp``) via the
MCP SDK's stdio client and talks JSON-RPC to it. The Emacs session itself is
module-scoped: it lives in tmux and survives across server connections, which
is exactly the persistence property the MCP server relies on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import Any, Awaitable, Callable

import anyio
import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HAVE_DEPS = bool(
    shutil.which("emacs") and shutil.which("tmux") and shutil.which("emacsclient")
)

pytestmark = pytest.mark.skipif(
    not HAVE_DEPS, reason="emacs, emacsclient, and tmux are required"
)

# Short name + short tmpdir prefix: the tmux/server unix sockets live under
# <home>/sessions/<name>/, and macOS caps socket paths at ~104 bytes.
NAME = f"m{os.getpid()}"

EXPECTED_TOOLS = {
    "elate_start", "elate_stop", "elate_list", "elate_info",
    "elate_keys", "elate_type", "elate_mouse", "elate_eval",
    "elate_state", "elate_screenshot",
    "elate_buffer", "elate_messages", "elate_echo",
    "elate_wait", "elate_describe",
    "elate_test", "elate_lint", "elate_popups",
    "elate_run_script", "elate_record",
    "elate_profile", "elate_bench",
}


@pytest.fixture(scope="module")
def elate_home() -> Iterator[str]:
    tmp = tempfile.mkdtemp(prefix="elmcp-")
    old = os.environ.get("ELATE_HOME")
    os.environ["ELATE_HOME"] = tmp  # for in-process safety-net cleanup
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop("ELATE_HOME", None)
        else:
            os.environ["ELATE_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def with_client(elate_home: str, fn: Callable[[ClientSession], Awaitable[Any]]) -> Any:
    """Run FN against a fresh stdio server connection; return its result."""

    async def main() -> Any:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "elate.cli", "mcp"],
            env={**os.environ, "ELATE_HOME": elate_home},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as cs:
                await cs.initialize()
                return await fn(cs)

    return anyio.run(main)


async def call(cs: ClientSession, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await cs.call_tool(tool, args)
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


def one_call(elate_home: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async def fn(cs: ClientSession) -> dict[str, Any]:
        return await call(cs, tool, args)

    return with_client(elate_home, fn)


@pytest.fixture(scope="module")
def mcp_session(elate_home: str) -> Iterator[dict[str, Any]]:
    """Module-scoped Emacs session started through the MCP server itself."""
    started = one_call(elate_home, "elate_start",
                       {"name": NAME, "size": "100x30"})
    assert started["ok"] is True, started
    try:
        yield started
    finally:
        try:
            one_call(elate_home, "elate_stop", {"session": NAME})
        except Exception:
            from elate import session as S  # safety net: no stray tmux servers
            try:
                S.stop_session(NAME)
            except Exception:
                pass


# -- protocol surface ----------------------------------------------------------

def test_list_tools(elate_home: str) -> None:
    async def fn(cs: ClientSession) -> None:
        tools = {t.name: t for t in (await cs.list_tools()).tools}
        assert set(tools) == EXPECTED_TOOLS
        # Descriptions must teach the workflow / notation.
        keys_params = tools["elate_keys"].inputSchema["properties"]
        assert "kbd" in keys_params["keys"]["description"]
        assert "prompt" in keys_params["delivery"]["description"]
        assert "events" in keys_params["delivery"]["description"]
        assert "cursor" in tools["elate_messages"].description.lower()
        # The screenshot tool teaches both UI behaviors (text vs PNG image).
        shot_desc = tools["elate_screenshot"].description
        assert "TTY" in shot_desc and "text" in shot_desc
        assert "GUI" in shot_desc and "PNG" in shot_desc
        assert "Screen Recording" in shot_desc
        # elate_start offers both UIs; mouse teaches targeting.
        ui_schema = tools["elate_start"].inputSchema["properties"]["ui"]
        assert set(ui_schema["enum"]) == {"tty", "gui"}
        mouse_params = tools["elate_mouse"].inputSchema["properties"]
        assert set(mouse_params["action"]["enum"]) == {
            "click", "double", "drag", "wheel"}
        assert mouse_params["button"]["minimum"] == 1
        assert mouse_params["button"]["maximum"] == 3
        assert "mode-line" in mouse_params["part"]["enum"]
        assert tools["elate_mouse"].inputSchema["properties"]["timeout"][
            "maximum"] == 120
        # Coordinates carry lower bounds (col=-5 must be a schema
        # rejection, not a raw elisp wholenump error) and wheel count
        # is capped. Optional ints render as anyOf [integer, null].
        for field, minimum in (("pos", 1), ("line", 1), ("col", 0),
                               ("to_pos", 1), ("to_line", 1), ("to_col", 0)):
            int_schema = next(s for s in mouse_params[field]["anyOf"]
                              if s.get("type") == "integer")
            assert int_schema["minimum"] == minimum, field
        assert mouse_params["count"]["maximum"] == 50
        # Mode-line double-click caveat is taught.
        assert "mouse-delete-other-windows" in mouse_params["part"]["description"]
        # Annotations: observation tools are read-only, stop is destructive.
        assert tools["elate_state"].annotations.readOnlyHint is True
        assert tools["elate_buffer"].annotations.readOnlyHint is True
        assert tools["elate_stop"].annotations.destructiveHint is True
        assert tools["elate_eval"].annotations.readOnlyHint is False
        assert tools["elate_mouse"].annotations.readOnlyHint is False
        # Timeouts are schema-bounded (a model cannot park the server on a
        # one-hour wait) and elate_type warns about control characters.
        wait_timeout = tools["elate_wait"].inputSchema["properties"]["timeout"]
        assert wait_timeout["exclusiveMinimum"] == 0
        assert wait_timeout["maximum"] == 120
        eval_timeout = tools["elate_eval"].inputSchema["properties"]["timeout"]
        assert eval_timeout["exclusiveMinimum"] == 0
        assert "ESC" in tools["elate_type"].inputSchema["properties"]["text"]["description"]
        # elate_type teaches the GUI size cap + why (per-character command
        # loop delivery); elate_stop no longer promises tmux for GUI.
        type_text_desc = tools["elate_type"].inputSchema["properties"]["text"]["description"]
        assert "10000" in type_text_desc and "command" in type_text_desc
        assert "TTY" in tools["elate_stop"].description
        # Phase 4 tools: ERT runner teaches selectors and the
        # failures-are-data contract; timeouts are schema-bounded.
        test_params = tools["elate_test"].inputSchema["properties"]
        assert "selector" in test_params
        assert "tag" in test_params["selector"]["description"]
        assert test_params["timeout"]["maximum"] == 600
        assert "unexpected" in tools["elate_test"].description
        assert "interactiv" in tools["elate_test"].description.lower()
        lint_params = tools["elate_lint"].inputSchema["properties"]
        assert lint_params["timeout"]["maximum"] == 120
        assert "path" in lint_params["files"]["description"].lower()
        assert "checkdoc" in tools["elate_lint"].description
        # elate_buffer grew the props flag; popups is read-only.
        buf_props = tools["elate_buffer"].inputSchema["properties"]["props"]
        assert "overlay" in buf_props["description"]
        assert "font-lock" in buf_props["description"]
        assert tools["elate_popups"].annotations.readOnlyHint is True
        assert "which-key" in tools["elate_popups"].description
        # elate_state teaches the popups field.
        assert "popups" in tools["elate_state"].description
        # Phase 5 tools: the script runner teaches the failures-are-data
        # contract and is timeout-bounded; record teaches TTY-only + agg.
        run_params = tools["elate_run_script"].inputSchema["properties"]
        assert "path" in run_params["script"]["description"]
        assert run_params["timeout"]["maximum"] == 600
        assert "success" in tools["elate_run_script"].description
        assert "fresh" in tools["elate_run_script"].description
        # run_script can override the emacs binary (matrix-style runs
        # over MCP) and teaches that screenshot steps overwrite files.
        assert "emacs" in run_params
        assert "matrix" in run_params["emacs"]["description"]
        assert "overwrite" in run_params["script"]["description"].lower()
        # Phase 6 tools: profiler/bench teach the fresh-session
        # recommendation (history skews numbers) and are schema-bounded.
        prof_params = tools["elate_profile"].inputSchema["properties"]
        assert set(prof_params["action"]["enum"]) == {
            "start", "stop", "report", "run"}
        assert set(prof_params["mode"]["enum"]) == {"cpu", "mem", "both"}
        assert prof_params["depth"]["minimum"] == 1
        assert prof_params["depth"]["maximum"] == 20
        assert prof_params["timeout"]["maximum"] == 600
        assert "fresh" in tools["elate_profile"].description
        bench_params = tools["elate_bench"].inputSchema["properties"]
        assert bench_params["repetitions"]["minimum"] == 1
        assert bench_params["repetitions"]["maximum"] == 1_000_000
        assert bench_params["timeout"]["maximum"] == 600
        assert "fresh" in tools["elate_bench"].description
        assert "memory-deltas" in tools["elate_bench"].description
        # elate_start grew the clean-install config mode.
        start_config = tools["elate_start"].inputSchema["properties"]["config"]
        assert "clean-install" in start_config["enum"]
        assert "package-install-file" in start_config["description"]
        record_desc = tools["elate_record"].description
        assert "asciicast" in record_desc
        assert "TTY" in record_desc
        # The crashed-session story matches the implementation (a dead
        # pane = stale recording, stop finalizes) and the output path
        # warning is explicit.
        assert "stale" in record_desc
        assert "overwritten" in tools["elate_record"].inputSchema[
            "properties"]["output"]["description"].lower()
        assert set(tools["elate_record"].inputSchema["properties"]["action"]
                   ["enum"]) == {"start", "stop", "status"}

    with_client(elate_home, fn)


def test_invalid_arguments_are_protocol_errors(elate_home: str,
                                               mcp_session: dict[str, Any]) -> None:
    """The "ok-flag JSON" contract covers *valid* calls; invalid arguments
    (missing fields, schema violations) surface as MCP protocol-level
    isError results with pydantic text. Pin that boundary."""
    async def fn(cs: ClientSession) -> None:
        result = await cs.call_tool("elate_eval", {"form": "(+ 1 1)"})
        assert result.isError is True
        assert "session" in result.content[0].text
        result = await cs.call_tool(
            "elate_wait", {"session": NAME, "condition": "idle", "timeout": 3600})
        assert result.isError is True
        assert "120" in result.content[0].text
        result = await cs.call_tool(
            "elate_eval", {"session": NAME, "form": "1", "timeout": -2})
        assert result.isError is True
        # Negative mouse coordinates are schema rejections (review: col=-5
        # used to reach elisp and fail as a raw wholenump error).
        result = await cs.call_tool(
            "elate_mouse", {"session": NAME, "action": "click",
                            "line": 1, "col": -5})
        assert result.isError is True

    with_client(elate_home, fn)


# -- lifecycle -----------------------------------------------------------------

def test_start_payload_and_list(elate_home: str, mcp_session: dict[str, Any]) -> None:
    assert mcp_session["alive"] is True
    assert mcp_session["emacs_version"]
    assert mcp_session["size"] == [100, 30]
    listed = one_call(elate_home, "elate_list", {})
    assert listed["ok"] is True
    entry = {s["name"]: s for s in listed["sessions"]}[NAME]
    assert entry["status"] == "running"


# -- eval ------------------------------------------------------------------------

def test_eval_roundtrip(elate_home: str, mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_eval", {"session": NAME, "form": "(+ 1 2)"})
    assert out["ok"] is True
    assert out["value"] == "3"

def test_eval_elisp_error_embeds_state(elate_home: str,
                                       mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_eval",
                   {"session": NAME, "form": '(error "mcp-boom")'})
    assert out["ok"] is False
    assert out["error"] == "mcp-boom"
    assert out["backtrace"]
    # The snapshot is flattened: "state" is the actual state (not state.state).
    assert "state" in out and out["state"]["buffer"]


def test_eval_non_unicode_value_keeps_json_contract(
        elate_home: str, mcp_session: dict[str, Any]) -> None:
    # Regression: surrogates in the value used to escape the agent's RPC
    # guard, cross the transport as garbage, and surface as an MCP isError
    # with a UnicodeDecodeError -- breaking the ok-flag contract.
    out = one_call(elate_home, "elate_eval",
                   {"session": NAME, "form": "(string #xD800)"})
    assert out["ok"] is True
    assert "�" in out["value"]
    # The session must stay fully observable afterwards.
    state = one_call(elate_home, "elate_state", {"session": NAME})
    assert state["ok"] is True and state["buffer"]


def test_info_tool(elate_home: str, mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_info", {"session": NAME})
    assert out["ok"] is True
    assert out["status"] == "running"
    assert out["size"] == [100, 30]
    assert "init_error" in out
    assert out["session_dir"]


# -- input + observation ---------------------------------------------------------

def test_keys_then_buffer(elate_home: str, mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_eval", {
            "session": NAME,
            "form": '(progn (switch-to-buffer "*scratch*") (erase-buffer))',
        })
        assert out["ok"] is True
        out = await call(cs, "elate_keys", {"session": NAME, "keys": "m c p RET x"})
        assert out["ok"] is True and out["channel"] == "semantic"
        out = await call(cs, "elate_buffer", {"session": NAME, "buffer": "*scratch*"})
        assert out["ok"] is True
        assert out["text"] == "mcp\nx"
        ranged = await call(cs, "elate_buffer",
                            {"session": NAME, "buffer": "*scratch*",
                             "from_line": 2, "to_line": 2})
        assert ranged["text"] == "x"

    with_client(elate_home, fn)

def test_type_literal_text(elate_home: str, mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        await call(cs, "elate_eval", {
            "session": NAME,
            "form": '(progn (switch-to-buffer "*scratch*") (erase-buffer))',
        })
        out = await call(cs, "elate_type", {"session": NAME, "text": "typed via mcp"})
        assert out["ok"] is True
        out = await call(cs, "elate_wait",
                         {"session": NAME, "condition": "text",
                          "pattern": "typed via mcp", "buffer": "*scratch*"})
        assert out["ok"] is True

    with_client(elate_home, fn)

def test_events_keys_hold_prompt_open_in_state(elate_home: str,
                                               mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_keys",
                         {"session": NAME, "keys": "M-x", "delivery": "events"})
        assert out["ok"] is True and out["delivered"] == "events"
        out = await call(cs, "elate_wait", {"session": NAME, "condition": "prompt"})
        assert out["ok"] is True
        assert out["prompt"].startswith("M-x")
        state = await call(cs, "elate_state", {"session": NAME})
        assert state["ok"] is True
        mb = state["minibuffer"]
        assert mb["prompt"].startswith("M-x")
        assert mb["depth"] == 1
        assert len(mb["completions"]["candidates"]) > 0
        # Layout fields are present alongside the prompt.
        assert "windows" in state and "messages-tail" in state
        # Cancel via raw keys (the documented unblock path).
        out = await call(cs, "elate_keys",
                         {"session": NAME, "keys": "C-g", "delivery": "raw"})
        assert out["ok"] is True and out["channel"] == "raw"
        out = await call(cs, "elate_wait", {"session": NAME, "condition": "idle"})
        assert out["ok"] is True

    with_client(elate_home, fn)

def test_echo_and_messages_cursor(elate_home: str,
                                  mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        await call(cs, "elate_messages", {"session": NAME})  # drain backlog
        out = await call(cs, "elate_eval",
                         {"session": NAME, "form": '(message "mcp-echo-marker")'})
        assert out["ok"] is True
        echo = await call(cs, "elate_echo", {"session": NAME})
        assert echo["ok"] is True and echo["echo"] == "mcp-echo-marker"
        delta = await call(cs, "elate_messages", {"session": NAME})
        assert "mcp-echo-marker" in delta["text"]
        again = await call(cs, "elate_messages", {"session": NAME})
        assert "mcp-echo-marker" not in again["text"]

    with_client(elate_home, fn)

def test_screenshot_text(elate_home: str, mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_screenshot", {"session": NAME})
    assert out["ok"] is True
    assert "*scratch*" in out["screen"]  # mode line
    ansi = one_call(elate_home, "elate_screenshot", {"session": NAME, "ansi": True})
    assert "\x1b[" in ansi["screen"]


def test_keys_raw_unencodable_chord_message(elate_home: str,
                                            mcp_session: dict[str, Any]) -> None:
    # The error is model-facing: it must point at the MCP spelling of the
    # way out (delivery='semantic'), not only at the CLI flag.
    out = one_call(elate_home, "elate_keys",
                   {"session": NAME, "keys": "C-%", "delivery": "raw"})
    assert out["ok"] is False
    assert "cannot encode" in out["error"]
    assert "delivery='semantic'" in out["error"]


def test_messages_cursor_write_failure_is_structured(
        elate_home: str, mcp_session: dict[str, Any]) -> None:
    # An OSError outside the ElateError hierarchy (cursor file replaced by
    # a directory) must still produce the ok-flag JSON shape, not a
    # FastMCP isError (robustness: catch-all in tool bodies).
    cursor = os.path.join(elate_home, "sessions", NAME, "messages.cursor")
    if os.path.exists(cursor):
        os.remove(cursor)
    os.mkdir(cursor)
    try:
        out = one_call(elate_home, "elate_messages", {"session": NAME})
        assert out["ok"] is False
        assert "directory" in out["error"].lower()
    finally:
        os.rmdir(cursor)


# -- wait / describe -------------------------------------------------------------

def test_wait_timeout_embeds_state(elate_home: str,
                                   mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_wait",
                   {"session": NAME, "condition": "text",
                    "pattern": "never-mcp-xyzzy", "buffer": "*scratch*",
                    "timeout": 0.5})
    assert out["ok"] is False
    assert "timed out" in out["error"]
    assert "state" in out

def test_wait_does_not_block_other_calls(elate_home: str,
                                         mcp_session: dict[str, Any]) -> None:
    """Tool bodies run via anyio.to_thread: during a multi-second
    elate_wait the server must keep answering (reviewer measured 4.76s
    elate_list latency before the fix; bar is sub-second)."""
    async def fn(cs: ClientSession) -> None:
        latency = {}

        async def waiter() -> None:
            out = await call(cs, "elate_wait", {
                "session": NAME, "condition": "text",
                "pattern": "never-mcp-blocking-xyzzy", "buffer": "*scratch*",
                "timeout": 3,
            })
            assert out["ok"] is False  # times out, that is the point

        async def prober() -> None:
            await anyio.sleep(0.5)  # let the wait get going
            t0 = time.monotonic()
            out = await call(cs, "elate_list", {})
            latency["list"] = time.monotonic() - t0
            assert out["ok"] is True

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            tg.start_soon(prober)
        assert latency["list"] < 1.0, latency

    with_client(elate_home, fn)


def test_two_clients_share_session_and_messages_cursor(
        elate_home: str, mcp_session: dict[str, Any]) -> None:
    """Two MCP servers on one session: both can act on it concurrently;
    the messages cursor is shared (documented), so a delta consumed by
    client B is never seen by client A."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "elate.cli", "mcp"],
        env={**os.environ, "ELATE_HOME": elate_home},
    )

    async def main() -> None:
        async with stdio_client(params) as (ra, wa), stdio_client(params) as (rb, wb):
            async with ClientSession(ra, wa) as a, ClientSession(rb, wb) as b:
                await a.initialize()
                await b.initialize()
                # Concurrent evals from both clients both succeed.
                results: dict[str, Any] = {}

                async def eval_on(cs: ClientSession, key: str, form: str) -> None:
                    results[key] = await call(cs, "elate_eval",
                                              {"session": NAME, "form": form})

                async with anyio.create_task_group() as tg:
                    tg.start_soon(eval_on, a, "a", "(+ 40 2)")
                    tg.start_soon(eval_on, b, "b", "(* 6 7)")
                assert results["a"]["ok"] and results["a"]["value"] == "42"
                assert results["b"]["ok"] and results["b"]["value"] == "42"
                # Cursor stealing (documented shared-cursor property).
                await call(a, "elate_messages", {"session": NAME})  # A drains
                out = await call(a, "elate_eval",
                                 {"session": NAME, "form": '(message "two-client-marker")'})
                assert out["ok"] is True
                b_delta = await call(b, "elate_messages", {"session": NAME})
                assert "two-client-marker" in b_delta["text"]  # B consumes it
                a_delta = await call(a, "elate_messages", {"session": NAME})
                assert "two-client-marker" not in a_delta["text"]  # A never sees it

    anyio.run(main)


def test_describe(elate_home: str, mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_describe",
                         {"session": NAME, "kind": "key", "name": "C-x C-f"})
        assert out["ok"] is True
        assert out["binding"] == "find-file"
        assert out["function"]["doc"]
        out = await call(cs, "elate_describe",
                         {"session": NAME, "kind": "variable", "name": "fill-column"})
        assert out["ok"] is True and out["custom"] is True
        out = await call(cs, "elate_describe",
                         {"session": NAME, "kind": "function", "name": "nope-xyzzy"})
        assert out["ok"] is True and out["defined"] is False

    with_client(elate_home, fn)


# -- testing & quality tooling (Phase 4) -------------------------------------------

def test_ert_and_lint_tools(elate_home: str, mcp_session: dict[str, Any],
                            tmp_path) -> None:
    fixture = tmp_path / "mcpfix-tests.el"
    fixture.write_text(
        ";;; mcpfix-tests.el --- fixture -*- lexical-binding: t; -*-\n"
        "(require 'ert)\n"
        "(ert-deftest mcpfix-pass () (should t))\n"
        "(ert-deftest mcpfix-fail () (should (= 1 2)))\n"
        "(provide 'mcpfix-tests)\n;;; mcpfix-tests.el ends here\n",
        encoding="utf-8")
    lintf = tmp_path / "mcplint.el"
    lintf.write_text(
        ";;; mcplint.el --- fixture -*- lexical-binding: t; -*-\n"
        "(defun mcplint-f () (setq mcplint-free 1))\n",
        encoding="utf-8")

    async def fn(cs: ClientSession) -> None:
        # ERT: failures are data (ok stays true); per-test details present.
        out = await call(cs, "elate_test", {
            "session": NAME, "selector": "mcpfix-",
            "load_files": [str(fixture)]})
        assert out["ok"] is True
        assert out["total"] == 2 and out["passed"] == 1
        assert out["unexpected"] == 1 and out["timed-out"] is False
        failed = next(t for t in out["tests"] if t["name"] == "mcpfix-fail")
        assert failed["status"] == "failed"
        assert failed["backtrace"] and "ert-test-failed" in failed["condition"]
        # A load failure IS a tool error, with a backtrace.
        out = await call(cs, "elate_test", {
            "session": NAME, "load_files": ["/no/such/mcp-tests.el"]})
        assert out["ok"] is False and "does not exist" in out["error"]
        # Lint: structured items with exact positions, plus the notes.
        out = await call(cs, "elate_lint",
                         {"session": NAME, "files": [str(lintf)]})
        assert out["ok"] is True and out["clean"] is False
        assert any(i["tool"] == "byte-compile" and i["line"] == 2
                   and "free variable" in i["message"]
                   for i in out["items"])
        assert any("package-lint" in n for n in out["notes"])

    with_client(elate_home, fn)


def test_buffer_props_and_popups_tools(elate_home: str,
                                       mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_eval", {
            "session": NAME,
            "form": '(with-current-buffer (get-buffer-create "mcpprops")'
                    " (erase-buffer) (emacs-lisp-mode)"
                    ' (insert "(defun mcp-x ())") (buffer-name))'})
        assert out["ok"] is True
        out = await call(cs, "elate_buffer",
                         {"session": NAME, "buffer": "mcpprops",
                          "props": True})
        assert out["ok"] is True
        runs = out["props"]["runs"]
        assert any("font-lock-keyword-face" in (r.get("face") or [])
                   for r in runs)
        assert "overlays" in out
        # Without the flag, no props payload.
        out = await call(cs, "elate_buffer",
                         {"session": NAME, "buffer": "mcpprops"})
        assert out["ok"] is True and "props" not in out
        # Popups: empty baseline; state carries the popups field.
        out = await call(cs, "elate_popups", {"session": NAME})
        assert out["ok"] is True and out["popups"] == []
        state = await call(cs, "elate_state", {"session": NAME})
        assert state["ok"] is True and state["popups"] == []

    with_client(elate_home, fn)


def test_profile_and_bench_tools(elate_home: str,
                                 mcp_session: dict[str, Any]) -> None:
    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_eval", {
            "session": NAME,
            "form": "(defun mcpfix-busy ()"
                    " (let ((t0 (float-time)) (s 0))"
                    "  (while (< (- (float-time) t0) 0.4) (setq s (1+ s)))"
                    "  s))"})
        assert out["ok"] is True
        # One-shot profile run: eval result + structured report.
        out = await call(cs, "elate_profile",
                         {"session": NAME, "action": "run",
                          "form": "(mcpfix-busy)"})
        assert out["ok"] is True
        assert out["eval"]["error"] is None
        assert out["cpu"]["total"] > 0
        assert out["cpu"]["functions"]
        names = {f["name"] for f in out["cpu"]["functions"]}
        assert any("mcpfix-busy" == n for n in names)
        # run without a form is a structured error.
        out = await call(cs, "elate_profile",
                         {"session": NAME, "action": "run"})
        assert out["ok"] is False and "form" in out["error"]
        # report with no data after a reset start/stop window is fine;
        # report on a never-profiled state was covered by 'run' above --
        # here pin the manual start/stop cycle.
        out = await call(cs, "elate_profile",
                         {"session": NAME, "action": "start", "mode": "mem"})
        assert out["ok"] is True and out["started"] == "mem"
        out = await call(cs, "elate_eval",
                         {"session": NAME, "form": "(length (make-list 50000 t))"})
        assert out["ok"] is True
        out = await call(cs, "elate_profile",
                         {"session": NAME, "action": "stop"})
        assert out["ok"] is True and out["mem"] is True
        out = await call(cs, "elate_profile",
                         {"session": NAME, "action": "report", "depth": 3})
        assert out["ok"] is True
        assert out["mem"]["units"] == "bytes" and out["mem"]["total"] > 0
        assert out["mem"]["depth"] == 3
        # Bench: fields + the elisp-error contract (ok=false + state).
        out = await call(cs, "elate_bench",
                         {"session": NAME, "form": "(make-list 100 t)",
                          "repetitions": 50})
        assert out["ok"] is True
        assert out["compiled"] is True and out["repetitions"] == 50
        assert out["elapsed"] >= 0 and out["mean"] >= 0
        assert out["memory-deltas"]["conses"] >= 5000
        out = await call(cs, "elate_bench",
                         {"session": NAME, "form": '(error "mcp-bench-boom")'})
        assert out["ok"] is False
        assert out["error"] == "mcp-bench-boom" and out["backtrace"]
        assert "state" in out
        # repetitions=0 is a schema-level rejection.
        result = await cs.call_tool(
            "elate_bench", {"session": NAME, "form": "1", "repetitions": 0})
        assert result.isError is True

    with_client(elate_home, fn)


# -- scenario scripts & recording (Phase 5) -----------------------------------------

def test_run_script_tool(elate_home: str, tmp_path) -> None:
    passing = tmp_path / "mcp-pass.json"
    passing.write_text(json.dumps({
        "session": {"config": "bare", "size": "80x24"},
        "steps": [
            {"type": "mcp-script"},
            {"wait": "text", "pattern": "mcp-script", "buffer": "*scratch*"},
            {"assert": {"buffer_contains": "mcp-script"}},
        ],
    }), encoding="utf-8")
    failing = tmp_path / "mcp-fail.json"
    failing.write_text(json.dumps({
        "session": {"config": "bare", "size": "80x24"},
        "steps": [{"assert": {"eval": "nil"}},
                  {"eval": "(never-runs)"}],
    }), encoding="utf-8")

    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_run_script", {"script": str(passing)})
        assert out["ok"] is True and out["success"] is True
        assert out["fresh_session"] is True and out["kept"] is False
        assert out["passed"] == 3
        # Script failures are data: ok stays true, success says no, the
        # failed step embeds a state snapshot, later steps are not-run.
        out = await call(cs, "elate_run_script", {"script": str(failing)})
        assert out["ok"] is True and out["success"] is False
        failed = out["steps"][0]
        assert failed["status"] == "failed"
        assert "state" in failed or "screen_tail" in failed
        assert out["steps"][1]["status"] == "not-run"
        assert out["kept"] is False
        # An unreadable script is an infrastructure error (ok=false).
        out = await call(cs, "elate_run_script",
                         {"script": str(tmp_path / "nope.json")})
        assert out["ok"] is False and "does not exist" in out["error"]
        # A wrong-typed step option is caught by validation up front:
        # flat ok=false error, no session boots, no steps lost -- the
        # "ok=false means the script could not run at all" contract
        # holds (REVIEW-phase5 bug 1).
        badtype = tmp_path / "mcp-badtype.json"
        badtype.write_text(json.dumps({
            "session": {"config": "bare", "size": "80x24"},
            "steps": [{"mouse": "click", "button": "left"}],
        }), encoding="utf-8")
        out = await call(cs, "elate_run_script", {"script": str(badtype)})
        assert out["ok"] is False and "must be" in out["error"]
        assert "steps" not in out
        # No fresh run sessions left running either way.
        listed = await call(cs, "elate_list", {})
        assert not any(s["name"].startswith("run-")
                       and s["status"] == "running"
                       for s in listed["sessions"])

    with_client(elate_home, fn)


def test_record_tool(elate_home: str, mcp_session: dict[str, Any],
                     tmp_path) -> None:
    cast = tmp_path / "mcp.cast"

    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_record",
                         {"session": NAME, "action": "start",
                          "output": str(cast)})
        assert out["ok"] is True and out["recording"] is True
        out = await call(cs, "elate_type",
                         {"session": NAME, "text": "mcp-cast-marker"})
        assert out["ok"] is True
        out = await call(cs, "elate_wait",
                         {"session": NAME, "condition": "text",
                          "pattern": "mcp-cast-marker", "buffer": "*scratch*"})
        assert out["ok"] is True
        out = await call(cs, "elate_record",
                         {"session": NAME, "action": "status"})
        assert out["ok"] is True and out["recording"] is True
        out = await call(cs, "elate_record",
                         {"session": NAME, "action": "stop"})
        assert out["ok"] is True and out["recording"] is False
        assert out["events"] >= 2
        header = json.loads(
            cast.read_text(encoding="utf-8").splitlines()[0])
        assert header["version"] == 2
        # output is start-only; the error names the rule.
        out = await call(cs, "elate_record",
                         {"session": NAME, "action": "stop",
                          "output": "x.cast"})
        assert out["ok"] is False and "start" in out["error"]

    with_client(elate_home, fn)


# -- errors -----------------------------------------------------------------------

def test_unknown_session_lists_known(elate_home: str,
                                     mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_eval",
                   {"session": "no-such-mcp-session", "form": "(+ 1 1)"})
    assert out["ok"] is False
    assert "no-such-mcp-session" in out["error"]
    assert NAME in out["known_sessions"]

def test_eval_in_dead_session_reports_state(elate_home: str) -> None:
    name = f"{NAME}dead"
    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_start",
                         {"name": name, "config": "bare", "size": "80x24"})
        assert out["ok"] is True, out
        out = await call(cs, "elate_stop", {"session": name})
        assert out["ok"] is True, out
        out = await call(cs, "elate_eval", {"session": name, "form": "(+ 1 1)"})
        assert out["ok"] is False
        assert "not running" in out["error"]
        assert "stopped" in out["error"]

    with_client(elate_home, fn)

def test_start_rejects_bad_size(elate_home: str) -> None:
    out = one_call(elate_home, "elate_start",
                   {"name": f"{NAME}bad", "size": "huge"})
    assert out["ok"] is False
    assert "COLSxROWS" in out["error"]


def test_postmortem_via_mcp(elate_home: str) -> None:
    """kill -9 -> elate_eval reports dead + points at the screenshot;
    elate_screenshot still captures the dead pane; once the pane is gone
    the screenshot error reports the *computed* status (regression: it
    used to echo the stale registry 'running')."""
    name = f"{NAME}pm"

    async def fn(cs: ClientSession) -> None:
        out = await call(cs, "elate_start",
                         {"name": name, "config": "bare", "size": "80x24"})
        assert out["ok"] is True, out
        info = await call(cs, "elate_info", {"session": name})
        os.kill(info["pid"], 9)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            listed = await call(cs, "elate_list", {})
            entry = {s["name"]: s for s in listed["sessions"]}[name]
            if entry["status"] == "dead":
                break
            await anyio.sleep(0.1)
        assert entry["status"] == "dead"
        # Live-session tools fail with the computed status + screenshot hint.
        out = await call(cs, "elate_eval", {"session": name, "form": "(+ 1 1)"})
        assert out["ok"] is False
        assert "not running" in out["error"] and "dead" in out["error"]
        assert "screenshot" in out["error"]
        # The dead pane is retained for post-mortem capture.
        out = await call(cs, "elate_screenshot", {"session": name})
        assert out["ok"] is True and "screen" in out
        # Kill the tmux server behind elate's back: no pane left at all.
        subprocess.run(["tmux", "-S", info["tmux_socket"], "kill-server"],
                       capture_output=True)
        out = await call(cs, "elate_screenshot", {"session": name})
        assert out["ok"] is False
        assert "no tmux pane" in out["error"]
        assert "status: dead" in out["error"]  # computed, not stale "running"
        out = await call(cs, "elate_stop", {"session": name})
        assert out["ok"] is True

    with_client(elate_home, fn)


# -- transcript / shutdown (keep last) --------------------------------------------

def test_mcp_calls_are_transcript_logged(elate_home: str,
                                         mcp_session: dict[str, Any]) -> None:
    transcript = os.path.join(elate_home, "sessions", NAME,
                              "log", "transcript.jsonl")
    events = [json.loads(line) for line in open(transcript, encoding="utf-8")]
    via_mcp = {e["event"] for e in events if e.get("via") == "mcp"}
    assert {"eval", "keys", "buffer", "state", "wait", "describe",
            "screenshot", "messages", "echo", "type"} <= via_mcp

def test_stop_session(elate_home: str, mcp_session: dict[str, Any]) -> None:
    out = one_call(elate_home, "elate_stop", {"session": NAME})
    assert out["ok"] is True and out["stopped"] is True
    listed = one_call(elate_home, "elate_list", {})
    entry = {s["name"]: s for s in listed["sessions"]}[NAME]
    assert entry["status"] == "stopped"
