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


def test_run_streaming_captures_lines_and_activity():
    command = [
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.write('line1\\n'); sys.stdout.flush(); time.sleep(0.1); sys.stdout.write('line2\\n'); sys.stdout.flush()",
    ]
    lines = []
    activities = []
    res = process.run_streaming(
        command,
        on_line=lines.append,
        on_activity=activities.append,
    )
    assert res.returncode == 0
    assert "line1\n" in lines
    assert "line2\n" in lines
    assert len(activities) >= 2


def test_run_streaming_bounded_timeout():
    command = [
        sys.executable,
        "-c",
        "import time; time.sleep(5)",
    ]
    with pytest.raises(TimeoutError) as exc_info:
        process.run_streaming(command, timeout=0.3)
    assert "timed out after" in str(exc_info.value)


def test_run_streaming_inactivity_timeout():
    command = [
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.write('init\\n'); sys.stdout.flush(); time.sleep(5)",
    ]
    lines = []
    with pytest.raises(TimeoutError) as exc_info:
        process.run_streaming(command, inactivity_timeout=0.3, on_line=lines.append)
    assert "stalled: no activity observed" in str(exc_info.value)
    assert "init\n" in lines


def test_run_streaming_cancellation():
    from deployment.installer.errors import CancelledError
    command = [
        sys.executable,
        "-c",
        "import time; time.sleep(5)",
    ]
    cancelled = [False]

    def trigger_cancel(ts):
        cancelled[0] = True

    # Use on_activity or timer to trigger cancel
    import threading
    t = threading.Timer(0.2, lambda: cancelled.__setitem__(0, True))
    t.start()
    try:
        with pytest.raises(CancelledError) as exc_info:
            process.run_streaming(command, check_cancelled=lambda: cancelled[0])
        assert "cancelled by user" in str(exc_info.value)
    finally:
        t.cancel()

