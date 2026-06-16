# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
"""The bridge token is the capability the MCP forwarder presents to act as a
user. Forging or replaying it would bypass every per-user ACL, so the
mint/verify round-trip and each rejection path are pinned here."""
import time

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "grokoo")
class TestBridgeToken(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Session = self.env["grokoo.session"]
        self.user = self.env["res.users"].create({
            "name": "Bridge Tester",
            "login": "grokoo_bridge_tester",
        })
        self.session = self.Session.create({"user_id": self.user.id})

    def test_roundtrip_valid(self):
        token = self.session._mint_bridge_token()
        verified = self.Session._verify_bridge_token(token)
        self.assertTrue(verified)
        self.assertEqual(verified.id, self.session.id)

    def test_tampered_signature_rejected(self):
        token = self.session._mint_bridge_token()
        body, _sig = token.split(".")
        forged = body + "." + ("A" * len(_sig))
        self.assertFalse(self.Session._verify_bridge_token(forged))

    def test_tampered_payload_rejected(self):
        # Re-mint for a second session, then graft its signature onto the first
        # session's body — the recomputed HMAC must not match.
        other = self.Session.create({"user_id": self.user.id})
        t1 = self.session._mint_bridge_token()
        t2 = other._mint_bridge_token()
        frankentoken = t1.split(".")[0] + "." + t2.split(".")[1]
        self.assertFalse(self.Session._verify_bridge_token(frankentoken))

    def test_expired_rejected(self):
        token = self.session._mint_bridge_token(ttl_seconds=-1)
        self.assertFalse(self.Session._verify_bridge_token(token))

    def test_revoked_jti_rejected(self):
        # Minting a new token rotates the stored jti/salt, so the old token —
        # though structurally valid — no longer matches and is rejected.
        old = self.session._mint_bridge_token()
        self.session._mint_bridge_token()  # rotates jti + salt
        self.assertFalse(self.Session._verify_bridge_token(old))

    def test_garbage_rejected(self):
        for bad in ("", "not-a-token", "no.dot.here.either", "abc."):
            self.assertFalse(
                self.Session._verify_bridge_token(bad), msg=bad)
