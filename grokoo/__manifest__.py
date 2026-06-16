# -*- coding: utf-8 -*-
# Copyright 2026 CICDoo (https://cicdoo.com)
# SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial
# Dual-licensed: open source (LGPL-3, see LICENSE) or commercial (see COMMERCIAL_LICENSE.md).
{
    'name': "Grokoo",
    'summary': "Grokoo — chat with your Odoo, safely. An xAI Grok-powered assistant "
               "with permission-aware ORM tools and read-only SQL reporting.",
    'description': """
Grokoo — AI Assistant for Odoo
===============================
A chat interface (menu + OWL client action) that drives the xAI Grok CLI headless on
the server. The assistant can query Odoo and build reports through a small set of safe
tools exposed over a sandboxed MCP bridge:

* Every ORM action runs with the **current user's** permission level (never superuser).
* Raw SQL reporting is **read-only** (SELECT only) and gated to the *AI SQL Analyst* group.
* Grok runs with its built-in shell/file/network tools withheld (only file reads are
  allowlisted); it can only affect Odoo through the gated tool endpoints. The
  authoritative boundary is server-side (loopback-only bridge token + per-user Odoo
  ACLs on every tool call).
* Replies stream into the chat in real time over the Odoo bus.

Authentication is per-user and pluggable: each user either imports their own Grok
login (``~/.grok/auth.json`` from ``grok login``, using their own xAI subscription)
or sets an xAI API key. Credentials are stored privately in a per-user ``.grok``
directory (mode 0600), never in the database. Chatting is gated until the current
user has connected.

Requires the xAI Grok CLI installed on the server (``curl -fsSL
https://x.ai/cli/install.sh | bash``; set its path via the ``grokoo.cli_path`` /
``grokoo.cli_glob`` system parameter).
""",
    'author': "CICDoo",
    'website': "https://cicdoo.com",
    'category': 'Productivity/AI',
    'version': '18.0.1.0.0',
    'license': 'LGPL-3',
    'depends': ['web', 'bus'],
    'data': [
        'security/grokoo_security.xml',
        'security/ir.model.access.csv',
        'data/grokoo_tool_data.xml',
        'data/grokoo_action.xml',
        'views/res_config_settings_views.xml',
        'views/res_users_views.xml',
        'views/grokoo_log_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'grokoo/static/src/scss/grokoo.scss',
            'grokoo/static/src/grokoo_service.js',
            'grokoo/static/src/markdown.js',
            'grokoo/static/src/artifacts.js',
            'grokoo/static/src/components/report_frame.js',
            'grokoo/static/src/components/report_frame.xml',
            'grokoo/static/src/components/message.js',
            'grokoo/static/src/components/message.xml',
            'grokoo/static/src/components/message_list.js',
            'grokoo/static/src/components/message_list.xml',
            'grokoo/static/src/components/composer.js',
            'grokoo/static/src/components/composer.xml',
            'grokoo/static/src/chat_action.js',
            'grokoo/static/src/chat_action.xml',
        ],
    },
    'application': True,
    'installable': True,
}
