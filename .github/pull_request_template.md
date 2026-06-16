## What does this PR do?

<!-- A short summary of the change and the motivation. Link any issue: Closes #123 -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / chore

## Checklist

- [ ] Targets Odoo 18.0 and follows the existing code style
- [ ] Added/updated tests under `tests/` (tagged `grokoo`) and they pass locally
- [ ] Updated `CHANGELOG.md` (bullet under `## [Unreleased]`)
- [ ] Updated `SECURITY.md` if this touches a safety-critical path (else N/A)
- [ ] All commits are signed off (`git commit -s` → DCO)
- [ ] I am the author and have reviewed/understand **all** code here, including any AI-assisted parts, and it is mine to license
- [ ] Tool endpoints still run **as the user, never superuser**
- [ ] Did not alter Grok engine references (auth-mode constants, per-user `.grok`
      home, CLI glob, the `.grok/config.toml` contract, `grok_session_id`)
- [ ] For security-sensitive changes: invariants in `SECURITY.md` are preserved
