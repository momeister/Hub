"""
skills/chat/skill.py — Direkter Chat mit lokalem LLM
======================================================
Unterstützt: Konversations-History, Modell-Auswahl per Name,
             abliterated/uncensored Modelle.
"""

import logging
import os
from core.llm_client import call, call_with_history, BASE_URL_V1

log = logging.getLogger("skill.chat")

DEFAULT_MODEL = os.environ.get("CHAT_MODEL", "qwen3-coder-next")


def run(
    request: str,
    history: list[dict] = None,
    model: str = None,
) -> str:
    """
    Führt einen Chat-Request aus.
    
    Args:
        request: Aktuelle Nachricht des Users
        history: Bisherige Konversation [{role, content}, ...]
        model:   Spezifisches Modell (optional)
    
    Returns:
        Antwort des Modells als String
    """
    use_model = model or DEFAULT_MODEL
    
    if history:
        # Vollständige History mitgeben
        messages = list(history)
        # Sicherstellen dass letzte Nachricht der aktuelle Request ist
        if not messages or messages[-1].get("content") != request:
            messages.append({"role": "user", "content": request})
        
        response = call_with_history(
            model=use_model,
            messages=messages,
            base_url=BASE_URL_V1,
            temperature=0.7,
            max_tokens=4096,
        )
    else:
        response = call(
            model=use_model,
            prompt=request,
            base_url=BASE_URL_V1,
            system="You are a helpful, knowledgeable assistant. Be concise but thorough.",
            temperature=0.7,
            max_tokens=4096,
        )
    
    return response
