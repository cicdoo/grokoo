# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""``_grokoo_effective_tools`` is the single source of truth the controller consults
before running any tool. These tests pin the default-deny posture for writes and
the zero-trust override."""
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.grokoo.models.grokoo_session import (
    READ_TOOLS,
    WRITE_TOOLS,
    READONLY_TOOL_SET,
    WEB_TOOLS,
)


@tagged("post_install", "-at_install", "grokoo")
class TestToolAccess(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Tool = self.env["grokoo.tool"]
        self.user = self.env["res.users"].create({
            "name": "Tool Access Tester",
            "login": "grokoo_tool_tester",
        })

    def _grant(self, *names):
        tools = self.Tool.search([("name", "in", list(names))])
        # Seed any missing tool catalog rows so the test is self-contained.
        existing = set(tools.mapped("name"))
        for n in names:
            if n not in existing:
                tools |= self.Tool.create({"name": n, "label": n})
        self.user.grokoo_tool_ids = [(6, 0, tools.ids)]

    def test_empty_selection_defaults_to_read_tools(self):
        # No explicit grant → exactly the read-only tool set (no writes).
        self.assertEqual(self.user._grokoo_effective_tools(), set(READ_TOOLS))

    def test_write_tool_requires_explicit_grant(self):
        self.assertNotIn("orm_write", self.user._grokoo_effective_tools())
        self._grant("orm_read", "orm_write")
        self.assertIn("orm_write", self.user._grokoo_effective_tools())

    def test_zero_trust_strips_writes_keeps_reads(self):
        self._grant("orm_read", "orm_write", "orm_unlink", "sql_select")
        self.user.grokoo_zero_trust_mode = "on"
        eff = self.user._grokoo_effective_tools()
        self.assertFalse(eff & set(WRITE_TOOLS), "writes must be stripped")
        self.assertIn("orm_read", eff)
        # sql_select is SELECT-only, so it survives zero-trust.
        self.assertIn("sql_select", eff)
        self.assertTrue(eff <= READONLY_TOOL_SET)

    def test_zero_trust_off_allows_granted_writes(self):
        self._grant("orm_write")
        self.user.grokoo_zero_trust_mode = "off"
        self.assertIn("orm_write", self.user._grokoo_effective_tools())

    def test_web_tools_are_opt_in(self):
        # Web access is never auto-granted: empty selection excludes it.
        self.assertFalse(
            set(self.user._grokoo_effective_tools()) & set(WEB_TOOLS),
            "web tools must require an explicit grant")

    def test_web_tool_grant_survives_zero_trust(self):
        # Web tools don't mutate Odoo data, so a granted web tool stays available
        # even under zero-trust (which only strips writes).
        self._grant("orm_read", "web_fetch")
        self.user.grokoo_zero_trust_mode = "on"
        self.assertIn("web_fetch", self.user._grokoo_effective_tools())

    def test_unknown_tool_names_are_ignored(self):
        self._grant("orm_read", "definitely_not_a_tool")
        self.assertNotIn("definitely_not_a_tool",
                         self.user._grokoo_effective_tools())

    def test_excluded_models_config(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "grokoo.excluded_models", "res.users, ir.config_parameter")
        excluded = self.env["grokoo.session"]._ai_excluded_models()
        self.assertEqual(excluded, {"res.users", "ir.config_parameter"})

    def test_default_action_method_patterns(self):
        patterns = self.env["grokoo.session"]._ai_action_method_patterns()
        self.assertIn("action_*", patterns)
        self.assertIn("button_*", patterns)
