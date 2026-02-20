"""
core/dispatcher.py — Zentraler Intent-Router mit Tool-Use + Smart Args
========================================================================
Nutzt GLM-4.7-flash (klein, schnell, CPU-fähig) um Nachrichten
zu analysieren und den richtigen Skill aufzurufen.

V2: Smart argument extraction for builder (auto-detects mode, scope, tests)
    so users can skip the wizard when their message contains enough info.

Tool-Use Schema: jeder Skill registriert sich mit einer JSON-Definition.
Der Dispatcher lädt alle skill.json Dateien und baut daraus die Tool-Liste.
"""

import json
import logging
import os
import re
from typing import Optional

from core.llm_client import call
from core.skill_registry import load_skill_definitions
from core.utils import warn, info, err

log = logging.getLogger("ai-hub.dispatcher")

# Dispatcher-Modell: klein + tool-use-fähig
# GLM-4.7-flash ist auf CPU lauffähig → kein GPU-Konflikt mit Builder
DISPATCHER_MODEL = os.environ.get("DISPATCHER_MODEL", "glm-4-flash")
DISPATCHER_CTX   = 4096

 # Skill registry moved to core.skill_registry


# ---------------------------------------------------------------------------
# SMART ARGUMENT EXTRACTION (no extra LLM call — pattern matching on CPU)
# ---------------------------------------------------------------------------

# Builder mode detection patterns (checked in priority order)
_MODE_PATTERNS = {
    "3": (  # GOD MODE
        r"\bgod\s*mode\b", r"\bbeste?\s*qualit", r"\bmaximum\b", r"\bbest\b",
        r"\bhigh\s*quality\b", r"\bprod(uction)?\b", r"\bgod\b",
    ),
    "1": (  # FAST
        r"\bfast\b", r"\bschnell\b", r"\bquick\b", r"\bsimple\b", r"\beinfach\b",
        r"\bskript\b", r"\bscript\b", r"\brapid\b",
    ),
    "4": (  # UNCENSORED
        r"\buncensored\b", r"\bunfiltered\b", r"\babliterated\b", r"\bno\s*filter\b",
        r"\bnsfw\b", r"\bunzensiert\b",
    ),
    "2": (  # AVERAGE (lowest priority)
        r"\baverage\b", r"\bnormal\b", r"\bstandard\b", r"\bmedium\b",
    ),
}

# Scope detection
_SCOPE_PATTERNS = {
    "2": (r"\bkompakt\b", r"\bcompact\b", r"\bminimal\b", r"\bsmall\b", r"\bklein\b"),
    "3": (r"\bfull\b", r"\bvoll\b", r"\bclean\s*arch", r"\bproduction\b", r"\benterprise\b"),
}

# Testing detection
_TEST_PATTERNS = {
    "2": (r"\bwith\s*tests?\b", r"\bunit\s*test", r"\btdd\b", r"\bmit\s*tests?\b"),
    "1": (r"\bno\s*test", r"\bskip\s*test", r"\bkeine?\s*test", r"\bwithout\s*test", r"\bohne\s*test"),
}


def _match_patterns(text: str, patterns: dict[str, tuple]) -> Optional[str]:
    """Check text against pattern groups. Returns first matching key, or None."""
    text_lower = text.lower()
    for key, pats in patterns.items():
        for pat in pats:
            if re.search(pat, text_lower, re.IGNORECASE):
                return key
    return None


def smart_extract_builder_args(text: str) -> dict:
    """
    Extract builder settings from natural language. Zero LLM calls — pure regex.
    Returns dict with detected settings (only keys that were detected).

    Defaults for projects (per user spec):
      - God Mode: ON (mode=3)
      - Testing: OFF (tests=1)
      - Scope: Auto (scope=1)
      - Internet: OFF (internet=n)

    Example: "Build a snake game in Rust, god mode, no tests"
    Returns: {"mode": "3", "tests": "1", "language_hint": "rust",
              "internet": "n", "opmode": "1", "auto_detected": True}
    """
    detected = {}

    # Mode (default: GOD MODE per user spec)
    mode = _match_patterns(text, _MODE_PATTERNS)
    detected["mode"] = mode if mode else "3"

    # Scope (default: auto)
    scope = _match_patterns(text, _SCOPE_PATTERNS)
    detected["scope"] = scope if scope else "1"

    # Testing (default: no tests per user spec)
    testing = _match_patterns(text, _TEST_PATTERNS)
    detected["tests"] = testing if testing else "1"

    # Internet (default off)
    if re.search(r"\binternet\b|\bonline\b|\bweb\s*search\b|\bfetch\b", text, re.IGNORECASE):
        detected["internet"] = "y"
    else:
        detected["internet"] = "n"

    # Operation mode (new vs existing project)
    detected["opmode"] = "1"  # New project by default

    # Language hint
    lang_map = {
        r"\brust\b": "rust", r"\bpython\b": "python", r"\bgo\b": "go",
        r"\bgolang\b": "go", r"\btypescript\b": "typescript", r"\bts\b": "typescript",
        r"\bjavascript\b": "javascript", r"\bjs\b": "javascript",
        r"\bjava\b": "java", r"\bc#\b": "csharp", r"\bcsharp\b": "csharp",
        r"\bhtml\b": "html", r"\bc\+\+\b": "cpp", r"\bcpp\b": "cpp",
    }
    for pat, lang in lang_map.items():
        if re.search(pat, text, re.IGNORECASE):
            detected["language_hint"] = lang
            break

    # Mark that this was auto-detected (gateway can skip wizard)
    detected["auto_detected"] = True

    if mode:
        log.info(f"Auto-detected mode: {mode}")
    else:
        log.info(f"No mode specified, defaulting to GOD MODE (3)")

    return detected


