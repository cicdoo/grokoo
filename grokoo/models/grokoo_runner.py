# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

from odoo import api, models
from odoo.modules.registry import Registry

from .grokoo_session import WEB_TOOLS

_logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an AI assistant embedded inside an Odoo 18 ERP system. "
    "You help the current Odoo user query data and build reports. "
    "You act through the Odoo tools provided by the `odoo` MCP server (model_introspect, "
    "orm_search_read, orm_read, orm_call, sql_select, and — when granted — orm_create/"
    "orm_write/orm_unlink/orm_action/run_wizard/run_server_action). "
    "Do NOT run shell commands, edit files, or use the network: the ONLY way to read or "
    "change Odoo data is through the `odoo` MCP tools. (You may use read_file to open files "
    "the user attached under your working directory when they ask about an attachment.) "
    "Web search is available only when the tool grant in the ODOO USER CONTEXT block below "
    "lists it; otherwise assume you cannot reach the web. "
    "All Odoo actions run with the user's own permissions, so respect AccessError results and "
    "never try to work around them. "
    "Use `model_introspect` to discover models/fields before querying — consult its "
    "`effective_access` map to see which actions (read/write/create/unlink) are actually "
    "available to you here before attempting them. "
    "Use `orm_search_read`/`orm_read`/`orm_call` for normal data access (these honor "
    "record rules). Use `sql_select` only for read-only reporting when available. "
    "Write tools may be available; only some users are granted them. When you change data, "
    "confirm what you did concisely. "
    "Action tools may also be available (check `model_introspect`'s `ai_tools_allowed`): "
    "use `orm_action` to call business methods like `action_confirm`/`action_post`/"
    "`button_validate` on records (method names are allowlisted), `run_wizard` to create "
    "and run a wizard in one step, and `run_server_action` to run an admin-allowlisted "
    "server action. Prefer these over raw `orm_write` when an Odoo business action exists, "
    "so state machines and validations run correctly. "
    "Be concise. When you present data, format it as a clear Markdown table when useful. "
    "IDENTITY: You are acting on behalf of a specific Odoo user, whose identity, company, "
    "locale and roles are given in the 'ODOO USER CONTEXT' block below. You are THAT Odoo "
    "user's assistant — never identify yourself as an xAI/Grok account, "
    "and address the user by their Odoo name. Any account email shown elsewhere in your "
    "environment is the underlying xAI/Grok account used to run you — it is NOT the user; "
    "the authoritative identity is the one in the ODOO USER CONTEXT block. Assume ONLY the "
    "roles and privileges listed in that block; never assume administrator rights you have "
    "not been shown. Every tool runs with this user's own Odoo permissions, so an AccessError "
    "is authoritative: report it plainly and never try to work around it. "
    "CRITICAL: Only ever call tools through the real tool-calling mechanism. NEVER write "
    "tool calls or fabricated tool results as text in your reply, and NEVER invent data. If "
    "the Odoo MCP tools are not visible to you, reply exactly 'AI tools are not available "
    "right now, please retry.' and nothing else — do not guess."
)

# Built-in Grok CLI tools the model is allowed to keep. We restrict the built-in
# surface to file READS (so it can open user attachments in the workspace) —
# everything else (run_terminal_cmd, write_file, edit, search_replace, glob,
# grep, list_dir, task, memory, web tools) is withheld, leaving the gated Odoo
# MCP tools as the only way to touch the database. Web tools are added here only
# when granted. `view_image` lets the model see attached images.
#
# Grok's `--tools` flag is an ALLOWLIST of *built-in* tools ("Only listed tools
# are available", headless-only). It does NOT touch MCP tools — those come from
# the `[mcp_servers.*]` block in the per-session .grok/config.toml the runner
# writes — so allowlisting only the read tools keeps the Odoo MCP tools fully
# available to the model. (This is unlike gemini-cli, where a core allowlist
# also hid MCP tools.) Tune the built-in names via the grokoo.core_tools system
# parameter if a future CLI build renames them.
CORE_TOOLS_BASE = ["read_file", "view_image"]
# Grok's built-in web tool names, mapped from our per-user web grants.
WEB_TOOL_BUILTINS = {"web_fetch": "web_fetch", "web_search": "web_search"}


