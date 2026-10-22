"""Quiet child processes; preserve output capture, streaming, and exit handling."""
import codecs
import os
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

_LINE_SPLIT_REGEX = re.compile(r"(\r\n|\r|\n)")


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
    return subprocess.Popen(*args, **_options(kwargs))


def terminate_process_tree(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """Safely terminate a process and its child processes."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        pass
    if proc.poll() is None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass


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

    start_time = time.time()
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

    while open_readers > 0 or proc.poll() is None:
        if check_cancelled and check_cancelled():
            terminate_process_tree(proc)
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)
            _drain_queue()
            from .errors import CancelledError
            raise CancelledError("Process execution was cancelled by user.")

        now = time.time()
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
            raise TimeoutError(f"Process stalled: no activity observed for {inactivity_timeout:.1f} seconds.")

        if quiet_threshold and (now - last_activity_time) >= quiet_threshold and not quiet_signalled:
            if on_quiet:
                on_quiet(now - last_activity_time)
            quiet_signalled = True

        try:
            item = q.get(timeout=0.1)
            name, kind, data = _handle_item(item)
            if kind == "eof":
                open_readers -= 1
                continue
            last_activity_time = time.time()
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

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout="".join(captured_stdout),
        stderr="".join(captured_stderr),
    )