def smart_extract_comfyui_args(text: str) -> dict:
    """
    Extract ComfyUI settings from natural language. Zero LLM calls.
    Returns dict with detected image generation settings.
    """
    detected = {}

    # Dimensions (WxH pattern)
    size_match = re.search(r'\b(\d{3,4})\s*[xX]\s*(\d{3,4})\b', text)
    if size_match:
        detected["width"] = int(size_match.group(1))
        detected["height"] = int(size_match.group(2))

    # Amount
    amount_match = re.search(r'\b(\d+)\s*(?:images?|bilder?|pics?|photos?)\b', text, re.IGNORECASE)
    if amount_match:
        n = int(amount_match.group(1))
        if 1 <= n <= 10:
            detected["amount"] = n

    # Steps
    steps_match = re.search(r'\b(\d+)\s*steps?\b', text, re.IGNORECASE)
    if steps_match:
        s = int(steps_match.group(1))
        if 1 <= s <= 100:
            detected["steps"] = s

    return detected


# ---------------------------------------------------------------------------
# MAIN DISPATCH
# ---------------------------------------------------------------------------

def dispatch(user_message: str, chat_history: list[dict] = None) -> dict:
    """
    Analysiert die Nachricht und gibt zurück:
    {
        "skill": "builder" | "chat" | "comfyui" | ...,
        "args": {...},
        "raw_response": "...",
    }

    Smart extraction enriches args with auto-detected settings.
    """
    tools = load_skill_definitions()
    if not tools:
        warn("Keine Skills gefunden – Fallback auf Chat")
        return {"skill": "chat", "args": {"request": user_message}, "raw_response": ""}

    # Kontext aus History
    history_ctx = ""
    if chat_history and len(chat_history) > 0:
        last = chat_history[-3:]
        history_ctx = "\n".join(
            f"{m['role'].upper()}: {m['content'][:200]}"
            for m in last
        )
        history_ctx = f"\nRecent context:\n{history_ctx}\n"

    # Memory-Kontext einfuegen
    memory_ctx = ""
    try:
        from core.memory import get_memory
        memory = get_memory()
        ctx = memory.get_context_for_llm()
        if ctx:
            memory_ctx = f"\nMemory context:\n{ctx}\n"
    except Exception:
        pass

    system_prompt = (
        "You are a smart dispatcher. Analyze the user's message and call the "
        "MOST appropriate tool/skill. "
        "If the user wants to build/create code or a project → use 'builder'. "
        "If the user wants to optimize/improve the AI HUB codebase itself → use 'self_optimizer'. "
        "If the user wants an image generated → use 'comfyui'. "
        "If the user wants to download something → use 'downloader'. "
        "If the user wants to organize files/desktop → use 'desktop'. "
        "For general questions, conversation, or talking to a specific model → use 'chat'. "
        "Always call a tool. Never respond with plain text."
    )

    prompt = (
        f"{history_ctx}"
        f"{memory_ctx}"
        f"User message: {user_message}\n\n"
        f"Call the appropriate tool."
    )

    try:
        result_str = call(
            model=DISPATCHER_MODEL,
            prompt=prompt,
            system=system_prompt,
            temperature=0.1,
            max_tokens=512,
            tools=tools,
        )

        # Tool-Call wurde zurückgegeben
        if result_str.startswith("{") and '"tool"' in result_str:
            result = json.loads(result_str)
            skill = result["tool"]
            args = result.get("args", {"request": user_message})

            # Smart enrichment: extract additional settings from text
            if skill == "builder":
                builder_args = smart_extract_builder_args(user_message)
                args.update(builder_args)
            elif skill == "comfyui":
                comfyui_args = smart_extract_comfyui_args(user_message)
                args.update(comfyui_args)

            info(f"Dispatcher → Skill: {skill}  Args: {list(args.keys())}")
            return {
                "skill": skill,
                "args": args,
                "raw_response": "",
            }
        else:
            # Kein Tool-Call → Chat-Fallback
            warn(f"Dispatcher: kein Tool-Call, Fallback Chat")
            return {
                "skill": "chat",
                "args": {"request": user_message},
                "raw_response": result_str,
            }

    except Exception as e:
        err(f"Dispatcher-Fehler: {e} → Chat-Fallback")
        return {
            "skill": "chat",
            "args": {"request": user_message},
            "raw_response": "",
        }


 # get_available_skills and get_skill_descriptions now come from core.skill_registry
