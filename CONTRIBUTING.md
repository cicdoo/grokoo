# Contributing to Grokoo

Thanks for your interest in improving Grokoo! Contributions of all kinds are
welcome — bug reports, documentation, tests, and code.

## Getting started

1. **Fork** the repository and clone your fork.
2. Add the addon to your Odoo `addons_path` (the repo *is* the module folder,
   so point the path at its parent directory).
3. Install with tests enabled on a throwaway database:
   ```bash
   odoo-bin -c odoo.conf -d grokoo_dev -i grokoo --test-enable --stop-after-init
   ```
4. Run the live server and try the **Grokoo** menu.

## Development guidelines

- **Target Odoo 18.0.** Match the surrounding code style (PEP 8, 4-space indent,
  `# -*- coding: utf-8 -*-` headers).
- **Security first.** Grokoo's whole value is its safety model. Any change that
  touches the tool endpoints, the SQL guard, the bridge token, or the auth method
  must keep the invariants in [`SECURITY.md`](SECURITY.md) and ship with tests.
- **Never run tools as superuser.** ORM tool endpoints must act as the user.
- **Keep the engine references intact.** Grokoo wraps the xAI Grok CLI; do not
  rename the auth-mode constants, the per-user `.grok` home, the CLI binary glob,
  the generated `.grok/config.toml` contract, or fields that store real Grok
  artifacts (`grok_session_id`).
- **Add tests** under `tests/` for any behavior change. Tests are tagged `grokoo`:
  ```bash
  odoo-bin ... -i grokoo --test-tags grokoo --stop-after-init
  ```

## Pull requests

Open one logical change per PR with a clear description; link any issue
(`Closes #123`). The items below are **enforced by CI and branch protection** — the
merge button stays disabled until they pass:

- **Changelog.** Add a bullet under the `## [Unreleased]` heading in
  [`CHANGELOG.md`](CHANGELOG.md). (A maintainer can apply the `skip-changelog` label
  for genuinely trivial PRs.)
- **Security docs.** If your PR touches a safety-critical path —
  `controllers/tools.py`, `controllers/main.py`, `models/ir_http.py`,
  `models/*_session.py`, `models/*_tool.py`, `bridge/`, or `security/` — update
  [`SECURITY.md`](SECURITY.md) to reflect the change (or have a maintainer apply the
  `security-reviewed` label).
- **Sign-off.** Every commit must be signed off — see *Developer Certificate of
  Origin* below.
- **Lint.** `flake8` must pass on the Python files you changed.
- **Review.** At least one maintainer approval.

There is no test job in CI, so run the suite locally before pushing:

```bash
odoo-bin ... -i grokoo --test-tags grokoo --stop-after-init
```

## AI-assisted contributions

AI coding tools are welcome here — this project is itself an AI assistant. The bar is
simply the same as for any other contribution:

- **You are the author.** You are fully accountable for every line, AI-generated or
  not. Only open PRs you understand and can explain; reviewers may ask how a change
  works.
- **Provenance.** AI can emit third-party or copyleft code verbatim. Submit only code
  that is your original work, or that you otherwise have the right to license under
  the terms in *Licensing of contributions* below. If you can't vouch for a snippet's
  origin, don't include it.
- **Never let AI weaken the security model.** Re-read [`SECURITY.md`](SECURITY.md).
  Changes that introduce `sudo()` / superuser execution, broaden the SQL tool past
  read-only `SELECT`, bypass `ir.model.access` or record rules, widen the bridge
  token's scope, or re-enable denied CLI built-ins **will be rejected** — AI
  assistants suggest these "helpfully" all the time. Keep the invariants and ship
  tests that prove them.
- **Verify.** Run the tests locally; never paste credentials, customer data, or
  proprietary code into an AI tool while working on this project.
- **Quality over volume.** Low-effort or bulk AI-generated PRs that don't meet the
  bar may be closed without a detailed review.

## Developer Certificate of Origin

Contributions are accepted under the **Developer Certificate of Origin 1.1** (full
text in the [`DCO`](DCO) file). Sign off every commit:

```bash
git commit -s -m "your message"
```

This appends a `Signed-off-by: Your Name <you@example.com>` trailer. Signing off
certifies the DCO **and**, for this project, that any AI-assisted code in the commit
is yours to license under the terms below. CI rejects unsigned commits; fix them with
`git commit -s --amend` (one commit) or `git rebase --signoff origin/18.0` (several).

### Licensing of contributions (important)

Grokoo is **dual-licensed** (open-source LGPL-3.0 **and** a commercial license —
see [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md)). For that model to work, the
maintainer must be able to ship every contribution under **both** licenses.

By submitting a contribution, you:

1. license your contribution under the **LGPL-3.0-or-later**; **and**
2. grant **CICDoo** a perpetual, worldwide, royalty-free, irrevocable right to
   also license your contribution under CICDoo's **commercial license** (and
   future versions of it), i.e. to relicense and sublicense it as part of
   Grokoo; **and**
3. confirm you have the right to grant this — the contribution is your original
   work (or you have authority to submit it) and is free of third-party claims.

This is a lightweight inbound=outbound + relicensing grant; it lets CICDoo fund
Grokoo's development through commercial licensing while keeping the project open.
If your employer owns your work, please ensure you have permission to contribute.

## Commercial support

Grokoo is maintained by [CICDoo](https://cicdoo.com). For managed hosting,
custom tool development, an SLA, or a commercial license, see the *Commercial*
section of the [README](README.md) or email **hello@cicdoo.com**.
