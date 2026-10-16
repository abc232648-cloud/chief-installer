"""Platform adapters for the Chief Unified Installer."""
from .base import PlatformAdapter
from .linux import LinuxPlatformAdapter

__all__ = ["LinuxPlatformAdapter", "PlatformAdapter"]
