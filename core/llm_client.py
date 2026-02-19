"""
core/llm_client.py — Direkter LLM-Client für Ollama (OpenAI-kompatibel)
=========================================================================
Kein crewAI / LiteLLM Routing — direkte httpx-Verbindung.
Behandelt: Timeouts, Retries, <think>-Stripping, Tool-Use (für GLM-4)
"""

import os
import re
import json
import logging
import time
from typing import Optional, Any

from core.config import get_service_config

log = logging.getLogger("ai-hub.llm")

_services = get_service_config()
BASE_URL = _services.ollama_base_url
BASE_URL_V1 = _services.ollama_v1_url

LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT", "1200"))  # 20 Min default (GOD MODE braucht das)
LLM_MAX_RETRIES     = int(os.environ.get("LLM_RETRIES", "4"))     # 4 Versuche

os.environ.setdefault("OPENAI_API_KEY", "NA")

_clients: dict = {}  # base_url → OpenAI client

def get_client(base_url: str = BASE_URL_V1) -> Any:
    """Gibt gecachten Client zurück – einer pro base_url."""
    if base_url not in _clients:
        try:
            import httpx
            from openai import OpenAI
            _clients[base_url] = OpenAI(
                api_key="NA",
                base_url=base_url,
                http_client=httpx.Client(
                    timeout=httpx.Timeout(
                        connect=30.0,
                        read=LLM_TIMEOUT_SECONDS,
                        write=120.0,
                        pool=30.0,
                    )
                ),
            )
        except ImportError as e:
            raise RuntimeError(f"openai / httpx nicht installiert: {e}")
    return _clients[base_url]


def call(
    model: str,
    prompt: str,
    base_url: str = BASE_URL_V1,
    system: str = "You are a helpful assistant.",
    temperature: float = 0.1,
    max_tokens: int = 4096,
    tools: Optional[list] = None,
) -> str:
    """
    Ruft Ollama direkt auf. Gibt Text zurück.
    Bei Tool-Use: gibt den Tool-Call als JSON-String zurück.
    Stripped <think>-Blöcke automatisch.
    """
    client = get_client(base_url)
    last_exc: Exception = RuntimeError("Kein Versuch")

    kwargs: dict = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if tools:
        kwargs["tools"] = tools

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(**kwargs)

            # Tool-Call zurückgeben
            msg = response.choices[0].message
            if tools and msg.tool_calls:
                tc = msg.tool_calls[0]
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                return json.dumps({
                    "tool": tc.function.name,
                    "args": args,
                })

            raw = msg.content or ""
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

            # Detect empty response — retry if possible
            if not raw and attempt < LLM_MAX_RETRIES:
                log.warning(f"LLM returned empty response (model={model}, attempt {attempt}/{LLM_MAX_RETRIES})")
                time.sleep(5)
                continue

            return raw

        except Exception as e:
            last_exc = e
            err_name = type(e).__name__.lower()
            err_str = str(e).lower()
            is_timeout = any(
                kw in err_name or kw in err_str
                for kw in ("timeout", "readtimeout", "timed out")
            )
            is_transient = is_timeout or any(
                kw in err_name or kw in err_str
                for kw in ("connection", "connect", "refused", "reset", "broken", "unavailable", "502", "503")
            )
            if is_transient and attempt < LLM_MAX_RETRIES:
                wait = attempt * 10
                log.warning(f"LLM error (attempt {attempt}/{LLM_MAX_RETRIES}): {type(e).__name__} – wait {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_exc


def call_with_history(
    model: str,
    messages: list[dict],
    base_url: str = BASE_URL_V1,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    tools: Optional[list] = None,
) -> str:
    """
    Für Chat-Skill: vollständiger Konversations-Verlauf.
    Gleiche Retry-Logik wie call() — kein Silent-Crash bei Timeout.
    """
    client = get_client(base_url)
    kwargs: dict = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if tools:
        kwargs["tools"] = tools

    last_exc: Exception = RuntimeError("Kein Versuch")
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(**kwargs)
            msg = response.choices[0].message

            if tools and msg.tool_calls:
                tc = msg.tool_calls[0]
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                return json.dumps({
                    "tool": tc.function.name,
                    "args": args,
                })

            raw = msg.content or ""
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

            if not raw and attempt < LLM_MAX_RETRIES:
                log.warning(f"Chat empty response (attempt {attempt}/{LLM_MAX_RETRIES})")
                time.sleep(5)
                continue

            return raw

        except Exception as e:
            last_exc = e
            err_name = type(e).__name__.lower()
            err_str = str(e).lower()
            is_timeout = any(
                kw in err_name or kw in err_str
                for kw in ("timeout", "readtimeout", "timed out")
            )
            is_transient = is_timeout or any(
                kw in err_name or kw in err_str
                for kw in ("connection", "connect", "refused", "reset", "broken", "unavailable", "502", "503")
            )
            if is_transient and attempt < LLM_MAX_RETRIES:
                wait = attempt * 10
                log.warning(f"Chat error (attempt {attempt}/{LLM_MAX_RETRIES}): {type(e).__name__} – wait {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_exc
