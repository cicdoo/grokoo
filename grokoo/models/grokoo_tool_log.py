# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
import json
import logging

import odoo
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AiAssistantToolLog(models.Model):
    _name = "grokoo.tool_log"
    _description = "Grokoo AI Assistant Tool Call Audit Log"
    _order = "create_date desc, id desc"

    session_id = fields.Many2one(
        "grokoo.session", ondelete="set null", index=True)
    user_id = fields.Many2one("res.users", index=True, string="Acting User")
    tool = fields.Char(index=True)
    model_name = fields.Char()
    method = fields.Char()
    record_ids = fields.Char()
    arguments = fields.Json()
    sql_text = fields.Text()
    result_count = fields.Integer()
    outcome = fields.Selection([
        ("success", "Success"),
        ("access_denied", "Access Denied"),
        ("validation_rejected", "Validation Rejected"),
        ("error", "Error"),
    ], index=True)
    error_message = fields.Text()
    duration_ms = fields.Integer()
    client_ip = fields.Char()

    @api.model
    def _record(self, vals):
        """Write an audit row on a SEPARATE committed cursor so it survives a
        rollback of the failed operation it describes."""
        dbname = self.env.cr.dbname
        # Truncate potentially large fields defensively.
        if vals.get("arguments") and not isinstance(vals["arguments"], (dict, list)):
            try:
                vals["arguments"] = json.loads(vals["arguments"])
            except Exception:
                vals["arguments"] = {"_raw": str(vals["arguments"])[:2000]}
        try:
            with odoo.registry(dbname).cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                env["grokoo.tool_log"].create(vals)
                cr.commit()
        except Exception:
            _logger.exception("Failed to write grokoo.tool_log")
