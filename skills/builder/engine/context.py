"""
skills/builder/engine/context.py - Shared builder context
=========================================================
Centralizes shared constants, logger, and core imports.
"""

from __future__ import annotations

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
        write_file,
        read_file,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.utils import (
        strip_code_fences as strip_fences,
        clean_json_output as clean_json,
        write_file,
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

DEFAULT_CTX_TOKENS = 131072
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
