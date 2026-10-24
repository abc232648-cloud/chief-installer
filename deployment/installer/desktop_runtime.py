"""Pinned Chromium desktop shell; never discover Chrome or a user's browser profile."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import sys
import tempfile
import time
import zipfile

VERSION = '45.0.0'
PINS = {
    'win32-x64': 'ae53887a23bb640369c93dbd38c408a2af5c0deb86f2fa76c6c244cb444b7f47',
    'linux-x64': '22f6a10bd3f7fef987ddbb379d3375ce664ad0c98a9a50f80c89f1718a0c3e3a',
}
ASSETS = ('package.json', 'main.cjs', 'policy.cjs', 'recovery.html', 'icon.png')
SERVICES = ('chief-agent-dashboard.service', 'chief-agent-worker.service',
            'chief-agent-scheduler.service', 'chief-tls-proxy.service')


def linux_runtime():
    return Path('/opt/chief-desktop') / (VERSION + '-linux-x64')


def verify_linux_ownership(runtime):
    for item in (runtime, *runtime.parents, *runtime.rglob('*')):
        info = plain(item).stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('Linux Chromium sandbox runtime must be root-owned and not user-writable.')
    if (runtime / 'chrome-sandbox').stat().st_mode & 0o4777 != 0o4755:
        raise ValueError('Linux Chromium sandbox helper is not configured.')


def protected_linux_runtime(archive):
    from .platforms.linux import LinuxPlatformAdapter
    protected = linux_runtime()
    if not protected.exists():
        LinuxPlatformAdapter()._run_privileged(['install-desktop-engine', str(archive)])
    verify_linux_ownership(protected)
    verify_runtime(archive, protected)
    return protected


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def plain(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Desktop paths cannot contain symbolic links or junctions.')
    return path


def write_json(path, value):
    path = plain(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.desktop-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def extract(archive, target):
    """Extract only bounded, regular, uniquely named entries into a new directory."""
    target = plain(target)
    if target.exists():
        raise ValueError('Desktop extraction destination must be new.')
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 5000 or sum(x.file_size for x in entries) > 1024**3:
            raise ValueError('Desktop archive exceeds limits.')
        seen = set()
        for entry in entries:
            name = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (not entry.filename or name.is_absolute() or '..' in name.parts or
                    '\\' in entry.orig_filename or ':' in entry.filename or
                    entry.filename.casefold() in seen or stat.S_ISLNK(mode) or
                    any(part.rstrip(' .') != part or part.split('.')[0].upper() in
                        {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                         *(f'LPT{i}' for i in range(1, 10))} for part in name.parts)):
                raise ValueError('Unsafe desktop archive entry.')
            seen.add(entry.filename.casefold())
        target.mkdir(mode=0o700, parents=True)
        for entry in entries:
            output = target / entry.filename
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with bundle.open(entry) as source, output.open('xb') as destination:
                shutil.copyfileobj(source, destination)
            output.chmod(0o700 if entry.external_attr >> 16 & 0o111 else 0o600)


def verify_runtime(archive, runtime):
    """A retry cannot bless an altered previously extracted binary or library."""
    expected = set()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            expected.add(entry.filename)
            target = plain(runtime / entry.filename)
            if not target.is_file() or target.stat().st_size != entry.file_size:
                raise ValueError('Desktop runtime content is missing or modified.')
            h = hashlib.sha256()
            with bundle.open(entry) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            if digest(target) != h.hexdigest():
                raise ValueError('Desktop runtime content is modified.')
    actual = {p.relative_to(runtime).as_posix() for p in runtime.rglob('*') if p.is_file()}
    if actual != expected:
        raise ValueError('Desktop runtime has unexpected files.')


def provision(paths, download, *, helper=None):
    machine = platform.machine().lower()
    if machine not in {'amd64', 'x86_64'} or sys.platform not in {'win32', 'linux'}:
        raise ValueError('This desktop release supports Windows/Linux x86-64.')
    key = sys.platform + '-x64'
    root = plain(paths.root / 'desktop')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = root / f'electron-v{VERSION}-{key}.zip'
    if not archive.exists() or digest(archive) != PINS[key]:
        download(f'https://github.com/electron/electron/releases/download/v{VERSION}/{archive.name}',
                 archive, substep='Downloading Chief desktop Chromium engine')
    if digest(archive) != PINS[key]:
        raise ValueError('Desktop engine checksum mismatch; no engine was activated.')
    runtime = root / f'electron-{VERSION}-{key}'
    if not runtime.exists():
        stage = root / ('stage-' + next(tempfile._get_candidate_names()))
        try:
            extract(archive, stage)
            stage.rename(runtime)
        except Exception:
            # Only our new, validated stage; never an existing installation.
            if stage.exists() and plain(stage).is_relative_to(root):
                shutil.rmtree(plain(stage))
            raise
    verify_runtime(archive, plain(runtime))
    if sys.platform == 'linux':
        runtime = protected_linux_runtime(archive)
    executable = runtime / ('electron.exe' if os.name == 'nt' else 'electron')
    if not executable.is_file():
        raise ValueError('Desktop engine executable is missing.')
    source = Path(__file__).with_name('desktop')
    asset_hashes = {name: digest(source / name) for name in ASSETS}
    identity = hashlib.sha256(json.dumps(asset_hashes, sort_keys=True).encode()).hexdigest()
    application = root / ('app-' + identity)
    application.mkdir(mode=0o700, exist_ok=True)
    for name in ASSETS:
        destination = plain(application / name)
        if destination.exists() and digest(destination) != asset_hashes[name]:
            raise ValueError('Installed desktop application content differs from its release.')
        if not destination.exists():
            shutil.copy2(source / name, destination)
    helper = helper or os.environ.get('CHIEF_INSTALLER_FROZEN_EXECUTABLE')
    if not helper or not Path(helper).is_file():
        raise ValueError('Desktop setup requires the packaged installer service helper.')
    helper_hash = digest(helper)
    helper_copy = root / ('service-helper-' + helper_hash + ('.exe' if os.name == 'nt' else ''))
    if not helper_copy.exists():
        shutil.copy2(helper, helper_copy)
    if digest(helper_copy) != helper_hash:
        raise ValueError('Desktop service helper checksum mismatch.')
    record = {'version': 1, 'engine': VERSION, 'archive_sha256': PINS[key],
              'executable': str(executable), 'executable_sha256': digest(executable),
              'application': str(application), 'assets': asset_hashes,
              'helper': str(helper_copy), 'helper_sha256': helper_hash}
    write_json(root / 'current.json', record)
    return record


def installed(paths):
    root = plain(paths.root / 'desktop')
    record = json.loads(plain(root / 'current.json').read_text(encoding='utf-8'))
    if record['version'] != 1 or record['engine'] != VERSION or record['archive_sha256'] not in PINS.values():
        raise ValueError('Desktop engine release does not match.')
    for key in ('executable', 'application', 'helper'):
        item = plain(record[key])
        protected_engine = sys.platform == 'linux' and key == 'executable' and item == linux_runtime() / 'electron'
        if protected_engine:
            verify_linux_ownership(linux_runtime())
        if not protected_engine and not item.is_relative_to(root):
            raise ValueError('Desktop component is outside its installation.')
    for key in ('executable', 'helper'):
        if digest(record[key]) != record[key + '_sha256']:
            raise ValueError('Desktop component failed integrity verification.')
    if set(record['assets']) != set(ASSETS):
        raise ValueError('Desktop asset manifest is incomplete.')
    source = Path(__file__).with_name('desktop')
    for name in ASSETS:
        expected = digest(source / name)
        if record['assets'][name] != expected or digest(plain(Path(record['application']) / name)) != expected:
            raise ValueError('Desktop assets failed release verification.')
    return record


def launch_command(paths, public_host):
    import re
    if not isinstance(public_host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', public_host):
        raise ValueError('Invalid Chief hostname.')
    record = installed(paths)
    config = json.loads(paths.config.read_text(encoding='utf-8'))
    ca = paths.caddy_root / 'data/caddy/pki/authorities/local/root.crt'
    settings = {'version': 1, 'origin': 'https://' + public_host.lower(),
                'stateRoot': str(paths.state_root), 'installationRoot': str(paths.root),
                'home': str(paths.get_home()), 'helper': record['helper'],
                'localCA': str(ca) if config.get('tls_mode', 'LOCAL') == 'LOCAL' else None}
    filename = paths.config_root / 'desktop.json'
    write_json(filename, settings)
    engine = [record['executable']]
    if sys.platform == 'linux' and os.environ.get('WAYLAND_DISPLAY'):
        engine.append('--ozone-platform=wayland')
    return [*engine, record['application'], '--chief-config', str(filename)]


def control_main(argv):
    """Normal-user service lifecycle only: no uninstall, policy changes or credentials."""
    from .orchestrator import InstallerPaths
    from .cli import get_default_platform_adapter
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop', 'status', 'startup-enable', 'startup-disable'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    options = parser.parse_args(argv)
    paths = InstallerPaths.create(custom_root=plain(options.root), home=plain(options.home))
    # Windows default config lives in Roaming, whereas custom-root layouts use
    # .config. Resolve the normal installation explicitly when roots agree.
    default = InstallerPaths.create(home=options.home)
    if default.root == paths.root:
        paths = default
    if not paths.config.is_file() or not paths.identity_config.is_file():
        print(json.dumps({'status': 'SETUP_REQUIRED'})); return 1
    adapter = get_default_platform_adapter()
    if options.action == 'status':
        states = {name: adapter.verify_service_active(name) for name in SERVICES}
        print(json.dumps({'status': 'SUCCESS', 'active': states})); return 0
    if options.action.startswith('startup-'):
        operation = 'enable' if options.action == 'startup-enable' else 'disable'
        result = adapter.control_services(operation, list(SERVICES))
        success = all(result.get(name) is True for name in SERVICES)
        print(json.dumps({'status': 'SUCCESS' if success else 'INCOMPLETE'}))
        return 0 if success else 1
    if options.action == 'stop':
        success = stop_services(paths, adapter)
    else:
        enabled = adapter.control_services('enable', list(SERVICES))
        if not all(enabled.get(name) is True for name in SERVICES):
            print(json.dumps({'status': 'INCOMPLETE', 'action': options.action})); return 1
        pending = [name for name in SERVICES if not adapter.verify_service_active(name)]
        outcome = adapter.control_services('start', pending)
        success = all(outcome.get(name) is True for name in pending)
        deadline = time.monotonic() + 30
        while success and not all(adapter.verify_service_active(name) for name in SERVICES):
            if time.monotonic() >= deadline:
                success = False; break
            time.sleep(.2)
        preference = plain(paths.state_root / 'desktop-preferences.json')
        if preference.is_file() and json.loads(preference.read_text()).get('startAtLogin') is False:
            restored = adapter.control_services('disable', list(SERVICES))
            success = success and all(restored.get(name) is True for name in SERVICES)
    print(json.dumps({'status': 'SUCCESS' if success else 'INCOMPLETE', 'action': options.action}))
    return 0 if success else 1


def stop_services(paths, adapter, timeout=45):
    """Use Chief's run-specific cooperative protocol before stopping the proxy.

    Never kill database writers to implement a normal window-close preference.
    If any writer cannot acknowledge shutdown, retain the shell and report failure.
    """
    pending = {}
    for component in ('dashboard', 'worker', 'scheduler'):
        status = plain(paths.production / 'service-control' / (component + '.json'))
        service = 'chief-agent-' + component + '.service'
        if not status.is_file():
            if adapter.verify_service_active(service):
                return False
            continue
        if status.stat().st_size > 4096:
            return False
        record = json.loads(status.read_text(encoding='utf-8'))
        if record.get('state') in {'STOPPED', 'REVIEW'}:
            if adapter.verify_service_active(service):
                return False
            continue
        token = record.get('run_id')
        if (record.get('state') not in {'STARTING', 'READY', 'STOPPING'} or
                not isinstance(token, str) or len(token) != 32 or
                any(c not in '0123456789abcdef' for c in token)):
            return False
        write_json(status.with_name(component + '.stop.json'), {'run_id': token})
        pending[component] = (status, token)
    deadline = time.monotonic() + timeout
    while pending:
        for component, (status, token) in list(pending.items()):
            record = json.loads(plain(status).read_text(encoding='utf-8'))
            if record.get('run_id') != token:
                return False  # A restart must not be mistaken for our stopped run.
            if record.get('state') == 'STOPPED':
                del pending[component]
            elif record.get('state') == 'REVIEW':
                return False
        if pending:
            if time.monotonic() >= deadline:
                return False
            time.sleep(.2)
    proxy = 'chief-tls-proxy.service'
    if os.name == 'nt':
        # Scope forced cleanup to the proxy directory only; never desktop or writers.
        adapter.quiesce_installation(plain(paths.caddy_root), [proxy])
    else:
        if not adapter.control_services('stop', [proxy]).get(proxy):
            return False
    return not any(adapter.verify_service_active(name) for name in SERVICES)
