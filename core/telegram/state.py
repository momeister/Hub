"""
core/telegram/state.py — Shared gateway state
=============================================
Holds mutable runtime state shared by handlers.
"""

from __future__ import annotations

import threading
from typing import Optional
import subprocess

user_sessions: dict[int, dict] = {}
active_build: Optional[int] = None
active_build_proc: Optional[subprocess.Popen] = None
build_signal_path: Optional[str] = None

dispatcher_alive: threading.Event = threading.Event()
dispatcher_alive.set()

voice_prefs: dict[int, dict] = {}

# Self-Optimizer State
optimizer_active: Optional[int] = None       # chat_id des Users der Optimizer gestartet hat
optimizer_engine: object = None              # Referenz auf OptimizationEngine
optimizer_state: dict = {
    "state": "idle",
    "mode": "",
    "task": "",
    "iteration": 0,
    "iterations_target": 0,
    "current_agent": "",
    "last_change": "",
    "last_decision": "",
    "errors": 0,
    "started_at": None,
    "version": "",
    "reasoning_model": "",
    "coding_model": "",
}

build_state: dict = {
    "phase": "",
    "detailed_phase": "",
    "files_done": 0,
    "files_total": 0,
    "current_file": "",
    "language": "",
    "errors": [],
    "started_at": None,
    "coder_model": "",
    "manager_model": "",
    "active_model": "",
    "current_action": "",
}
