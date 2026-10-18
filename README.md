# Chief Unified Installer

Dedicated cross-platform installer repository for **Chief**.

Chief Unified Installer provides one shared installation engine, one shared GUI flow, one state machine, one release-manifest contract, and thin operating-system adapters.

## Architecture

- **Common Core (`deployment/installer/`):**
  - `state.py`: Explicit, deterministic state machine with 20 sequential installation steps.
  - `orchestrator.py`: Authoritative installation engine coordinating preflight, archival, dependencies, runtime, PWAs, identity, services, and health probes.
  - `manifest.py`: Release manifest contract pinning exact component commit SHAs.
  - `contract.py`: Explicit, versioned compatibility client interfacing with Chief Agent deployment contracts.
  - `events.py`: Decoupled `EventBus` pub/sub system for UI/CLI consumption without stdout scraping.
  - `logging.py`: Secure file logger with regex credential redaction and automated diagnostic reporting.
  - `errors.py`: Standardized installer exception taxonomy with retryable flags.
- **User Interface (`deployment/installer/ui/`):**
  - `app.py`: Tkinter/TTK multi-screen wizard with minimal setup questions, native OS privilege requests, and immediate launch.
  - `controller.py`: UI controller handling async background installation threads and form validation.
  - `cli.py`: Non-interactive/interactive terminal CLI sharing the exact same state machine and orchestrator.
- **Platform Adapters (`deployment/installer/platforms/`):**
  - `base.py`: Abstract platform adapter contract.
  - `linux.py`: Linux adapter (Xubuntu reference platform; Ubuntu & Kali compatible). Handles zero-loss archival, Avahi mDNS discovery, and systemd user services.
- **Privilege Boundary (`deployment/installer/privileged/`):**
  - `linux.py`: Strictly constrained helper accepting only whitelisted packages and safe parameters.

## Building and Packaging

Build self-contained Linux executable bundle:

```bash
python packaging/installer/linux/build_package.py
```

On Ubuntu/Xubuntu, use `Chief-Installer-Linux-amd64.deb` from the Linux
artifact bundle. Open it with the system's Software Install or GDebi application,
install the setup application, then open **Chief Installer** from the applications
menu. Installing the package does not deploy Chief; deployment starts only in
the wizard. A file manager may require choosing the package installer once.
If no graphical package installer is present, run
`sudo apt install ./Chief-Installer-Linux-amd64.deb`, then use the menu entry.

The bare `chief-installer` executable is for terminal use; Linux file managers
are not guaranteed to launch downloaded ELF binaries by double-click. The native
Linux build now emits the `.deb` and its SHA-256 checksum alongside that binary.
The zipapp development fallback does not produce a desktop package. Real desktop
launch and installation acceptance remain required before release.

## Running Tests

```bash
pytest -v
```

## Chief DNS review branch

The wizard offers **Quad9 Secure DoH** (default) and **System DNS**. The CLI
accepts `--dns-mode quad9|system`. The choice is persisted as `dns_mode` in the
Chief production instance through the deployment contract. This does not change
host OS DNS. Covered Chief browsing/download paths stop on secure-DNS errors;
there is no Google or System DNS automatic fallback. Device PWA browsers,
provider SDKs, SMTP/push, local integrations, and installation dependencies retain
their own transports; this is not complete host-wide DNS enforcement.

The installer requires the agent capability `chief_scoped_dns`; an older agent
must fail compatibility validation rather than silently ignore the selection.
The accepted-release manifest remains unchanged. To review the new combination,
use the separately pinned `review-dns-manifest.json` with `--manifest`; this file
is a review candidate, not an accepted release or deployment authorization.
See the agent's `docs/Chief-Scoped-DNS.md` for coverage, safety audit, and browser
qualification limits. No real-host acceptance is claimed by CI.
