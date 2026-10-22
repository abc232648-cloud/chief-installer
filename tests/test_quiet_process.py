import json
import os
import subprocess
import sys
import threading
import time

import pytest
from deployment.installer import process

def test_non_windows_options_unchanged(monkeypatch):
    monkeypatch.setattr(process.sys, 'platform', 'linux')
    calls = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: calls.append(kwargs))
    process.popen(['tool'], stdout=subprocess.PIPE, env={'TEST':'value'})
    assert calls == [{'stdout':subprocess.PIPE,'env':{'TEST':'value'},'start_new_session':True}]


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
    try:
        child_pid_str = proc.stdout.readline().strip()
        child_pid = int(child_pid_str)
        proc.wait(timeout=5.0)
        assert proc.poll() is not None, "Parent process should have exited"
        assert _is_pid_alive(child_pid), "Child process should still be alive before terminate_process_tree"

        # Terminate tree
        process.terminate_process_tree(proc, timeout=3.0)
        assert not _is_pid_alive(child_pid), "Child process must be terminated even though parent already exited"
    finally:
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()


def test_terminate_process_tree_multi_generation_grandchild():
    """Verify termination handles parent, child, and grandchild generations."""
    gc_code = "import time; time.sleep(60)"
    child_code = f"import subprocess, sys, time; gc = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}]); print('GC:' + str(gc.pid)); sys.stdout.flush(); time.sleep(60)"
    parent_code = f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', {repr(child_code)}], stdout=subprocess.PIPE, text=True); print('C:' + str(c.pid)); print(c.stdout.readline().strip()); sys.stdout.flush()"

    proc = process.popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    try:
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
    finally:
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()


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
    fake_proc = process.popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    fake_proc.wait()

    if os.name == "nt":
        monkeypatch.setattr(process, "_win_get_job_pids", lambda job: [999999])
    else:
        monkeypatch.setattr(process, "_posix_is_pgid_alive", lambda pgid: True)

    with pytest.raises(RuntimeError) as exc_info:
        process.terminate_process_tree(fake_proc, timeout=0.3)
    assert "Failed to terminate process tree" in str(exc_info.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object race test")
def test_windows_immediate_grandchild_containment_race():
    """Verify that a child spawning a grandchild on line 1 before parent exits is captured and terminated."""
    gc_code = "import time; time.sleep(30)"
    parent_code = (
        f"import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"print('GC_PID:' + str(p.pid), flush=True)\n"
        f"sys.exit(0)\n"
    )
    proc = process.popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=5.0)
    assert "GC_PID:" in out
    gc_pid = int(out.split("GC_PID:")[1].strip())
    assert proc.poll() == 0, "Parent process should have exited cleanly"

    # Grandchild should be alive in the job
    assert process._win_is_pid_alive(gc_pid)

    # Terminate process tree using parent's Popen object
    process.terminate_process_tree(proc, timeout=3.0)
    assert not process._win_is_pid_alive(gc_pid), "Grandchild must be terminated even if child spawned it on line 1"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object launch failure test")
@pytest.mark.parametrize("fail_point", ["assign", "resume"])
def test_windows_job_launch_failure_no_leaks(fail_point):
    """Verify that launch failure in Job assignment or thread resume safely terminates the suspended process with no leaks."""
    process._popen_sim_fail.value = fail_point
    failed = False
    try:
        proc = process.popen([sys.executable, "-c", "import time; time.sleep(30)"])
    except OSError as e:
        failed = True
        assert fail_point in str(e).lower()
    finally:
        leaked_pid = process._popen_sim_fail.last_created_pid
        process._popen_sim_fail.value = None

    assert failed, f"Expected popen to fail safely at {fail_point}"
    assert leaked_pid is not None
    time.sleep(0.3)
    assert not process._win_is_pid_alive(leaked_pid), "Suspended process must be terminated on launch failure"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object creation failure test")