class AiAssistantRunner(models.AbstractModel):
    _name = "grokoo.runner"
    _description = "Grokoo AI Assistant CLI Runner"

    # ------------------------------------------------------------------
    # Launch (runs in the request env, as the user)
    # ------------------------------------------------------------------
    def _launch(self, session, prompt, is_first, token):
        """Resolve config in the request env, then spawn a background thread."""
        cli_path = session._resolve_cli_path()
        scratch = session._get_scratch_dir()
        uploads_dir = session._uploads_dir(create=True)
        # Empty = let Grok use the account's default model.
        model = session._config("model", "")
        # streaming-json (JSONL events, real-time streaming) when the installed
        # CLI supports it; "json" (single final object) is the conservative
        # fallback. These are Grok's own --output-format values.
        output_format = session._config("output_format", "streaming-json")
        timeout_s = int(session._config("timeout_s", 900))
        base_url = session._config("base_url", "http://127.0.0.1:8069")
        # Optional override of the allowlisted built-in tool set (comma-separated).
        core_tools = (session._config("core_tools", "") or "").strip()
        # Optional extra CLI args (space-separated) for tuning per deployment /
        # CLI version, e.g. "--permission-mode bypassPermissions".
        extra_args = (session._config("extra_args", "") or "").split()
        # Interpreter used to run the bundled bridge script. Defaults to the
        # Python currently running Odoo; override via `grokoo.python_bin`.
        python_bin = session._config("python_bin", "") or sys.executable
        target_db = session._target_db()
        routing_sid = session._mint_routing_sid(target_db)
        bridge_script = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "bridge", "mcp_server.py")
        grok_env = session._grok_env()
        # Effective tool set + model denylist for THIS user (computed once here,
        # in the request env, as the user). The controller re-derives them
        # authoritatively; these only shape the bridge's advertised tools.
        allowed_tools = sorted(session._effective_tools())
        web_builtins = [WEB_TOOL_BUILTINS[t]
                        for t in allowed_tools if t in WEB_TOOLS]
        excluded_models = sorted(session._ai_excluded_models())
        # Per-user identity/role block prepended to the prompt so the model acts
        # AS this Odoo user (built here, in the request env, as the user).
        identity = session.user_id._grokoo_identity_prompt(set(allowed_tools))

        # Only resume when this conversation already has a captured Grok
        # session id; otherwise start fresh (and replay history in the prompt).
        resuming = (not is_first) and bool(session.grok_session_id)

        ctx = {
            "dbname": self.env.cr.dbname,
            "uid": session.user_id.id,
            "session_id": session.id,
            "grok_session_id": session.grok_session_id or "",
            "prompt": prompt,
            "is_first": is_first,
            "resuming": resuming,
            "history": session._history_for_prompt() if not resuming else "",
            "cli_path": cli_path,
            "scratch": scratch,
            "uploads_dir": uploads_dir,
            "model": model,
            "output_format": output_format,
            "core_tools": core_tools,
            "extra_args": extra_args,
            "timeout_s": timeout_s,
            "base_url": base_url,
            "python_bin": python_bin,
            "target_db": target_db,
            "routing_sid": routing_sid,
            "bridge_script": bridge_script,
            "token": token,
            "grok_env": grok_env,
            "allowed_tools": allowed_tools,
            "web_builtins": web_builtins,
            "excluded_models": excluded_models,
            "identity": identity,
        }
        thread = threading.Thread(
            target=self._run_worker, args=(ctx,), daemon=True,
            name="grokoo_run_%s" % session.id)
        # Start only AFTER the request transaction commits, so the worker's own
        # cursor sees the committed session state (state=running) and bridge
        # token (bridge_jti). Starting inline races the request transaction.
        self.env.cr.postcommit.add(thread.start)

    # ------------------------------------------------------------------
    # Background worker (own cursor)
    # ------------------------------------------------------------------
    def _run_worker(self, ctx):
        dbname = ctx["dbname"]
        registry = Registry(dbname)
        with registry.cursor() as cr:
            env = api.Environment(cr, ctx["uid"], {})
            session = env["grokoo.session"].browse(ctx["session_id"])
            runner = env["grokoo.runner"]
            try:
                runner._execute(env, session, ctx)
            except Exception as e:
                _logger.exception("AI assistant run failed")
                session.write({"state": "error", "last_error": str(e)})
                runner._emit(session, {"kind": "error", "error": str(e)})
                cr.commit()

    def _execute(self, env, session, ctx):
        config_path = self._write_config(ctx)
        prompt_path = self._write_prompt(ctx)
        argv = self._build_argv(ctx, prompt_path)
        run_env = self._build_env(ctx)

        _logger.info("AI assistant: spawning %s (session %s)",
                     ctx["cli_path"], ctx["session_id"])
        proc = subprocess.Popen(
            argv, cwd=ctx["scratch"], env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, bufsize=1)

        session.write({"last_run_pid": proc.pid})
        self._emit(session, {"kind": "started"})
        env.cr.commit()

        deadline = time.time() + ctx["timeout_s"]
        # Per-turn streaming state: the single assistant message record for this
        # turn (lazily created) plus a {grok tool-call id -> tool_calls index} map.
        st = {"msg_id": None, "tool_idx": {}}
        streaming = ctx["output_format"] == "streaming-json"
        json_buf = []  # accumulates raw stdout when output_format == "json"
        # Read stdout via a helper thread feeding a queue, so the deadline is
        # enforced even when the CLI goes silent (no output, no exit).
        line_q = queue.Queue()

        def _pump(pipe, q):
            try:
                for ln in pipe:
                    q.put(ln)
            finally:
                q.put(None)  # sentinel: stdout closed (process exiting)

        reader = threading.Thread(
            target=_pump, args=(proc.stdout, line_q), daemon=True,
            name="grokoo_read_%s" % ctx["session_id"])
        reader.start()
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    raise TimeoutError("Run exceeded timeout")
                try:
                    line = line_q.get(timeout=min(remaining, 5))
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if line is None:  # stdout closed -> process is exiting
                    break
                if not streaming:
                    json_buf.append(line)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    # streaming-json should emit pure JSONL; ignore stray lines.
                    continue
                self._handle_event(env, session, evt, st)
                env.cr.commit()
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        finally:
            for p in (config_path, prompt_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            env["grokoo.session"]._delete_routing_sid(ctx.get("routing_sid"))

        stderr = proc.stderr.read() if proc.stderr else ""
        # Non-streaming mode: parse the single final JSON object now.
        if not streaming and session.state == "running":
            self._handle_final_json(env, session, "".join(json_buf), st)
        session.write({
            "last_run_pid": False,
            "turn_count": session.turn_count + 1,
        })
        if session.state == "running":
            # No explicit result event; treat as done unless rc indicates failure.
            if rc != 0:
                session.write({"state": "error", "last_error": stderr[:2000]})
                self._emit(session, {"kind": "error",
                                     "error": stderr[:500] or "CLI exited %s" % rc})
            else:
                session.write({"state": "done"})
                self._emit(session, {"kind": "done"})
        env.cr.commit()

    # ------------------------------------------------------------------
    # Event handling: Grok streaming-json (JSONL) -> DB + bus
    #
    # Grok's streaming-json mirrors the Claude Code / Anthropic agent event
    # shape: a `system` (subtype=init) event carries the session id; `assistant`
    # events carry a `message` whose `content` is a list of blocks (text +
    # tool_use); `user` events carry tool_result blocks; a final `result`
    # (subtype=success) event closes the turn. The handlers below stay defensive
    # about field names so minor CLI revisions keep parsing.
    # ------------------------------------------------------------------
    def _handle_event(self, env, session, evt, st):
        etype = evt.get("type") or ""

        # init / system carries the Grok session id used to resume later turns.
        if etype in ("system", "init", "session"):
            self._capture_session_id(session, evt)
            return

        if etype == "assistant":
            self._handle_assistant(env, session, evt, st)
            return

        if etype == "user":
            # User-role events in the stream carry tool_result blocks.
            self._handle_user(env, session, evt, st)
            return

        # Some builds emit flat tool events too; handle them defensively.
        if etype in ("tool_use", "tool_call"):
            self._upsert_tool_call(env, session, evt, st)
            return
        if etype == "tool_result":
            self._patch_tool_result(env, session, evt, st)
            return

        if etype == "result":
            self._capture_session_id(session, evt)
            subtype = evt.get("subtype")
            if evt.get("is_error") or (subtype and subtype not in ("success",)):
                self._fail(session, self._error_text(
                    evt.get("error") or evt.get("result") or subtype))
                return
            text = evt.get("result")
            if isinstance(text, str) and text.strip():
                rec = self._ensure_assistant(env, session, st)
                if not (rec.body or "").strip():
                    rec.body = text
                    self._emit_message(session, rec)
            session.write({"state": "done"})
            self._emit(session, {"kind": "done"})
            return

        if etype == "error":
            self._fail(session, self._error_text(evt.get("error") or evt))
            return

    def _capture_session_id(self, session, evt):
        sid = (evt.get("session_id") or evt.get("sessionId")
               or (evt.get("session") or {}).get("id"))
        if sid and session.grok_session_id != sid:
            session.grok_session_id = sid

    def _handle_assistant(self, env, session, evt, st):
        """An assistant event: a message whose content is a list of text and
        tool_use blocks. Append text to the turn's single assistant record and
        upsert any tool_use blocks into its tool_calls list."""
        msg = evt.get("message") if isinstance(evt.get("message"), dict) else evt
        content = msg.get("content")
        touched = False
        if isinstance(content, str):
            if content:
                rec = self._ensure_assistant(env, session, st)
                rec.body = (rec.body or "") + content
                touched = True
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype in ("text", "output_text") or block.get("text"):
                    text = block.get("text") or block.get("content") or ""
                    if isinstance(text, str) and text:
                        rec = self._ensure_assistant(env, session, st)
                        rec.body = (rec.body or "") + text
                        touched = True
                elif btype in ("tool_use", "tool_call"):
                    self._upsert_tool_call(env, session, block, st)
                # `thinking`/`reasoning` blocks are intentionally dropped.
        if touched and st["msg_id"]:
            self._emit_message(session, env["grokoo.message"].browse(st["msg_id"]))

    def _handle_user(self, env, session, evt, st):
        """A user-role event in the stream carries tool_result blocks (the
        outcome of the Odoo MCP tool calls)."""
        msg = evt.get("message") if isinstance(evt.get("message"), dict) else evt
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._patch_tool_result(env, session, block, st)

    def _handle_final_json(self, env, session, raw, st):
        """Parse a single `--output-format json` object.

        Grok prints a final object whose assistant text lives under `text`
        (also accepts `result`/`response` defensively)."""
        raw = (raw or "").strip()
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except ValueError:
            # Not JSON (older CLI / plain text): treat the whole output as text.
            rec = self._ensure_assistant(env, session, st)
            rec.body = raw[:20000]
            self._emit_message(session, rec)
            return
        if obj.get("is_error") or obj.get("error"):
            self._fail(session, self._error_text(obj.get("error") or obj.get("text")))
            return
        self._capture_session_id(session, obj)
        text = obj.get("text") or obj.get("result") or obj.get("response") or ""
        if text:
            rec = self._ensure_assistant(env, session, st)
            rec.body = text
            self._emit_message(session, rec)

    def _upsert_tool_call(self, env, session, evt, st):
        rec = self._ensure_assistant(env, session, st)
        calls = list(rec.tool_calls or [])
        item_id = (evt.get("id") or evt.get("tool_use_id")
                   or evt.get("tool_call_id") or evt.get("call_id")
                   or evt.get("callId") or evt.get("tool_id"))
        name = (evt.get("name") or evt.get("tool_name")
                or evt.get("tool") or "tool")
        # Strip a leading MCP server namespace for display. Grok exposes MCP
        # tools as "mcp__<server>__<tool>" (e.g. mcp__odoo__orm_read); keep the
        # bare tool name for the chat transcript.
        if isinstance(name, str):
            for prefix in ("mcp__odoo__", "mcp_odoo_", "odoo__", "odoo.", "odoo_"):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
        entry = {
            "id": item_id or ("tool-%s" % len(calls)),
            "name": name,
            "input": (evt.get("input") or evt.get("arguments")
                      or evt.get("args") or evt.get("parameters") or {}),
            "result": None,
            "status": "running",
        }
        idx = st["tool_idx"].get(entry["id"])
        if idx is None:
            st["tool_idx"][entry["id"]] = len(calls)
            calls.append(entry)
        else:
            entry["result"] = calls[idx].get("result")
            entry["status"] = calls[idx].get("status") or "running"
            calls[idx] = entry
        rec.tool_calls = calls
        self._emit_message(session, rec)

    def _patch_tool_result(self, env, session, evt, st):
        item_id = (evt.get("tool_use_id") or evt.get("tool_call_id")
                   or evt.get("id") or evt.get("call_id") or evt.get("callId")
                   or evt.get("tool_id"))
        idx = st["tool_idx"].get(item_id)
        if idx is None or not st["msg_id"]:
            return
        rec = env["grokoo.message"].browse(st["msg_id"])
        calls = list(rec.tool_calls or [])
        if idx >= len(calls):
            return
        result = (evt.get("content") if evt.get("content") is not None
                  else evt.get("result") if evt.get("result") is not None
                  else evt.get("output"))
        result = self._flatten_result(result)
        is_error = bool(evt.get("is_error") or evt.get("isError")
                        or evt.get("error") or evt.get("status") == "error")
        calls[idx]["result"] = (str(result) if result is not None else "")[:4000]
        calls[idx]["status"] = "error" if is_error else "done"
        rec.tool_calls = calls
        self._emit_message(session, rec)

    @staticmethod
    def _flatten_result(result):
        """Tool results may be a string, a dict, or a list of content blocks
        ([{type:text, text:...}]). Flatten to a string for display."""
        if isinstance(result, list):
            parts = []
            for b in result:
                if isinstance(b, dict):
                    parts.append(b.get("text") or b.get("content")
                                 or json.dumps(b, default=str))
                else:
                    parts.append(str(b))
            return "".join(parts)
        if isinstance(result, dict):
            return json.dumps(result, default=str)
        return result

    def _fail(self, session, text):
        text = text or "Grok run failed."
        session.write({"state": "error", "last_error": text[:2000]})
        self._emit(session, {"kind": "error", "error": text[:500]})

    @staticmethod
    def _error_text(err):
        if isinstance(err, dict):
            return (err.get("message") or err.get("error")
                    or json.dumps(err, default=str))
        return err if isinstance(err, str) else "Grok run failed."

    def _ensure_assistant(self, env, session, st):
        """Lazily create (once per turn) the assistant message record."""
        if st["msg_id"]:
            return env["grokoo.message"].browse(st["msg_id"])
        rec = env["grokoo.message"].create({
            "session_id": session.id,
            "role": "assistant",
            "body": "",
        })
        st["msg_id"] = rec.id
        return rec

    # ------------------------------------------------------------------
    # Bus
    # ------------------------------------------------------------------
    def _emit(self, session, payload):
        payload = dict(payload, session_id=session.id)
        # Push to the user's partner channel via bus.bus._sendone
        # (frontend filters by the "grokoo" type).
        self.env["bus.bus"]._sendone(
            session.user_id.partner_id, "grokoo", payload)

    def _emit_message(self, session, rec):
        self._emit(session, {
            "kind": "message",
            "message": rec._to_frontend()[0],
        })

    # ------------------------------------------------------------------
    # Command / env / config construction
    # ------------------------------------------------------------------
    def _build_argv(self, ctx, prompt_path):
        """Build the headless `grok` argv for this turn.

        The Odoo MCP server comes from the per-session `.grok/config.toml` in the
        scratch cwd (see _write_config). The prompt is read from a file via
        `--prompt-file`, so the SYSTEM/IDENTITY block (which starts with `---`)
        can never be mistaken for a CLI flag.

        `--always-approve` auto-approves tool executions (there is no TTY to
        approve them in headless mode); combined with the built-in tool allowlist
        (`--tools`, file reads only) the only capabilities the model has are the
        loopback-only, token-gated Odoo MCP tools (which run under the acting
        user's own ACLs) plus reading the user's attachments in the workspace.
        `--output-format` + `--prompt-file` put the CLI in single-turn headless
        mode; `-r <id>` resumes the prior Grok session when we have its id.
        """
        argv = [ctx["cli_path"], "--output-format", ctx["output_format"]]
        if ctx["model"]:
            argv += ["--model", ctx["model"]]
        argv += ["--always-approve"]
        # Allowlist of built-in tools (file reads + any granted web tools). MCP
        # tools are unaffected by --tools, so the Odoo tools stay available.
        if ctx["core_tools"]:
            base = [t.strip() for t in ctx["core_tools"].split(",") if t.strip()]
        else:
            base = list(CORE_TOOLS_BASE)
        allowed_builtins = base + ctx["web_builtins"]
        argv += ["--tools", ",".join(allowed_builtins)]
        # Belt-and-suspenders: turn web search/fetch off unless explicitly granted
        # (they are also absent from the allowlist above).
        if not ctx["web_builtins"]:
            argv += ["--disable-web-search"]
        argv += ["--prompt-file", prompt_path]
        if ctx["resuming"]:
            argv += ["-r", ctx["grok_session_id"]]
        argv += ctx["extra_args"]
        return argv

    def _write_config(self, ctx):
        """Write `<scratch>/.grok/config.toml` for this turn.

        Holds the Odoo MCP server definition (a stdio server: the bundled bridge
        script run under the configured Python). The bridge bearer token is NOT
        written to the file — it is passed through the process environment (see
        _build_env), which Grok forwards to the stdio MCP child process. This
        keeps the secret out of any on-disk file the model's read_file tool could
        open in the workspace.
        """
        bridge_env = {
            "AI_ODOO_BASE": ctx["base_url"],
            "AI_ODOO_DB": ctx["target_db"],
            "AI_ODOO_SID": ctx["routing_sid"],
            "AI_SESSION_ID": str(ctx["session_id"]),
            "AI_ALLOWED_TOOLS": ",".join(
                t for t in ctx["allowed_tools"] if t not in WEB_TOOLS),
            "AI_EXCLUDED_MODELS": ",".join(ctx["excluded_models"]),
        }
        lines = [
            "[mcp_servers.odoo]",
            "command = %s" % self._toml_str(ctx["python_bin"]),
            "args = [%s]" % self._toml_str(ctx["bridge_script"]),
            "enabled = true",
            "",
            "[mcp_servers.odoo.env]",
        ]
        for key in sorted(bridge_env):
            lines.append("%s = %s" % (key, self._toml_str(bridge_env[key])))
        toml = "\n".join(lines) + "\n"
        gdir = os.path.join(ctx["scratch"], ".grok")
        os.makedirs(gdir, mode=0o700, exist_ok=True)
        path = os.path.join(gdir, "config.toml")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(toml)
        return path

    @staticmethod
    def _toml_str(value):
        """Render a Python string as a TOML basic string (double-quoted)."""
        s = str(value or "")
        s = s.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
        return '"%s"' % s

    def _write_prompt(self, ctx):
        """Write the composed prompt to a file passed via `--prompt-file`.

        Lives in the scratch root (not the uploads dir) and is removed after the
        run. It carries no secrets — only the system prompt, the identity block,
        any replayed history, and the user message."""
        path = os.path.join(ctx["scratch"], "grok_prompt.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self._compose_prompt(ctx))
        return path

    def _compose_prompt(self, ctx):
        """Fold the system prompt, identity block, prior history (when not
        resuming) and the user message into the prompt file.

        The full system prompt is included on the first turn or whenever we are
        not resuming a live Grok session; the identity block (which reflects the
        current tool grant) is included every turn."""
        parts = []
        if ctx["is_first"] or not ctx["resuming"]:
            parts.append(SYSTEM_PROMPT)
        parts.append(ctx["identity"])
        if ctx["history"]:
            parts.append("--- CONVERSATION SO FAR ---\n" + ctx["history"])
        parts.append("--- USER MESSAGE ---\n" + ctx["prompt"])
        return "\n\n".join(parts)

    def _build_env(self, ctx):
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            # The real bridge token, forwarded to the stdio MCP child by Grok
            # (kept out of any on-disk file).
            "AI_BRIDGE_TOKEN": ctx["token"],
        }
        # HOME (-> <home>/.grok holding auth.json) + auth (XAI_API_KEY).
        env.update(ctx["grok_env"])
        return env
