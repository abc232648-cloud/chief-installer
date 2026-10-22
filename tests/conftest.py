"""Pytest configuration and environment fixtures for Chief Unified Installer."""
from pathlib import Path
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def installer_removals_are_confined_to_test_tmp(tmp_path, monkeypatch):
    """Fail any test that asks the installer to delete outside its own tmp tree."""
    from deployment.installer import orchestrator

    real_safe_remove_path = orchestrator._safe_remove_path
    allowed_root = tmp_path.resolve()

    def guarded_safe_remove_path(path, *args, **kwargs):
        target = Path(path).resolve()
        if not target.is_relative_to(allowed_root):
            raise AssertionError(
                f"Test attempted installer removal outside pytest temporary directory: {target}"
            )
        return real_safe_remove_path(path, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "_safe_remove_path", guarded_safe_remove_path)
