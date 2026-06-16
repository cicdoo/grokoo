#!/usr/bin/env python3
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""Dependency-free stdio MCP server that forwards tool calls to Odoo.

Launched by the MCP client (the Grok CLI, via its mcpServers config) as a child process. It holds
NO database credentials and never imports Odoo: every tool call is forwarded over
loopback HTTP to the Odoo `ai_bridge` endpoints, authenticated with the short-lived
per-session bearer token passed in the environment.

Protocol: MCP over stdio = newline-delimited JSON-RPC 2.0.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# Opt-in wire logging: set AI_BRIDGE_DEBUG=/path/to/log to capture the stdio
# JSON-RPC traffic (timestamped) for diagnosing MCP handshake issues.
_DEBUG = os.environ.get("AI_BRIDGE_DEBUG")


def _dbg(direction, data):
    if not _DEBUG:
        return
    try:
        with open(_DEBUG, "a") as f:
            f.write("%.3f %s %s\n" % (time.time(), direction, str(data)[:600]))
    except OSError:
        pass

BASE = os.environ.get("AI_ODOO_BASE", "http://127.0.0.1:8069").rstrip("/")
TOKEN = os.environ.get("AI_BRIDGE_TOKEN", "")
# Target database + a session id pinned to it. Odoo resolves the database for a
# request from its session_id cookie, so sending this makes every callback reach
# the configured database even in multi-database deployments.
ODOO_DB = os.environ.get("AI_ODOO_DB", "")
ODOO_SID = os.environ.get("AI_ODOO_SID", "")
# Comma-separated allowlist of tool names this user may use (advisory UX; the
# Odoo controller enforces the same set authoritatively). Unset/empty => fall
# back to the read-only tools so a half-deployed upgrade fails safe.
_DEFAULT_ALLOWED = {"model_introspect", "orm_search_read", "orm_read", "orm_call"}
_raw_allowed = os.environ.get("AI_ALLOWED_TOOLS", "")
ALLOWED_TOOLS = ({t.strip() for t in _raw_allowed.split(",") if t.strip()}
                 or _DEFAULT_ALLOWED)

PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Tool definitions (input schemas advertised to the model)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "model_introspect",
        "description": "Inspect an Odoo model: its fields and your access rights "
                       "(read/write/create/unlink). Call this before querying an "
                       "unfamiliar model.",
        "inputSchema": {
            "type": "object",
            "properties": {"model": {"type": "string"}},
            "required": ["model"],
        },
    },
    {
        "name": "orm_search_read",
        "description": "Search and read records of an Odoo model. Honors the user's "
                       "access rights and record rules. domain is an Odoo domain "
                       "(list of triples).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "domain": {"type": "array", "default": []},
                "fields": {"type": "array", "items": {"type": "string"},
                           "default": []},
                "limit": {"type": "integer", "default": 80},
                "offset": {"type": "integer", "default": 0},
                "order": {"type": "string", "default": ""},
            },
            "required": ["model"],
        },
    },
    {
        "name": "orm_read",
        "description": "Read specific records by id from an Odoo model.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "fields": {"type": "array", "items": {"type": "string"},
                           "default": []},
            },
            "required": ["model", "ids"],
        },
    },
    {
        "name": "orm_call",
        "description": "Call a read-only model method (allowlisted: name_search, "
                       "read_group, search_count, fields_get, default_get).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "method": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "sql_select",
        "description": "Run a READ-ONLY SQL SELECT for reporting. Only SELECT/WITH "
                       "queries are allowed; any write is rejected. Available only to "
                       "members of the AI SQL Analyst group.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "params": {"type": "array", "default": []},
                "max_rows": {"type": "integer", "default": 1000},
            },
            "required": ["query"],
        },
    },
    {
        "name": "orm_create",
        "description": "Create a record on an Odoo model. Honors the user's access "
                       "rights. values is a dict of field -> value. Only available "
                       "when granted to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "values": {"type": "object"},
            },
            "required": ["model", "values"],
        },
    },
    {
        "name": "orm_write",
        "description": "Update existing records on an Odoo model. Honors the user's "
                       "access rights and record rules. Only available when granted "
                       "to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "values": {"type": "object"},
            },
            "required": ["model", "ids", "values"],
        },
    },
    {
        "name": "orm_unlink",
        "description": "Delete records of an Odoo model by id. Honors the user's "
                       "access rights and record rules. Only available when granted "
                       "to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["model", "ids"],
        },
    },
    {
        "name": "orm_action",
        "description": "Call an allowlisted business/action method (e.g. "
                       "action_confirm, action_post, button_validate) on a "
                       "recordset. Omit ids for a model-level call. Honors the "
                       "user's access rights. Only available when granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "default": []},
                "method": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "run_wizard",
        "description": "Create a wizard (transient model) with values and run its "
                       "allowlisted button method in one step. Honors the user's "
                       "access rights. Only available when granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "values": {"type": "object", "default": {}},
                "method": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            "required": ["model", "method"],
        },
    },
    {
        "name": "run_server_action",
        "description": "Run an admin-allowlisted Odoo server action "
                       "(ir.actions.server) against record ids. action is the "
                       "server action's id or xmlid. Only available when granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": ["integer", "string"]},
                "ids": {"type": "array", "items": {"type": "integer"},
                        "default": []},
            },
            "required": ["action"],
        },
    },
]


