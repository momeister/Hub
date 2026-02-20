"""
skills/self_optimizer/config.py -- Optimizer-Konfiguration
==========================================================
Defaults mit Environment-Variable Overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OptimizerConfig:
    # Agent-Timeouts (Sekunden)
    developer_timeout: int = 300       # 5 Minuten
    tester_timeout: int = 120          # 2 Minuten
    reviewer_timeout: int = 180        # 3 Minuten
    test_timeout: int = 60             # Docker Test-Ausfuehrung

    # Fehler-Schwellen
    max_consecutive_errors: int = 5
    max_total_errors: int = 15
    max_iterations: int = 100          # Hard Cap auch fuer infinite

    # Docker Container-Limits
    container_memory_limit: str = "512m"
    container_cpu_limit: str = "2"

    # Approval-Timeouts
    approval_timeout: int = 3600       # 1 Stunde fuer Download/Internet
    merge_approval_timeout: int = 7200 # 2 Stunden fuer Merge

    # Default-Modelle (ueberschrieben durch Modus-Auswahl)
    default_reasoning_model: str = "deepseek-r1:32b"
    default_coding_model: str = "qwen3-coder-next"

    # Geschuetzte Dateien (duerfen nicht modifiziert werden)
    protected_paths: tuple = (
        "skills/self_optimizer/",
        ".env",
        ".git/",
    )

    @classmethod
    def from_env(cls) -> "OptimizerConfig":
        """Lade Konfiguration mit Environment-Variable Overrides."""
        return cls(
            developer_timeout=int(os.environ.get("OPT_DEVELOPER_TIMEOUT", "300")),
            tester_timeout=int(os.environ.get("OPT_TESTER_TIMEOUT", "120")),
            reviewer_timeout=int(os.environ.get("OPT_REVIEWER_TIMEOUT", "180")),
            test_timeout=int(os.environ.get("OPT_TEST_TIMEOUT", "60")),
            max_consecutive_errors=int(os.environ.get("OPT_MAX_CONSEC_ERRORS", "5")),
            max_total_errors=int(os.environ.get("OPT_MAX_TOTAL_ERRORS", "15")),
            max_iterations=int(os.environ.get("OPT_MAX_ITERATIONS", "100")),
            container_memory_limit=os.environ.get("OPT_CONTAINER_MEMORY", "512m"),
            container_cpu_limit=os.environ.get("OPT_CONTAINER_CPU", "2"),
            default_reasoning_model=os.environ.get("OPT_REASONING_MODEL", "deepseek-r1:32b"),
            default_coding_model=os.environ.get("OPT_CODING_MODEL", "qwen3-coder-next"),
        )
