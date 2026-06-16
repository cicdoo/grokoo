# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import fnmatch
import json
import logging
import re
import time

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

# Hard caps on how many rows a single tool call may return. Kept deliberately
# modest: every returned row is fed back into the CLI's context and accumulates
# in its (Bun/JS) heap across a conversation, so oversized results were a primary
# driver of "API Error: Out of memory" CLI crashes on memory-constrained hosts.
MAX_LIMIT = 50
SQL_MAX_ROWS = 1000

# Read-only model methods callable via orm_call.
SAFE_METHODS = {
    "name_search", "search_count", "read_group", "fields_get",
    "default_get", "search_read", "read", "name_get", "get_views",
    "fields_view_get", "web_search_read", "web_read_group",
}

# --- SQL read-only validation -------------------------------------------------
_DENY_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"comment|reindex|vacuum|analyze|cluster|copy|do|call|execute|prepare|"
    r"deallocate|listen|notify|set|reset|lock|refresh|import|into)\b",
    re.IGNORECASE)
_DENY_FUNCS = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|"
    r"lo_export|lo_get|lo_put|dblink|dblink_exec|pg_sleep|pg_terminate_backend|"
    r"pg_cancel_backend|pg_reload_conf|set_config|nextval|setval|"
    r"pg_logical_emit_message)\b",
    re.IGNORECASE)
_FOR_LOCK = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b",
                       re.IGNORECASE)


