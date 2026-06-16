# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""End-to-end check of the ``ai_bridge`` auth method on a real tool endpoint:
missing/invalid bearer tokens are rejected, and a valid token runs the call as
the session's user (never superuser)."""
import json

from odoo.tests.common import HttpCase, tagged


@tagged("post_install", "-at_install", "grokoo")
class TestIrHttpAuth(HttpCase):

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Bridge HTTP User",
            "login": "grokoo_http_user",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("grokoo.group_ai_user").id,
            ])],
        })
        self.session = self.env["grokoo.session"].create(
            {"user_id": self.user.id})
        self.url = "/grokoo/tool/model_introspect"

    def _post(self, params, headers=None):
        payload = {"jsonrpc": "2.0", "method": "call", "params": params}
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        resp = self.url_open(self.url, data=json.dumps(payload), headers=hdrs)
        return resp.json()

    def test_missing_bearer_rejected(self):
        body = self._post({"model": "res.partner"})
        self.assertIn("error", body)
        self.assertNotIn("result", body)

    def test_invalid_bearer_rejected(self):
        body = self._post({"model": "res.partner"},
                          headers={"Authorization": "Bearer not-a-real-token"})
        self.assertIn("error", body)

    def test_valid_bearer_runs_as_user(self):
        token = self.session._mint_bridge_token()
        body = self._post(
            {"model": "res.partner"},
            headers={"Authorization": "Bearer %s" % token})
        self.assertNotIn("error", body, body.get("error"))
        result = body["result"]
        self.assertEqual(result["model"], "res.partner")
        # The handler aborts hard if it ever runs as superuser, so a populated
        # fields map here confirms it ran as the (non-su) session user.
        self.assertIn("fields", result)
        self.assertIn("access", result)
