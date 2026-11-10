"""Exercise the shipped browser handlers offline when Node is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_interview_submission_and_navigation_handlers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional; needed for browser-handler regression checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("interview_browser_checks.cjs"))],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
