import json
import os
import subprocess
import sys

import pytest
from deployment.installer import process


@pytest.mark.parametrize('method', ['run', 'popen'])
def test_non_windows_options_unchanged(monkeypatch, method):
    monkeypatch.setattr(process.sys, 'platform', 'linux')
    calls=[]
    monkeypatch.setattr(subprocess, 'run' if method=='run' else 'Popen',
                        lambda *args, **kwargs: calls.append(kwargs))
    getattr(process,method)(['tool'], stdout=subprocess.PIPE, env={'TEST':'value'})
    assert calls==[{'stdout':subprocess.PIPE,'env':{'TEST':'value'}}]


@pytest.mark.skipif(os.name!='nt', reason='Windows console allocation check')
@pytest.mark.parametrize('method', ['run', 'popen'])
def test_windows_child_has_no_console_and_preserves_output_and_failure(method):
    command=[sys.executable,'-c',
             'import ctypes,json,sys; print(json.dumps({"console":bool(ctypes.windll.kernel32.GetConsoleWindow())})); print("diagnostic",file=sys.stderr); sys.exit(7)']
    child=getattr(process,method)(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if method=='run': output,error=child.stdout,child.stderr
    else: output,error=child.communicate(timeout=15)
    assert child.returncode==7
    assert json.loads(output)=={'console':False}
    assert error.strip()=='diagnostic'
