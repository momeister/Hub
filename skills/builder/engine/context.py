"""
skills/builder/engine/context.py - Shared builder context
=========================================================
Centralizes shared constants, logger, and core imports.
"""

from __future__ import annotations

import hashlib
import os
import sys

# LLM client - single source of truth
try:
    from core.llm_client import call as llm_call, BASE_URL_V1
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.llm_client import call as llm_call, BASE_URL_V1

# Utilities - shared versions
try:
    from core.utils import (
        strip_code_fences as strip_fences,
        clean_json_output as clean_json,
        write_file as _raw_write_file,
        read_file,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.utils import (
        strip_code_fences as strip_fences,
        clean_json_output as clean_json,
        write_file as _raw_write_file,
        read_file,
    )

# Dual logger
try:
    from core.dual_logger import BuildLogger
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.dual_logger import BuildLogger

# Safe stack policy
try:
    from core.safe_stack import validate_blueprint
except ImportError:
    def validate_blueprint(blueprint: dict) -> list[str]:
        return []

IN_DOCKER = os.path.exists("/.dockerenv")

# Internet access flag (set by user)
USE_INTERNET = False

DEFAULT_CTX_TOKENS = 262144
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "1800"))
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "4"))

MAX_REPAIR_ATTEMPTS = {
    "rust": 5,
    "go": 4,
    "typescript": 3,
    "javascript": 3,
    "python": 3,
}

MAX_SANDBOX_RETRIES = 3

blog = BuildLogger(
    jsonl_mode=IN_DOCKER or os.environ.get("TRIGGERED_BY") == "telegram",
)

# ---------------------------------------------------------------------------
# Fix 1: Path-traversal protection for LLM-generated file paths
# Fix 4: File size limit to prevent disk/RAM exhaustion
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

_ACTIVE_OUTPUT_DIR: str | None = None


def set_active_output_dir(output_dir: str) -> None:
    """Set the active output directory for write-path validation."""
    global _ACTIVE_OUTPUT_DIR
    _ACTIVE_OUTPUT_DIR = os.path.realpath(output_dir)


def _safe_write_path(output_dir: str, relative_path: str) -> str:
    """
    Gibt den absoluten Zielpfad zurück, wenn er sicher ist.
    Wirft ValueError wenn der Pfad aus output_dir ausbricht.
    """
    output_dir = os.path.realpath(output_dir)
    # Absolute Pfade und Windows-Laufwerksbuchstaben ablehnen
    if os.path.isabs(relative_path) or (len(relative_path) > 1 and relative_path[1] == ':'):
        raise ValueError(f"Absoluter Pfad nicht erlaubt: {relative_path}")
    target = os.path.realpath(os.path.join(output_dir, relative_path))
    if not target.startswith(output_dir + os.sep) and target != output_dir:
        raise ValueError(f"Path traversal erkannt: {relative_path} -> {target}")
    return target


def write_file(path: str, content: str) -> None:
    """Safe write_file with path-traversal protection and file size limit."""
    # Fix 4: File size limit
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_SIZE_BYTES:
        blog.error(
            f"Datei zu groß ({len(encoded):,} Bytes > {MAX_FILE_SIZE_BYTES:,}): {path} – übersprungen",
            severity="file_write",
        )
        return

    # Fix 1: Path-traversal protection via _safe_write_path
    if _ACTIVE_OUTPUT_DIR:
        try:
            rel = os.path.relpath(os.path.abspath(path), _ACTIVE_OUTPUT_DIR)
            _safe_write_path(_ACTIVE_OUTPUT_DIR, rel)
        except ValueError as e:
            blog.error(f"Path traversal blocked: {e} – übersprungen", severity="file_write")
            return

    _raw_write_file(path, content)


# ---------------------------------------------------------------------------
# Helpers for repair-loop hardening (Fix 3 + Fix 6)
# ---------------------------------------------------------------------------
def _code_hash(code: str) -> str:
    """MD5-Hash für Stagnationserkennung bei Repair-Loops."""
    return hashlib.md5(code.encode()).hexdigest()


def _meaningful_lines(code: str) -> int:
    """Zählt nicht-leere, nicht-reine-Kommentar-Zeilen."""
    count = 0
    for line in code.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "//", "*", "/*")):
            count += 1
    return count
