"""
skills/self_optimizer/memory_hook.py — Memory-Integration fuer den Optimizer
=============================================================================
Schreibt Optimierungs-Ergebnisse in den MemoryManager nach jedem
erfolgreichen Zyklus.
"""

from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("ai-hub.optimizer.memory_hook")


def record_optimization(
    files_changed: list[str],
    description: str,
    tests_passed: bool,
    decision: str,
    quality_score: int = 0,
) -> None:
    """
    Optimierungs-Ergebnis im Memory speichern.

    Args:
        files_changed: Liste der geaenderten Dateien
        description: Was verbessert wurde
        tests_passed: Ob die Tests bestanden haben
        decision: merge / reject / retry
        quality_score: Reviewer-Bewertung (1-10)
    """
    try:
        from core.memory import get_memory
        memory = get_memory()

        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        for fpath in files_changed[:5]:  # Max 5 Eintraege pro Zyklus
            memory.add_optimization(
                date_str=now,
                file_changed=fpath,
                description=f"{description[:150]} [Score:{quality_score}, Decision:{decision}]",
                tests_passed=tests_passed,
            )

        # Session-artig zusammenfassen
        summary = (
            f"Optimizer: {description[:200]} | "
            f"Files: {', '.join(files_changed[:3])} | "
            f"Tests: {'OK' if tests_passed else 'FAIL'} | "
            f"Decision: {decision}"
        )
        memory.remember(
            key=f"last_optimization",
            value=summary[:500],
            category="optimizer",
        )

        log.info(f"Optimization recorded in memory: {description[:80]}")

    except Exception as exc:
        log.warning(f"Memory hook fehlgeschlagen: {exc}")


def get_optimization_context() -> str:
    """
    Liefert die bisherigen Optimierungen als Kontext-String
    fuer den Planner-Agenten.
    """
    try:
        from core.memory import get_memory
        memory = get_memory()
        history = memory.get_optimization_history()

        if not history:
            return ""

        lines = ["Previous optimizations (avoid duplicates):"]
        for entry in history[-20:]:  # Letzte 20
            status = "OK" if entry["tests_passed"] else "FAIL"
            lines.append(
                f"  - [{entry['date']}] {entry['file']}: "
                f"{entry['description'][:100]} ({status})"
            )

        return "\n".join(lines)

    except Exception:
        return ""
