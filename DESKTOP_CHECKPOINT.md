# Desktop implementation checkpoint — 24 October 2026

Status: Draft / HOLD. Source checkpoint only; no merge, publication, production installation or data migration.

The desktop shell uses a pinned Chromium engine with renderer sandboxing, context isolation, no Node access from the page, an isolated profile, exact-origin navigation and local-CA verification. Normal Chrome/Edge discovery and automation-browser reuse were removed from the console launcher. Native Windows and Ubuntu hidden HTTPS acceptance proved successful valid-certificate navigation and rejection of an invalid CA. Error-page load events are not accepted as successful navigation. Ubuntu requires the engine's Wayland switch before application arguments.

The shell provides background/stop/ask close choices and automatic background-service startup preferences. Stop uses run-specific cooperative requests in the production state directory, waits for writer acknowledgement, then scopes Windows proxy cleanup to the Caddy directory. It must report incomplete shutdown rather than kill database writers. Shortcuts use a copied installed helper, not the original downloaded installer.

Linux production provisioning now requests administrator approval for a root-owned pinned engine under `/opt/chief-desktop`, including its sandbox helper. The user explicitly authorized this installation behavior. The privileged helper hashes a root-owned copy before extraction, rejects a mismatched archive and does not execute archive content. Chief runs as a normal user. This production helper has NOT yet completed a packaged end-to-end installation test. Native Ubuntu acceptance used the separately administrator-prepared `/opt/chief-desktop-test-45.0.0` engine, so it does not prove the production helper path.

Recorded checks:
- Native hidden HTTPS acceptance: Windows passed; Ubuntu passed with sandbox and Wayland.
- Desktop policy tests: six passed before the final startup-preference extension; rerun after final edits.
- Latest focused Windows Python run: 50 passed, zero failures/errors/skips; includes native desktop, runtime, console, guidance and orchestrator tests.
- Earlier full installer run: 260 collected, 254 passed, two failed, four skipped. Both failures were stale Linux package metadata/CLI artifacts. After rebuild, 29 targeted package/desktop checks passed. Later source edits require another rebuild and final full suite.

Remaining work before desktop clearance:
1. Qualify the production Linux privileged helper and fresh dependencies, cancellation/retry, ownership, reinstall and uninstall retention behavior. Do not disable sandboxing or install a setuid binary in a user-writable directory.
2. Verify lifecycle end to end on installed services, including concurrent menu requests, failed preference writes, already-stopped services, run replacement, worker shutdown delays, startup preference persistence and proxy restart. Current focused tests are not full installed-service acceptance.
3. Rebuild both installers from the final commit and rerun complete native suites. Existing local packages are intermediate and obsolete for final-source clearance.
4. Qualify shortcut branding, initial visible progress/recovery, terminal/focus behavior and the installation/first-opening consequence summary.
5. Replace the separate agent automation browser acquisition with the approved Chromium-only solution. Desktop work does not replace the existing automation runtime.
6. Complete the remaining product plan and reconcile manifests again after backend/client changes. Backend pin currently remains `4aaf66872142349f0507a9a6b497c3d6d06c8d69`.

At the user's explicit request, further checks were stopped for handover. Do not interpret this checkpoint as full-product completion. Mandatory neutral terminology rules in RELEASE_INSTRUCTIONS.md apply to all continuation work.
