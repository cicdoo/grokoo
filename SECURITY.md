# Security Policy

Grokoo bridges a large-language-model agent to your Odoo database, so we take
security seriously and welcome responsible disclosure.

## Supported versions

| Version  | Supported |
|----------|-----------|
| 18.0.x   | ✅        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, email **security@cicdoo.com** with:

- a description of the issue and its impact,
- steps to reproduce (a proof-of-concept is ideal),
- the Grokoo version and your Odoo version.

We aim to acknowledge reports within **3 business days** and to provide a
remediation timeline after triage. We will credit reporters in the release notes
unless you prefer to remain anonymous.

## Scope & design notes

Grokoo's threat model assumes the LLM output is **untrusted**. The defenses that
are in scope for security reports include:

- **No superuser execution** — tool endpoints abort if `request.env.su` is true;
  every ORM call runs as the acting user with `ir.model.access` + record rules.
- **Read-only SQL** — `sql_select` is validated (SELECT/WITH only, no stacked
  statements, no row locks, no dangerous functions) *and* executed under
  `SET TRANSACTION READ ONLY` with a statement timeout.
- **Capability tokens** — the MCP bridge holds only a short-lived, session-scoped
  HMAC bearer token (no DB credentials) and is reachable on loopback only via the
  `grokoo_bridge` auth method.
- **Restricted engine** — Grok is launched headless with its built-in shell/
  write/network tools withheld (file reads only, for attachments) and tool calls
  auto-approved only for the trusted Odoo MCP server, so it cannot reach the shell,
  filesystem, or network on its own; it acts only through the Odoo MCP tools. The
  bridge token is passed via the process environment and never written to a file
  the model could open. The authoritative boundary is server-side (the loopback
  bridge token + per-user ACLs).
- **Per-user credential isolation** — Grok credentials (imported OAuth or an API
  key) are stored per user in a private `.grok` dir with mode `0600` and never
  exposed on a record.
- **Sandboxed report/chart rendering** — HTML the model emits (charts, reports,
  and `grokoo.session._post_report` output) is rendered in an
  `<iframe sandbox="allow-same-origin">` **without** `allow-scripts`, so its own
  scripts never execute and its styles are isolated from the Odoo UI; the parent
  only reads the frame to size it. `allow-scripts` is deliberately never combined
  with `allow-same-origin` (that pairing lets a frame drop its own sandbox).

If you find a way to (a) escalate beyond the acting user's ACLs, (b) mutate data
through `sql_select`, (c) forge or replay a bridge token, (d) make the model
affect Odoo or the host outside the `mcp__odoo__*` tool surface, or (e) execute
script or escape the iframe sandbox via rendered model HTML, that is a
security bug — please report it.
