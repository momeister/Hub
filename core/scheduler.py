"""
core/scheduler.py — GPU-Konflikt-Management
=============================================
Stellt sicher dass nur ein GPU-intensiver Skill gleichzeitig läuft.
Dispatcher (GLM-4.7) läuft auf CPU → kein Konflikt.
Builder, ComfyUI etc. brauchen GPU → sequenziell.
"""

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("ai-hub.scheduler")

# Skills die GPU brauchen
GPU_SKILLS = {"builder", "comfyui", "self_optimizer"}

_gpu_lock    = threading.Lock()
_gpu_owner   = None
_gpu_owner_ts: Optional[float] = None
MAX_GPU_HOLD_SECONDS = 3600  # 1 Stunde max


def acquire_gpu(skill_name: str, timeout: int = 30) -> bool:
    """
    Versucht die GPU zu reservieren.
    Gibt True zurück wenn erfolgreich, False wenn Timeout.
    """
    global _gpu_owner, _gpu_owner_ts

    if skill_name not in GPU_SKILLS:
        return True  # Kein GPU nötig

    deadline = time.time() + timeout
    while time.time() < deadline:
        with _gpu_lock:
            # GPU frei?
            if _gpu_owner is None:
                _gpu_owner   = skill_name
                _gpu_owner_ts = time.time()
                log.info(f"GPU reserviert: {skill_name}")
                return True
            # Timeout des aktuellen Owners?
            if _gpu_owner_ts and (time.time() - _gpu_owner_ts) > MAX_GPU_HOLD_SECONDS:
                log.warning(f"GPU-Timeout von '{_gpu_owner}' – Zwangs-Release")
                _gpu_owner    = skill_name
                _gpu_owner_ts = time.time()
                return True
        time.sleep(2)

    log.error(f"GPU-Acquire Timeout für {skill_name}")
    return False


def release_gpu(skill_name: str) -> None:
    global _gpu_owner, _gpu_owner_ts
    with _gpu_lock:
        if _gpu_owner == skill_name:
            log.info(f"GPU freigegeben: {skill_name}")
            _gpu_owner    = None
            _gpu_owner_ts = None


def gpu_status() -> dict:
    with _gpu_lock:
        held_for = None
        if _gpu_owner_ts:
            held_for = int(time.time() - _gpu_owner_ts)
        return {
            "owner":    _gpu_owner,
            "held_sec": held_for,
            "free":     _gpu_owner is None,
        }
