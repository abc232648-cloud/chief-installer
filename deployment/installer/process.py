"""Quiet Windows child processes; preserve output capture and exit handling."""
import subprocess
import sys


def _options(options):
    options = dict(options)
    if sys.platform == "win32":
        options["creationflags"] = options.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return options


def run(*args, **kwargs):
    return subprocess.run(*args, **_options(kwargs))


def popen(*args, **kwargs):
    return subprocess.Popen(*args, **_options(kwargs))
