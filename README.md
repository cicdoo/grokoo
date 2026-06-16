<div align="center">

<img src="https://cicdoo.com/assets/img/cicdoo_colored.png" alt="CICDoo" width="120" />

# Grokoo

### Chat with your Odoo — safely.

[![Deploy on CICDoo](https://img.shields.io/badge/🚀%20Deploy%20on%20CICDoo-1a73e8?style=for-the-badge)](https://www.cicdoo.com)
[![Get Support](https://img.shields.io/badge/🛟%20Get%20Support-0f9d58?style=for-the-badge)](https://www.cicdoo.com)
[![Sponsor](https://img.shields.io/badge/♥%20Sponsor-EA4AAA?style=for-the-badge)](https://github.com/sponsors/cicdoo)

**An in-Odoo AI assistant powered by the xAI Grok CLI. It answers questions,
queries your data, and builds reports — running as the logged-in user, never as
superuser, with a full audit trail.**

[![License: LGPL-3.0](https://img.shields.io/badge/License-LGPL%20v3-blue.svg)](LICENSE)
[![Odoo](https://img.shields.io/badge/Odoo-18.0-714B67.svg)](https://www.odoo.com)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[Quick start](#-quick-start) · [Security model](#-security-model) ·
[Configuration](#-configuration) · [Commercial support](#-commercial-support) ·
[Roadmap](#-roadmap)

</div>

---

## Why Grokoo?

Most "AI for Odoo" add-ons either ship your data to a third-party prompt or hand
the model a superuser connection and hope for the best. **Grokoo is built the
other way around — security first:**

| | Grokoo |
|---|---|
| 🔒 **Runs as the user** | Every ORM action executes with `su=False`, so the user's `ir.model.access` rights and record rules are enforced automatically. The model can never see or do more than the person using it. |
| 🧮 **Read-only SQL, really** | `sql_select` is double-guarded: a SELECT-only text validator (no stacked statements, row locks, or dangerous functions) **and** Postgres `SET TRANSACTION READ ONLY` + a statement timeout. Gated to a dedicated *AI SQL Analyst* group. |
| 🧾 **Full audit log** | Every tool call — model, method, SQL, row counts, outcome, even denials — is written to an immutable log on a separate committed cursor, so it survives rollbacks. |
| 🔑 **Per-user credentials, no shared key** | Each user connects Grok with their own credentials — either by importing their Grok login (`auth.json`, so their **your xAI subscription** access applies) or a Grok API key. Credentials are stored per user under a private `.grok` dir, mode `0600`, never in the database, never echoed back. |
| 🚫 **Sandboxed engine, no surprises** | Grok runs with its built-in shell/write/network tools withheld and tool calls auto-approved only for the gated Odoo MCP server, so the model cannot touch the shell, filesystem, or network on its own. The authoritative boundary is server-side: the model can affect Odoo **only** through a small set of tools over a loopback bridge that holds *no database credentials*. |
| ⚡ **Streaming UX** | Replies and tool "chips" stream into the chat in real time over the Odoo bus. |

If you sell or run Odoo for clients, the security posture **is** the feature: you
can give business users a natural-language window into their ERP without widening
anyone's access by a single record.

---

## 🏗 How it works

```
OWL chat UI ──/grokoo/send (auth=user)──► Odoo worker ──► daemon thread
   ▲ bus stream (deltas, tool chips)                          │ spawns
   │                  grok --output-format streaming-json --always-approve
   │                  tool surface = odoo MCP server ONLY
   │                                       │ stdio MCP
   │                                       ▼
   │               bridge/mcp_server.py  (no DB creds, loopback only)
   │                                       │ Bearer(session token)
   └──── bus.bus ◄── controllers/tools.py (auth='grokoo_bridge', runs AS the user) ──► Postgres
```

- **Only Odoo touches the database.** The CLI subprocess and the MCP forwarder
  hold no credentials — only a short-lived, session-scoped HMAC bearer token
  (passed via the process environment, never written to a file).
- The bridge is reachable on **loopback only**; the `grokoo_bridge` auth method
  rejects any non-local caller and rebinds the request to the session's user
  (`su=False`).
- Grok is launched headless with its built-in tools restricted (file reads
  only, for attachments) and the odoo MCP server trusted, so its only path to
  your data is the gated Odoo tool surface.

---

## 🚀 Quick start

### Requirements

- **Odoo 18.0**
- The **[xAI Grok CLI](https://x.ai/cli)** installed
  on the Odoo server (`curl -fsSL https://x.ai/cli/install.sh | bash`). Set its path via the
  `grokoo.cli_path` / `grokoo.cli_glob` system parameter; `grok` on PATH is
  auto-detected.
- Per-user credentials: either an imported Grok login (uses the user's own
  **xAI subscription**) or an xAI API key.

### Install

```bash
# Put the module folder (grokoo/) on your addons_path, then install it:
odoo-bin -c odoo.conf -d <your-db> -i grokoo --stop-after-init
# Restart your live server afterwards (Odoo does not re-import changed Python).
```

### First run

1. Open the **Grokoo** app from the top menu.
2. Click **Connect Grok** and either:
   - **Grok login (auth.json):** run `grok login` (or `grok login --device-auth`
     on a headless box) and sign in on any machine with a browser, then paste
     the contents of your `~/.grok/auth.json`; or
   - **API key:** paste an xAI API key from
     [the xAI Console](https://console.x.ai).
3. Ask away: *"How many sale orders are still in draft this month?"*

Grant the **AI SQL Analyst** group to users who should be able to run read-only
SQL reports, and **AI Assistant Manager** to those who configure it and read the
audit log.

---

## 🔐 Security model

Grokoo treats LLM output as **untrusted** and defends accordingly:

- **Never superuser.** Tool endpoints abort if `request.env.su` is true.
- **ACLs + record rules** apply to every read and write, because the call runs as
  the user.
- **Writes are opt-in.** Read tools are the default; each write tool
  (`orm_create/write/unlink`, `orm_action`, `run_wizard`, `run_server_action`)
  must be granted per user, and **zero-trust mode** strips them entirely.
- **SQL can only read.** See the guard above.
- **Restricted engine.** Grok's built-in tool surface is locked to file reads
  (for attachments); the server-side bridge + per-user ACLs are the authoritative
  boundary. The bridge token is passed via the environment and never written to a
  file the model could open.
- **Capability tokens** expire, are session-scoped, single-jti, and HMAC-signed
  with the database secret.

See [SECURITY.md](SECURITY.md) for the full threat model and how to report a
vulnerability.

---

## ⚙️ Configuration

**Settings → Grokoo AI Assistant** (or System Parameters `grokoo.*`):

| Parameter | Purpose |
|---|---|
| `cli_path` / `cli_glob` | Path (or glob) to the Grok CLI binary (auto-detected if blank) |
| `model` | Grok model id (blank = your account's default model) |
| `auth_mode` | `oauth_import` (imported `grok login`) or `api_key` |
| `api_key` | Optional shared xAI API key fallback |
| `core_tools` | Override the allowlisted built-in tools (default `read_file,view_image`) |
| `output_format` | `streaming-json` (real-time) or `json` (final only, more compatible) |
| `max_turns`, `timeout_s` | Per-turn limits |
| `home_root`, `scratch_root` | Per-user `.grok` home and CLI scratch directories |
| `base_url` | Loopback URL the bridge calls back on |
| `sql_enabled` | Master switch for `sql_select` |
| `zero_trust_default` | Global default for read-only (zero-trust) mode |
| `python_bin` | Interpreter used to run the bundled bridge script (defaults to Odoo's) |

### Permissions / groups

- **AI Assistant User** — can use the chat (ORM reads).
- **AI SQL Analyst** — additionally may run `sql_select`.
- **AI Assistant Manager** — configuration + audit log; sees all sessions.

---

## 🧪 Tests

```bash
odoo-bin -c odoo.conf -d <db> -i grokoo --test-enable --test-tags grokoo --stop-after-init
```

Covers the SQL guard, bridge-token mint/verify, the tool-access policy, the
`grokoo_bridge` auth method, the audit log, and the runner's argv/settings wiring.

---

## 💼 Commercial support

Grokoo is **free and open source (LGPL-3.0)** and always will be. It is built and
maintained by **[CICDoo](https://cicdoo.com)**, who also offer:

- 🚀 **Managed deployment & hosting** — Grokoo running securely on your Odoo, you manage your business and we manage your infrastructure
- 🛟 **Priority support & SLAs** for production rollouts.
- 🧩 **Custom tools & integrations** — extend the safe tool surface to your models
  and workflows.
- 📜 **Commercial license** for organizations that cannot adopt LGPL terms —
  modify Grokoo and keep your changes private, embed it in a closed product, or
  get warranty/indemnification. See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md).
- 🎓 **Training & security review** for teams enabling AI access to their ERP.

👉 **Get in touch: [sam@cicdoo.com](mailto:sam@cicdoo.com) · [cicdoo.com](https://cicdoo.com)**

If Grokoo saves your team time, consider [sponsoring](https://github.com/sponsors/cicdoo)
its development.

---

## 🗺 Roadmap

- Approval-gated `orm_create`/`orm_write`/`orm_unlink` with before→after diff cards
- An admin allow/deny **policy model** for tools and models
- A dedicated `ai_sql_ro` Postgres role + view scoping
- Tighter OS-level sandboxing of the CLI subprocess
- Per-session rate and tool budgets

Have an idea? Open a [feature request](.github/ISSUE_TEMPLATE/feature_request.yml)
or a PR — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 🙌 Acknowledgements

Grokoo wraps xAI's **[Grok CLI](https://x.ai/cli)**.
"Grok" and "xAI" are trademarks of X.AI Corp. Grokoo is
an independent, community project and is not affiliated with or endorsed by X.AI Corp.

## 📄 License

Grokoo is **dual-licensed** — choose whichever fits you:

- 🆓 **Open source:** [GNU LGPL-3.0-or-later](LICENSE). Free for any use, including
  on both Odoo Community and Enterprise; modifications to Grokoo's own files stay
  under the LGPL.
- 💼 **Commercial:** a proprietary-friendly license from CICDoo that lifts the LGPL
  obligations (keep modifications private, embed in closed products, warranty/SLA).
  See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) · **license@cicdoo.com**.

`SPDX-License-Identifier: LGPL-3.0-or-later OR LicenseRef-Grokoo-Commercial`

© 2026 [CICDoo](https://cicdoo.com)
</content>
