# Release instructions — 24 October 2026

Release: `chief-2026-10-24-console-review`. This file supersedes older deployment instructions and installer download recommendations for the current candidate. Historical manifest constants remain evidence only; do not select them for a new installation.

## Exact release selection
Chief PR #39 is included. The backend commit is `4aaf66872142349f0507a9a6b497c3d6d06c8d69`. All component/runtime pins are in review-dns-manifest.json and must equal the packaged CURRENT_RELEASE_MANIFEST. CI reads that manifest rather than a separate backend SHA. The final accompanying RELEASE_SET.json records the installer commit and hashes of the actual rebuilt Windows EXE and Linux DEB/native payload.

Before using an artifact, verify its SHA-256 against RELEASE_SET.json, then run it with `--print-release-manifest` and compare the complete JSON to the supplied release manifest. This command does not install or start services. Do not use old service-startup-fix/cache-retry-fix/verified-cleanup-fix executables with this candidate. Do not mix revisions or assume an old accepted manifest qualifies this new package.

## Software clearance versus deployment
The accompanying CLEARANCE_RECORD.md must state the exact tested commits, full Windows/Ubuntu counts, browser matrix, installer CI/build evidence, artifact hashes, skipped checks and remaining gates. Passing software/package checks permits review and controlled acceptance testing only. It is not permission to overwrite a farm installation or declare every phone supported.

Folio, physical phone and existing Farm-data acceptance remain OPEN. No production deployment, real-data migration, merge or destructive uninstall is authorized by this record. PR #39 and installer PR #5 remain Draft/HOLD pending those gates.

## When the physical acceptance phase is authorized
- Confirm Folio OS/version, supported package format, available space and the exact artifact hash. Use a disposable/test installation first.
- Verify install, login, selected domains, local HTTPS and discovery, service startup/reboot, cancellation/retry, shutdown/background behavior and scoped uninstall. Required OS permission prompts must be distinguished from unwanted terminal popups.
- Test actual Owner/Staff/Job PWA access and role separation on the intended phones/browsers. Check certificate trust, installability, offline queue isolation/replay, notifications after timeout, logout and account switching.
- Before touching existing Farm data, take a consistent backup with writers stopped and validate a restore into an isolated instance. Reconcile birds/eggs/feed, opening balances, historical adjustments, debts/payments, finance permissions, attachments and audit/history. Record counts and exceptions without exporting customer data or credentials into source or public evidence. Keep the original data until acceptance and rollback are proven.
- After those checks, issue a separate deployment decision recording the actual devices and data-validation evidence. Do not convert UNKNOWN or untested checks to passed.

This candidate still uses the existing browser-based Console. The new Chromium desktop shell, expanded provider activation/spending controls, SOP/framework expansion and OpenClaw/Hermes qualification remain separate planned work. No qualification label or authority restriction is bypassed to complete this release reconciliation.
