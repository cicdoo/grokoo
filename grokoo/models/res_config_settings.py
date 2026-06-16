# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    grokoo_cli_path = fields.Char(
        string="Grok CLI Path",
        config_parameter="grokoo.cli_path",
        help="Absolute path to the grok binary. Leave empty to use PATH/auto-detect.")
    grokoo_model = fields.Char(
        string="Model", config_parameter="grokoo.model", default="",
        help="Model passed to `grok --model` (e.g. grok-4). Leave EMPTY "
             "to use your account's default model.")
    grokoo_auth_mode = fields.Selection(
        [("oauth_import", "Import Grok login (auth.json)"),
         ("api_key", "xAI API key")],
        string="Authentication Mode", config_parameter="grokoo.auth_mode",
        default="oauth_import",
        help="How users connect Grok. 'Import' lets each user paste their "
             "~/.grok/auth.json (from `grok login`, using their own xAI "
             "subscription); 'API key' uses a per-user (or the shared) xAI API key.")
    grokoo_api_key = fields.Char(
        string="Shared xAI API Key", config_parameter="grokoo.api_key",
        help="Optional shared xAI API key used for users who have not set "
             "their own. Leave empty to require per-user credentials.")
    grokoo_output_format = fields.Selection(
        [("streaming-json", "Streaming (streaming-json)"),
         ("json", "Final only (json)")],
        string="Output Format", config_parameter="grokoo.output_format",
        default="streaming-json",
        help="streaming-json streams replies in real time; json returns a single "
             "final answer per turn (more compatible).")
    grokoo_max_turns = fields.Integer(
        string="Max Turns", config_parameter="grokoo.max_turns", default=30)
    grokoo_timeout_s = fields.Integer(
        string="Run Timeout (s)", config_parameter="grokoo.timeout_s",
        default=900)
    grokoo_max_concurrent_runs = fields.Integer(
        string="Max Concurrent Runs",
        config_parameter="grokoo.max_concurrent_runs", default=0,
        help="Maximum number of AI runs allowed to execute at the same time "
             "across all users. Each run is a separate CLI subprocess, so this "
             "caps peak memory and protects the host from OOM. 0 = unlimited.")
    grokoo_scratch_root = fields.Char(
        string="Scratch Directory", config_parameter="grokoo.scratch_root",
        default="/var/lib/odoo/grokoo_scratch")
    grokoo_home_root = fields.Char(
        string="Per-User Grok Home Root", config_parameter="grokoo.home_root",
        default="/var/lib/odoo/grokoo_home",
        help="Root directory under which each user gets a private "
             "<root>/<uid>/.grok holding their own credentials.")
    grokoo_base_url = fields.Char(
        string="Odoo Host URL", config_parameter="grokoo.base_url",
        default="http://127.0.0.1:8069",
        help="Host URL the MCP bridge connects back to (loopback by default).")
    grokoo_db_name = fields.Char(
        string="Odoo Database", config_parameter="grokoo.db_name",
        help="Database the assistant connects to. Leave empty to use the current "
             "database. Set this in multi-database deployments so the bridge "
             "always reaches the right database.")
    grokoo_sql_enabled = fields.Boolean(
        string="Enable SQL Reporting Tool",
        config_parameter="grokoo.sql_enabled", default=True,
        help="Allow the read-only SQL tool (members of the AI SQL Analyst group).")
    grokoo_zero_trust_default = fields.Boolean(
        string="Zero-Trust by Default (read-only)",
        config_parameter="grokoo.zero_trust_default", default=False,
        help="Instance-wide default: when on, users left on 'Inherit' may only "
             "use read-only tools. A per-user override can still allow writes.")
    grokoo_action_method_patterns = fields.Char(
        string="AI Action Method Patterns",
        config_parameter="grokoo.action_methods",
        default="action_*,button_*",
        help="Comma-separated fnmatch patterns for methods orm_action/run_wizard "
             "may call (e.g. action_*,button_*). Private/dunder methods are "
             "always blocked.")
    # Non-stored: persisted as a CSV of technical names in the
    # grokoo.excluded_models system parameter (see get/set_values).
    # Explicit relation table/columns (NOT the auto-derived name): res.config.settings
    # ↔ ir.model would otherwise collide with any other module's M2M between the
    # same two models (e.g. claudoo's), since the auto name is identical.
    grokoo_excluded_model_ids = fields.Many2many(
        "ir.model", "grokoo_settings_excluded_model_rel",
        "settings_id", "model_id", string="Models Hidden from AI",
        help="The AI Assistant will refuse to introspect, read, or write these "
             "models for every user.")
    # Non-stored: persisted as a CSV of ids in the grokoo.server_action_ids
    # system parameter (see get/set_values). Only these may be run via
    # run_server_action; an allowlisted server action is trusted-by-admin.
    grokoo_allowed_server_action_ids = fields.Many2many(
        "ir.actions.server", "grokoo_settings_server_action_rel",
        "settings_id", "action_id", string="AI-Runnable Server Actions",
        help="Only these server actions may be run via run_server_action. "
             "Empty = none. A 'code' server action runs arbitrary Python, so "
             "only allowlist actions you trust.")

    def get_values(self):
        res = super().get_values()
        names = self.env["grokoo.session"]._ai_excluded_models()
        models = self.env["ir.model"].search([("model", "in", list(names))])
        res["grokoo_excluded_model_ids"] = [(6, 0, models.ids)]
        sa_ids = self.env["grokoo.session"]._ai_allowed_server_action_ids()
        res["grokoo_allowed_server_action_ids"] = [(6, 0, list(sa_ids))]
        return res

    def set_values(self):
        super().set_values()
        names = ",".join(sorted(self.grokoo_excluded_model_ids.mapped("model")))
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("grokoo.excluded_models", names)
        ICP.set_param("grokoo.server_action_ids",
                      ",".join(map(str, sorted(self.grokoo_allowed_server_action_ids.ids))))
