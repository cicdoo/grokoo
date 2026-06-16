# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
from odoo import fields, models


class AiAssistantTool(models.Model):
    """Catalog of the tools the assistant can expose to the model.

    One record per tool (seeded from data/grokoo_tool_data.xml and kept in
    sync with the READ_TOOLS/WRITE_TOOLS code constants in grokoo_session).
    It is the join target for the per-user `res.users.grokoo_tool_ids`
    Many2many that decides which tools each user may invoke.
    """
    _name = "grokoo.tool"
    _description = "Grokoo AI Assistant Tool"
    _order = "readonly desc, name"
    _rec_name = "label"

    name = fields.Char(
        required=True, index=True, copy=False,
        help="Technical tool name (matches the MCP/controller endpoint), "
             "e.g. orm_write.")
    label = fields.Char(required=True, help="Human-readable label.")
    readonly = fields.Boolean(
        default=False,
        help="Read-only tools stay available when zero-trust mode is on.")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("name_uniq", "unique(name)", "Tool name must be unique."),
    ]
