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

_LINE_SPLIT_REGEX = re.compile(r"(\r\n|\r|\n)")

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32

    _JobObjectExtendedLimitInformation = 9
    _JobObjectBasicProcessIdList = 3
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _STILL_ACTIVE = 259
    _TH32CS_SNAPPROCESS = 0x00000002

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

    class _JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 256),
        ]

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    class _JobWrapper:
        """Encapsulates a Win32 Job Object handle ensuring single-close semantics."""

        def __init__(self, handle: int):
            self.handle = handle

        def close(self) -> None:
            h = self.handle
            self.handle = 0
            if h:
                try:
                    _kernel32.CloseHandle(h)
                except Exception:
                    pass

    def _win_create_job() -> Optional[_JobWrapper]:
        try:
            h_job = _kernel32.CreateJobObjectW(None, None)
            if not h_job:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            success = _kernel32.SetInformationJobObject(
                h_job,
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not success:
                _kernel32.CloseHandle(h_job)
                return None
            return _JobWrapper(h_job)
        except Exception:
            return None

    def _win_is_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if _kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return False
        finally:
            _kernel32.CloseHandle(h)

    def _win_kill_pid(pid: int) -> bool:
        if pid <= 0:
            return False
        h = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(_kernel32.TerminateProcess(h, 1))
        finally:
            _kernel32.CloseHandle(h)

    def _win_get_job_pids(job: Union[_JobWrapper, int]) -> List[int]:
        h_job = job.handle if isinstance(job, _JobWrapper) else job
        if not h_job:
            return []
        pid_list = _JOBOBJECT_BASIC_PROCESS_ID_LIST()
        res = _kernel32.QueryInformationJobObject(
            h_job,
            _JobObjectBasicProcessIdList,
            ctypes.byref(pid_list),
            ctypes.sizeof(pid_list),
            None,
        )
        if not res:
            return []
        pids = list(pid_list.ProcessIdList[: pid_list.NumberOfProcessIdsInList])
        return [p for p in pids if p > 0 and _win_is_pid_alive(p)]

    def _win_find_descendant_pids(parent_pid: int) -> List[int]:
        """Snapshot-based descendant PID discovery fallback."""
        if parent_pid <= 0:
            return []
        h_snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not h_snap:
            return []
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        parent_map: Dict[int, List[int]] = {}
        try:
            if _kernel32.Process32First(h_snap, ctypes.byref(entry)):
                while True:
                    ppid = entry.th32ParentProcessID
                    cpid = entry.th32ProcessID
                    parent_map.setdefault(ppid, []).append(cpid)
                    if not _kernel32.Process32Next(h_snap, ctypes.byref(entry)):
                        break
        finally:
            _kernel32.CloseHandle(h_snap)
        descendants: List[int] = []
        stack = [parent_pid]
        visited: Set[int] = set()
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            for child in parent_map.get(curr, []):
                if child not in visited:
                    descendants.append(child)
                    stack.append(child)
        return [p for p in descendants if _win_is_pid_alive(p)]

    def _win_close_job(job: Optional[Union[_JobWrapper, int]]) -> None:
        if job is None:
            return
        if isinstance(job, _JobWrapper):
            job.close()
        elif job:
            try:
                _kernel32.CloseHandle(job)
            except Exception:
                pass


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


def run(*args, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(*args, **_options(kwargs))


def popen(*args, **kwargs) -> subprocess.Popen:
    opts = _options(kwargs)
    if sys.platform != "win32":
        opts.setdefault("start_new_session", True)
    proc = subprocess.Popen(*args, **opts)
    if proc is not None and getattr(proc, "pid", None) is not None:
        if sys.platform == "win32":
            job = _win_create_job()
            if job:
                proc_handle = getattr(proc, "_handle", None)
                if proc_handle is not None:
                    try:
                        if _kernel32.AssignProcessToJobObject(job.handle, int(proc_handle)):
                            proc._chief_job = job
                            weakref.finalize(proc, job.close)
                        else:
                            job.close()
                    except Exception:
                        job.close()
                else:
                    job.close()
        else:
            proc._chief_pgid = proc.pid
    return proc


def terminate_process_tree(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """Safely terminate a process and all its descendants with bounded shutdown and verification.

    Establishes ownership via Windows Job Objects or POSIX process groups,
    ensuring descendants are cleanly terminated even if the parent has already exited.
    """
    if proc is None:
        return

    # Check if process and all descendants are already dead
    if sys.platform == "win32":
        job = getattr(proc, "_chief_job", None)
        if job:
            active_pids = _win_get_job_pids(job)
            if not active_pids and proc.poll() is not None:
                _win_close_job(job)
                proc._chief_job = None
                return
        elif proc.poll() is not None:
            active_pids = _win_find_descendant_pids(proc.pid)
            if not active_pids:
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
            if job:
                if not _win_get_job_pids(job) and proc.poll() is not None:
                    _win_close_job(job)
                    proc._chief_job = None
                    return
            elif proc.poll() is not None and not _win_find_descendant_pids(proc.pid):
                return
        else:
            pgid = getattr(proc, "_chief_pgid", proc.pid)
            if proc.poll() is not None and not _posix_is_pgid_alive(pgid) and not _posix_get_descendant_pids(proc.pid):
                return
        time.sleep(0.05)

    # 2. Forceful phase
    if sys.platform == "win32":
        job = getattr(proc, "_chief_job", None)
        h_job = job.handle if isinstance(job, _JobWrapper) else job
        if h_job:
            try:
                _kernel32.TerminateJobObject(h_job, 1)
            except Exception:
                pass
        else:
            for cpid in _win_find_descendant_pids(proc.pid):
                _win_kill_pid(cpid)
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2.0,
                    )
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
            if job:
                if not _win_get_job_pids(job) and proc.poll() is not None:
                    break
            elif proc.poll() is not None and not _win_find_descendant_pids(proc.pid):
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
        if job:
            surviving = _win_get_job_pids(job)
            _win_close_job(job)
            proc._chief_job = None
        else:
            surviving = _win_find_descendant_pids(proc.pid)
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
        except Exception:
            pass

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
                terminate_process_tree(proc)
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                _drain_queue()
                from .errors import CancelledError

                raise CancelledError("Process execution was cancelled by user.")

            now = time.monotonic()
            if timeout and (now - start_time) > timeout:
                terminate_process_tree(proc)
                t_out.join(timeout=1.0)
                t_err.join(timeout=1.0)
                _drain_queue()
                raise TimeoutError(f"Process timed out after {timeout:.1f} seconds.")

            if inactivity_timeout and (now - last_activity_time) > inactivity_timeout:
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

        if sys.platform == "win32":
            job = getattr(proc, "_chief_job", None)
            if job:
                _win_close_job(job)
                proc._chief_job = None

        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout="".join(captured_stdout),
            stderr="".join(captured_stderr),
        )
    except Exception:
        terminate_process_tree(proc)
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        _drain_queue()
        raise