def test_windows_job_creation_failure_fails_safely(monkeypatch):
    """Verify that if Job Object creation fails, popen fails closed and does not run uncontained."""
    monkeypatch.setattr(process, "_win_create_job", lambda: None)
    with pytest.raises(RuntimeError) as exc_info:
        process.popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    assert "Failed to create and configure Windows Job Object" in str(exc_info.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object buffer growth test")
def test_windows_query_buffer_growth_retrieves_all_processes():
    """Verify that when more processes exist than the initial query buffer can hold, buffer is dynamically resized."""
    process._popen_sim_fail.initial_query_buffer_size = 1
    job = process._win_create_job()
    assert job is not None
    procs = []
    try:
        for _ in range(3):
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
            process._kernel32.AssignProcessToJobObject(
                process.wintypes.HANDLE(job.handle),
                process.wintypes.HANDLE(int(p._handle)),
            )
            procs.append(p)

        # Buffer size of 1 triggers ERROR_MORE_DATA, dynamically resizing
        pids = process._win_get_job_pids(job)
        for p in procs:
            assert p.pid in pids, f"PID {p.pid} must be in returned job PIDs: {pids}"
    finally:
        process._popen_sim_fail.initial_query_buffer_size = 64
        job.close()
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=1.0)
            except Exception:
                pass


@pytest.mark.skipif(os.name != "nt", reason="Windows API access-denied check")
def test_windows_is_pid_alive_access_denied_treated_as_alive(monkeypatch):
    """Verify that OpenProcess returning ERROR_ACCESS_DENIED is treated as process alive."""
    monkeypatch.setattr(process._kernel32, "OpenProcess", lambda *args: 0)
    monkeypatch.setattr(process._kernel32, "GetLastError", lambda: 5)  # ERROR_ACCESS_DENIED
    assert process._win_is_pid_alive(12345) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows handle validation check")
def test_windows_handle_validation():
    """Verify explicit handle validation handles None, 0, -1, and INVALID_HANDLE_VALUE."""
    assert process._is_valid_handle(None) is False
    assert process._is_valid_handle(0) is False
    assert process._is_valid_handle(-1) is False
    assert process._is_valid_handle(process._INVALID_HANDLE_VALUE) is False
    assert process._is_valid_handle(12345) is True
    assert process._is_valid_handle(process.wintypes.HANDLE(12345)) is True


def test_persistent_service_lifecycle_boundary():
    """Verify that persistent services created with contained=False survive installer temporary process cleanup."""
    # Persistent service (contained=False)
    service_proc = process.popen([sys.executable, "-c", "import time; time.sleep(30)"], contained=False)
    # Temporary installer process (contained=True)
    temp_proc = process.popen([sys.executable, "-c", "import time; time.sleep(30)"], contained=True)

    try:
        assert _is_pid_alive(service_proc.pid)
        assert _is_pid_alive(temp_proc.pid)

        # Terminating temporary process tree must not affect persistent service
        process.terminate_process_tree(temp_proc, timeout=2.0)

        assert not _is_pid_alive(temp_proc.pid), "Temporary process must be terminated"
        assert _is_pid_alive(service_proc.pid), "Persistent service must remain alive"
        assert service_proc.poll() is None
    finally:
        try:
            temp_proc.wait(timeout=1.0)
        except Exception:
            pass
        try:
            service_proc.terminate()
            service_proc.wait(timeout=2.0)
        except Exception:
            pass


def test_run_streaming_callback_exception_terminates_process_and_chains():
    """Verify that an exception raised by a streaming callback cleanly terminates the process tree and preserves the exception."""
    child_pid = []

    def failing_callback(line):
        if "STARTED:" in line:
            child_pid.append(int(line.split("STARTED:")[1].strip()))
            raise ValueError("Intentional callback error")

    code = "import sys, os; print('STARTED:' + str(os.getpid()), flush=True); import time; time.sleep(30)"

    with pytest.raises(ValueError) as exc_info:
        process.run_streaming([sys.executable, "-u", "-c", code], on_line=failing_callback)

    assert "Intentional callback error" in str(exc_info.value)
    assert child_pid, "Should have received PID before failing"
    time.sleep(0.3)
    assert not _is_pid_alive(child_pid[0]), "Child process must be terminated when callback raises"