def _visible_tools():
    return [t for t in TOOLS if t["name"] in ALLOWED_TOOLS]


# ---------------------------------------------------------------------------
# HTTP forwarding to Odoo
# ---------------------------------------------------------------------------
def _call_odoo(tool_name, arguments):
    url = "%s/grokoo/tool/%s" % (BASE, tool_name)
    if ODOO_DB:
        url += "?db=%s" % urllib.parse.quote(ODOO_DB)
    envelope = {"jsonrpc": "2.0", "method": "call",
                "params": arguments or {}, "id": 1}
    data = json.dumps(envelope).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer %s" % TOKEN)
    if ODOO_SID:
        req.add_header("Cookie", "session_id=%s" % ODOO_SID)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %s: %s" % (e.code, e.reason)}
    except Exception as e:  # noqa: BLE001
        return {"error": "Bridge call failed: %s" % e}

    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        msg = err.get("data", {}).get("message") or err.get("message") or str(err)
        return {"error": msg}
    return payload.get("result", payload) if isinstance(payload, dict) else payload


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------
def _send(obj):
    _dbg(">>", obj)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def _handle(msg):
    method = msg.get("method")
    req_id = msg.get("id")

    if method == "initialize":
        # Echo the client's requested protocol version. Modern clients (e.g.
        # Grok) may send a newer version and will DROP this server's tools if
        # we answer with a different/older version, even though the handshake
        # otherwise succeeds. We support the protocol generically, so we accept
        # whatever the client offers (falling back to our baseline if absent).
        client_version = (msg.get("params") or {}).get("protocolVersion")
        _result(req_id, {
            "protocolVersion": client_version or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "odoo", "version": "1.0.0"},
        })
    elif method in ("notifications/initialized", "initialized"):
        pass  # notification, no response
    elif method == "ping":
        _result(req_id, {})
    elif method == "tools/list":
        _result(req_id, {"tools": _visible_tools()})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in {t["name"] for t in _visible_tools()}:
            _error(req_id, -32601, "Unknown tool: %s" % name)
            return
        out = _call_odoo(name, arguments)
        is_error = isinstance(out, dict) and "error" in out and len(out) == 1
        text = json.dumps(out, default=str, ensure_ascii=False)
        _result(req_id, {
            "content": [{"type": "text", "text": text}],
            "isError": bool(is_error),
        })
    elif req_id is not None:
        _error(req_id, -32601, "Method not found: %s" % method)


def main():
    # NB: use readline() rather than `for line in sys.stdin`. Iterating a text
    # stream uses a hidden read-ahead buffer that withholds already-received
    # lines until the buffer fills or EOF — which deadlocks a request/response
    # stdio protocol like MCP (the client waits for our reply to `initialize`
    # while we wait for more input). readline() returns each line as soon as it
    # arrives, so the handshake completes and the tools attach reliably.
    _dbg("--", "bridge started, waiting for stdin")
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF — client closed the pipe
            _dbg("--", "EOF on stdin, exiting")
            break
        line = line.strip()
        if not line:
            continue
        _dbg("<<", line)
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001
            if msg.get("id") is not None:
                _error(msg.get("id"), -32603, "Internal error: %s" % e)


if __name__ == "__main__":
    main()
