"""
skills/self_optimizer/skill.py -- Skill Entry Point
=====================================================
Standard run() Funktion fuer das Skills-System.
Wird vom Dispatcher oder direkt aufgerufen.
"""

from __future__ import annotations

import logging

log = logging.getLogger("ai-hub.self_optimizer")


def run(
    request: str = "",
    mode: str = "task",
    iterations: int = 1,
    **kwargs,
) -> dict:
    """
    Self-Optimizer Skill Entry Point.

    Args:
        request: Was optimiert werden soll (oder 'auto')
        mode: 'task' fuer spezifische Aufgabe, 'auto' fuer KI-Entscheidung
        iterations: Anzahl Iterationen (0 = unendlich)

    Returns:
        {"success": bool, "message": str}

    Hinweis: Der Optimizer wird normalerweise ueber den Telegram-Handler
    gestartet (/optimize), nicht direkt ueber diese Funktion.
    Dieser Entry Point dient der Kompatibilitaet mit dem Skills-System.
    """
    return {
        "success": False,
        "message": (
            "Self-Optimizer wird ueber /optimize in Telegram gesteuert.\n"
            "Direkte Ausfuehrung nicht unterstuetzt -- "
            "nutze den Telegram-Command /optimize"
        ),
    }
