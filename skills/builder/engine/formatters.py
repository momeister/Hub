"""
skills/builder/engine/formatters.py - Code formatting helpers
=============================================================
"""

from __future__ import annotations

import shutil
import subprocess


def format_code(project_dir: str, language: str) -> None:
    """Auto-format code after repair to fix trivial style issues."""
    try:
        if language == "rust" and shutil.which("cargo"):
            subprocess.run(["cargo", "fmt"], cwd=project_dir, capture_output=True, timeout=30)
        elif language == "go" and shutil.which("gofmt"):
            subprocess.run(["go", "fmt", "./..."], cwd=project_dir, capture_output=True, timeout=30)
        elif language == "python" and shutil.which("black"):
            subprocess.run(["black", "--quiet", project_dir], capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, Exception):
        pass
