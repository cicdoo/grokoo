# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Tools the model is allowed to call (Phase 1 = read-only). Kept in sync with the
# MCP forwarder's advertised tools and the CLI --allowedTools list.
READ_TOOLS = [
    "orm_search_read",
    "orm_read",
    "orm_call",
    "model_introspect",
    "sql_select",
]

# Mutating tools (Phase 2). Each is individually grantable per user and is
# stripped entirely when zero-trust mode is on.
WRITE_TOOLS = [
    "orm_create",
    "orm_write",
    "orm_unlink",
    "orm_action",
    "run_wizard",
    "run_server_action",
]

# Built-in web tools (Grok's native web_fetch / web_search). Unlike the
# ORM tools these are NOT served by the odoo MCP bridge — they are the CLI's own
# built-ins, withheld by default. They are opt-in per user (granted via
# res.users.grokoo_tool_ids) and deliberately kept OUT of READ_TOOLS so an empty
# selection never auto-grants web access. The runner keeps them OFF the built-in
# tool denylist (see grokoo_runner.WEB_TOOL_BUILTINS) only when granted.
WEB_TOOLS = ["web_fetch", "web_search"]

ALL_TOOLS = READ_TOOLS + WRITE_TOOLS + WEB_TOOLS
# sql_select is read-only (SELECT-only), so it survives zero-trust mode. Web
# tools don't mutate Odoo data, so they survive zero-trust too; move them out of
# this set if web access should be stripped under zero-trust.
READONLY_TOOL_SET = set(READ_TOOLS) | set(WEB_TOOLS)

# Default fnmatch patterns for methods orm_action/run_wizard may invoke. Used
# when the grokoo.action_methods config parameter is unset.
DEFAULT_ACTION_METHOD_PATTERNS = ["action_*", "button_*"]

# The Grok CLI's built-in tool surface is restricted via the per-session
# `--disallowed-tools` denylist the runner passes (everything but file reads is
# withheld). The authoritative boundary stays server-side: every Odoo mutation
# goes through the loopback-only, token-gated tool endpoints and runs under the
# user's own ACLs.

# Fallback glob for the grok binary when it is not on PATH and no explicit
# `grokoo.cli_path` is set. The official installer puts it at ~/.grok/bin/grok;
# adjust per deployment.
DEFAULT_CLI_GLOB = "/usr/local/bin/grok"

# How much prior conversation to replay into the prompt when we cannot resume a
# live Grok session (headless does not always emit a session id). Bounded so
# long threads don't blow the prompt size.
HISTORY_MAX_MESSAGES = 12
HISTORY_MAX_CHARS = 8000

# --- File upload support -------------------------------------------------
# Files the user may attach to a turn. The CLI's native read_file/view_image
# tools understand these (text/code verbatim, images visually, PDFs), and the
# attachments are written to the per-session uploads dir referenced in the prompt.
UPLOAD_TEXT_EXTS = {
    ".txt", ".csv", ".tsv", ".json", ".xml", ".md", ".markdown", ".rst",
    ".log", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".toml", ".env",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css", ".scss",
    ".sql", ".sh", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rb", ".php",
}
UPLOAD_PDF_EXTS = {".pdf"}
UPLOAD_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
ALLOWED_UPLOAD_EXTS = UPLOAD_TEXT_EXTS | UPLOAD_PDF_EXTS | UPLOAD_IMAGE_EXTS

# Subdirectory of the scratch dir that holds uploaded files. Attachments are
# referenced to the model by their absolute path under this dir; the scratch
# root also holds the per-turn .grok/config.toml and the prompt file.
UPLOADS_SUBDIR = "uploads"

DEFAULT_MAX_UPLOAD_MB = 15


