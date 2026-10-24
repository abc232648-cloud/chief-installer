"""CI checkout of the exact backend selected by the review manifest."""
import json
from pathlib import Path
import re
import subprocess
import sys


def checkout(destination):
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'review-dns-manifest.json').read_text(encoding='utf-8'))
    commit = manifest['chief_agent_commit']
    if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Release requires an exact backend commit.')
    destination = Path(destination).absolute()
    if destination.exists():
        raise ValueError('CI peer destination already exists; refusing to replace it.')
    subprocess.run(['git', 'clone', '--no-checkout', 'https://github.com/abc232648-cloud/chief-agent.git', str(destination)], check=True)
    subprocess.run(['git', '-C', str(destination), 'checkout', '--detach', commit], check=True)
    actual = subprocess.check_output(['git', '-C', str(destination), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != commit:
        raise RuntimeError('Backend checkout does not match the release manifest.')
    print('Verified backend release commit: ' + actual)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: checkout_release_peer.py DESTINATION')
    checkout(sys.argv[1])