def _strip_sql_literals(sql):
    """Remove comments and string/dollar-quoted literals so keyword checks aren't
    fooled by their contents."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", " ", sql, flags=re.DOTALL)  # dollar-quoted
    sql = re.sub(r"'(?:[^']|'')*'", " ", sql)  # single-quoted strings
    sql = re.sub(r'"(?:[^"]|"")*"', " ", sql)  # quoted identifiers
    return sql


def _validate_select(query):
    if not query or len(query) > 20000:
        raise ValidationError("Query missing or too long.")
    bare = _strip_sql_literals(query).strip().rstrip(";")
    if ";" in bare:
        raise ValidationError("Multiple statements are not allowed.")
    if not re.match(r"^\s*(select|with)\b", bare, re.IGNORECASE):
        raise ValidationError("Only SELECT/WITH queries are allowed.")
    if _DENY_KEYWORDS.search(bare):
        raise ValidationError("Query contains a disallowed (write) keyword.")
    if _DENY_FUNCS.search(bare):
        raise ValidationError("Query references a disallowed function.")
    if _FOR_LOCK.search(bare):
        raise ValidationError("Row-locking clauses are not allowed.")
    return True


class AiAssistantTools(http.Controller):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _guard(self):
        """Hard invariant: tool endpoints must NEVER run as superuser."""
        if request.env.su:
            raise AccessError("Tool endpoint running as superuser — aborting.")
        return request.grokoo_session

    def _check_tool(self, tool):
        """Refuse a tool the acting user is not granted (per-user + zero-trust).

        Authoritative gate: --allowedTools / the bridge are only advisory. Called
        inside the _do closure so denials are audited as access_denied via _run."""
        if tool not in request.grokoo_session._effective_tools():
            raise AccessError("Tool '%s' is not enabled for you." % tool)

    def _check_model(self, model):
        """Validate the model exists and is not on the global AI denylist."""
        if model not in request.env:
            raise UserError("Unknown model: %s" % model)
        if model in request.env["grokoo.session"]._ai_excluded_models():
            raise AccessError(
                "Model '%s' is not accessible to the AI Assistant." % model)

    @staticmethod
    def _check_access(records, operation):
        """Enforce model ACLs and (for a concrete recordset) record rules.

        Odoo 18 exposes a unified ``check_access`` that validates both the
        model-level ACL and the record rules in one call, raising AccessError on
        denial. ``records`` may be an empty recordset standing in for the bare
        model, in which case only the ACL is checked (there are no records to
        evaluate rules against)."""
        records.check_access(operation)

    def _check_action_method(self, method):
        """Gate a business/action method: block private/dunder unconditionally,
        then require a match against the admin-configured allowlist patterns."""
        if not method or method.startswith("_"):
            raise ValidationError("Private/dunder methods are not callable.")
        patterns = request.env["grokoo.session"]._ai_action_method_patterns()
        if not any(fnmatch.fnmatchcase(method, p) for p in patterns):
            raise ValidationError(
                "Method '%s' is not in the AI action allowlist." % method)

    def _resolve_server_action(self, action):
        """Resolve a server action given a DB id (int/digit) or an xmlid."""
        env = request.env
        if isinstance(action, bool):
            raise ValidationError("action must be a server-action id or xmlid.")
        if isinstance(action, int) or (isinstance(action, str) and action.isdigit()):
            sa = env["ir.actions.server"].browse(int(action))
        elif isinstance(action, str) and "." in action:
            try:
                sa = env.ref(action)
            except ValueError:
                raise UserError("Unknown server action xmlid: %s" % action)
        else:
            raise ValidationError("action must be a server-action id or xmlid.")
        if not sa or sa._name != "ir.actions.server" or not sa.exists():
            raise UserError("Server action not found: %s" % action)
        return sa

    @staticmethod
    def _serialize_result(result):
        """Coerce a method/action return value into one JSON-safe shape."""
        if hasattr(result, "_name") and hasattr(result, "ids"):
            return {"result_type": "recordset",
                    "model": result._name, "ids": result.ids}
        if isinstance(result, dict):
            # Action dicts often carry non-serializable context values; coerce
            # them to strings rather than letting json.dumps raise downstream.
            safe = json.loads(json.dumps(result, default=str))
            return {"result_type": "action", "action": safe}
        return {"result_type": "value", "result": result}

    def _audit(self, started, vals):
        vals["duration_ms"] = int((time.time() - started) * 1000)
        vals["client_ip"] = request.httprequest.remote_addr
        session = getattr(request, "grokoo_session", None)
        if session:
            vals["session_id"] = session.id
        vals["user_id"] = request.env.uid
        request.env["grokoo.tool_log"].sudo()._record(vals)

    def _run(self, tool, fn, base_vals):
        started = time.time()
        try:
            result = fn()
            self._audit(started, dict(base_vals, tool=tool, outcome="success",
                                      result_count=self._count(result)))
            return result
        except AccessError as e:
            self._audit(started, dict(base_vals, tool=tool,
                                      outcome="access_denied", error_message=str(e)))
            return {"error": "AccessError: %s" % e}
        except (ValidationError, UserError) as e:
            self._audit(started, dict(base_vals, tool=tool,
                                      outcome="validation_rejected",
                                      error_message=str(e)))
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            _logger.exception("AI tool %s failed", tool)
            self._audit(started, dict(base_vals, tool=tool, outcome="error",
                                      error_message=str(e)))
            return {"error": "%s: %s" % (type(e).__name__, e)}

    @staticmethod
    def _count(result):
        if isinstance(result, dict) and "records" in result:
            return len(result["records"])
        if isinstance(result, dict) and "rows" in result:
            return result.get("row_count", 0)
        if isinstance(result, dict) and result.get("result_type") == "recordset":
            return len(result.get("ids") or [])
        if isinstance(result, list):
            return len(result)
        return 0

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    @http.route("/grokoo/tool/model_introspect", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def model_introspect(self, model=None, **kw):
        self._guard()

        def _do():
            self._check_tool("model_introspect")
            self._check_model(model)
            Model = request.env[model]
            self._check_access(Model, "read")
            fg = Model.fields_get(attributes=[
                "string", "type", "required", "relation", "readonly",
                "selection", "help"])
            access = {op: Model.has_access(op)
                      for op in ("read", "write", "create", "unlink")}
            # AND the raw Odoo ACL with whether the matching AI tool is enabled,
            # so the model only attempts actions it can actually perform here.
            eff = request.grokoo_session._effective_tools()
            tool_for = {"read": "orm_read", "write": "orm_write",
                        "create": "orm_create", "unlink": "orm_unlink"}
            effective_access = {op: bool(access[op] and tool_for[op] in eff)
                                for op in access}
            return {"model": model, "access": access,
                    "effective_access": effective_access,
                    "ai_tools_allowed": sorted(eff), "fields": fg}

        return self._run("model_introspect", _do, {"model_name": model})

    @http.route("/grokoo/tool/orm_search_read", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_search_read(self, model=None, domain=None, fields=None,
                        limit=80, offset=0, order="", **kw):
        self._guard()

        def _do():
            self._check_tool("orm_search_read")
            self._check_model(model)
            dom = domain or []
            if not isinstance(dom, list):
                raise ValidationError("domain must be a list.")
            lim = min(int(limit or 80), MAX_LIMIT)
            Model = request.env[model]
            self._check_access(Model, "read")
            records = Model.search(dom, offset=int(offset or 0), limit=lim,
                                   order=order or None)
            data = records.read(fields or [])
            total = Model.search_count(dom)
            return {"records": data, "returned": len(data), "total": total}

        return self._run("orm_search_read", _do,
                         {"model_name": model, "arguments": {
                             "domain": domain, "fields": fields,
                             "limit": limit, "offset": offset, "order": order}})

    @http.route("/grokoo/tool/orm_read", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_read(self, model=None, ids=None, fields=None, **kw):
        self._guard()

        def _do():
            self._check_tool("orm_read")
            self._check_model(model)
            recs = request.env[model].browse(ids or [])
            self._check_access(recs, "read")
            return {"records": recs.read(fields or [])}

        return self._run("orm_read", _do,
                         {"model_name": model, "record_ids": str(ids)})

    @http.route("/grokoo/tool/orm_call", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_call(self, model=None, method=None, args=None, kwargs=None, **kw):
        self._guard()

        def _do():
            self._check_tool("orm_call")
            self._check_model(model)
            if method not in SAFE_METHODS:
                raise ValidationError(
                    "Method '%s' is not in the read-only allowlist." % method)
            if method.startswith("_"):
                raise ValidationError("Private methods are not callable.")
            Model = request.env[model]
            self._check_access(Model, "read")
            result = getattr(Model, method)(*(args or []), **(kwargs or {}))
            # Recordsets aren't JSON serializable; surface ids.
            if hasattr(result, "_name") and hasattr(result, "ids"):
                return {"ids": result.ids}
            return {"result": result}

        return self._run("orm_call", _do,
                         {"model_name": model, "method": method})

    @http.route("/grokoo/tool/sql_select", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def sql_select(self, query=None, params=None, max_rows=1000, **kw):
        self._guard()

        def _do():
            self._check_tool("sql_select")
            if not request.env.user.has_group(
                    "grokoo.group_ai_sql_analyst"):
                raise AccessError(
                    "You are not a member of the AI SQL Analyst group.")
            _validate_select(query)
            cap = min(int(max_rows or 1000), SQL_MAX_ROWS)
            # Statement timeout (ms) is overridable per database via the system
            # parameter 'grokoo.sql_statement_timeout_ms' (default 10s).
            try:
                timeout_ms = max(1000, int(request.env["ir.config_parameter"].sudo()
                                           .get_param("grokoo.sql_statement_timeout_ms", 10000)))
            except (TypeError, ValueError):
                timeout_ms = 10000
            cr = request.env.cr
            with cr.savepoint():
                cr.execute("SET TRANSACTION READ ONLY")
                cr.execute("SET LOCAL statement_timeout = %s", (timeout_ms,))
                cr.execute(query, tuple(params or ()))
                rows = cr.fetchmany(cap + 1)
                truncated = len(rows) > cap
                rows = rows[:cap]
                cols = [d.name for d in cr.description] if cr.description else []
            return {
                "columns": cols,
                "rows": [list(r) for r in rows],
                "row_count": len(rows),
                "truncated": truncated,
            }

        return self._run("sql_select", _do, {"sql_text": query})

    # ------------------------------------------------------------------
    # Write tools — gated per-user (M2M + zero-trust) and by Odoo ACLs.
    # ------------------------------------------------------------------
    @http.route("/grokoo/tool/orm_create", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_create(self, model=None, values=None, **kw):
        self._guard()

        def _do():
            self._check_tool("orm_create")
            self._check_model(model)
            if not isinstance(values, dict):
                raise ValidationError("values must be an object.")
            Model = request.env[model]
            self._check_access(Model, "create")
            rec = Model.create(values)
            return {"ids": rec.ids}

        return self._run("orm_create", _do,
                         {"model_name": model, "arguments": {"values": values}})

    @http.route("/grokoo/tool/orm_write", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_write(self, model=None, ids=None, values=None, **kw):
        self._guard()

        def _do():
            self._check_tool("orm_write")
            self._check_model(model)
            if not isinstance(values, dict):
                raise ValidationError("values must be an object.")
            recs = request.env[model].browse(ids or [])
            self._check_access(recs, "write")
            recs.write(values)
            return {"written": len(recs), "ids": recs.ids}

        return self._run("orm_write", _do,
                         {"model_name": model, "record_ids": str(ids),
                          "arguments": {"values": values}})

    @http.route("/grokoo/tool/orm_unlink", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_unlink(self, model=None, ids=None, **kw):
        self._guard()

        def _do():
            self._check_tool("orm_unlink")
            self._check_model(model)
            recs = request.env[model].browse(ids or [])
            self._check_access(recs, "unlink")
            count = len(recs)
            recs.unlink()
            return {"unlinked": count}

        return self._run("orm_unlink", _do,
                         {"model_name": model, "record_ids": str(ids)})

    # ------------------------------------------------------------------
    # Action tools — invoke business logic (allowlisted methods / wizards /
    # server actions). Write-class: gated per-user + stripped by zero-trust.
    # ------------------------------------------------------------------
    @http.route("/grokoo/tool/orm_action", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def orm_action(self, model=None, ids=None, method=None,
                   args=None, kwargs=None, **kw):
        self._guard()

        def _do():
            self._check_tool("orm_action")
            self._check_model(model)
            self._check_action_method(method)
            if args is not None and not isinstance(args, list):
                raise ValidationError("args must be a list.")
            if kwargs is not None and not isinstance(kwargs, dict):
                raise ValidationError("kwargs must be an object.")
            Model = request.env[model]
            target = Model.browse(ids) if ids else Model
            self._check_access(target, "write")
            result = getattr(target, method)(*(args or []), **(kwargs or {}))
            return self._serialize_result(result)

        return self._run("orm_action", _do,
                         {"model_name": model, "record_ids": str(ids),
                          "method": method,
                          "arguments": {"args": args, "kwargs": kwargs}})

    @http.route("/grokoo/tool/run_wizard", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def run_wizard(self, model=None, values=None, method=None,
                   args=None, kwargs=None, **kw):
        self._guard()

        def _do():
            self._check_tool("run_wizard")
            self._check_model(model)
            self._check_action_method(method)
            if values is not None and not isinstance(values, dict):
                raise ValidationError("values must be an object.")
            if args is not None and not isinstance(args, list):
                raise ValidationError("args must be a list.")
            if kwargs is not None and not isinstance(kwargs, dict):
                raise ValidationError("kwargs must be an object.")
            Model = request.env[model]
            if not Model._transient:
                raise ValidationError(
                    "run_wizard only operates on transient (wizard) models.")
            self._check_access(Model, "create")
            # create() merges default_get; the button then runs as the acting
            # user so real-model ACLs/record rules still apply inside it.
            wizard = Model.create(values or {})
            result = getattr(wizard, method)(*(args or []), **(kwargs or {}))
            return self._serialize_result(result)

        return self._run("run_wizard", _do,
                         {"model_name": model, "method": method,
                          "arguments": {"values": values, "args": args,
                                        "kwargs": kwargs}})

    @http.route("/grokoo/tool/run_server_action", type="json",
                auth="grokoo_bridge", methods=["POST"])
    def run_server_action(self, action=None, ids=None, **kw):
        self._guard()

        def _do():
            self._check_tool("run_server_action")
            sa = self._resolve_server_action(action)
            allowed = request.env[
                "grokoo.session"]._ai_allowed_server_action_ids()
            if not allowed or sa.id not in allowed:
                raise AccessError(
                    "Server action %s is not allowlisted for the AI Assistant."
                    % action)
            target_model = sa.model_id.model
            self._check_model(target_model)
            Model = request.env[target_model]
            recs = Model.browse(ids or [])
            self._check_access(recs if ids else Model, "write")
            ctx = dict(request.env.context,
                       active_model=target_model,
                       active_ids=recs.ids,
                       active_id=recs.ids[0] if recs.ids else False)
            result = sa.with_context(**ctx).run()
            return self._serialize_result(result)

        return self._run("run_server_action", _do,
                         {"method": "ir.actions.server",
                          "record_ids": str(ids),
                          "arguments": {"action": str(action)}})
