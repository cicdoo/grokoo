# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Pin how the Grok runner wires a turn: headless `grok --output-format
streaming-json --always-approve --tools read_file --prompt-file ...`, the Odoo
MCP server written to the per-session .grok/config.toml (the bridge token passed
via the process env, never written to the file; web tools never advertised by
the bridge), and `-r <id>` resume on later turns."""
import os
import sys
import tempfile

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.grokoo.models.grokoo_runner import WEB_TOOL_BUILTINS


@tagged("post_install", "-at_install", "grokoo")
class TestRunnerArgv(TransactionCase):

    def setUp(self):
        super().setUp()
        self.runner = self.env["grokoo.runner"]
        self.scratch = tempfile.mkdtemp(prefix="grokoo_test_")
        self.uploads = os.path.join(self.scratch, "uploads")
        os.makedirs(self.uploads, exist_ok=True)
        self.prompt_path = os.path.join(self.scratch, "grok_prompt.txt")

    def _ctx(self, allowed_tools, is_first=True, resuming=False, sid=""):
        web_builtins = [WEB_TOOL_BUILTINS[t]
                        for t in allowed_tools if t in ("web_fetch", "web_search")]
        return {
            "scratch": self.scratch,
            "uploads_dir": self.uploads,
            "python_bin": sys.executable,
            "cli_path": "/usr/local/bin/grok",
            "model": "grok-4",
            "output_format": "streaming-json",
            "core_tools": "",
            "extra_args": [],
            "allowed_tools": allowed_tools,
            "web_builtins": web_builtins,
            "identity": "--- ODOO USER CONTEXT ---",
            "is_first": is_first,
            "resuming": resuming,
            "grok_session_id": sid,
            "history": "" if (is_first or resuming) else "User: earlier",
            "prompt": "hi",
            "base_url": "http://127.0.0.1:8069",
            "target_db": "testdb",
            "routing_sid": "sid",
            "token": "tok-123",
            "session_id": 1,
            "bridge_script": "/tmp/bridge.py",
            "excluded_models": [],
        }

    def test_first_turn_argv_is_headless_auto_approve(self):
        argv = self.runner._build_argv(self._ctx(["orm_read"]), self.prompt_path)
        self.assertEqual(argv[0], "/usr/local/bin/grok")
        self.assertNotIn("-r", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "streaming-json")
        self.assertIn("--always-approve", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "grok-4")
        self.assertEqual(argv[argv.index("--prompt-file") + 1], self.prompt_path)
        # Built-in tools are an allowlist of file reads only; web search is off.
        tools = argv[argv.index("--tools") + 1].split(",")
        self.assertIn("read_file", tools)
        self.assertNotIn("web_search", tools)
        self.assertIn("--disable-web-search", argv)

    def test_web_grant_allowlists_web_tools(self):
        argv = self.runner._build_argv(
            self._ctx(["orm_read", "web_search"]), self.prompt_path)
        tools = argv[argv.index("--tools") + 1].split(",")
        self.assertIn("web_search", tools)
        self.assertNotIn("--disable-web-search", argv)

    def test_resume_turn_passes_session_id(self):
        argv = self.runner._build_argv(
            self._ctx(["orm_read"], is_first=False, resuming=True, sid="sess_42"),
            self.prompt_path)
        self.assertEqual(argv[argv.index("-r") + 1], "sess_42")

    def test_config_carries_bridge_token_via_env_not_file(self):
        ctx = self._ctx(["orm_read", "web_fetch"])
        path = self.runner._write_config(ctx)
        raw = open(path).read()
        # The real token is NEVER written to the config file (passed via env).
        self.assertNotIn("tok-123", raw)
        self.assertNotIn("AI_BRIDGE_TOKEN", raw)
        if tomllib:
            cfg = tomllib.loads(raw)
            server = cfg["mcp_servers"]["odoo"]
            self.assertEqual(server["command"], sys.executable)
            self.assertEqual(server["args"], ["/tmp/bridge.py"])
            # The bridge advertises ORM tools, never the CLI's own web tools.
            self.assertIn("orm_read", server["env"]["AI_ALLOWED_TOOLS"])
            self.assertNotIn("web_fetch", server["env"]["AI_ALLOWED_TOOLS"])

    def test_streaming_json_tool_call_renders(self):
        """Grok streaming-json mirrors the Anthropic shape: an `assistant` event
        carries tool_use content blocks; a `user` event carries the tool_result.
        The runner must render the call with its real name, arguments and result,
        stripping the mcp__odoo__ namespace."""
        session = self.env["grokoo.session"].create({"user_id": self.env.uid})
        st = {"msg_id": None, "tool_idx": {}}
        self.runner._handle_event(self.env, session, {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Looking that up."},
                {"type": "tool_use", "id": "tid-1", "name": "mcp__odoo__orm_read",
                 "input": {"model": "res.users"}},
            ]},
        }, st)
        self.runner._handle_event(self.env, session, {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tid-1",
                 "content": "ok", "is_error": False},
            ]},
        }, st)
        rec = self.env["grokoo.message"].browse(st["msg_id"])
        self.assertIn("Looking that up.", rec.body)
        calls = rec.tool_calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "orm_read")  # mcp__odoo__ stripped
        self.assertEqual(calls[0]["input"], {"model": "res.users"})
        self.assertEqual(calls[0]["status"], "done")
        self.assertEqual(calls[0]["result"], "ok")

    def test_streaming_json_tool_error_status(self):
        """A tool_result with is_error=True marks the call failed."""
        session = self.env["grokoo.session"].create({"user_id": self.env.uid})
        st = {"msg_id": None, "tool_idx": {}}
        self.runner._handle_event(self.env, session, {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tid-9", "name": "mcp__odoo__orm_read",
                 "input": {}},
            ]},
        }, st)
        self.runner._handle_event(self.env, session, {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tid-9",
                 "content": "Error: denied", "is_error": True},
            ]},
        }, st)
        calls = self.env["grokoo.message"].browse(st["msg_id"]).tool_calls
        self.assertEqual(calls[0]["status"], "error")

    def test_result_event_captures_session_and_closes(self):
        session = self.env["grokoo.session"].create({"user_id": self.env.uid})
        st = {"msg_id": None, "tool_idx": {}}
        self.runner._handle_event(self.env, session, {
            "type": "system", "subtype": "init", "session_id": "sess_77"}, st)
        self.runner._handle_event(self.env, session, {
            "type": "result", "subtype": "success", "result": "All done.",
            "session_id": "sess_77", "is_error": False}, st)
        self.assertEqual(session.grok_session_id, "sess_77")
        self.assertEqual(session.state, "done")

    def test_system_prompt_on_first_and_when_not_resuming(self):
        first = self.runner._compose_prompt(self._ctx(["orm_read"]))
        resumed = self.runner._compose_prompt(
            self._ctx(["orm_read"], is_first=False, resuming=True, sid="s1"))
        stateless = self.runner._compose_prompt(
            self._ctx(["orm_read"], is_first=False, resuming=False))
        self.assertIn("embedded inside an Odoo", first)
        self.assertNotIn("embedded inside an Odoo", resumed)
        self.assertIn("ODOO USER CONTEXT", resumed)
        # When we cannot resume, we re-send the system prompt AND replay history.
        self.assertIn("embedded inside an Odoo", stateless)
        self.assertIn("CONVERSATION SO FAR", stateless)
