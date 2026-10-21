"""Smoke-mode dispatch must construct the GUI without starting setup."""
import sys
from types import SimpleNamespace
from deployment.installer import __main__ as entry


def test_gui_smoke_constructs_and_closes_window_without_cli(monkeypatch):
    calls=[]
    class Window:
        def __init__(self):calls.append('created')
        def update_idletasks(self):calls.append('rendered')
        def destroy(self):calls.append('closed')
    monkeypatch.setitem(sys.modules,'deployment.installer.ui.app',SimpleNamespace(InstallerApp=Window))
    monkeypatch.setattr(sys,'argv',['chief-installer','--gui-smoke-test'])
    monkeypatch.setattr(entry,'_configure_packaged_python',lambda:None)
    def unexpected_cli(*args):raise AssertionError('GUI smoke must not run installation')
    monkeypatch.setattr(entry,'run_cli',unexpected_cli)
    assert entry.main()==0
    assert calls==['created','rendered','closed']
