# Changelog

All notable changes to **Grokoo** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
Odoo-style versioning (`18.0.MAJOR.MINOR.PATCH`). Add new entries under
`## [Unreleased]`; a maintainer assigns the version at release time.

## [Unreleased]

## [18.0.1.0.0] — First public release

First open-source release of Grokoo, an in-Odoo AI assistant that drives the
xAI **Grok CLI** over a sandboxed, permission-aware bridge. (Grokoo is a sibling
of CICDoo's Claudoo / Codexoo / Geminoo, built for the Grok CLI.)

### Features
- **Chat client action** (OWL) with real-time streaming over the Odoo bus.
- **Permission-aware ORM tools** — every action runs as the *current user*
  (never superuser); `ir.model.access` and record rules are always enforced.
- **Read-only SQL reporting** (`sql_select`) gated to the *AI SQL Analyst* group,
  guarded by a SELECT-only text validator plus Postgres `SET TRANSACTION READ ONLY`
  and a statement timeout.
- **Pluggable per-user authentication, no shared key required** — each user
  either imports their own Grok login (`~/.grok/auth.json` from `grok login`, so
  their own xAI subscription applies) or sets an xAI API key. Credentials are
  stored privately per user under a `.grok` dir, mode `0600`, never in the
  database. An optional shared API key can be configured as a fallback.
- **Restricted engine** — Grok runs headless (`grok --output-format
  streaming-json --always-approve`) with its built-in shell / write / network
  tools withheld via the `--tools` allowlist (file reads only, for attachments),
  and tool calls auto-approved only for the trusted Odoo MCP server. The Odoo
  MCP server is declared in a per-session `.grok/config.toml`; the bridge token
  is passed via the process environment and never written to a file. The
  authoritative boundary is server-side (loopback `grokoo_bridge` token +
  per-user ACLs).
- **Rendered charts & HTML reports in chat** — HTML emitted in a fenced
  ` ```html ` block renders as a CSS/SVG chart or report inside a **sandboxed
  iframe** (`sandbox="allow-same-origin"`, no scripts) in a dedicated artifact
  panel. A `report` message role plus a `grokoo.session._post_report(html)`
  helper let server-side code push a full report into the conversation.
- **Streaming or final-only output** — `streaming-json` streams replies in real
  time; `json` (single final object per turn) is a compatible fallback.
- **Conversation continuity** — resumes the Grok session id (`grok -r <id>`)
  when emitted; otherwise replays recent history into the prompt.
- **Immutable audit log** (`grokoo.tool_log`) written on a separate committed
  cursor so it survives rollbacks.
- **Zero-trust mode** (global default + per-user override) that strips all write
  tools.
- **Test suite** covering the SQL guard, bridge-token mint/verify, tool-access
  policy, the `grokoo_bridge` auth method, the audit log, and the runner wiring.

### Licensing
- **Dual-licensed**: open-source **LGPL-3.0-or-later** *or* a **commercial license**
  from CICDoo (see `COMMERCIAL_LICENSE.md`). Every source file carries an SPDX
  `LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial` notice.
- Runs on **both Odoo Community and Enterprise** editions (depends only on
  Community modules: `web`, `bus`).

[18.0.1.0.0]: https://github.com/cicdoo/grokoo/releases/tag/18.0.1.0.0
