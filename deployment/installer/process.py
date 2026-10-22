"""Quiet child processes; preserve output capture, streaming, process tree ownership and exit handling."""
import codecs
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

class ProcessCleanupError(RuntimeError):
    """Owned processes could not be verified stopped; preserve working files."""


_LINE_SPLIT_REGEX = re.compile(r"(\r\n|\r|\n)")

if sys.platform == "win32":
    import _winapi
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32

    # Windows API signatures with documented pointer-sized and explicit types
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]

    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]

    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]

    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]

    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _kernel32.GetLastError.restype = wintypes.DWORD
    _kernel32.GetLastError.argtypes = []

    _CREATE_SUSPENDED = 0x00000004
    _JobObjectExtendedLimitInformation = 9
    _JobObjectBasicProcessIdList = 3
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _ERROR_MORE_DATA = 234
    _ERROR_ACCESS_DENIED = 5
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def _is_valid_handle(h: Any) -> bool:
        if h is None:
            return False
        val = getattr(h, "value", h)
        if val is None:
            return False
        try:
            val = int(val)
        except (ValueError, TypeError):
            return False
        return val not in (0, -1, _INVALID_HANDLE_VALUE)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    def _make_process_id_list_struct(count: int):
        class _DYNAMIC_JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * max(1, count)),
            ]
        return _DYNAMIC_JOBOBJECT_BASIC_PROCESS_ID_LIST

    class _JobWrapper:
        """Encapsulates a Win32 Job Object handle ensuring single-close semantics."""

        def __init__(self, handle: int):
            self.handle = handle

        def is_valid(self) -> bool:
            return _is_valid_handle(self.handle)

        def close(self) -> None:
            h = self.handle
            self.handle = 0
            if _is_valid_handle(h):
                try:
                    _kernel32.CloseHandle(wintypes.HANDLE(h))
                except Exception:
                    pass

    def _win_create_job() -> Optional[_JobWrapper]:
        try:
            h_job = _kernel32.CreateJobObjectW(None, None)
            if not _is_valid_handle(h_job):
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            success = _kernel32.SetInformationJobObject(
                wintypes.HANDLE(h_job),
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not success:
                _kernel32.CloseHandle(wintypes.HANDLE(h_job))
                return None
            return _JobWrapper(h_job)
        except Exception:
            return None

    def _win_is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
        if not _is_valid_handle(h):
            err = _kernel32.GetLastError()
            if err == _ERROR_ACCESS_DENIED:
                return True
            return False
        try:
            code = wintypes.DWORD()
            if _kernel32.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return True
        finally:
            _kernel32.CloseHandle(wintypes.HANDLE(h))

    def _win_kill_pid(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, wintypes.DWORD(pid))
        if not _is_valid_handle(h):
            return False
        try:
            return bool(_kernel32.TerminateProcess(wintypes.HANDLE(h), 1))
        finally:
            _kernel32.CloseHandle(wintypes.HANDLE(h))

    # Save a direct reference to the real _winapi.CreateProcess BEFORE module
    # initialisation completes so we own it regardless of any external patching.
    # We NEVER reassign _winapi.CreateProcess – no monkeypatching.
    _real_CreateProcess = _winapi.CreateProcess

    class _SimFail:
        """Thread-local-free test injection point for simulating win32 launch failures."""
        value: Optional[str] = None
        last_created_pid: Optional[int] = None

    _popen_sim_fail = _SimFail()

    def _win_create_process_in_job(args_or_str, job, opts, sim_fail=None):
        """Use CPython pipe/handle setup with a per-launch Windows API facade.

        The function globals are copied, never modified in subprocess itself.
        This private CPython seam is covered by the supported Python CI matrix.
        """
        import types

        class LaunchAPI:
            def __getattr__(self, name):
                return getattr(_winapi, name)

            def CreateProcess(self, *args, **kwargs):
                arguments = list(args)
                arguments[5] |= _CREATE_SUSPENDED
                hp, ht, pid, tid = _real_CreateProcess(*arguments, **kwargs)
                _popen_sim_fail.last_created_pid = pid
                try:
                    if sim_fail == "assign" or not _kernel32.AssignProcessToJobObject(
                        wintypes.HANDLE(job.handle), wintypes.HANDLE(hp)
                    ):
                        raise OSError(_kernel32.GetLastError() or 5, "AssignProcessToJobObject failed")
                    result = (0xFFFFFFFF if sim_fail == "resume" else
                              _kernel32.ResumeThread(wintypes.HANDLE(ht)))
                    if result == 0xFFFFFFFF:
                        raise OSError(_kernel32.GetLastError() or 5, "ResumeThread failed")
                    return hp, ht, pid, tid
                except BaseException:
                    try:
                        if not _kernel32.TerminateProcess(wintypes.HANDLE(hp), 1):
                            raise ctypes.WinError(_kernel32.GetLastError())
                        if _winapi.WaitForSingleObject(hp, 3000) != 0:
                            raise RuntimeError("Failed to reap suspended child")
                    finally:
                        _winapi.CloseHandle(ht)
                        _winapi.CloseHandle(hp)
                    raise

        original = subprocess.Popen._execute_child
        namespace = dict(original.__globals__)
        namespace['_winapi'] = LaunchAPI()
        execute = types.FunctionType(original.__code__, namespace,
                                     original.__name__, original.__defaults__,
                                     original.__closure__)
        execute.__kwdefaults__ = original.__kwdefaults__
        class ContainedPopen(subprocess.Popen):
            _execute_child = execute
        return ContainedPopen(args_or_str, **opts)

    def _win_get_job_pids(job: Union[_JobWrapper, int]) -> List[int]:
        """Retrieve currently active process IDs in the Job Object.

        Dynamically expands query buffer if more processes exist.
        Raises RuntimeError on API query failure to ensure errors are never treated as empty.
        """
        h_job = job.handle if isinstance(job, _JobWrapper) else job
        if not _is_valid_handle(h_job):
            raise ValueError("Invalid Job Object handle provided to _win_get_job_pids")

        if getattr(_popen_sim_fail, "sim_query_fail", False):
            raise RuntimeError("Simulated QueryInformationJobObject failure")

        init_size = getattr(_popen_sim_fail, "initial_query_buffer_size", 64)
        struct_cls = _make_process_id_list_struct(init_size)
        buf = struct_cls()

        success = _kernel32.QueryInformationJobObject(
            wintypes.HANDLE(h_job),
            _JobObjectBasicProcessIdList,
            ctypes.byref(buf),
            ctypes.sizeof(buf),
            None,
        )
        if not success:
            err = _kernel32.GetLastError()
            if err == _ERROR_MORE_DATA:
                needed = max(buf.NumberOfAssignedProcesses, init_size * 2)
                struct_cls = _make_process_id_list_struct(needed)
                buf = struct_cls()
                success = _kernel32.QueryInformationJobObject(
                    wintypes.HANDLE(h_job),
                    _JobObjectBasicProcessIdList,
                    ctypes.byref(buf),
                    ctypes.sizeof(buf),
                    None,
                )
                if not success:
                    err2 = _kernel32.GetLastError()
                    raise RuntimeError(f"QueryInformationJobObject failed on resize retry: {err2}")
            else:
                raise RuntimeError(f"QueryInformationJobObject failed with error {err}")

        raw_pids = list(buf.ProcessIdList[: buf.NumberOfProcessIdsInList])
        return [p for p in raw_pids if p > 0]

    def _job_is_valid(job: Any) -> bool:
        if job is None:
            return False
        if hasattr(job, "is_valid"):
            return job.is_valid()
        return _is_valid_handle(job)

    def _win_close_job(job: Optional[Union[_JobWrapper, int]]) -> None:
        if job is None:
            return
        if isinstance(job, _JobWrapper):
            job.close()


def _posix_is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _posix_is_pgid_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _posix_get_descendant_pids(parent_pid: int) -> List[int]:
    """Find descendant PIDs on Linux using /proc."""
    if parent_pid <= 0:
        return []
    descendants: List[int] = []
    stack = [parent_pid]
    visited: Set[int] = set()
    while stack:
        curr = stack.pop()
        if curr in visited:
            continue
        visited.add(curr)
        children_file = Path(f"/proc/{curr}/task/{curr}/children")
        if children_file.is_file():
            try:
                content = children_file.read_text(encoding="utf-8").strip()
                if content:
                    for part in content.split():
                        try:
                            cpid = int(part)
                            if cpid not in visited and _posix_is_pid_alive(cpid):
                                descendants.append(cpid)
                                stack.append(cpid)
                        except ValueError:
                            pass
            except Exception:
                pass
    return descendants


def _extract_lines(buffer: str, is_final: bool = False) -> Tuple[List[str], str]:
    lines = []
    pos = 0
    while True:
        m = _LINE_SPLIT_REGEX.search(buffer, pos)
        if not m:
            break
        # If the match is a trailing '\r' at the very end of the buffer and not final,
        # wait to see if the next chunk starts with '\n'
        if not is_final and m.group(0) == "\r" and m.end() == len(buffer):
            break
        lines.append(buffer[pos:m.start()] + "\n")
        pos = m.end()
    remaining = buffer[pos:]
    if is_final and remaining:
        lines.append(remaining)
        remaining = ""
    return lines, remaining


def _options(options: Dict[str, Any]) -> Dict[str, Any]:
    options = dict(options)
    if sys.platform == "win32":
        options["creationflags"] = options.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        si = options.get("startupinfo")
        if si is None:
            si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = si
    return options


def run(*args, contained: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Synchronous temporary command with the same ownership as streaming."""
    if not contained:
        return subprocess.run(*args, **_options(kwargs))
    check = kwargs.pop("check", False)
    input_ = kwargs.pop("input", None)
    timeout = kwargs.pop("timeout", None)
    capture = kwargs.pop("capture_output", False)
    if capture:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("capture_output is mutually exclusive with stdout/stderr")
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input_ is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    proc = popen(*args, contained=True, **kwargs)
    try:
        stdout_data, stderr_data = proc.communicate(input=input_, timeout=timeout)
    except BaseException as operation_error:
        try:
            terminate_process_tree(proc)
        except Exception as cleanup_error:
            raise cleanup_error from operation_error
        raise
    else:
        terminate_process_tree(proc)
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout_data, stderr_data)
    if check:
        result.check_returncode()
    return result


def popen(*args, contained: bool = True, **kwargs) -> subprocess.Popen:
    opts = _options(kwargs)
    if sys.platform != "win32":
        if contained:
            opts.setdefault("start_new_session", True)
        proc = subprocess.Popen(*args, **opts)
        if contained and proc is not None and getattr(proc, "pid", None) is not None:
            proc._chief_cleanup_lock = threading.RLock()
            proc._chief_pgid = proc.pid
        return proc

    # Windows implementation:
    if not contained:
        return subprocess.Popen(*args, **opts)

    job = _win_create_job()
    if job is None:
        raise RuntimeError("Failed to create and configure Windows Job Object for process containment")

    cmd = args[0] if args else opts.pop("args", None)
    sim_fail = _popen_sim_fail.value
    try:
        proc = _win_create_process_in_job(cmd, job, opts, sim_fail=sim_fail)
    except Exception:
        job.close()
        raise

    proc._chief_cleanup_lock = threading.RLock()
    proc._chief_job = job
    weakref.finalize(proc, job.close)
    return proc


def terminate_process_tree(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    if proc is None:
        return
    lock = getattr(proc, '_chief_cleanup_lock', None)
    if lock is None:
        raise ProcessCleanupError('Cannot verify ownership of this process')
    with lock:
        try:
            _terminate_owned_tree(proc, timeout)
        except Exception as exc:
            raise ProcessCleanupError(str(exc)) from exc


def _terminate_owned_tree(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """Safely terminate a process and all its descendants with bounded shutdown and verification.

    Establishes ownership via Windows Job Objects or POSIX process groups,
    ensuring descendants are cleanly terminated even if the parent has already exited.
    """
    if proc is None:
        return

    # Check if process and all descendants are already dead
    if sys.platform == "win32":
        job = getattr(proc, "_chief_job", None)
        if job and _job_is_valid(job):
            try:
                active_pids = _win_get_job_pids(job)
                if not active_pids and proc.poll() is not None:
                    _win_close_job(job)
                    proc._chief_job = None
                    return
            except Exception:
                pass
        elif proc.poll() is not None:
            return
    else:
        pgid = getattr(proc, "_chief_pgid", proc.pid)
        if proc.poll() is not None and not _posix_is_pgid_alive(pgid) and not _posix_get_descendant_pids(proc.pid):
            return

    grace_timeout = min(1.0, max(0.2, timeout / 2))
    force_timeout = max(0.5, timeout - grace_timeout)

    # 1. Graceful phase
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass

    if sys.platform != "win32":
        pgid = getattr(proc, "_chief_pgid", proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass
        for cpid in _posix_get_descendant_pids(proc.pid):
            try:
                os.kill(cpid, signal.SIGTERM)
            except Exception:
                pass

    # Wait grace period
    grace_deadline = time.monotonic() + grace_timeout
    while time.monotonic() < grace_deadline:
        if sys.platform == "win32":
            job = getattr(proc, "_chief_job", None)
            if job and _job_is_valid(job):
                try:
                    active_pids = _win_get_job_pids(job)
                    if not active_pids and proc.poll() is not None:
                        _win_close_job(job)
                        proc._chief_job = None
                        return
                except Exception:
                    pass
            elif proc.poll() is not None:
                return
        else:
            pgid = getattr(proc, "_chief_pgid", proc.pid)
            if proc.poll() is not None and not _posix_is_pgid_alive(pgid) and not _posix_get_descendant_pids(proc.pid):
                return
        time.sleep(0.05)

    # 2. Forceful phase
    if sys.platform == "win32":
        job = getattr(proc, "_chief_job", None)
        if job and _job_is_valid(job):
            try:
                h_job = job.handle if isinstance(job, _JobWrapper) else job
                _kernel32.TerminateJobObject(wintypes.HANDLE(h_job), 1)
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        pgid = getattr(proc, "_chief_pgid", proc.pid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass
        for cpid in _posix_get_descendant_pids(proc.pid):
            try:
                os.kill(cpid, signal.SIGKILL)
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    # Wait bounded forceful period
    force_deadline = time.monotonic() + force_timeout
    while time.monotonic() < force_deadline:
        if sys.platform == "win32":
            job = getattr(proc, "_chief_job", None)
            if job and _job_is_valid(job):
                try:
                    active_pids = _win_get_job_pids(job)
                    if not active_pids and proc.poll() is not None:
                        break
                except Exception:
                    pass
            elif proc.poll() is not None:
                break
        else:
            pgid = getattr(proc, "_chief_pgid", proc.pid)
            if proc.poll() is not None and not _posix_is_pgid_alive(pgid) and not _posix_get_descendant_pids(proc.pid):
                break
        time.sleep(0.05)

    try:
        proc.wait(timeout=0.2)
    except Exception:
        pass

    # 3. Verification phase: ensure zero surviving processes
    surviving: List[int] = []
    if sys.platform == "win32":
        job = getattr(proc, "_chief_job", None)
        if job and _job_is_valid(job):
            # Query active PIDs; any query error will raise directly rather than returning []
            surviving = _win_get_job_pids(job)
            _win_close_job(job)
            proc._chief_job = None
        else:
            if proc.poll() is None:
                surviving.append(proc.pid)
    else:
        pgid = getattr(proc, "_chief_pgid", proc.pid)
        if _posix_is_pgid_alive(pgid):
            surviving.append(pgid)
        for cpid in _posix_get_descendant_pids(proc.pid):
            if cpid not in surviving:
                surviving.append(cpid)

    if proc.poll() is None and proc.pid not in surviving:
        surviving.append(proc.pid)

    if surviving:
        raise RuntimeError(
            f"Failed to terminate process tree for PID {proc.pid}: surviving PIDs {surviving}"
        )


def run_streaming(
    cmd: List[str],
    *,
    cwd: Optional[Union[str, os.PathLike]] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    inactivity_timeout: Optional[float] = None,
    quiet_threshold: float = 30.0,
    on_line: Optional[Callable[[str], None]] = None,
    on_activity: Optional[Callable[[float], None]] = None,
    on_quiet: Optional[Callable[[float], None]] = None,
    on_proc: Optional[Callable[[subprocess.Popen], None]] = None,
    check_cancelled: Optional[Callable[[], bool]] = None,
) -> subprocess.CompletedProcess:
    """Execute command with quiet windowing, real-time output streaming, activity tracking, and timeouts."""
    proc = popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if on_proc:
        try:
            on_proc(proc)
        except BaseException as operation_error:
            try:
                terminate_process_tree(proc)
            except Exception as cleanup_error:
                raise cleanup_error from operation_error
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            raise

    q: queue.Queue = queue.Queue()

    def _reader(stream, name: str):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        try:
            while True:
                chunk = stream.read1(1024) if hasattr(stream, "read1") else stream.read(1024)
                if not chunk:
                    break
                q.put((name, "activity", None))
                if isinstance(chunk, bytes):
                    text = decoder.decode(chunk, final=False)
                else:
                    text = str(chunk)
                if text:
                    buffer += text
                    lines, buffer = _extract_lines(buffer, is_final=False)
                    for l in lines:
                        q.put((name, "line", l))
            trailing = decoder.decode(b"", final=True)
            if trailing:
                buffer += trailing
            lines, buffer = _extract_lines(buffer, is_final=True)
            for l in lines:
                q.put((name, "line", l))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass
            q.put((name, "eof", None))

    t_out = threading.Thread(target=_reader, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    start_time = time.monotonic()
    last_activity_time = start_time
    captured_stdout: List[str] = []
    captured_stderr: List[str] = []
    open_readers = 2
    quiet_signalled = False
    cleaned_up = False

    def _handle_item(item):
        if len(item) == 3:
            return item[0], item[1], item[2]
        elif len(item) == 2:
            return item[0], ("line" if item[1] is not None else "eof"), item[1]
        return None, None, None

    def _drain_queue():
        while not q.empty():
            try:
                name, kind, data = _handle_item(q.get_nowait())
                if kind == "line" and data is not None:
                    if name == "out":
                        captured_stdout.append(data)
                    else:
                        captured_stderr.append(data)
                    if on_line:
                        on_line(data)
            except queue.Empty:
                break

    try:
        while open_readers > 0 or proc.poll() is None:
            if check_cancelled and check_cancelled():
                cleaned_up = True
                terminate_process_tree(proc)
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                _drain_queue()
                from .errors import CancelledError

                raise CancelledError("Process execution was cancelled by user.")

            now = time.monotonic()
            if timeout and (now - start_time) > timeout:
                cleaned_up = True
                terminate_process_tree(proc)
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                _drain_queue()
                raise TimeoutError(f"Process timed out after {timeout:.1f} seconds.")

            if inactivity_timeout and (now - last_activity_time) > inactivity_timeout:
                cleaned_up = True
                terminate_process_tree(proc)
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                _drain_queue()
                raise TimeoutError(
                    f"Process stalled: no activity observed for {inactivity_timeout:.1f} seconds."
                )

            if (
                quiet_threshold
                and (now - last_activity_time) >= quiet_threshold
                and not quiet_signalled
            ):
                if on_quiet:
                    on_quiet(now - last_activity_time)
                quiet_signalled = True

            try:
                item = q.get(timeout=0.1)
                name, kind, data = _handle_item(item)
                if kind == "eof":
                    open_readers -= 1
                    continue
                last_activity_time = time.monotonic()
                quiet_signalled = False
                if on_activity:
                    on_activity(last_activity_time)
                if kind == "line" and data is not None:
                    if name == "out":
                        captured_stdout.append(data)
                    else:
                        captured_stderr.append(data)
                    if on_line:
                        on_line(data)
            except queue.Empty:
                pass

        proc.wait()
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        # Drain any remaining lines in queue
        _drain_queue()

        # A successful root exit must not leave temporary descendants running.
        cleaned_up = True
        terminate_process_tree(proc)

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="".join(captured_stdout),
            stderr="".join(captured_stderr),
        )
    except Exception as op_err:
        if not cleaned_up:
            try:
                terminate_process_tree(proc)
            except Exception as cleanup_err:
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                _drain_queue()
                raise cleanup_err from op_err
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        _drain_queue()
        raise