def test_run_streaming_cancellation_concurrent_with_normal_exit():
    """Verify that cancellation racing with normal exit does not cause errors or hangs."""
    code = "import sys, time; time.sleep(0.05); sys.exit(0)"
    cancel_flag = [False]

    import threading
    t = threading.Timer(0.04, lambda: cancel_flag.__setitem__(0, True))
    t.start()
    try:
        try:
            res = process.run_streaming([sys.executable, "-c", code], check_cancelled=lambda: cancel_flag[0])
            assert res.returncode == 0
        except Exception as e:
            from deployment.installer.errors import CancelledError
            assert isinstance(e, CancelledError)
    finally:
        t.cancel()


def test_descendants_holding_pipes_open_unblocks_on_termination():
    """Verify that when a grandchild inherits and holds open stdout, timeout terminates the tree and unblocks readers."""
    gc_code = "import time; time.sleep(60)"
    parent_code = (
        f"import subprocess, sys\n"
        f"c = subprocess.Popen([sys.executable, '-c', {repr(gc_code)}])\n"
        f"print('GC:' + str(c.pid), flush=True)\n"
        f"import time; time.sleep(60)\n"
    )
    lines = []
    start_t = time.monotonic()
    with pytest.raises(TimeoutError):
        process.run_streaming([sys.executable, "-c", parent_code], on_line=lines.append, timeout=1.0)
    elapsed = time.monotonic() - start_t
    assert elapsed < 4.0, f"Cleanup took too long ({elapsed:.2f}s); pipe was likely held open"

    gc_pid = None
    for l in lines:
        if "GC:" in l:
            gc_pid = int(l.split("GC:")[1].strip())
    assert gc_pid is not None
    assert not _is_pid_alive(gc_pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX graceful resistance test")
def test_posix_graceful_resistance_forces_kill():
    """Verify that on POSIX, a process ignoring SIGTERM is forcefully killed with SIGKILL."""
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    proc = process.popen([sys.executable, "-c", code])
    time.sleep(0.2)
    assert _is_pid_alive(proc.pid)
    process.terminate_process_tree(proc, timeout=2.0)
    assert not _is_pid_alive(proc.pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows monkeypatch guard")
def test_import_does_not_patch_winapi():
    """Verify that importing process.py does not replace _winapi.CreateProcess."""
    import _winapi
    import importlib
    original_fn = _winapi.CreateProcess
    # Reload the module; it must NOT reassign _winapi.CreateProcess
    importlib.reload(process)
    assert _winapi.CreateProcess is original_fn, (
        "Importing/reloading deployment.installer.process must not replace "
        "_winapi.CreateProcess"
    )


@pytest.mark.parametrize('exit_parent', [False, True])
def test_run_owns_descendants(tmp_path, exit_parent):
    pid_file = tmp_path / 'child.pid'
    parent = ("import subprocess,sys,time; from pathlib import Path; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
              "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
              + ("" if exit_parent else "time.sleep(60)"))
    if exit_parent:
        process.run([sys.executable, '-c', parent], capture_output=True, timeout=5)
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            process.run([sys.executable, '-c', parent], capture_output=True, timeout=2)
    assert pid_file.exists()
    assert not _is_pid_alive(int(pid_file.read_text()))


def test_run_preserves_input_encoding_and_check():
    result = process.run([sys.executable, '-c', 'import sys; print(sys.stdin.read())'],
                         input='hello', capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'hello'
    with pytest.raises(subprocess.CalledProcessError) as error:
        process.run([sys.executable, '-c', 'import sys; sys.exit(7)'], check=True)
    assert error.value.returncode == 7


def test_streaming_success_reaps_detached_output_child(tmp_path):
    marker = tmp_path / 'descendant.pid'
    command = ("import subprocess,sys; from pathlib import Path; "
               "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
               "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
               f"Path({str(marker)!r}).write_text(str(p.pid))")
    result = process.run_streaming([sys.executable, '-c', command], timeout=5)
    assert result.returncode == 0
    assert not _is_pid_alive(int(marker.read_text()))


def test_process_registration_callback_failure_reaps_child():
    children = []
    def fail(proc):
        children.append(proc)
        raise ValueError('registration failed')
    with pytest.raises(ValueError, match='registration failed'):
        process.run_streaming([sys.executable, '-c', 'import time; time.sleep(30)'], on_proc=fail)
    assert children[0].poll() is not None
