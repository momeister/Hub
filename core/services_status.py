"""
core/services_status.py — Service health checks
================================================
Centralizes health checks for Ollama, Voicebox, and ComfyUI.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse
import urllib.request

import httpx

from core.config import ServiceConfig

log = logging.getLogger("ai-hub.services")


def _port_or_default(url: str, default: int) -> int:
    try:
        parsed = urlparse(url)
        return parsed.port or default
    except Exception:
        return default


def check_ollama(base_url: str) -> bool:
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def get_ollama_models(services: ServiceConfig) -> list[str]:
    try:
        r = httpx.get(f"{services.ollama_base_url}/api/tags", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception as exc:
        log.debug("Ollama models check failed: %s", exc)
    return []


def check_voicebox(voicebox_url: str) -> bool:
    try:
        r = httpx.get(f"{voicebox_url}/health", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def check_comfyui(comfyui_url: str) -> bool:
    try:
        req = urllib.request.Request(f"{comfyui_url}/system_stats", method="GET")
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False


def get_services_status(services: ServiceConfig) -> list[dict]:
    """Return list of service status dicts used by Telegram UI."""
    return [
        {
            "name": "Ollama",
            "port": _port_or_default(services.ollama_base_url, 11434),
            "online": check_ollama(services.ollama_base_url),
            "can_stop": False,
        },
        {
            "name": "Voicebox",
            "port": _port_or_default(services.voicebox_url, 17493),
            "online": check_voicebox(services.voicebox_url),
            "can_stop": True,
        },
        {
            "name": "ComfyUI",
            "port": _port_or_default(services.comfyui_url, 8188),
            "online": check_comfyui(services.comfyui_url),
            "can_stop": True,
        },
    ]
