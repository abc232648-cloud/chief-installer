"""Platform adapters for the Chief Unified Installer."""
from .base import PlatformAdapter
from .linux import LinuxPlatformAdapter
from .windows import WindowsPlatformAdapter

__all__ = ["LinuxPlatformAdapter", "PlatformAdapter", "WindowsPlatformAdapter"]
