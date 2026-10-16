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

## Running Tests

```bash
pytest -v
```
