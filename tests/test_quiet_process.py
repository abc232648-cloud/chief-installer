import json
import os
import subprocess
import sys
import time

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
        process.run_streaming(command, inactivity_timeout=1.5, on_line=lines.append)
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
    t = threading.Timer(0.4, lambda: cancelled.__setitem__(0, True))
    t.start()
    try:
        with pytest.raises(CancelledError) as exc_info:
            process.run_streaming(command, check_cancelled=lambda: cancelled[0])
        assert "cancelled by user" in str(exc_info.value)
    finally:
        t.cancel()


def test_run_streaming_partial_output_resets_inactivity():
    script = (
        "import sys, time\n"
        "for _ in range(5):\n"
        "    sys.stdout.write('.')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.15)\n"
    )
    command = [sys.executable, "-u", "-c", script]
    activities = []
    res = process.run_streaming(
        command,
        inactivity_timeout=0.4,
        on_activity=activities.append,
    )
    assert res.returncode == 0
    assert res.stdout == "....."
    assert len(activities) >= 5


def test_run_streaming_carriage_return_progress():
    script = (
        "import sys, time\n"
        "sys.stdout.write('Step 1\\r')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('Step 2\\r')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.05)\n"
        "sys.stdout.write('Step 3\\n')\n"
        "sys.stdout.flush()\n"
    )
    command = [sys.executable, "-u", "-c", script]
    lines = []
    res = process.run_streaming(
        command,
        on_line=lines.append,
    )
    assert res.returncode == 0
    assert lines == ["Step 1\n", "Step 2\n", "Step 3\n"]
    assert res.stdout == "Step 1\nStep 2\nStep 3\n"


def test_run_streaming_nonzero_exit_and_diagnostics():
    script = (
        "import sys\n"
        "sys.stdout.write('standard output\\n')\n"
        "sys.stderr.write('diagnostic error\\n')\n"
        "sys.exit(17)\n"
    )
    command = [sys.executable, "-c", script]
    res = process.run_streaming(command)
    assert res.returncode == 17
    assert "standard output\n" in res.stdout
    assert "diagnostic error\n" in res.stderr


def test_terminate_process_tree_kills_children():
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
        "time.sleep(10)\n"
    )
    proc = process.popen([sys.executable, "-c", script])
    time.sleep(0.2)
    process.terminate_process_tree(proc, timeout=1.0)
    assert proc.poll() is not None

