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
    if method == 'popen':
        assert calls==[{'stdout':subprocess.PIPE,'env':{'TEST':'value'},'start_new_session':True}]
    else:
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
        "    time.sleep(0.08)\n"
    )
    command = [sys.executable, "-u", "-c", script]
    activities = []
    res = process.run_streaming(
        command,
        inactivity_timeout=0.8,
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



def _is_pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return process._win_is_pid_alive(pid)
    return process._posix_is_pid_alive(pid)


def test_terminate_process_tree_parent_exits_before_descendants():
    """Verify that when a parent process exits, terminate_process_tree still terminates surviving descendants."""
    gc_code = "import time; time.sleep(60)"
    parent_code = f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}]); print(c.pid); sys.stdout.flush()"
    proc = process.popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    child_pid_str = proc.stdout.readline().strip()
    child_pid = int(child_pid_str)
    proc.wait(timeout=5.0)
    assert proc.poll() is not None, "Parent process should have exited"
    assert _is_pid_alive(child_pid), "Child process should still be alive before terminate_process_tree"

    # Terminate tree
    process.terminate_process_tree(proc, timeout=3.0)
    assert not _is_pid_alive(child_pid), "Child process must be terminated even though parent already exited"


def test_terminate_process_tree_multi_generation_grandchild():
    """Verify termination handles parent, child, and grandchild generations."""
    gc_code = "import time; time.sleep(60)"
    child_code = f"import subprocess, sys, time; gc = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}]); print('GC:' + str(gc.pid)); sys.stdout.flush(); time.sleep(60)"
    parent_code = f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', {repr(child_code)}], stdout=subprocess.PIPE, text=True); print('C:' + str(c.pid)); print(c.stdout.readline().strip()); sys.stdout.flush()"

    proc = process.popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    c_line = proc.stdout.readline().strip()
    gc_line = proc.stdout.readline().strip()
    c_pid = int(c_line.split(":")[1])
    gc_pid = int(gc_line.split(":")[1])
    proc.wait(timeout=5.0)

    assert _is_pid_alive(c_pid), "Child should be alive"
    assert _is_pid_alive(gc_pid), "Grandchild should be alive"

    process.terminate_process_tree(proc, timeout=3.0)

    assert not _is_pid_alive(c_pid), "Child must be terminated"
    assert not _is_pid_alive(gc_pid), "Grandchild must be terminated"


def test_run_streaming_cancellation_terminates_descendants():
    """Verify that cancellation in run_streaming cleanly terminates descendant processes."""
    from deployment.installer.errors import CancelledError

    gc_code = "import time; time.sleep(60)"
    parent_code = f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}]); print('GC:' + str(c.pid)); sys.stdout.flush(); import time; time.sleep(60)"

    lines = []
    cancel_flag = [False]
    import threading

    def trigger():
        time.sleep(0.3)
        cancel_flag[0] = True

    t = threading.Thread(target=trigger, daemon=True)
    t.start()

    start_t = time.monotonic()
    with pytest.raises(CancelledError):
        process.run_streaming(
            [sys.executable, "-c", parent_code],
            on_line=lines.append,
            check_cancelled=lambda: cancel_flag[0],
            timeout=10.0,
        )
    elapsed = time.monotonic() - start_t
    assert elapsed < 4.0, f"Cancellation took too long: {elapsed:.2f}s"

    gc_pid = None
    for l in lines:
        if "GC:" in l:
            gc_pid = int(l.split("GC:")[1].strip())

    assert gc_pid is not None, f"Expected grandchild PID in output: {lines}"
    assert not _is_pid_alive(gc_pid), "Grandchild process must be terminated on cancellation"


def test_run_streaming_timeout_terminates_descendants():
    """Verify that execution timeout cleanly terminates descendant processes holding open pipes."""
    gc_code = "import time; time.sleep(60)"
    parent_code = f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}]); print('GC:' + str(c.pid)); sys.stdout.flush(); import time; time.sleep(60)"

    lines = []
    start_t = time.monotonic()
    with pytest.raises(TimeoutError):
        process.run_streaming(
            [sys.executable, "-c", parent_code],
            on_line=lines.append,
            timeout=1.0,
        )
    elapsed = time.monotonic() - start_t
    assert elapsed < 4.0, f"Timeout cleanup took too long: {elapsed:.2f}s"

    gc_pid = None
    for l in lines:
        if "GC:" in l:
            gc_pid = int(l.split("GC:")[1].strip())

    assert gc_pid is not None, f"Expected grandchild PID in output: {lines}"
    assert not _is_pid_alive(gc_pid), "Grandchild process must be terminated on timeout"


def test_terminate_process_tree_preserves_unrelated_process():
    """Verify that terminating an installer process tree never terminates unrelated processes."""
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        sentinel_pid = sentinel.pid
        assert _is_pid_alive(sentinel_pid)

        installer_proc = process.popen([sys.executable, "-c", "import time; time.sleep(10)"])
        process.terminate_process_tree(installer_proc, timeout=2.0)

        assert _is_pid_alive(sentinel_pid), "Unrelated sentinel process must survive untouched"
        assert sentinel.poll() is None
    finally:
        try:
            sentinel.terminate()
            sentinel.wait(timeout=2.0)
        except Exception:
            pass


def test_terminate_process_tree_verification_failure_raises(monkeypatch):
    """Verify that terminate_process_tree raises RuntimeError if cleanup cannot be confirmed."""
    fake_proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    fake_proc.wait()

    if os.name == "nt":
        monkeypatch.setattr(process, "_win_get_job_pids", lambda job: [999999])
        fake_proc._chief_job = 1234
    else:
        monkeypatch.setattr(process, "_posix_is_pgid_alive", lambda pgid: True)

    with pytest.raises(RuntimeError) as exc_info:
        process.terminate_process_tree(fake_proc, timeout=0.3)
    assert "Failed to terminate process tree" in str(exc_info.value)