class AiAssistantSession(models.Model):
    _name = "grokoo.session"
    _description = "Grokoo AI Assistant Conversation Session"
    _order = "write_date desc, id desc"

    name = fields.Char(default=lambda self: _("New conversation"), required=True)
    user_id = fields.Many2one(
        "res.users", string="User", required=True, ondelete="cascade",
        default=lambda self: self.env.user, index=True,
    )
    # Stable, locally-minted UUID used to name this session's scratch directory.
    # Independent of the Grok session id (which Grok only assigns on a run).
    scratch_uuid = fields.Char(
        string="Scratch UUID", copy=False, index=True,
        help="Locally-minted UUID naming this session's scratch working directory.",
    )
    # Grok's own session id, captured from the `system`/init streaming-json
    # event and passed to `grok -r <id>` on later turns. When absent we replay
    # the conversation history into the prompt instead.
    grok_session_id = fields.Char(
        string="Grok Session ID", copy=False, index=True,
        help="Session id Grok assigns on a run; used to resume the conversation.",
    )
    state = fields.Selection(
        [("idle", "Idle"), ("running", "Running"),
         ("done", "Done"), ("error", "Error")],
        default="idle", required=True, copy=False,
    )
    turn_count = fields.Integer(default=0, copy=False)
    working_dir = fields.Char(copy=False)
    last_run_pid = fields.Integer(copy=False)
    last_error = fields.Text(copy=False)

    message_ids = fields.One2many(
        "grokoo.message", "session_id", string="Messages")

    # --- Bridge token bookkeeping (only the jti/salt are stored, never the token) ---
    bridge_jti = fields.Char(copy=False, groups="base.group_system")
    bridge_salt = fields.Char(copy=False, groups="base.group_system")
    bridge_exp = fields.Integer(copy=False, groups="base.group_system")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault("scratch_uuid", str(uuid.uuid4()))
        return super().create(vals_list)

    @api.model
    def _cron_reap_stale(self):
        """Reset sessions stuck in 'running' past the timeout and GC old scratch dirs."""
        timeout = int(self._config("timeout_s", 900))
        cutoff = fields.Datetime.now() - __import__("datetime").timedelta(
            seconds=timeout + 120)
        stale = self.search([("state", "=", "running"), ("write_date", "<", cutoff)])
        for session in stale:
            if session.last_run_pid:
                try:
                    os.kill(session.last_run_pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass
            session.write({"state": "error", "last_run_pid": False,
                           "last_error": "Reaped by cron (stale run)."})
        return True

    def action_stop(self):
        """Terminate the running CLI subprocess for this session, if any."""
        self.ensure_one()
        if self.last_run_pid:
            try:
                os.kill(self.last_run_pid, 15)  # SIGTERM
            except (ProcessLookupError, PermissionError):
                pass
        self.write({"state": "idle", "last_run_pid": False})
        return True

    # ------------------------------------------------------------------
    # Scratch working directory (CLI cwd) — NOT under /opt/odoo source
    # ------------------------------------------------------------------
    def _get_scratch_dir(self):
        self.ensure_one()
        root = self._config("scratch_root", "/var/lib/odoo/grokoo_scratch")
        path = os.path.join(root, self.scratch_uuid)
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as e:
            raise UserError(_("Cannot create scratch dir %s: %s") % (path, e))
        if self.working_dir != path:
            self.working_dir = path
        return path

    def _uploads_dir(self, create=True):
        """Directory (under scratch) holding this session's uploaded files.

        Kept in a dedicated subdir so the Read-tool allow rule can be scoped to
        it without exposing the scratch root (which holds mcp.json with the
        bridge token)."""
        self.ensure_one()
        path = os.path.join(self._get_scratch_dir(), UPLOADS_SUBDIR)
        if create:
            try:
                os.makedirs(path, mode=0o700, exist_ok=True)
            except OSError as e:
                raise UserError(_("Cannot create uploads dir %s: %s") % (path, e))
        return path

    # ------------------------------------------------------------------
    # File uploads
    # ------------------------------------------------------------------
    @api.model
    def _max_upload_bytes(self):
        return int(self._config("max_upload_mb", DEFAULT_MAX_UPLOAD_MB)) * 1024 * 1024

    @staticmethod
    def _safe_upload_name(filename):
        """A traversal-safe basename, restricted to the allowed extensions."""
        base = os.path.basename(filename or "").strip().replace("\x00", "")
        # Collapse anything that isn't a sane filename char.
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".") or "file"
        ext = os.path.splitext(base)[1].lower()
        if ext not in ALLOWED_UPLOAD_EXTS:
            raise UserError(_(
                "File type '%s' is not supported. You can upload text/CSV/code, "
                "PDF and image (PNG/JPG/GIF/WebP) files.") % (ext or base))
        return base

    def _store_upload(self, filename, content):
        """Validate and persist one uploaded file as an ir.attachment linked to
        this session, and write it to the per-session uploads dir.

        Returns the frontend descriptor dict."""
        self.ensure_one()
        safe = self._safe_upload_name(filename)
        if not content:
            raise UserError(_("Uploaded file '%s' is empty.") % safe)
        max_bytes = self._max_upload_bytes()
        if len(content) > max_bytes:
            raise UserError(_(
                "File '%s' is too large (limit %s MB).")
                % (safe, max_bytes // (1024 * 1024)))
        att = self.env["ir.attachment"].create({
            "name": safe,
            "raw": content,
            "res_model": self._name,
            "res_id": self.id,
        })
        self._write_attachment_to_disk(att)
        return self._attachment_descriptor(att)

    def _attachment_descriptor(self, att):
        return {"id": att.id, "name": att.name, "mimetype": att.mimetype or ""}

    def _write_attachment_to_disk(self, att):
        """Materialize one attachment into the uploads dir; return its abs path.

        The on-disk name is prefixed with the attachment id so distinct uploads
        with the same filename never collide."""
        self.ensure_one()
        disk_name = "%s_%s" % (att.id, self._safe_upload_name(att.name))
        path = os.path.join(self._uploads_dir(), disk_name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(att.raw or b"")
        return path

    def _materialize_attachments(self, attachment_ids):
        """Ensure the given (own) attachments exist on disk.

        Returns ``(abs_paths, attachments)``; ids that aren't attachments of
        this session are ignored."""
        self.ensure_one()
        if not attachment_ids:
            return [], self.env["ir.attachment"]
        atts = self.env["ir.attachment"].browse(attachment_ids).exists().filtered(
            lambda a: a.res_model == self._name and a.res_id == self.id)
        paths = [self._write_attachment_to_disk(a) for a in atts]
        return paths, atts

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @api.model
    def _config(self, key, default=None):
        val = self.env["ir.config_parameter"].sudo().get_param(
            "grokoo.%s" % key)
        return val if val not in (None, False, "") else default

    # ------------------------------------------------------------------
    # Tool / model gating — single server-side source of truth.
    # Consumed by the controller (enforcement), the runner (--allowedTools +
    # bridge env) and the bridge (_visible_tools). Only the controller is
    # authoritative; the others are advisory UX.
    # ------------------------------------------------------------------
    def _effective_tools(self):
        """Tool names this session's user may actually invoke.

        Policy read (which tools / zero-trust) is done sudo — it only reads
        configuration, never bypasses the per-model check_access that the tool
        endpoints still run as the acting user."""
        self.ensure_one()
        return self.user_id.sudo()._grokoo_effective_tools()

    @api.model
    def _ai_excluded_models(self):
        """Global denylist of models the assistant may never touch."""
        raw = self._config("excluded_models", "") or ""
        return {m.strip() for m in raw.split(",") if m.strip()}

    @api.model
    def _ai_action_method_patterns(self):
        """fnmatch patterns for methods orm_action/run_wizard may call."""
        raw = self._config("action_methods", "") or ""
        pats = [p.strip() for p in raw.split(",") if p.strip()]
        return pats or DEFAULT_ACTION_METHOD_PATTERNS

    @api.model
    def _ai_allowed_server_action_ids(self):
        """ir.actions.server ids run_server_action may execute (empty = none)."""
        raw = self._config("server_action_ids", "") or ""
        return {int(x) for x in raw.split(",") if x.strip().isdigit()}

    def _target_db(self):
        """The database the bridge should connect back to.

        Defaults to the current database (where this session and its user live).
        An admin may pin it explicitly for multi-database deployments — it must
        name the same database the assistant runs in.
        """
        return self._config("db_name") or self.env.cr.dbname

    def _mint_routing_sid(self, db):
        """Create a short anonymous Odoo session pinned to ``db`` and return its
        sid. The bridge sends it as a session_id cookie so its loopback calls
        resolve to this exact database (reliable even in multi-db setups).

        The session carries no user — the bridge token still establishes the
        acting user — only the database routing hint.
        """
        from odoo.http import root
        try:
            store = root.session_store
            sess = store.new()
            sess.db = db
            store.save(sess)
            return sess.sid
        except Exception:  # noqa: BLE001
            _logger.exception("AI assistant: could not mint routing session")
            return ""

    @api.model
    def _delete_routing_sid(self, sid):
        if not sid:
            return
        from odoo.http import root
        try:
            store = root.session_store
            store.delete(store.get(sid))
        except Exception:  # noqa: BLE001
            pass

    @api.model
    def _resolve_cli_path(self):
        """Explicit config param, else `grok` on PATH, else newest glob match."""
        explicit = self._config("cli_path")
        if explicit and os.path.exists(explicit):
            return explicit
        which = __import__("shutil").which("grok")
        if which:
            return which
        pattern = self._config("cli_glob", DEFAULT_CLI_GLOB)
        matches = glob.glob(pattern)
        if not matches:
            raise UserError(_(
                "Grok CLI not found. Install it on the Odoo server, then either "
                "put it on the service PATH or set the System Parameter "
                "'grokoo.cli_path' to its absolute path.\n\n"
                "Install:\n"
                "  • curl -fsSL https://x.ai/cli/install.sh | bash\n\n"
                "Then verify with 'grok --version'. "
                "Tried PATH and: %s"
            ) % pattern)
        return sorted(matches)[-1]

    def _grok_env(self):
        """Return env vars pointing the Grok CLI at THIS user's private home
        plus the user's chosen authentication.

        Each user's credentials/config live under ``<home_root>/<uid>/.grok``
        (the imported ``auth.json`` from `grok login` and/or an xAI API key).
        Isolating HOME per user keeps credentials, config and session state fully
        separate (Grok reads ``$HOME/.grok``), and lets the CLI refresh that
        user's OAuth token in place. ``_grokoo_auth_env`` adds XAI_API_KEY when
        an API key is configured.
        """
        self.ensure_one()
        user = self.user_id.sudo()
        home = user._grokoo_home_dir()
        env = {
            "HOME": home,  # Grok reads <home>/.grok (auth.json, config.toml)
        }
        env.update(user._grokoo_auth_env())
        return env

    def _history_for_prompt(self):
        """A compact text transcript of this session's prior turns, used to give
        the model context when we cannot resume a live Grok session.

        Returns "" for a fresh session. Bounded by HISTORY_MAX_MESSAGES /
        HISTORY_MAX_CHARS so long threads don't blow the prompt size."""
        self.ensure_one()
        msgs = self.message_ids.filtered(
            lambda m: m.role in ("user", "assistant") and (m.body or "").strip())
        if not msgs:
            return ""
        msgs = msgs.sorted("id")[-HISTORY_MAX_MESSAGES:]
        lines = []
        for m in msgs:
            who = "User" if m.role == "user" else "Assistant"
            lines.append("%s: %s" % (who, (m.body or "").strip()))
        text = "\n\n".join(lines)
        if len(text) > HISTORY_MAX_CHARS:
            text = "…" + text[-HISTORY_MAX_CHARS:]
        return text

    # ------------------------------------------------------------------
    # Bridge token: a short-lived, session-scoped HMAC capability.
    # ------------------------------------------------------------------
    def _signing_secret(self):
        self.ensure_one()
        db_secret = self.env["ir.config_parameter"].sudo().get_param(
            "database.secret") or ""
        salt = self.sudo().bridge_salt or ""
        return (db_secret + "|" + salt).encode()

    def _mint_bridge_token(self, ttl_seconds=900):
        """Create (and store the jti/salt of) a fresh bridge token for this session."""
        self.ensure_one()
        salt = secrets.token_urlsafe(24)
        jti = secrets.token_urlsafe(18)
        exp = int(time.time()) + ttl_seconds
        self.sudo().write({
            "bridge_salt": salt,
            "bridge_jti": jti,
            "bridge_exp": exp,
        })
        payload = {
            "sid": self.id,
            "uid": self.user_id.id,
            "db": self._target_db(),
            "scope": "grokoo_bridge",
            "jti": jti,
            "exp": exp,
        }
        body = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
        sig = hmac.new(self._signing_secret(), body, hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=")
        return (body + b"." + sig_b64).decode()

    @api.model
    def _verify_bridge_token(self, token):
        """Validate a bridge token. Returns the (sudo) session record or None."""
        try:
            body_b64, sig_b64 = token.encode().split(b".")
            padded = body_b64 + b"=" * (-len(body_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except Exception:
            return None
        if payload.get("scope") != "grokoo_bridge":
            return None
        if payload.get("db") != self.env.cr.dbname:
            return None
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        session = self.sudo().browse(payload.get("sid"))
        if not session.exists():
            return None
        # Recompute signature using this session's stored salt.
        expected = hmac.new(
            session._signing_secret(), body_b64, hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=")
        if not hmac.compare_digest(sig_b64, expected_b64):
            return None
        if not session.bridge_jti or session.bridge_jti != payload.get("jti"):
            return None
        if int(session.bridge_exp or 0) < int(time.time()):
            return None
        return session

    # ------------------------------------------------------------------
    # Public entry point (called from the controller)
    # ------------------------------------------------------------------
    def send_message(self, body, attachment_ids=None):
        """Persist the user message and launch a CLI turn in the background.

        ``attachment_ids`` are ids of ir.attachment records previously uploaded
        for this session (via the upload route). They are written into the
        sandboxed uploads dir and referenced in the prompt so the model can open
        them with the (scratch-scoped) Read tool."""
        self.ensure_one()
        body = (body or "").strip()
        if self.state == "running":
            raise UserError(_("The assistant is still working on the previous message."))
        # Soft global cap on concurrent CLI runs to bound peak memory: each run is
        # a separate Grok subprocess (~hundreds of MB), so too many at once can
        # OOM the host, especially with little/no swap. 0 (or empty) disables it.
        # Soft because it counts committed `running` sessions, so simultaneous
        # bursts may briefly exceed the cap; stale 'running' rows are reaped by the
        # _cron_reap_stale cron, so a crashed run cannot wedge a slot permanently.
        max_runs = int(self._config("max_concurrent_runs", 0) or 0)
        if max_runs > 0:
            active = self.search_count([("state", "=", "running")])
            if active >= max_runs:
                raise UserError(_(
                    "The AI assistant is busy right now (%(active)s of %(max)s runs "
                    "in progress). Please wait a moment and send your message again.",
                    active=active, max=max_runs))
        if not self.user_id.sudo()._grokoo_is_authenticated():
            raise UserError(_(
                "You need to connect Grok before chatting. "
                "Open “Connect Grok” to import your Grok login (auth.json) or set "
                "an xAI API key."))

        paths, atts = self._materialize_attachments(attachment_ids)
        if not body and not atts:
            raise UserError(_("Empty message."))

        user_msg = self.env["grokoo.message"].create({
            "session_id": self.id,
            "role": "user",
            "body": body,
            "attachments": [self._attachment_descriptor(a) for a in atts] or False,
        })
        # Auto-title from the first user message (fall back to a file name).
        if self.turn_count == 0 and self.name in (_("New conversation"), False):
            title = body or (atts[:1].name or "")
            self.name = (title[:60] + ("…" if len(title) > 60 else "")) or self.name

        prompt = self._build_prompt(body, paths)
        is_first = self.turn_count == 0
        self.write({"state": "running"})
        # Mint the token + resolve config inside the request env (as the user),
        # then hand off to the runner which spawns a background thread.
        token = self._mint_bridge_token()
        self.env["grokoo.runner"]._launch(self, prompt, is_first, token)
        return {"message_id": user_msg.id, "session_id": self.id}

    def _post_report(self, html):
        """Create a ``report``-role message holding raw report HTML and push it
        to the live chat.

        ``html`` is rendered in the OWL UI inside a sandboxed iframe (scripts
        blocked, styles isolated), so it may be a full HTML document — e.g. the
        output of ``ir.actions.report._render_qweb_html``. Reuses the runner's
        bus broadcast so an open chat updates immediately, just like streamed
        assistant messages.

        Returns the created ``grokoo.message`` record.
        """
        self.ensure_one()
        rec = self.env["grokoo.message"].create({
            "session_id": self.id,
            "role": "report",
            "body": html or "",
        })
        self.env["grokoo.runner"]._emit_message(self, rec)
        return rec

    def _build_prompt(self, body, paths):
        """Append an attachment manifest to the user's text so the model knows
        which uploaded files it may open from its working directory (the
        per-session scratch cwd, under the uploads/ subdir)."""
        if not paths:
            return body
        listing = "\n".join("- %s" % p for p in paths)
        note = _(
            "The user attached the following file(s), saved under your working "
            "directory; you may read them when relevant (text/CSV/code is plain "
            "text; images and PDFs are binary):\n%s"
        ) % listing
        return (body + "\n\n" + note) if body else note
