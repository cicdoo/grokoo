# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import json
import logging
import os

from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.tools import str2bool

from .grokoo_session import READ_TOOLS, ALL_TOOLS, READONLY_TOOL_SET, WEB_TOOLS

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = "res.users"

    # Pure status indicator computed from the presence of valid per-user
    # credentials on disk. No token material is ever exposed on the record.
    grokoo_oauth_set = fields.Boolean(
        compute="_compute_grokoo_oauth_set", string="Grok Connected")

    # --- Per-user AI tool permissions (manager-managed) ---
    grokoo_tool_ids = fields.Many2many(
        "grokoo.tool", "grokoo_user_tool_rel", "user_id", "tool_id",
        string="AI Tools Allowed",
        help="Tools this user may invoke through the AI Assistant. "
             "Leave empty to allow all read-only tools.")
    grokoo_zero_trust_mode = fields.Selection(
        [("inherit", "Inherit global default"),
         ("on", "Zero-trust (read-only)"),
         ("off", "Allow writes")],
        default="inherit", string="AI Zero-Trust Mode",
        help="When zero-trust is active, only read-only tools are available to "
             "this user — write tools are stripped regardless of the selection "
             "above. 'Inherit' follows the global default in Settings.")

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ["grokoo_oauth_set"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS

    def _compute_grokoo_oauth_set(self):
        for user in self:
            user.grokoo_oauth_set = bool(user.id) and user._grokoo_is_authenticated()

    # ------------------------------------------------------------------
    # Effective AI tool set (single source of truth, see grokoo.session)
    # ------------------------------------------------------------------
    def _grokoo_allowed_tool_names(self):
        """Per-user tool selection, independent of zero-trust.

        Empty selection falls back to all read tools (write tools require an
        explicit grant)."""
        self.ensure_one()
        sel = self.grokoo_tool_ids
        return set(sel.mapped("name")) if sel else set(READ_TOOLS)

    def _grokoo_zero_trust(self):
        """Whether zero-trust (read-only) mode is active for this user."""
        self.ensure_one()
        if self.grokoo_zero_trust_mode != "inherit":
            return self.grokoo_zero_trust_mode == "on"
        raw = self.env["grokoo.session"]._config("zero_trust_default")
        return str2bool(raw, False) if raw is not None else False

    def _grokoo_effective_tools(self):
        """THE source of truth: tool names this user may actually invoke."""
        self.ensure_one()
        names = self._grokoo_allowed_tool_names() & set(ALL_TOOLS)
        if self._grokoo_zero_trust():
            names = {n for n in names if n in READONLY_TOOL_SET}
        return names

    def _grokoo_account_email(self):
        """The email the assistant should treat as the user's."""
        self.ensure_one()
        return self.email or self.partner_id.email or self.login or ""

    # ------------------------------------------------------------------
    # Identity / role context injected into the assistant's system prompt
    # ------------------------------------------------------------------
    def _grokoo_identity_prompt(self, effective_tools=None):
        """A concise, authoritative text block telling the model WHO the Odoo
        user is and what roles/permissions they hold.

        Called from the request env as the user (see grokoo.runner._launch),
        so it reflects exactly what this user can see. It carries no secrets — only
        identity, company, locale, privilege flags, application roles and the AI
        tool grant — so the model stops self-identifying as the xAI/Grok account
        and instead acts for, and within the rights of, this Odoo user."""
        self.ensure_one()
        if effective_tools is None:
            effective_tools = self._grokoo_effective_tools()

        email = self._grokoo_account_email() or "—"
        lines = [
            "--- ODOO USER CONTEXT (authoritative — this is who you are acting for) ---",
            "Name: %s" % (self.name or "—"),
            "Login: %s" % (self.login or "—"),
            "Email: %s" % email,
            "Odoo user id: %s" % self.id,
            "Company: %s" % (self.company_id.name or "—"),
        ]
        if len(self.company_ids) > 1:
            lines.append(
                "Allowed companies: %s"
                % ", ".join(self.company_ids.mapped("name")))
        lines.append("Language: %s" % (self.lang or "—"))
        lines.append("Timezone: %s" % (self.tz or "—"))

        # Privilege flags — stated plainly so the model never over-assumes rights.
        if self._is_admin():
            priv = "Administrator (full access)"
        elif self._is_system():
            priv = "Settings/System access"
        elif self.has_group("base.group_user"):
            priv = "Internal user (not an administrator)"
        else:
            priv = "Portal/public user (limited access)"
        lines.append("Privilege level: %s" % priv)

        # Curated roles: only groups that belong to an application category.
        roles = sorted(
            "%s / %s" % (g.category_id.name, g.name)
            for g in self.groups_id if g.category_id
        )
        if roles:
            lines.append("Roles:")
            lines.extend("  - %s" % r for r in roles)
        else:
            lines.append("Roles: (none beyond base access)")

        # What the assistant may actually do on this user's behalf.
        if not effective_tools:
            lines.append("AI tools available to you: none.")
        else:
            writeable = sorted(set(effective_tools) - READONLY_TOOL_SET)
            if writeable:
                lines.append(
                    "AI tool grant: read-only data access PLUS write/action tools "
                    "(%s). Use Odoo business actions when available." % ", ".join(writeable))
            else:
                lines.append(
                    "AI tool grant: READ-ONLY. You cannot create, modify or delete "
                    "data for this user — only query and report.")
        if set(effective_tools) & set(WEB_TOOLS):
            lines.append(
                "Web access: you may use web search to retrieve and search "
                "public web content.")
        lines.append("--- END ODOO USER CONTEXT ---")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Per-user Grok config dir (.grok) and credential files
    # ------------------------------------------------------------------
    def _grokoo_home_root(self):
        """Root under which each user gets `<root>/<uid>/.grok`."""
        return self.env["ir.config_parameter"].sudo().get_param(
            "grokoo.home_root") or "/var/lib/odoo/grokoo_home"

    def _grokoo_home_dir(self):
        """The HOME directory for this user's CLI runs."""
        self.ensure_one()
        return os.path.join(self._grokoo_home_root(), str(self.id))

    def _grokoo_config_dir(self, create=False):
        """This user's private GROK config dir (holds auth.json)."""
        self.ensure_one()
        path = os.path.join(self._grokoo_home_dir(), ".grok")
        if create:
            try:
                os.makedirs(path, mode=0o700, exist_ok=True)
            except OSError as e:
                raise UserError(_("Cannot create Grok config dir %s: %s") % (path, e))
        return path

    def _grokoo_auth_path(self):
        """Per-user ``auth.json`` produced by `grok login` (the OAuth path)."""
        self.ensure_one()
        return os.path.join(self._grokoo_config_dir(), "auth.json")

    def _grokoo_api_key_path(self):
        """Per-user xAI API key file (kept on disk, mode 0600, never the DB)."""
        self.ensure_one()
        return os.path.join(self._grokoo_config_dir(), "api_key")

    # ------------------------------------------------------------------
    # Authentication state + env (pluggable: login import and/or API key)
    # ------------------------------------------------------------------
    def _grokoo_has_oauth(self):
        """True if a usable imported ``auth.json`` (from `grok login`) is present."""
        self.ensure_one()
        try:
            with open(self._grokoo_auth_path(), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        # auth.json is an opaque token bundle written by `grok login`; treat any
        # non-empty JSON object as a usable credential and let the CLI validate.
        return isinstance(data, dict) and bool(data)

    def _grokoo_api_key(self):
        """The effective xAI API key for this user: the per-user key if set,
        else the shared `grokoo.api_key` system parameter (may be empty)."""
        self.ensure_one()
        try:
            with open(self._grokoo_api_key_path(), "r") as f:
                key = f.read().strip()
            if key:
                return key
        except OSError:
            pass
        return self.env["grokoo.session"]._config("api_key", "") or ""

    def _grokoo_is_authenticated(self):
        """True if this user can run the CLI: imported login or an API key."""
        self.ensure_one()
        return bool(self._grokoo_has_oauth() or self._grokoo_api_key())

    def _grokoo_auth_env(self):
        """Auth-related env for a CLI run. An imported ``auth.json`` is read from
        disk by the CLI (no env needed beyond HOME); an API key is passed as
        XAI_API_KEY. The imported login takes precedence: if auth.json exists we
        do NOT set XAI_API_KEY, so the CLI uses the OAuth path."""
        self.ensure_one()
        env = {}
        if not self._grokoo_has_oauth():
            key = self._grokoo_api_key()
            if key:
                env["XAI_API_KEY"] = key
        return env

    # ------------------------------------------------------------------
    # Credential management (called from the controller / preferences)
    # ------------------------------------------------------------------
    def _grokoo_import_credentials(self, blob):
        """Validate and store an imported Grok ``auth.json`` blob.

        ``blob`` is the JSON content of a ``~/.grok/auth.json`` produced by
        `grok login` on a machine with a browser (or `grok login --device-auth`
        on a headless box). We persist it for this user; the CLI refreshes the
        token in place.
        """
        self.ensure_one()
        blob = (blob or "").strip()
        if not blob:
            raise UserError(_("Paste your Grok auth.json content."))
        try:
            data = json.loads(blob)
        except ValueError:
            raise UserError(_("That is not valid JSON. Paste the full content of "
                              "your ~/.grok/auth.json file."))
        if not isinstance(data, dict) or not data:
            raise UserError(_(
                "The pasted content is not a valid Grok credential file. Run "
                "`grok login` (or `grok login --device-auth`) on a machine with a "
                "browser, then paste the contents of ~/.grok/auth.json."))
        self._grokoo_write_file(self._grokoo_auth_path(), json.dumps(data))
        return True

    def _grokoo_set_api_key(self, key):
        """Store (or clear) this user's xAI API key on disk (mode 0600)."""
        self.ensure_one()
        key = (key or "").strip()
        path = self._grokoo_api_key_path()
        if not key:
            try:
                os.remove(path)
            except OSError:
                pass
            return True
        self._grokoo_write_file(path, key)
        return True

    def _grokoo_write_file(self, path, content):
        """Atomically write a per-user credential file (mode 0600)."""
        self.ensure_one()
        self._grokoo_config_dir(create=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
        return True

    def _grokoo_logout(self):
        """Remove this user's stored credentials (forces re-connection)."""
        self.ensure_one()
        for path in (self._grokoo_auth_path(), self._grokoo_api_key_path()):
            try:
                os.remove(path)
            except OSError:
                pass
        return True

    def action_grokoo_logout(self):
        """Button: disconnect this user's Grok credentials from preferences."""
        for user in self:
            user._grokoo_logout()
        return True
